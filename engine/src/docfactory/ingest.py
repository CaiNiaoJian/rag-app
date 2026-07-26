"""导入层：磁盘预检 · 文件探测 · 复制入库（02 章 §3 磁盘治理、03 章 §1 格式路由表）。

职责边界：本模块只负责「把用户选中的文件安全地搬进 workspace 并登记」，不做任何解析。

两个刻意的设计取舍：
1. **源文件必须复制一份进 workspace**。之后所有阶段（解析/重解析/导出）只读这份副本，
   用户随后移动、改名、删除原文件都不影响已导入文档 —— 离线桌面产品里用户对文件的
   操控极其随意，把原路径当作长期依赖是错误的。
2. **documents.src_path 存「用户导入时的原始路径」**，workspace 副本路径由
   ``workspace_source(paths, doc_id, fmt)`` 从 doc_id + fmt 推导，不入库。
   同一事实只有一处真相；副本路径是纯函数结果，没必要再存一列等它和目录布局失配。

去重语义：SHA-256 命中已有文档时**照常导入**（用户可能真的想再解析一遍），
只在返回值里带 ``duplicate_of`` 供 UI 提示「这个文件你已经导入过」。
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from docfactory.config import Paths
from docfactory.db import Database
from docfactory.errors import DocFactoryError

# 支持的输入格式（03 章 §1；旧格式 doc/ppt/xls 由 LibreOffice 归一化后进主管线）
SUPPORTED_EXTS: frozenset[str] = frozenset({"doc", "docx", "pdf", "ppt", "pptx", "xls", "xlsx"})

# 单文件体积上限：超过这个量级的 Office/PDF 基本是嵌了大量位图的异常文件，
# 解析必然打爆内存预算（03 章 §8 ≤4GB），不如在导入口就拦下并给出拆分建议。
MAX_FILE_BYTES = 500 * 1024 * 1024

# 磁盘预检系数（02 章 §3）：源文件副本 + 解析产物(IR/md/assets/preview) + 导出产物
# 峰值约为源文件的 3 倍，再留 2GB 给数据库、日志与系统本身。
DISK_FACTOR = 3
DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024

_HASH_CHUNK = 1024 * 1024  # 1MB/次：大文件也不整块进内存


def workspace_source(paths: Paths, doc_id: str, fmt: str) -> Path:
    """workspace 内源文件副本路径：``workspace/{docId}/source.{ext}``（02 章 §3）。"""
    return paths.doc_dir(doc_id) / f"source.{fmt}"


def required_free_bytes(source_bytes: int) -> int:
    """导入 ``source_bytes`` 字节的源文件所需的最小剩余空间（02 章 §3 公式）。"""
    return source_bytes * DISK_FACTOR + DISK_RESERVE_BYTES


def check_disk(paths: Paths, needed_bytes: int) -> None:
    """导入前磁盘预检（E07 事前预防）。

    ``needed_bytes`` 传**本批源文件总大小**，函数内部按「总大小 × 3 + 2GB」换算实际需求 ——
    调用方（单文件导入 / 批量拖入）只需累加源文件大小，不必各自记住系数。
    """
    paths.workspace.mkdir(parents=True, exist_ok=True)
    required = required_free_bytes(max(0, int(needed_bytes)))
    try:
        free = shutil.disk_usage(paths.workspace).free
    except OSError as exc:  # 盘符失效/权限异常：按空间不足处理，给用户可操作的指引
        raise DocFactoryError("E07", f"无法读取数据目录所在磁盘的剩余空间：{exc}") from exc
    if free < required:
        raise DocFactoryError(
            "E07",
            f"导入需要约 {_mb(required)} MB 可用空间（源文件 ×3 + 2GB 余量），"
            f"数据目录所在磁盘当前剩余 {_mb(free)} MB",
        )


def _mb(n: int) -> int:
    return int(n) // (1024 * 1024)


def sha256_file(path: Path) -> str:
    """分块计算 SHA-256（十六进制小写）。文件被占用/读失败一律抛 E01。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(_HASH_CHUNK)
                if not block:
                    break
                h.update(block)
    except PermissionError as exc:
        raise DocFactoryError("E01", _busy_detail(path)) from exc
    except OSError as exc:
        raise DocFactoryError("E01", f"读取文件失败：{path.name}（{exc.strerror or exc}）") from exc
    return h.hexdigest()


def _busy_detail(path: Path) -> str:
    return (
        f"文件「{path.name}」正被其他程序占用或无读取权限，"
        f"请关闭源程序（Word/Excel/PowerPoint/PDF 阅读器）后重试"
    )


def probe_file(src_path: Path) -> dict[str, Any]:
    """轻量探测：只读文件元信息判断能否导入，**不落盘、不算哈希、不入库**。

    供 UI 在拖入文件的瞬间给出「支持/不支持」反馈（拖 100 个文件也是毫秒级）；
    ``is_kmod`` 单独标出来，是因为 .kmod 拖到文档区是高频误操作，
    上层据此引导去「设置 → 模组」安装而不是报一句「格式不支持」。
    """
    p = Path(src_path)
    try:
        st = p.stat()
    except OSError as exc:
        raise DocFactoryError("E01", f"未找到文件或无法读取：{p}（{exc.strerror or exc}）") from exc
    if not p.is_file():
        raise DocFactoryError("E01", f"不是一个文件：{p}")
    ext = p.suffix.lower().lstrip(".")
    return {
        "name": p.name,
        "ext": ext,
        "size": st.st_size,
        "supported": ext in SUPPORTED_EXTS,
        "is_kmod": ext == "kmod",
    }


def import_file(db: Database, paths: Paths, src_path: Path) -> dict[str, Any]:
    """导入单个文件：校验 → 磁盘预检 → 算哈希查重 → 复制进 workspace → 建 documents 行。

    返回：``{doc_id, name, fmt, size, hash, duplicate_of, src_path, stored_path}``；
    ``status`` 落库为 ``imported``，解析由调用方另建 parse 任务触发（导入与解析解耦，
    UI 才能做到「先秒级入库列出来，再排队慢慢解析」）。

    失败纪律：任一步失败都清掉本次新建的 ``workspace/{docId}/`` 目录，不留半成品。
    """
    src = Path(src_path)
    info = probe_file(src)
    ext: str = info["ext"]
    size: int = info["size"]

    if info["is_kmod"]:
        raise DocFactoryError("E03", f"「{info['name']}」是模组安装包，请到「设置 → 模组」中安装")
    if not info["supported"]:
        raise DocFactoryError(
            "E03",
            f"暂不支持 .{ext or '(无扩展名)'} 格式；支持：{'、'.join(sorted(SUPPORTED_EXTS))}",
        )
    if size <= 0:
        raise DocFactoryError("E05", f"文件「{info['name']}」是空文件，没有可提取的内容")
    if size > MAX_FILE_BYTES:
        raise DocFactoryError(
            "E05",
            f"文件「{info['name']}」约 {_mb(size)} MB，超过单文件 {_mb(MAX_FILE_BYTES)} MB 上限，"
            f"建议在原程序中拆分后分批导入",
        )

    check_disk(paths, size)
    digest = sha256_file(src)
    duplicate = db.find_document_by_hash(digest)

    doc_id = str(uuid.uuid4())
    dest = workspace_source(paths, doc_id, ext)
    doc_dir = dest.parent
    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        # copy2 保留 mtime：文档库按「原文件修改时间」排序/核对时有据可依
        shutil.copy2(src, dest)
        db.insert_document({
            "id": doc_id,
            "name": info["name"],
            "src_path": str(src),          # 原始来源路径（副本路径由 workspace_source 推导）
            "fmt": ext,
            "size": size,
            "hash": digest,
            "status": "imported",
            "ir_version": None,
        })
    except PermissionError as exc:
        _cleanup(doc_dir)
        raise DocFactoryError("E01", _busy_detail(src)) from exc
    except OSError as exc:
        _cleanup(doc_dir)
        if getattr(exc, "errno", None) == 28:  # ENOSPC：复制到一半没空间了
            raise DocFactoryError("E07", f"复制文件时磁盘空间耗尽：{info['name']}") from exc
        raise DocFactoryError(
            "E01", f"复制文件失败：{info['name']}（{exc.strerror or exc}）"
        ) from exc
    except Exception:
        _cleanup(doc_dir)
        raise

    db.bump_metrics(imported=1)
    db.log_event(
        level="info",
        doc_id=doc_id,
        message=f"已导入「{info['name']}」（{_mb(size)} MB）",
        detail={"hash": digest, "duplicate_of": duplicate["id"] if duplicate else None},
    )
    return {
        "doc_id": doc_id,
        "name": info["name"],
        "fmt": ext,
        "size": size,
        "hash": digest,
        "duplicate_of": duplicate["id"] if duplicate else None,
        "src_path": str(src),
        "stored_path": str(dest),
    }


def _cleanup(doc_dir: Path) -> None:
    """尽力删除半成品目录；清理失败绝不掩盖真正的错误。"""
    with suppress(OSError):
        shutil.rmtree(doc_dir, ignore_errors=True)
