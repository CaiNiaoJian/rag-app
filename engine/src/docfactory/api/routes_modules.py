"""模组管理端点（02 章 §2.1，06 章）。

- GET  /modules           模组列表（含当前版本指针与可回滚性）
- POST /modules/install   建 module_install 后台任务（验签/解包耗时，不能占 HTTP 线程）
- POST /modules/rollback  同步回滚（只改指针，瞬时完成）

依赖统一从 request.app.state 取（db/paths/scheduler），Bearer 鉴权由应用层统一处理。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from docfactory.errors import DocFactoryError, error_payload
from docfactory.modules.manager import module_dir_ok, rollback

router = APIRouter()


class InstallBody(BaseModel):
    """安装请求：.kmod 的绝对路径（文件一律传路径不传内容，02 章 §2.1）。"""

    kmod_path: str


class RollbackBody(BaseModel):
    module_id: str


@router.get("/modules")
def list_modules(request: Request) -> dict[str, Any]:
    """模组清单 + 每项的可回滚性判定（prev_version 存在且旧版本目录完好）。"""
    db = request.app.state.db
    paths = request.app.state.paths
    out: list[dict[str, Any]] = []
    for row in db.list_modules():
        manifest: dict[str, Any] | None = None
        if row.get("manifest_json"):
            try:
                manifest = json.loads(row["manifest_json"])
            except ValueError:
                manifest = None  # 入库数据异常不阻断列表展示
        prev = row.get("prev_version")
        out.append({
            "id": row["id"],
            "name": row.get("name"),
            "type": row.get("type"),
            "version": row.get("version"),
            "prev_version": prev,
            "enabled": bool(row.get("enabled")),
            "installed_at": row.get("installed_at"),
            # 当前指针目录是否完好（异常时 UI 可提示重装）
            "dir_ok": module_dir_ok(paths, row["id"], row["version"]) if row.get("version") else False,
            # 可回滚 = 有回滚指针且旧版本目录仍完好
            "rollbackable": bool(prev) and module_dir_ok(paths, row["id"], prev),
            "manifest": manifest,
        })
    return {"modules": out}


@router.post("/modules/install")
def install_module(request: Request, body: InstallBody) -> dict[str, Any]:
    """创建 module_install 任务；进度经 GET /tasks/{id}/events 订阅。"""
    db = request.app.state.db
    task_id = str(uuid.uuid4())
    db.create_task(task_id, "module_install", None, {"kmod_path": body.kmod_path})
    request.app.state.scheduler.notify()
    return {"task_id": task_id}


@router.post("/modules/rollback")
def rollback_module(request: Request, body: RollbackBody) -> Any:
    """同步回滚到上一版本；失败返回错误三级呈现结构（人话/建议/详情）。"""
    db = request.app.state.db
    paths = request.app.state.paths
    try:
        return rollback(db, paths, body.module_id)
    except DocFactoryError as exc:
        return JSONResponse(status_code=400, content=error_payload(exc.code, exc.detail))
