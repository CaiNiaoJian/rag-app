"""日志初始化（08 章 §1）。

约束：
- stdout 由 READY 握手独占（02 章 §1.1）：移除 loguru 默认控制台 sink，
  所有日志只落盘 logs/engine-*.jsonl；
- loguru serialize=True 输出结构化 JSONL（task_id/doc_id/code/page 等业务字段
  经 logger.bind(...) 进入 record.extra）；
- 轮转 10MB / 最多 10 份 / 保留 14 天（loguru 原生 retention 二选一，
  这里用自定义回调同时满足份数与天数上限）；
- uvicorn / fastapi 等标准 logging 全部拦截进 loguru，防止旁路污染 stdout；
- 日志不含文档内容（diagnose=False，不展开变量值）。
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import suppress

from loguru import logger

from docfactory.config import Paths

_MAX_FILES = 10
_MAX_AGE_S = 14 * 24 * 3600

_configured = False


def _retention(files: list[str]) -> None:
    """保留策略：按修改时间新→旧排序，超出 10 份或超过 14 天的删除。"""
    now = time.time()
    entries: list[tuple[float, str]] = []
    for f in files:
        try:
            entries.append((os.path.getmtime(f), f))
        except OSError:
            continue
    entries.sort(reverse=True)
    for idx, (mtime, f) in enumerate(entries):
        if idx >= _MAX_FILES or now - mtime > _MAX_AGE_S:
            # 文件被占用（用户正开着日志看）等情形留待下次轮转清理
            with suppress(OSError):
                os.remove(f)


class InterceptHandler(logging.Handler):
    """把标准 logging 记录转发进 loguru（loguru 官方配方，保留调用点与异常栈）。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(paths: Paths) -> None:
    """进程级初始化（幂等）；须在 Paths.ensure() 之后、uvicorn 启动之前调用。"""
    global _configured
    if _configured:
        return
    _configured = True

    logger.remove()  # 关键：默认 stderr sink 移除，控制台零输出
    logger.add(
        str(paths.logs / "engine-{time:YYYYMMDD-HHmmss}.jsonl"),
        serialize=True,
        rotation="10 MB",
        retention=_retention,
        level="INFO",
        enqueue=True,      # 队列化写入：调度器 worker 线程并发打日志安全
        backtrace=False,
        diagnose=False,    # 不展开变量值：日志承诺不含文档内容
        encoding="utf-8",
    )

    # 标准 logging（root）整体接管；uvicorn/fastapi 自带 logger 清空 handler 走冒泡
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "sse_starlette"):
        std_logger = logging.getLogger(name)
        std_logger.handlers = []
        std_logger.propagate = True
