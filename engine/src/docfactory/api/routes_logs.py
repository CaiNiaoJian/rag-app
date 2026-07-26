"""日志查询与诊断包（02 章 §2.1，08 章 §1）。

- GET  /logs              查 task_events（结构化日志）：level / task_id / doc_id / q + 分页。
                          UI 日志页与失败抽屉的「相关日志」都走这里。
- POST /logs/diagnostics  打包 logs/ + 系统信息 + 最近 500 条 task_events 成 zip，
                          落 ``{root}/diagnostics-{时间戳}.zip``，只回路径（文件本体由 UI 用
                          shell.showItemInFolder 呈现，不经 HTTP 传输）。

**隐私红线**：诊断包只含引擎日志、事件元数据与环境信息，**不含任何文档内容**——
日志侧由 logsetup 的 diagnose=False 保证，事件侧由 task_events 只记文件名与元数据保证。
体积红线：日志按新→旧装入，累计超过 _LOG_BUDGET 就停（用户要的是最近这次故障的现场，
不是全部历史；一个几百 MB 的 zip 谁也发不出来）。
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from loguru import logger

from docfactory import API_VERSION, APP_NAME, ENGINE_VERSION, IR_VERSION, SCHEMA_VERSION
from docfactory.db import now_iso
from docfactory.errors import DocFactoryError

router = APIRouter()

_MB = 1024 * 1024
_LOG_BUDGET = 20 * _MB       # 装入诊断包的日志原始字节上限
_ZIP_MARGIN = 8 * _MB        # 磁盘预检余量（压缩产物远小于原始日志，留够即可）
_EVENT_LIMIT = 500           # 随包附带的最近事件条数


@router.get("/logs")
def list_logs(
    request: Request,
    level: Annotated[str | None, Query()] = None,
    task_id: Annotated[str | None, Query()] = None,
    doc_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """结构化日志查询（时间倒序）。q 同时匹配 message 与错误码。"""
    items, total = request.app.state.db.query_events(
        level=level, task_id=task_id, doc_id=doc_id, q=q, page=page, page_size=page_size
    )
    return {"items": items, "total": total}


@router.post("/logs/diagnostics")
def export_diagnostics(request: Request) -> dict[str, Any]:
    """导出诊断包，返回 {"path": ...}。"""
    db = request.app.state.db
    paths = request.app.state.paths

    log_files = _pick_log_files(paths.logs)
    needed = sum(size for _, size in log_files) + _ZIP_MARGIN
    try:
        free = shutil.disk_usage(paths.root).free
    except OSError:
        free = needed  # 拿不到磁盘信息就别拦着用户导出，真写不下 zip 会自己报错
    if free < needed:
        raise DocFactoryError(
            "E07", f"导出诊断包需要约 {needed // _MB} MB，当前可用 {free // _MB} MB"
        )

    events, _ = db.query_events(page=1, page_size=_EVENT_LIMIT)
    system = _system_info(request, [f.name for f, _ in log_files])

    out_path = paths.root / f"diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file, _size in log_files:
                # 日志文件可能正被 loguru 持有写句柄；Windows 下同时读是允许的，
                # 单个文件读失败也只跳过它，不能让整个诊断包导不出来
                try:
                    zf.write(file, f"logs/{file.name}")
                except OSError as exc:
                    logger.warning(f"诊断包跳过日志文件 {file.name}：{exc}")
            zf.writestr("system.json", _dump(system))
            zf.writestr("events.json", _dump(events))
            zf.writestr("README.txt", _README)
    except OSError as exc:
        out_path.unlink(missing_ok=True)  # 半成品 zip 只会误导人
        raise DocFactoryError("E07", f"诊断包写入失败：{exc.strerror or exc}") from exc

    logger.info(f"诊断包已导出：{out_path.name}")
    return {"path": str(out_path)}


# ---------------------------------------------------------------- 内容采集

_README = (
    "DocFactory 诊断包\n"
    "  logs/        引擎运行日志（JSONL，按新→旧截取）\n"
    "  system.json  版本、运行环境、设置快照、任务与数据统计\n"
    "  events.json  最近 500 条结构化事件（含错误码与阶段）\n"
    "本包不含任何文档内容，仅含文件名与元数据。\n"
)


def _pick_log_files(logs_dir: Path) -> list[tuple[Path, int]]:
    """按修改时间新→旧收集日志文件，累计到预算上限为止。"""
    entries: list[tuple[float, Path, int]] = []
    try:
        candidates = list(logs_dir.iterdir())
    except OSError:
        return []
    for f in candidates:
        try:
            if f.is_file():
                stat = f.stat()
                entries.append((stat.st_mtime, f, stat.st_size))
        except OSError:
            continue
    entries.sort(key=lambda e: e[0], reverse=True)

    picked: list[tuple[Path, int]] = []
    used = 0
    for _mtime, f, size in entries:
        if picked and used + size > _LOG_BUDGET:
            break
        picked.append((f, size))
        used += size
    return picked


def _system_info(request: Request, log_names: list[str]) -> dict[str, Any]:
    """环境与运行态快照：定位问题最常需要的那几项，全部本机可得，无任何外联。"""
    db = request.app.state.db
    paths = request.app.state.paths
    scheduler = request.app.state.scheduler

    total_mb, avail_mb = _memory_mb()
    disk_total, disk_free = _disk_mb(paths.root)

    with db.connect() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in ("documents", "tasks", "chunks", "task_events", "modules")
        }

    return {
        "generated_at": now_iso(),
        "app": {
            "name": APP_NAME,
            "engine_version": ENGINE_VERSION,
            "api_version": API_VERSION,
            "ir_version": IR_VERSION,
            "schema_version": SCHEMA_VERSION,
            "db_schema_version": db.schema_version(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "resources": {
            "cpu_count": os.cpu_count(),
            "memory_total_mb": total_mb,
            "memory_available_mb": avail_mb,
            "disk_total_mb": disk_total,
            "disk_free_mb": disk_free,
        },
        # 只给目录位置，便于用户按图索骥；不含目录内的任何文件内容
        "paths": {"root": str(paths.root), "workspace": str(paths.workspace)},
        "settings": request.app.state.settings.get().model_dump(),
        "scheduler": scheduler.snapshot(),
        "counts": counts,
        "modules": db.list_modules(),
        "logs_included": log_names,
    }


def _memory_mb() -> tuple[int | None, int | None]:
    """物理内存总量/可用（MB）。psutil 是可选依赖，缺失时退回 Windows 原生接口。"""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return int(vm.total // _MB), int(vm.available // _MB)
        except Exception:  # noqa: BLE001 —— 诊断信息缺一项也不该让导出失败
            return None, None

    if sys.platform == "win32":
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys // _MB), int(status.ullAvailPhys // _MB)
        except OSError:
            return None, None
    return None, None


class _MemoryStatusEx(ctypes.Structure):
    """Win32 MEMORYSTATUSEX：用 ctypes 直接问系统，省掉一个第三方依赖。"""

    _fields_ = (
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def _disk_mb(root: Path) -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None, None
    return int(usage.total // _MB), int(usage.free // _MB)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
