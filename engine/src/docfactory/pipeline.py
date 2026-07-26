"""解析流水线（parse 任务 runner）—— 串起 03/04/05 章的五段处理。

```
documents.status=parsing → 归一化/解析(parsers) → IR 落盘 → doc.md → 切片入库 → 回填指标
```

本文件只做**编排**，不含任何格式知识：格式差异在 parsers/ 里，切片规则在 chunking.py，
Markdown 渲染在 exporters/。这样「加一种格式」「改切片参数」都不需要动流水线。

两个刻意的健壮性设计：

- **doc.md 与切片是延迟 import 的软依赖**：它们由其他模块提供，任一模块尚未落地或出异常时，
  只记 warning 并继续 —— 解析结果（IR）已经是最有价值的产物，不该因为渲染 Markdown 失败
  就把整篇文档判成解析失败。
- **状态判定锚在「是否发生运行时降级/告警」上**（见 ``_decide_status`` 注释）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from loguru import logger

from docfactory import IR_VERSION
from docfactory.config import Paths
from docfactory.db import Database, now_iso
from docfactory.errors import DocFactoryError
from docfactory.ingest import workspace_source
from docfactory.ir import IRDocument
from docfactory.parsers import parse_document
from docfactory.taskspec import EVENT_STAGE_CHANGE, TaskCancelled, TaskContext, TaskOutcome

# 03 章 §5.2：全 L0 且覆盖率达标才算 ok
COVERAGE_OK = 0.97

# 影响文档状态的告警码（03 章 §5.2「有降级页或 E04/E05 → warning」）
_WARNING_CODES = frozenset({"E04", "E05"})


def run_parse(ctx: TaskContext) -> TaskOutcome:
    """parse 任务入口；payload: ``{"doc_id": "..."}``（文档已由 /documents/import 入库）。"""
    started = time.monotonic()
    db: Database = ctx.db
    paths: Paths = ctx.paths

    doc_id = str(ctx.payload.get("doc_id") or ctx.doc_id or "").strip()
    if not doc_id:
        raise DocFactoryError("E03", "缺少参数 doc_id")
    row = db.get_document(doc_id)
    if row is None:
        raise DocFactoryError("E03", f"文档不存在：{doc_id}")

    fmt = str(row.get("fmt") or "").lower()
    src = _resolve_source(paths, doc_id, fmt, row.get("src_path"))

    db.update_document(doc_id, status="parsing")
    try:
        ir = parse_document(
            src=src, doc_id=doc_id, fmt=fmt,
            paths=paths, settings=ctx.settings, ctx=ctx,
        )
    except TaskCancelled:
        # 取消后回到「未解析」而不是停在 parsing —— 否则文档库里会留一行永远转圈的记录
        db.update_document(doc_id, status="imported")
        raise
    except DocFactoryError:
        db.update_document(doc_id, status="failed", parsed_at=now_iso())
        db.bump_metrics(parsed_fail=1, total_ms=_elapsed_ms(started))
        raise
    except Exception:
        db.update_document(doc_id, status="failed", parsed_at=now_iso())
        db.bump_metrics(parsed_fail=1, total_ms=_elapsed_ms(started))
        raise

    # 解析读的是 workspace 副本（文件名恒为 source.docx），但 IR 的 source_file 必须是
    # **用户认识的原始文件名**：导出层拿它当输出文件名（headings.md），预览页拿它当标题。
    # 不回填的话所有文档导出后都叫 source.md，批量导出到同一目录会互相覆盖。
    ir.doc.source_file = str(row.get("name") or ir.doc.source_file)

    ir_path = paths.doc_ir_path(doc_id)
    ir.save(ir_path)

    md_path = _write_markdown(ctx, ir, doc_id)
    chunk_cnt = _write_chunks(ctx, ir, doc_id)

    metrics = ir.doc.metrics
    page_cnt = _page_count(ir)
    status = _decide_status(ctx, ir)
    db.update_document(
        doc_id,
        status=status,
        page_cnt=page_cnt,
        parse_level=ir.doc.parse_level,
        text_coverage=metrics.text_coverage,
        table_confidence=metrics.table_confidence,
        ocr_confidence=metrics.ocr_confidence,
        degraded_pages=metrics.degraded_pages,
        ir_version=ir.ir_version or IR_VERSION,
        parsed_at=now_iso(),
    )
    db.bump_metrics(
        parsed_ok=1 if status == "ok" else 0,
        parsed_warn=1 if status == "warning" else 0,
        chunk_cnt=chunk_cnt,
        ocr_pages=_ocr_pages(ir),
        total_ms=_elapsed_ms(started),
    )

    result: dict[str, Any] = {
        "doc_id": doc_id,
        "status": status,
        "page_cnt": page_cnt,
        "parse_level": ir.doc.parse_level,
        "degraded_pages": metrics.degraded_pages,
        "text_coverage": metrics.text_coverage,
        "chunk_cnt": chunk_cnt,
        "ir_path": str(ir_path),
        "md_path": str(md_path) if md_path else None,
        "node_cnt": len(ir.nodes),
    }
    db.log_event(
        level="info", task_id=ctx.task_id, doc_id=doc_id, stage="parse",
        message=f"解析完成：{page_cnt} 页 / {len(ir.nodes)} 节点 / {chunk_cnt} 切片（{status}）",
        detail=result,
    )
    return TaskOutcome(status="done", message=f"解析完成（{status}）", result=result)


# ---------------------------------------------------------------- 各步骤


def _resolve_source(paths: Paths, doc_id: str, fmt: str, src_path: Any) -> Path:
    """优先用 workspace 副本；副本缺失（老数据/被清理）才退回原始路径。"""
    copied = workspace_source(paths, doc_id, fmt)
    if copied.is_file():
        return copied
    original = Path(str(src_path)) if src_path else None
    if original is not None and original.is_file():
        return original
    raise DocFactoryError(
        "E01", f"源文件缺失：{copied}（原始路径 {src_path or '未知'} 也不可用），请重新导入"
    )


def _write_markdown(ctx: TaskContext, ir: IRDocument, doc_id: str) -> Path | None:
    """生成 parsed/doc.md（预览与「导出 Markdown」共用同一渲染实现）。"""
    target = ctx.paths.doc_md_path(doc_id)
    try:
        from docfactory.exporters import export_markdown

        produced = export_markdown(
            ir,
            ctx.paths.doc_parsed(doc_id),
            cs=ctx.settings.chunk,
            assets_dir=ctx.paths.doc_assets(doc_id),
        )
        produced = Path(produced)
        if produced != target and produced.is_file():
            # 导出器按自己的命名规则产文件；流水线要的是固定的 parsed/doc.md（02 章 §3）
            shutil.move(str(produced), str(target))
        return target if target.is_file() else produced
    except Exception as exc:
        logger.warning(f"生成 doc.md 失败（不影响解析结果）doc={doc_id}：{type(exc).__name__}: {exc}")
        ctx.db.log_event(
            level="warning", task_id=ctx.task_id, doc_id=doc_id, stage="parse",
            message=f"Markdown 预览生成失败：{type(exc).__name__}: {exc}",
        )
        return None


def _write_chunks(ctx: TaskContext, ir: IRDocument, doc_id: str) -> int:
    """解析完成即按默认参数切片入库（04 章 §3.4，用户无感）。"""
    ctx.progress(EVENT_STAGE_CHANGE, {"stage": "chunk"})
    try:
        from docfactory.chunking import chunk_document

        rows = chunk_document(ir, ctx.settings.chunk, doc_id=doc_id)
        return ctx.db.replace_chunks(doc_id, rows)
    except Exception as exc:
        logger.warning(f"切片失败（IR 已保存，可稍后重切）doc={doc_id}：{type(exc).__name__}: {exc}")
        ctx.db.log_event(
            level="warning", task_id=ctx.task_id, doc_id=doc_id, stage="chunk",
            message=f"切片失败，可在导出中心「重切」：{type(exc).__name__}: {exc}",
        )
        return 0


# ---------------------------------------------------------------- 指标与状态


def _page_count(ir: IRDocument) -> int:
    """页数 = prov 里出现过的最大页号（PDF 页 / slide 数 / sheet 数 / docx 估算页）。"""
    pages = [p.page for node in ir.nodes for p in node.prov if p.page]
    return max(pages) if pages else 0


def _ocr_pages(ir: IRDocument) -> int:
    """经过 OCR 的页数（M1 恒为 0；M2 接入 OCR 后自动有值）。"""
    return len({
        p.page for node in ir.nodes if node.content.ocr for p in node.prov
    })


def _decide_status(ctx: TaskContext, ir: IRDocument) -> str:
    """文档最终状态（03 章 §5.2）。

    契约原文是「全部页 L0 且 coverage ≥ 0.97 → ok；有降级页或 E04/E05 → warning」。
    M1 尚无 L0 引擎（Docling 属 M2），若把「非 L0 即 warning」字面执行，
    **每一份 PDF 都会恒为 warning**，警示就此失去意义、用户也无从分辨真正有问题的文档。
    因此这里把判定锚在「本次解析有没有发生运行时降级或质量告警」上：
      - 有降级页（异常/超时/扫描页无 OCR）或 E04/E05 告警 → warning
      - 覆盖率不达标 → warning
      - 否则 → ok
    L0 缺席作为「基线能力」由 parsers 单独记 info 事件，不污染文档状态。
    """
    metrics = ir.doc.metrics
    if metrics.degraded_pages > 0:
        return "warning"
    if metrics.text_coverage is not None and metrics.text_coverage < COVERAGE_OK:
        return "warning"
    if _has_quality_warning(ctx):
        return "warning"
    return "ok"


def _has_quality_warning(ctx: TaskContext) -> bool:
    try:
        events, _ = ctx.db.query_events(
            level="warning", task_id=ctx.task_id, page=1, page_size=200
        )
    except Exception:
        return False
    return any((e.get("code") or "") in _WARNING_CODES for e in events)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
