"""文档库端点：导入 / 列表 / 详情 / IR / 切片 / 页快照 / 删除（02 章 §2.1）。

设计要点：
- **大内容一律传路径**：``/ir`` 与 ``/preview/{page}`` 只回文件路径与摘要，
  正文由 renderer 经 preload 白名单读本地文件（02 章 §2.2）。
- **导入是「整批预检 + 逐个隔离」**：磁盘空间对整批做一次预检（不足直接 E07 拦下，
  避免导到一半没空间产生半成品）；单个文件的失败只进 skipped，不牵连其余文件——
  用户拖 20 个文件时最怕的就是「有一个坏的就全军覆没」（07 章 §2 流程 A）。
- ingest 模块由解析流水线负责，本文件**函数内延迟 import**：该文件尚未落地时，
  只让导入端点报错，文档库的浏览/删除仍可用。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from loguru import logger
from pydantic import BaseModel, Field

from docfactory.errors import DocFactoryError

router = APIRouter()

# 文档仍在处理时删除会与 worker 抢文件（Windows 上直接触发占用错误），先挡住
_ACTIVE_STATUS = ("queued", "running")

# IR 摘要愿意为「数节点数」付出的最大读盘量（超过就只回路径，见 get_document_ir）
_IR_SUMMARY_MAX_BYTES = 16 * 1024 * 1024


class ImportBody(BaseModel):
    """导入请求：绝对路径列表（文件夹由 Electron 侧 files.expandPaths 先展开）。"""

    paths: list[str] = Field(default_factory=list)


def _ingest() -> ModuleType:
    """延迟 import ingest：并行开发期该模块可能尚未落地，不能在模块加载期就炸掉整个路由。"""
    try:
        from docfactory import ingest
    except ImportError as exc:
        raise DocFactoryError("E06", "导入模块尚未就绪") from exc
    return ingest


@router.post("/documents/import")
def import_documents(request: Request, body: ImportBody) -> dict[str, Any]:
    """批量导入：探测 → 磁盘预检 → 复制入库 → 建 parse 任务。

    返回 ``{"imported": [{doc_id, name, task_id, duplicate_of}], "skipped": [{path, name, reason}],
    "items": <同 imported>, "total": N}``；重复文件仍然导入（duplicate_of 只作提示），
    是否合并由用户在 UI 上决定。
    """
    ingest = _ingest()
    db = request.app.state.db
    paths = request.app.state.paths

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    accepted: list[tuple[Path, dict[str, Any]]] = []

    for raw in body.paths:
        src = Path(raw)
        name = src.name or raw
        try:
            if src.is_dir():
                skipped.append(_skip(raw, name, "这是文件夹，请展开后选择其中的文件"))
                continue
            if not src.is_file():
                skipped.append(_skip(raw, name, "文件不存在或无法访问"))
                continue
            info = ingest.probe_file(src)
        except DocFactoryError as exc:
            skipped.append(_skip(raw, name, exc.detail or str(exc)))
            continue
        except OSError as exc:
            skipped.append(_skip(raw, name, f"无法读取文件：{exc.strerror or exc}"))
            continue

        if info.get("is_kmod"):
            # 模组包走 /modules/install，不进文档库（07 章 §2 流程 C：拖到任意界面都能识别）
            skipped.append(_skip(raw, info.get("name", name), "这是模组安装包，请到「设置 → 模组」中安装", is_kmod=True))
            continue
        if not info.get("supported"):
            ext = info.get("ext") or src.suffix.lstrip(".")
            skipped.append(_skip(raw, info.get("name", name), f"暂不支持的文件格式（.{ext}）"))
            continue
        accepted.append((src, info))

    if not accepted:
        return _import_result(imported, skipped)

    # 整批预检（02 章 §3 磁盘治理）：check_disk 内部持有「源文件总大小 × 3 + 2GB」的策略，
    # 这里只负责把本批次的原始字节数报给它。
    needed = sum(int(info.get("size") or 0) for _, info in accepted)
    ingest.check_disk(paths, needed)

    for src, info in accepted:
        try:
            record = ingest.import_file(db, paths, src)
        except DocFactoryError as exc:
            skipped.append(_skip(str(src), info.get("name", src.name), exc.detail or str(exc)))
            continue
        except OSError as exc:
            logger.warning(f"导入失败：{src.name} —— {exc}")
            skipped.append(_skip(str(src), info.get("name", src.name), f"复制文件失败：{exc.strerror or exc}"))
            continue

        doc_id = record["doc_id"]
        task_id = str(uuid.uuid4())
        db.create_task(task_id, "parse", doc_id, {"doc_id": doc_id})
        imported.append({
            "doc_id": doc_id,
            "name": record.get("name"),
            "task_id": task_id,
            "duplicate_of": record.get("duplicate_of"),
        })

    if imported:
        # 全部入队后统一唤醒：避免每建一个任务就抖动一次 worker
        request.app.state.scheduler.notify()
        logger.info(f"导入 {len(imported)} 个文件，跳过 {len(skipped)} 个")
    return _import_result(imported, skipped)


@router.get("/documents")
def list_documents(
    request: Request,
    status: Annotated[str | None, Query()] = None,
    fmt: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """文档列表（导入时间倒序），含完整性指标列。"""
    items, total = request.app.state.db.list_documents(
        status=status, fmt=fmt, q=q, page=page, page_size=page_size
    )
    return {"items": items, "total": total}


@router.get("/documents/{doc_id}")
def get_document(request: Request, doc_id: str) -> dict[str, Any]:
    """文档详情：表行 + 产物路径与存在标志（UI 据此决定哪些视图可点）。"""
    db = request.app.state.db
    paths = request.app.state.paths
    row = _require_document(db, doc_id)

    ir_path = paths.doc_ir_path(doc_id)
    md_path = paths.doc_md_path(doc_id)
    preview_dir = paths.doc_preview(doc_id)
    with db.connect() as conn:
        chunk_count = conn.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE doc_id=?", (doc_id,)
        ).fetchone()["c"]

    return {
        **row,
        "ir_path": str(ir_path),
        "ir_exists": ir_path.is_file(),
        "md_path": str(md_path),
        "md_exists": md_path.is_file(),
        "assets_dir": str(paths.doc_assets(doc_id)),
        "preview_dir": str(preview_dir),
        "preview_pages": _count_previews(preview_dir),
        "exports_dir": str(paths.doc_exports(doc_id)),
        "source_exists": Path(row["src_path"]).is_file() if row.get("src_path") else False,
        "chunk_count": chunk_count,
    }


@router.get("/documents/{doc_id}/ir")
def get_document_ir(request: Request, doc_id: str) -> dict[str, Any]:
    """IR 摘要：只回路径与规模，**不回 IR 本体**（可达数十 MB，走文件读取）。

    超过 ``_IR_SUMMARY_MAX_BYTES`` 的 IR 不做解析：为了数一下节点数就把几十 MB JSON
    读进内存、再展开成上百 MB 的 Python 对象，与「大结果落盘传路径」的初衷正好相反。
    此时 node_count 给 None（UI 只用 path，节点数是锦上添花），size 照常回。
    """
    db = request.app.state.db
    paths = request.app.state.paths
    _require_document(db, doc_id)

    ir_path = paths.doc_ir_path(doc_id)
    if not ir_path.is_file():
        raise HTTPException(status_code=404, detail="该文档尚未解析，暂无 IR 文件")
    try:
        size = ir_path.stat().st_size
        if size > _IR_SUMMARY_MAX_BYTES:
            logger.info(f"IR 文件较大（{size // (1024 * 1024)} MB），跳过摘要解析：{doc_id}")
            return {"path": str(ir_path), "size": size, "ir_version": None, "node_count": None}
        data = json.loads(ir_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DocFactoryError("E01", f"IR 文件损坏：{exc}") from exc
    except OSError as exc:
        raise DocFactoryError("E06", f"IR 文件读取失败：{exc.strerror or exc}") from exc

    nodes = data.get("nodes") if isinstance(data, dict) else None
    return {
        "path": str(ir_path),
        "size": size,
        "ir_version": data.get("ir_version") if isinstance(data, dict) else None,
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
    }


@router.get("/documents/{doc_id}/chunks")
def get_document_chunks(
    request: Request,
    doc_id: str,
    kind: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """切片分页（按 seq 升序）。分页下沉到 SQL：切片正文可能很大，不在内存里切片。"""
    db = request.app.state.db
    _require_document(db, doc_id)

    cond = "WHERE doc_id=?" + (" AND kind=?" if kind else "")
    args: list[Any] = [doc_id] + ([kind] if kind else [])
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM chunks {cond}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM chunks {cond} ORDER BY seq LIMIT ? OFFSET ?",
            (*args, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total}


@router.get("/documents/{doc_id}/preview/{page}")
def get_document_preview(
    request: Request, doc_id: str, page: Annotated[int, PathParam(ge=1)]
) -> dict[str, Any]:
    """页快照路径（workspace/{docId}/preview/p{n}.png）；缺页返回 404 由 UI 显示占位。"""
    db = request.app.state.db
    paths = request.app.state.paths
    _require_document(db, doc_id)

    snapshot = paths.doc_preview(doc_id) / f"p{page}.png"
    if not snapshot.is_file():
        raise HTTPException(status_code=404, detail=f"第 {page} 页快照不存在")
    return {"path": str(snapshot)}


@router.delete("/documents/{doc_id}")
def delete_document(request: Request, doc_id: str) -> dict[str, Any]:
    """删除文档：先删 workspace 目录再删表行。

    顺序是刻意的——目录删失败（Windows 文件占用）时表行还在，用户可以重试；
    反过来先删行会留下无人认领的孤儿目录，只能靠人工清理。
    """
    db = request.app.state.db
    paths = request.app.state.paths
    _require_document(db, doc_id)

    with db.connect() as conn:
        active = conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks WHERE doc_id=? AND status IN ({','.join('?' * len(_ACTIVE_STATUS))})",
            (doc_id, *_ACTIVE_STATUS),
        ).fetchone()["c"]
    if active:
        raise HTTPException(status_code=409, detail="该文档仍有任务在排队或处理中，请先取消任务再删除")

    doc_dir = paths.doc_dir(doc_id)
    shutil.rmtree(doc_dir, ignore_errors=True)
    removed = not doc_dir.exists()
    if not removed:
        logger.warning(f"文档目录未能完全删除（可能被占用）：{doc_dir}")

    with db.connect() as conn:
        # 先清掉「挂在本文档任务下、但事件行自身没带 doc_id」的日志：
        # db.delete_document 按 doc_id 清 task_events、按 doc_id 清 tasks，
        # 而 task_events.task_id 有外键指向 tasks——只要有一条这样的事件残留，
        # 删 tasks 就会 FOREIGN KEY constraint failed，整个删除端点 500。
        conn.execute(
            "DELETE FROM task_events WHERE task_id IN (SELECT id FROM tasks WHERE doc_id=?)",
            (doc_id,),
        )
    db.delete_document(doc_id)
    return {"ok": True, "doc_id": doc_id, "workspace_removed": removed}


# ---------------------------------------------------------------- 辅助


def _import_result(
    imported: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> dict[str, Any]:
    """导入响应：imported/skipped 是主体，items/total 是与其余列表端点一致的别名。

    别名不是冗余——渲染进程对所有列表响应走同一个 ``{items,total}`` 解包器，
    少了这两个键，前端会把「成功导入 N 个」读成 0 个（内容都在，只是它找不到）。
    """
    return {
        "imported": imported,
        "skipped": skipped,
        "items": imported,
        "total": len(imported),
    }


def _skip(path: str, name: str, reason: str, *, is_kmod: bool = False) -> dict[str, Any]:
    """跳过项：reason 是给普通用户看的人话，UI 直接展示在确认清单里。"""
    return {"path": path, "name": name, "reason": reason, "is_kmod": is_kmod}


def _require_document(db: Any, doc_id: str) -> dict[str, Any]:
    row = db.get_document(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")
    return row


def _count_previews(preview_dir: Path) -> int:
    try:
        return sum(1 for _ in preview_dir.glob("p*.png"))
    except OSError:
        return 0
