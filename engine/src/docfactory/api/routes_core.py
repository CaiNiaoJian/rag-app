"""核心端点：健康探活 / 设置读写 / 优雅退出（02 章 §2.1）。

- GET  /health    免鉴权（app.py 的 _PUBLIC_PATHS 白名单）：主进程启动探活与守护心跳都打这里，
                  探活先于凭据可用，且响应只含版本不含任何用户数据。
- GET  /settings  返回 Settings 全量（前端 EngineSettings 是其镜像）。
- PUT  /settings  **部分更新**：body 允许只带变化的字段（含嵌套子对象），
                  由 SettingsHolder.patch 深合并 → pydantic 校验 → 原子落盘。
                  之所以不做整体替换：UI 的设置页分 Tab 分块保存，整体替换会让并发保存互相覆盖。
- POST /shutdown  先把 {"ok": true} 发出去，再经 BackgroundTask 置 uvicorn should_exit。
                  顺序很关键——同一个连接上先关服务再回包，主进程会把正常退出误判为引擎崩溃并触发重启。

生效边界：新任务立刻读到新设置（调度器每次建 TaskContext 都取当前值）；
``parallel_tasks`` 决定 worker 线程数，只在引擎重启时读取，改了要重启才见效。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Request
from loguru import logger
from pydantic import ValidationError

from docfactory.app import health_payload
from docfactory.errors import DocFactoryError

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    """存活与版本核对面（02 章 §1.1 启动握手后的版本比对由主进程完成）。"""
    return health_payload()


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    """当前设置全量快照。"""
    return request.app.state.settings.get().model_dump()


@router.put("/settings")
def put_settings(
    request: Request, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """部分更新设置，返回合并后的全量值（UI 直接用返回值刷新本地状态，不必再 GET 一次）。"""
    holder = request.app.state.settings
    try:
        updated = holder.patch(body)
    except ValidationError as exc:
        # 错误码表里没有「参数非法」这一档：E06 是唯一语义上能覆盖「引擎拒绝了这次请求」的码，
        # 真正有用的信息放 detail（三级结构的技术详情），UI 会原样展示给用户。
        raise DocFactoryError("E06", f"设置项非法：{_format_validation(exc)}") from exc
    except OSError as exc:
        # 落盘失败绝大多数是磁盘满/目录被占用，按 E07 给出「清理磁盘」的建议更有指导性
        raise DocFactoryError("E07", f"设置写入失败：{exc}") from exc

    logger.info(f"设置已更新：{sorted(body)}")
    return updated.model_dump()


@router.post("/shutdown")
def shutdown(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    """优雅退出：置 uvicorn should_exit，收尾由 main.py 的 finally 停调度器完成。"""
    request_shutdown = request.app.state.request_shutdown
    logger.info("收到退出请求，响应返回后开始停机")
    # BackgroundTask 在响应体发送完毕后才执行，保证客户端一定收得到 {"ok": true}
    background.add_task(request_shutdown)
    return {"ok": True}


def _format_validation(exc: ValidationError) -> str:
    """把 pydantic 报错压成一行「字段: 原因」，避免把整棵错误树塞进 UI。"""
    parts: list[str] = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc or '(root)'}: {err.get('msg', '')}")
    return "；".join(parts)
