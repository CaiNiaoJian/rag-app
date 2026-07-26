"""引擎进程入口（02 章 §1.1 生命周期）。

启动序列（顺序有意义，勿调换）：
    ① 离线闸 install()          —— 必须早于任何网络组件初始化
    ② 解析参数 / 建目录布局
    ③ 日志初始化               —— 之后 stdout 只允许出现 READY 一行
    ④ SQLite 迁移（迁移前自动备份到 backup\\）
    ⑤ 启动自检：running 任务标 interrupted、模组目录校验（失败自动回滚指针）
    ⑥ 起调度器 → 装配 FastAPI → uvicorn 绑定 127.0.0.1:{port}
    ⑦ 绑定成功后向 stdout 打印单行 ``READY {"port":54321,"pid":1234}`` 完成握手

退出：``POST /shutdown`` 置 should_exit → uvicorn 收尾 → 调度器停机（在跑的任务收到取消
信号，宽限 10s）→ 进程返回 0。异常退出由主进程 EngineSupervisor 负责重启。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

# 离线闸必须在 import 任何网络库之前生效（02 章 §7）
from docfactory import offline_guard

offline_guard.install()

import uvicorn  # noqa: E402
from loguru import logger  # noqa: E402

from docfactory import APP_NAME, ENGINE_VERSION  # noqa: E402
from docfactory.app import SettingsHolder, create_app  # noqa: E402
from docfactory.config import Paths, default_data_root  # noqa: E402
from docfactory.db import Database  # noqa: E402
from docfactory.logsetup import setup_logging  # noqa: E402
from docfactory.scheduler import Scheduler  # noqa: E402

_STARTUP_TIMEOUT_S = 15.0  # 与主进程侧的启动超时保持一致（02 章 §1.1）


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="docfactory-engine", description=f"{APP_NAME} 解析引擎")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示由 OS 随机分配")
    parser.add_argument("--token", default=None, help="Bearer 凭据；未提供时自动生成并随 READY 回传")
    parser.add_argument("--data-dir", default=None, help="数据根目录（默认 %%LOCALAPPDATA%%\\DocFactory）")
    parser.add_argument("--version", action="version", version=ENGINE_VERSION)
    return parser.parse_args(argv)


def _emit_ready(payload: dict[str, Any]) -> None:
    """stdout 握手行（全进程唯一一次 stdout 写入，其余日志一律落盘）。"""
    sys.stdout.write("READY " + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _bootstrap(paths: Paths) -> Database:
    """建目录 → 迁移 → 启动自检。任何一步失败都抛出，由 main() 统一转非零退出。"""
    paths.ensure()
    setup_logging(paths)
    logger.info(f"{APP_NAME} 引擎 {ENGINE_VERSION} 启动，数据目录：{paths.root}")

    db = Database(paths.db_path)
    version = db.migrate(backup_dir=paths.backup)
    logger.info(f"数据库就绪，schema_version={version}")

    interrupted = db.mark_interrupted()
    if interrupted:
        logger.warning(f"上次运行有 {interrupted} 个任务被中断，已标记 interrupted")

    # 模组启动自检（06 章 §2 第⑦步）：目录/清单损坏则自动回滚指针
    try:
        from docfactory.modules.manager import startup_check

        issues = startup_check(db, paths)
        if issues:
            logger.warning(f"模组启动自检处置 {len(issues)} 项：{issues}")
    except Exception as exc:  # 模组自检不该阻断引擎启动
        logger.exception(f"模组启动自检异常：{exc}")

    return db


async def _serve(server: uvicorn.Server, ready: dict[str, Any]) -> None:
    """跑 uvicorn，并在真正绑定端口后补发 READY（端口为 0 时才知道实际值）。"""
    task = asyncio.ensure_future(server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_TIMEOUT_S
    while not server.started and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.02)

    if not server.started:
        if not task.done():
            server.should_exit = True
        await task  # 让 uvicorn 把真实异常抛出来
        raise RuntimeError("引擎在超时前未能完成端口绑定")

    sock = server.servers[0].sockets[0]
    ready["port"] = int(sock.getsockname()[1])
    _emit_ready(ready)
    logger.info(f"HTTP 服务就绪：127.0.0.1:{ready['port']}")
    await task


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.data_dir) if args.data_dir else default_data_root()
    paths = Paths(root=root)

    try:
        db = _bootstrap(paths)
    except Exception as exc:
        # 日志可能尚未就绪：这条兜底走 stderr（stdout 留给 READY 握手）
        sys.stderr.write(f"engine bootstrap failed: {type(exc).__name__}: {exc}\n")
        logger.exception(f"引擎启动失败：{exc}")
        return 2

    # token：主进程传入即用；缺省时自动生成并随 READY 回传（开发/手工调试路径）
    generated = args.token is None
    token = args.token or secrets.token_hex(16)

    settings = SettingsHolder(paths)
    scheduler = Scheduler(db, paths, settings.get)

    server_ref: dict[str, uvicorn.Server] = {}

    def request_shutdown() -> None:
        srv = server_ref.get("server")
        if srv is not None:
            srv.should_exit = True

    app = create_app(
        db=db, paths=paths, settings=settings, scheduler=scheduler,
        token=token, request_shutdown=request_shutdown,
    )

    config = uvicorn.Config(
        app,
        host="127.0.0.1",     # 绝不 0.0.0.0：局域网内其他主机连不上（02 章 §7）
        port=int(args.port),
        log_config=None,      # 日志已由 logsetup 全面接管，禁止 uvicorn 另起 sink
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=10,
    )
    server = uvicorn.Server(config)
    server_ref["server"] = server

    ready: dict[str, Any] = {"pid": os.getpid()}
    if generated:
        ready["token"] = token

    scheduler.start()
    try:
        asyncio.run(_serve(server, ready))
    except KeyboardInterrupt:
        logger.info("收到中断信号，开始收尾")
    except Exception as exc:
        logger.exception(f"HTTP 服务异常退出：{exc}")
        return 3
    finally:
        scheduler.stop()
        logger.info("引擎已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
