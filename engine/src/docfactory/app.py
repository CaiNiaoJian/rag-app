"""FastAPI 应用装配（02 章 §2 IPC 协议）。

- **鉴权**：除 `/health` 之外一律要求 `Authorization: Bearer <token>`（token 每次启动随机，
  由主进程经命令行传入）。`/health` 放行是刻意的：主进程要用它做启动探活与守护心跳，
  探活先于凭据可用，且该端点不暴露任何用户数据。
- **依赖注入**：db / paths / settings / scheduler 全挂 `app.state`，路由从 `request.app.state`
  取——不用模块级单例，测试可并起多个互不干扰的实例。
- **错误收口**：DocFactoryError → 错误三级呈现结构（人话/建议/详情，FR-13）；
  未预期异常 → E06，且不把栈回给 UI（栈只进日志）。
- 路由模块**按需挂载**：某个 routes_*.py 尚未落地时只记 warning，不阻断引擎启动，
  避免并行开发期一个文件缺失导致整机起不来。
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from docfactory import API_VERSION, APP_NAME, ENGINE_VERSION, IR_VERSION, SCHEMA_VERSION
from docfactory.config import Paths, Settings, load_settings, save_settings
from docfactory.db import Database
from docfactory.errors import DocFactoryError, error_payload
from docfactory.scheduler import Scheduler

# 免鉴权路径（仅存活探测；不返回任何用户数据）
_PUBLIC_PATHS = frozenset({"/health"})

# 路由模块挂载表（模块名 → 说明，仅用于缺失时的日志）
_ROUTE_MODULES: list[tuple[str, str]] = [
    ("docfactory.api.routes_core", "健康/设置/退出"),
    ("docfactory.api.routes_tasks", "任务与 SSE"),
    ("docfactory.api.routes_documents", "文档库"),
    ("docfactory.api.routes_stats", "仪表盘"),
    ("docfactory.api.routes_logs", "日志与诊断包"),
    ("docfactory.api.routes_modules", "模组管理"),
    ("docfactory.api.routes_v1", "本地模型接口"),
]


class SettingsHolder:
    """设置的可变持有者：PUT /settings 后立即对新任务生效（读多写少，用锁最省心）。"""

    def __init__(self, paths: Paths, settings: Settings | None = None):
        self._paths = paths
        self._lock = threading.Lock()
        self._settings = settings if settings is not None else load_settings(paths)

    def get(self) -> Settings:
        with self._lock:
            return self._settings

    def replace(self, settings: Settings) -> Settings:
        """整体替换并持久化（原子写）。"""
        with self._lock:
            self._settings = settings
            save_settings(self._paths, settings)
            return settings

    def patch(self, changes: dict[str, Any]) -> Settings:
        """部分更新：与当前值深合并后校验，非法值直接由 pydantic 抛出。"""
        with self._lock:
            merged = _deep_merge(self._settings.model_dump(), changes)
            updated = Settings.model_validate(merged)
            self._settings = updated
            save_settings(self._paths, updated)
            return updated


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def health_payload() -> dict[str, Any]:
    """GET /health 响应（02 章 §2.1）；同时作为启动握手后的版本核对面。"""
    return {
        "status": "ok",
        "app": APP_NAME,
        "engine_version": ENGINE_VERSION,
        "api_version": API_VERSION,
        "ir_version": IR_VERSION,
        "schema_version": SCHEMA_VERSION,
    }


def create_app(
    *,
    db: Database,
    paths: Paths,
    settings: SettingsHolder,
    scheduler: Scheduler,
    token: str,
    request_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    app = FastAPI(
        title=f"{APP_NAME} Engine",
        version=ENGINE_VERSION,
        docs_url=None,       # 离线桌面应用不需要交互文档，少一个可被访问的面
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db = db
    app.state.paths = paths
    app.state.settings = settings
    app.state.scheduler = scheduler
    app.state.token = token
    app.state.request_shutdown = request_shutdown or (lambda: None)

    @app.middleware("http")
    async def _auth(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path not in _PUBLIC_PATHS:
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not _token_eq(presented.strip(), token):
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)

    @app.exception_handler(DocFactoryError)
    async def _on_business_error(_: Request, exc: DocFactoryError) -> JSONResponse:
        # 业务异常：带错误码的 400，UI 直接拿三级结构渲染
        return JSONResponse(status_code=400, content=error_payload(exc.code, exc.detail))

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"未处理异常 {request.method} {request.url.path}：{exc}")
        # 栈只进日志；回给 UI 的 detail 仅含异常类型，不泄漏路径与内容
        return JSONResponse(
            status_code=500, content=error_payload("E06", type(exc).__name__)
        )

    for module_name, label in _ROUTE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning(f"路由模块未就绪，跳过挂载：{label}（{module_name}）—— {exc}")
            continue
        router = getattr(module, "router", None)
        if router is None:
            logger.warning(f"路由模块缺少 router：{module_name}")
            continue
        app.include_router(router)

    return app


def _token_eq(a: str, b: str) -> bool:
    """常量时间比较：本地回环虽无远程攻击面，凭据比较仍按规矩来。"""
    import hmac

    return hmac.compare_digest(a, b)
