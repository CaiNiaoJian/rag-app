"""导出层入口：六格式编排 + 各 export_* 的统一再导出（05 章）。

`run_export` 是 export 任务的 runner（scheduler.RUNNERS 指向这里），职责只有编排：
解析 payload → 逐文档逐格式产文件 → 逐单元上报进度 → 汇总结果；
具体渲染分散在 markdown.py / chunks.py / dataset.py / pdfhtml.py。

三条关键纪律：
1. **批次纪律（FR-10）**：单个文档/单个格式失败只记 task_events 并计入 result.failed，
   绝不中断整批 —— 20 个文件的批量导出不该被第 3 个坏文件毁掉。
2. **大结果落盘**：result 里只回路径与计数，不回内容（02 章 §2 约定）。
3. **PDF 只做半程**：引擎产 `.print.html`，在 result 打上 `needs_electron_pdf`
   由 Electron 的 printToPDF 接力（02 章架构：PDF 引擎就是 Chromium，完全离线）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from docfactory.config import (
    ChunkSettings,
    DatasetSettings,
    Paths,
    PdfExportSettings,
    Settings,
)
from docfactory.errors import DocFactoryError
from docfactory.exporters.chunks import (
    CHUNKS_SCHEMA_VERSION,
    CSV_COLUMNS,
    export_chunks_csv,
    export_chunks_json,
)
from docfactory.exporters.dataset import export_alpaca, export_sharegpt
from docfactory.exporters.markdown import (
    collect_image_refs,
    copy_assets,
    default_md_name,
    export_markdown,
    render_markdown,
    safe_filename,
    write_markdown,
)
from docfactory.exporters.pdfhtml import default_html_name, export_pdf_html
from docfactory.ir import IRDocument
from docfactory.taskspec import (
    EVENT_PROGRESS,
    EVENT_STAGE_CHANGE,
    TaskCancelled,
    TaskContext,
    TaskOutcome,
)

__all__ = [
    "CHUNKS_SCHEMA_VERSION",
    "CSV_COLUMNS",
    "SUPPORTED_FORMATS",
    "export_alpaca",
    "export_chunks_csv",
    "export_chunks_json",
    "export_markdown",
    "export_pdf_html",
    "export_sharegpt",
    "merge_settings",
    "render_markdown",
    "resolve_doc_ids",
    "resolve_out_dir",
    "run_export",
    "safe_filename",
]

# 六种导出格式（05 章 §1 矩阵）
SUPPORTED_FORMATS: tuple[str, ...] = ("md", "json", "csv", "alpaca", "sharegpt", "pdf")

# Markdown 与 PDF 永远逐文档产出：合并多篇的版面与书签毫无意义，
# 而切片/数据集类产物合并成一份才是训练管线要的形态。
_PER_DOC_ONLY = frozenset({"md", "pdf"})
_MERGEABLE = frozenset({"json", "csv", "alpaca", "sharegpt"})

_STAGE = "export"


# ---------------------------------------------------------------- payload 解析


def resolve_doc_ids(ctx: TaskContext) -> list[str]:
    raw = ctx.payload.get("doc_ids")
    ids = [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []
    if not ids and ctx.doc_id:
        ids = [ctx.doc_id]
    if not ids:
        raise DocFactoryError("E03", "缺少参数 doc_ids（要导出的文档 id 列表）")
    return list(dict.fromkeys(ids))          # 去重保序：UI 全选 + 单选可能重复传


def _resolve_formats(ctx: TaskContext) -> list[str]:
    raw = ctx.payload.get("formats")
    formats = [str(f).strip().lower() for f in raw if str(f).strip()] if isinstance(raw, list) else []
    if not formats:
        formats = ["md"]
    unknown = [f for f in formats if f not in SUPPORTED_FORMATS]
    if unknown:
        raise DocFactoryError(
            "E03",
            f"不支持的导出格式：{'、'.join(unknown)}；可选 {'、'.join(SUPPORTED_FORMATS)}",
        )
    return list(dict.fromkeys(formats))


def merge_settings(base: Any, override: Any, model: type) -> Any:
    """设置合并：以当前 Settings 为底，payload 里的同名键覆盖（None 视为不覆盖）。"""
    data = base.model_dump()
    if isinstance(override, dict):
        data.update({k: v for k, v in override.items() if v is not None})
    return model.model_validate(data)


def resolve_out_dir(ctx: TaskContext, doc_id: str | None) -> Path:
    """导出目录优先级：payload.out_dir > settings.output_dir > 每文档 exports 目录。

    合并导出（doc_id=None）没有「所属文档」，落到 workspace/exports 作为公共出口
    （05 章 §5 输出目录默认值）。
    """
    explicit = str(ctx.payload.get("out_dir") or "").strip()
    if explicit:
        return Path(explicit)
    configured = str(getattr(ctx.settings, "output_dir", None) or "").strip()
    if configured:
        return Path(configured)
    paths: Paths = ctx.paths
    return paths.doc_exports(doc_id) if doc_id else paths.workspace / "exports"


def _unique_path(path: Path, used: set[str], tag: str) -> Path:
    """同名文档导到同一目录时用 doc_id 前 8 位区分，避免后写的覆盖先写的。"""
    if str(path).casefold() not in used:
        return path
    return path.with_name(f"{tag[:8]}-{path.name}")


def _safe_log(ctx: TaskContext, **fields: Any) -> None:
    """落一条 task_events；失败只降级到文件日志。

    批次纪律（FR-10）不能被日志故障反噬：文件都产出了，不该因为一条审计日志写不进去
    就把整批任务判失败。
    """
    try:
        ctx.db.log_event(task_id=ctx.task_id, stage=_STAGE, **fields)
    except Exception as exc:
        logger.warning(f"导出事件落库失败：{exc}")


def _load_ir(paths: Paths, doc_id: str) -> IRDocument:
    ir_path = paths.doc_ir_path(doc_id)
    if not ir_path.is_file():
        raise DocFactoryError("E05", f"文档尚未解析或 IR 文件缺失：{ir_path.name}")
    try:
        return IRDocument.load(ir_path)
    except (OSError, ValueError) as exc:
        raise DocFactoryError("E01", f"IR 文件无法读取：{exc}") from exc


# ---------------------------------------------------------------- runner


def run_export(ctx: TaskContext) -> TaskOutcome:
    """export 任务入口。

    payload::

        {"doc_ids": [...], "formats": ["md"|"json"|"csv"|"alpaca"|"sharegpt"|"pdf"],
         "out_dir": str|None, "merge": bool,
         "chunk": {...可选覆盖 ChunkSettings}, "dataset": {...可选覆盖 DatasetSettings},
         "pdf_export": {...可选覆盖 PdfExportSettings}}

    result::

        {"files": [...], "needs_electron_pdf": [{"html","pdf"}], "pdf_html": 同上（UI 用名）,
         "failed": [{"doc_id","format","error_code","message"}], "counts": {...},
         "out_dir": str}
    """
    doc_ids = resolve_doc_ids(ctx)
    formats = _resolve_formats(ctx)
    merge = bool(ctx.payload.get("merge"))
    settings: Settings = ctx.settings
    cs: ChunkSettings = merge_settings(settings.chunk, ctx.payload.get("chunk"), ChunkSettings)
    ds: DatasetSettings = merge_settings(
        settings.dataset, ctx.payload.get("dataset"), DatasetSettings
    )
    # PDF 字号与页眉页脚是导出中心右栏的参数（05 章 §5），随本次任务下发而非全局设置：
    # 不接 payload 会让 UI 上的滑杆变成哑控件
    ps: PdfExportSettings = merge_settings(
        settings.pdf_export, ctx.payload.get("pdf_export"), PdfExportSettings
    )

    per_doc_formats = [f for f in formats if f in _PER_DOC_ONLY or (f in _MERGEABLE and not merge)]
    merged_formats = [f for f in formats if f in _MERGEABLE and merge]
    total = len(per_doc_formats) * len(doc_ids) + len(merged_formats)

    files: list[str] = []
    needs_pdf: list[dict[str, str]] = []
    failed: list[dict[str, Any]] = []
    used: set[str] = set()
    all_docs: list[dict[str, Any]] = []
    all_chunks: list[dict[str, Any]] = []
    done = 0

    ctx.progress(EVENT_STAGE_CHANGE, {"stage": _STAGE})
    ctx.progress(EVENT_PROGRESS, {"page": 0, "total": total, "stage": _STAGE})

    def tick() -> None:
        nonlocal done
        done += 1
        ctx.progress(EVENT_PROGRESS, {"page": done, "total": total, "stage": _STAGE})

    def record_failure(doc_id: str | None, fmt: str, exc: Exception) -> None:
        code = exc.code if isinstance(exc, DocFactoryError) else "E06"
        detail = exc.detail if isinstance(exc, DocFactoryError) else f"{type(exc).__name__}: {exc}"
        failed.append({"doc_id": doc_id, "format": fmt, "error_code": code, "message": detail})
        try:
            ctx.db.log_event(
                level="error", task_id=ctx.task_id, doc_id=doc_id, code=code, stage=_STAGE,
                message=f"导出失败（{fmt}）：{detail}",
                detail={"format": fmt, "doc_id": doc_id},
            )
        except Exception as log_exc:
            # 记录失败本身失败时只降级到文件日志：批次纪律不能被日志故障反噬
            logger.warning(f"导出失败事件落库失败：{log_exc}")
        logger.bind(task_id=ctx.task_id, doc_id=doc_id).warning(f"导出失败（{fmt}）：{detail}")

    for doc_id in doc_ids:
        if ctx.cancelled():
            raise TaskCancelled()

        row = ctx.db.get_document(doc_id)
        if row is None:
            missing = DocFactoryError("E03", f"文档不存在：{doc_id}")
            for fmt in per_doc_formats:
                record_failure(doc_id, fmt, missing)
                tick()
            if not per_doc_formats:
                # 纯合并导出时该文档没有对应的进度单元，仍要留下失败记录，
                # 否则它只是从合并产物里凭空消失，用户无从知晓
                record_failure(doc_id, "merged", missing)
            continue

        chunks = ctx.db.get_chunks(doc_id)
        if not chunks:
            _safe_log(
                ctx, level="warning", doc_id=doc_id,
                message=f"文档「{row.get('name')}」没有切片记录，切片类导出将是空文件",
            )
        if merge:
            all_docs.append(row)
            all_chunks.extend(chunks)

        # IR 只在需要时读一次；读失败不影响该文档的 json/csv/数据集导出
        ir: IRDocument | None = None
        ir_error: Exception | None = None
        if any(f in ("md", "pdf") for f in per_doc_formats):
            try:
                ir = _load_ir(ctx.paths, doc_id)
            except Exception as exc:
                ir_error = exc

        base = safe_filename(Path(str(row.get("name") or doc_id)).stem)
        out_dir = resolve_out_dir(ctx, doc_id)

        for fmt in per_doc_formats:
            if ctx.cancelled():
                raise TaskCancelled()
            try:
                if fmt in ("md", "pdf"):
                    if ir is None:
                        raise ir_error or DocFactoryError("E05", "缺少 IR，无法导出")
                    written = _export_doc_ir(
                        fmt, ir, doc_id, base, out_dir, ctx, cs, ps, used, needs_pdf
                    )
                else:
                    written = _export_doc_chunks(fmt, [row], chunks, base, out_dir, ds, used, doc_id)
                used.add(str(written).casefold())
                files.append(str(written))
            except TaskCancelled:
                raise
            except Exception as exc:
                record_failure(doc_id, fmt, exc)
            finally:
                tick()

    # ---- 合并产物（多文档一份），Markdown/PDF 不参与 ----
    if merged_formats:
        merge_dir = resolve_out_dir(ctx, None)
        merge_base = _merge_base_name(all_docs)
        for fmt in merged_formats:
            if ctx.cancelled():
                raise TaskCancelled()
            try:
                written = _export_doc_chunks(
                    fmt, all_docs, all_chunks, merge_base, merge_dir, ds, used, "merged"
                )
                used.add(str(written).casefold())
                files.append(str(written))
            except TaskCancelled:
                raise
            except Exception as exc:
                record_failure(None, fmt, exc)
            finally:
                tick()

    result: dict[str, Any] = {
        "files": files,
        # 同一份数据给两个键：needs_electron_pdf 是引擎侧的语义名，
        # pdf_html 是渲染进程（ExportCenter.tsx）实际读取的键。少了它 UI 会退回
        # 「扫 files 里的 .html」兜底路径，把 PDF 命名成 {doc}.print.pdf（05 章要求 {doc}.pdf）
        "needs_electron_pdf": needs_pdf,
        "pdf_html": needs_pdf,
        "failed": failed,
        "counts": {"docs": len(doc_ids), "files": len(files), "failed": len(failed)},
        "out_dir": str(resolve_out_dir(ctx, doc_ids[0] if not merge else None)),
    }

    if files:
        _safe_log(
            ctx, level="info",
            message=f"导出完成：{len(files)} 个文件，{len(failed)} 个失败",
            detail={"files": len(files), "failed": len(failed)},
        )
        message = f"已导出 {len(files)} 个文件" + (f"，{len(failed)} 个失败" if failed else "")
        return TaskOutcome(status="done", message=message, result=result)

    # 一个文件都没产出：整批失败，用首个错误码回报（比笼统的 E06 更有指导性）
    code = str(failed[0]["error_code"]) if failed else "E05"
    return TaskOutcome(
        status="failed", error_code=code,
        message=str(failed[0]["message"]) if failed else "没有可导出的内容",
        result=result,
    )


# ---------------------------------------------------------------- 单文件产出


def _export_doc_ir(
    fmt: str,
    ir: IRDocument,
    doc_id: str,
    base: str,
    out_dir: Path,
    ctx: TaskContext,
    cs: ChunkSettings,
    ps: PdfExportSettings,
    used: set[str],
    needs_pdf: list[dict[str, str]],
) -> Path:
    """基于 IR 的两种产物：Markdown 与打印用 HTML（PDF 半程）。"""
    assets_dir = ctx.paths.doc_assets(doc_id)
    if fmt == "md":
        # 唯一名必须在**写盘之前**算出：同名文档导到同一目录时，
        # 先写后改名会让第二篇先覆盖掉第一篇的内容，改名搬走的已是错的那份
        target = _unique_path(Path(out_dir) / default_md_name(ir), used, doc_id)
        return write_markdown(ir, target, cs=cs, assets_dir=assets_dir)

    # pdf：先把图片搬到导出目录，再产 HTML（HTML 里的 file:// 才解析得到）
    out_dir.mkdir(parents=True, exist_ok=True)
    copy_assets(collect_image_refs(ir), assets_dir, out_dir)
    html_path = _unique_path(out_dir / default_html_name(base), used, doc_id)
    export_pdf_html(ir, html_path, pdf_settings=ps)
    # 目标 PDF 与 HTML 同名同目录：Electron 侧渲染完直接落在用户预期的位置
    pdf_name = html_path.name.removesuffix(".print.html") + ".pdf"
    needs_pdf.append({"html": str(html_path), "pdf": str(html_path.with_name(pdf_name))})
    return html_path


def _export_doc_chunks(
    fmt: str,
    docs: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    base: str,
    out_dir: Path,
    ds: DatasetSettings,
    used: set[str],
    tag: str,
) -> Path:
    """基于 chunks 的四种产物：切片 JSON/CSV 与 Alpaca/ShareGPT 数据集。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = {
        "json": ".chunks.json",
        "csv": ".chunks.csv",
        "alpaca": ".alpaca." + ds.file_format,
        "sharegpt": ".sharegpt." + ds.file_format,
    }[fmt]
    out_path = _unique_path(out_dir / f"{base}{suffix}", used, tag)
    if fmt == "json":
        return export_chunks_json(docs, chunks, out_path)
    if fmt == "csv":
        return export_chunks_csv(docs, chunks, out_path)
    # 数据集侧的 parent/child 去重由 build_pairs_from_chunks 统一兜底（04 章 §3.3）
    if fmt == "alpaca":
        return export_alpaca(docs, chunks, out_path, ds)
    return export_sharegpt(docs, chunks, out_path, ds)


def _merge_base_name(docs: list[dict[str, Any]]) -> str:
    """合并产物的文件名：单篇沿用文档名，多篇用「合并导出-时间戳」保证可区分。"""
    if len(docs) == 1:
        return safe_filename(Path(str(docs[0].get("name") or "document")).stem)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"合并导出-{stamp}"
