"""日志初始化（08 章 §1）。

约束：
- stdout 由 READY 握手独占（02 章 §1.1）：移除 loguru 默认控制台 sink，
  所有日志只落盘 logs/engine-*.jsonl；
- 自定义 formatter 输出**扁平九字段 JSONL**（08 章 §1 契约：
  ts/level/src/task_id/doc_id/code/page/msg/detail），与 Electron 侧同格式 ——
  诊断包里两类日志从此可用同一套解析器合并时间线；业务字段（task_id/doc_id/code/page）
  经 logger.bind(...) 进入 record.extra，未绑定即为 null；
- 轮转 10MB / 最多 10 份 / 保留 14 天（loguru 原生 retention 二选一，
  这里用自定义回调同时满足份数与天数上限）；
- uvicorn / fastapi 等标准 logging 全部拦截进 loguru，防止旁路污染 stdout；
- 日志不含文档内容（diagnose=False，不展开变量值）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from typing import Any

from loguru import logger

from docfactory.config import Paths

_MAX_FILES = 10
_MAX_AGE_S = 14 * 24 * 3600

# 08 章 §1 日志契约里的 src 字段：本进程恒为 "engine"（Electron 侧写 "app"），
# 合并时间线时用它区分两个产日志的进程。
_SRC = "engine"

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


def _is_disconnect_noise(record: logging.LogRecord) -> bool:
    """识别 Windows ProactorEventLoop 的客户端断连噪声。

    UI 每次收完 SSE 就主动断开（任务完成、切页面、关窗口都会），asyncio 直到关闭
    传输时才发现连接已重置，于是抛 ConnectionResetError 并以 **ERROR** 级别记一条
    `Exception in callback _ProactorBasePipeTransport._call_connection_lost()`。
    这是完全正常的断连，却是高频事件 —— 放任它进日志，用户在诊断包与日志查看器里
    会看到一片红色假故障，「失败可解释」这个卖点就先被自己的日志毁掉了。
    降级为 DEBUG（低于 sink 的 INFO 门槛，等于不落盘），但不直接丢弃：
    真要排查断连时把 sink 调到 DEBUG 仍然看得到。
    """
    if record.name != "asyncio" or record.levelno < logging.ERROR:
        return False
    exc_type = record.exc_info[0] if record.exc_info else None
    if exc_type is not None and not issubclass(exc_type, ConnectionError):
        return False
    return "_call_connection_lost" in record.getMessage()


class InterceptHandler(logging.Handler):
    """把标准 logging 记录转发进 loguru（loguru 官方配方，保留调用点与异常栈）。"""

    def emit(self, record: logging.LogRecord) -> None:
        if _is_disconnect_noise(record):
            record.levelname, record.levelno = "DEBUG", logging.DEBUG
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _detail(record: dict[str, Any]) -> Any:
    """detail 字段：有异常时给「类型: 值」摘要（backtrace/diagnose=False，不含栈与变量值，
    承诺不含文档内容）；无异常则 None。"""
    exc = record.get("exception")
    if exc is None:
        return None
    etype = getattr(exc, "type", None)
    evalue = getattr(exc, "value", None)
    if etype is not None:
        return f"{etype.__name__}: {evalue}"
    return str(exc)


def _jsonl_line(record: dict[str, Any]) -> str:
    """把一条 loguru record 拍平成 08 章 §1 的九字段 JSONL 行（不含换行）。

    纯函数、无副作用：既是 formatter 的核心，也便于单测直接喂合成 record 断言字段形态。
    """
    extra = record.get("extra") or {}
    payload = {
        "ts": record["time"].isoformat(timespec="milliseconds"),
        "level": record["level"].name.lower(),
        "src": _SRC,
        "task_id": extra.get("task_id"),
        "doc_id": extra.get("doc_id"),
        "code": extra.get("code"),
        "page": extra.get("page"),
        "msg": record["message"],
        "detail": _detail(record),
    }
    return json.dumps(payload, ensure_ascii=False)


def _formatter(record: dict[str, Any]) -> str:
    """loguru 的 format 回调：把序列化结果塞进 extra，模板只引用它。

    用 **callable** formatter（而非 serialize=True）是关键——loguru 对 callable formatter
    不会自动追加异常栈与换行，两者都由我们掌控，输出严格是「一行一个 JSON 对象」；
    serialize=True 只能吐 loguru 私有的嵌套结构（无 src、键名层级全不同），不符契约。
    """
    record["extra"]["_jsonl"] = _jsonl_line(record)
    return "{extra[_jsonl]}\n"


def setup_logging(paths: Paths) -> None:
    """进程级初始化（幂等）；须在 Paths.ensure() 之后、uvicorn 启动之前调用。"""
    global _configured
    if _configured:
        return
    _configured = True

    logger.remove()  # 关键：默认 stderr sink 移除，控制台零输出
    logger.add(
        str(paths.logs / "engine-{time:YYYYMMDD-HHmmss}.jsonl"),
        format=_formatter,  # 扁平九字段 JSONL（08 章 §1 契约），见 _jsonl_line
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
