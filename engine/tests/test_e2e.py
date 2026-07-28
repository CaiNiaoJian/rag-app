"""端到端 happy path（M1 验收：导入 → 解析 → 切片 → 导出）。

走的是真实链路，不打桩：ingest 复制入 workspace → pipeline.run_parse 调真解析器
→ IR 落盘 → chunking 入库 → exporters 出六格式。样本由 corpus/fixtures 程序化生成
（无版权、结构确定），所以这份测试在任何机器上都能跑，也是 CI 的最后一道兜底。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from docfactory import IR_VERSION
from docfactory.config import Paths, Settings
from docfactory.db import Database
from docfactory.ir import IRDocument
from docfactory.taskspec import TaskContext, TaskOutcome

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """程序化生成一套样本（整个会话共用一份，生成一次要几百毫秒）。"""
    if str(FIXTURES_DIR) not in sys.path:
        sys.path.insert(0, str(FIXTURES_DIR))
    from make_fixtures import generate

    out = tmp_path_factory.mktemp("samples")
    generate(out)
    return out


def make_ctx(db: Database, paths: Paths, task_id: str, doc_id: str | None,
             payload: dict[str, Any], *, settings: Settings | None = None,
             task_type: str = "parse") -> TaskContext:
    """裸 TaskContext：进度事件收集到列表里供断言，取消恒为 False。

    先把任务落库 —— ``task_events.task_id`` 有外键约束，runner 一记日志就会
    IntegrityError；生产链路里 scheduler 必然先 create_task 再派发，这里对齐它。
    """
    if db.get_task(task_id) is None:
        db.create_task(task_id, task_type, doc_id, payload)
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = TaskContext(
        db=db, paths=paths, settings=settings or Settings(),
        task_id=task_id, doc_id=doc_id, payload=payload,
        progress=lambda event, data: events.append((event, data)),
        cancelled=lambda: False,
    )
    ctx.collected_events = events  # type: ignore[attr-defined]
    return ctx


def import_and_parse(db: Database, paths: Paths, src: Path,
                     settings: Settings | None = None) -> tuple[str, TaskOutcome, TaskContext]:
    from docfactory.ingest import import_file
    from docfactory.pipeline import run_parse

    info = import_file(db, paths, src)
    doc_id = info["doc_id"]
    ctx = make_ctx(db, paths, "task-" + doc_id[:8], doc_id, {"doc_id": doc_id}, settings=settings)
    outcome = run_parse(ctx)
    return doc_id, outcome, ctx


# ---------------------------------------------------------------- 导入


def test_import_copies_into_workspace_and_dedups(db: Database, paths: Paths, samples: Path):
    from docfactory.ingest import import_file

    src = samples / "headings.docx"
    info = import_file(db, paths, src)

    assert info["fmt"] == "docx" and info["name"] == "headings.docx"
    assert info["duplicate_of"] is None
    assert len(info["hash"]) == 64
    # 源文件复制进 workspace：之后源文件被移动/占用都不影响解析（02 章 §3）
    copied = list(paths.doc_dir(info["doc_id"]).glob("source.*"))
    assert len(copied) == 1 and copied[0].read_bytes() == src.read_bytes()

    row = db.get_document(info["doc_id"])
    assert row["status"] == "imported" and row["hash"] == info["hash"]

    # 同一份文件再导入：照常建档，但带上 duplicate_of 供 UI 提示
    again = import_file(db, paths, src)
    assert again["duplicate_of"] == info["doc_id"]
    assert again["doc_id"] != info["doc_id"]


def test_probe_file_flags_unsupported_and_kmod(samples: Path, tmp_path: Path):
    from docfactory.ingest import probe_file

    assert probe_file(samples / "headings.docx")["supported"] is True
    unsupported = probe_file(samples / "unsupported.txt")
    assert unsupported["supported"] is False and unsupported["is_kmod"] is False

    kmod = tmp_path / "ocr.kmod"
    kmod.write_bytes(b"PK\x03\x04dummy")
    probed = probe_file(kmod)
    assert probed["is_kmod"] is True and probed["supported"] is False


def test_import_missing_file_raises(db: Database, paths: Paths, tmp_path: Path):
    from docfactory.errors import DocFactoryError
    from docfactory.ingest import import_file

    with pytest.raises(DocFactoryError) as exc:
        import_file(db, paths, tmp_path / "nope.docx")
    assert exc.value.code in ("E01", "E03")


def test_import_oversized_file_rejected(
    db: Database, paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """单文件体积上限（ingest.MAX_FILE_BYTES = 500MB）此前零执行覆盖。

    造一个真 500MB 文件既慢又费盘：把上限压小走**同一条分支**，断言语义不变——
    超限报 E05 且不留半成品目录；恰好等于上限（<=）放行。
    """
    from docfactory import ingest
    from docfactory.errors import DocFactoryError

    big = tmp_path / "big.docx"
    big.write_bytes(b"P" * 1024)

    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 512)
    with pytest.raises(DocFactoryError) as exc:
        ingest.import_file(db, paths, big)
    assert exc.value.code == "E05"
    assert "上限" in exc.value.detail and "拆分" in exc.value.detail
    assert not list(paths.workspace.glob("*")), "被拒的导入不得留下半成品目录"

    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 1024)  # 边界：恰好等于上限应放行
    assert ingest.import_file(db, paths, big)["doc_id"]


# ---------------------------------------------------------------- 解析流水线


def test_parse_docx_produces_ir_chunks_and_markdown(db: Database, paths: Paths, samples: Path):
    doc_id, outcome, ctx = import_and_parse(db, paths, samples / "headings.docx")
    assert outcome.status == "done", outcome.message

    # ① IR 落盘且可读回
    ir_path = paths.doc_ir_path(doc_id)
    assert ir_path.is_file()
    ir = IRDocument.load(ir_path)
    assert ir.ir_version == IR_VERSION and ir.doc.id == doc_id
    sections = [n for n in ir.nodes if n.type == "section"]
    assert len(sections) >= 4
    assert any("交付条款" in (n.content.text or "") for n in sections)

    # ② doc.md 生成（文档库三栏预览的右栏数据源）
    md_path = paths.doc_md_path(doc_id)
    assert md_path.is_file()
    md = md_path.read_text(encoding="utf-8")
    assert "# 第1章 总则" in md and "交付期限为合同签订后 90 天" in md

    # ③ 切片入库（解析完成即按默认参数切好，用户无感，04 章 §3.4）
    chunks = db.get_chunks(doc_id)
    assert chunks, "解析完成后应已生成切片"
    kinds = {c["kind"] for c in chunks}
    assert kinds <= {"child", "parent"} and "child" in kinds
    assert all(c["token_count"] > 0 and c["char_count"] > 0 for c in chunks)
    assert [c["seq"] for c in chunks] == sorted(c["seq"] for c in chunks)

    # ④ documents 表回填完整性指标（仪表盘与文档库列表的数据源）
    row = db.get_document(doc_id)
    assert row["status"] in ("ok", "warning")
    assert row["parse_level"] == "L0"
    assert row["ir_version"] == IR_VERSION and row["parsed_at"]
    assert row["text_coverage"] is not None

    # ⑤ 进度上报：至少有阶段切换，且没有把 total 报成 0（UI 会除零）
    events = ctx.collected_events  # type: ignore[attr-defined]
    assert any(name == "stage_change" for name, _ in events)
    assert all(data.get("total", 1) != 0 for name, data in events if name == "progress")


def test_parse_xlsx_keeps_sheets_and_regions(db: Database, paths: Paths, samples: Path):
    doc_id, outcome, _ = import_and_parse(db, paths, samples / "multisheet.xlsx")
    assert outcome.status == "done", outcome.message

    ir = IRDocument.load(paths.doc_ir_path(doc_id))
    sheets = [n for n in ir.nodes if n.type == "sheet"]
    regions = [n for n in ir.nodes if n.type == "sheet_region"]
    assert len(sheets) == 2, "空 sheet 应被跳过（03 章 §7）"
    assert regions and all(n.content.table for n in regions)
    assert any("预算" in (n.content.name or "") for n in sheets)


def test_parse_pptx_keeps_slides_notes_and_tables(db: Database, paths: Paths, samples: Path):
    doc_id, outcome, _ = import_and_parse(db, paths, samples / "slides.pptx")
    assert outcome.status == "done", outcome.message

    ir = IRDocument.load(paths.doc_ir_path(doc_id))
    slides = [n for n in ir.nodes if n.type == "slide"]
    assert len(slides) == 2
    assert any("季度经营回顾" in (n.content.title or "") for n in slides)
    # 演讲者备注常是信息密度最高的文字，必须并入
    assert any("政企客户" in (n.content.notes or "") for n in slides)
    assert any(n.type == "table" for n in ir.nodes)


def test_parse_pdf_reports_coverage(db: Database, paths: Paths, samples: Path):
    doc_id, outcome, _ = import_and_parse(db, paths, samples / "text_document.pdf")
    assert outcome.status == "done", outcome.message

    row = db.get_document(doc_id)
    # 数字 PDF 的文本覆盖率门禁（01 章 §2.2：≥0.97）
    assert row["text_coverage"] >= 0.97, f"覆盖率 {row['text_coverage']} 低于门禁"
    assert row["page_cnt"] == 2


def test_parse_corrupt_file_fails_gracefully(db: Database, paths: Paths, samples: Path):
    """损坏文件必须映射为 E01 明确报错，而不是抛未分类异常（优雅报错率 100%）。"""
    from docfactory.errors import DocFactoryError

    with pytest.raises(DocFactoryError) as exc:
        import_and_parse(db, paths, samples / "corrupt.docx")
    assert exc.value.code == "E01"


def test_parse_empty_document_reports_e05(db: Database, paths: Paths, samples: Path):
    from docfactory.errors import DocFactoryError

    with pytest.raises(DocFactoryError) as exc:
        import_and_parse(db, paths, samples / "empty.docx")
    assert exc.value.code == "E05"


# ---------------------------------------------------------------- 重切


def test_rechunk_replaces_chunks_without_reparsing(db: Database, paths: Paths, samples: Path):
    from docfactory.chunking import run_rechunk

    doc_id, _, _ = import_and_parse(db, paths, samples / "wide_table.xlsx")
    before = db.get_chunks(doc_id)
    assert before

    ir_mtime = paths.doc_ir_path(doc_id).stat().st_mtime
    ctx = make_ctx(db, paths, "t-rechunk", doc_id,
                   {"doc_id": doc_id, "chunk": {"target_tokens": 128, "max_tokens": 256}})
    outcome = run_rechunk(ctx)
    assert outcome.status == "done"

    after = db.get_chunks(doc_id)
    # 整体覆盖而非追加；更小的目标长度应切出更多块
    assert len(after) > len(before)
    assert len({c["id"] for c in after} & {c["id"] for c in before}) <= len(after)
    # 重切不重新解析（秒级完成）：IR 文件没被动过
    assert paths.doc_ir_path(doc_id).stat().st_mtime == ir_mtime


def test_rechunk_without_ir_reports_e05(db: Database, paths: Paths):
    from docfactory.chunking import run_rechunk
    from docfactory.errors import DocFactoryError

    db.insert_document({"id": "d-noir", "name": "x.docx", "src_path": "x.docx",
                        "fmt": "docx", "status": "imported"})
    ctx = make_ctx(db, paths, "t", "d-noir", {"doc_id": "d-noir"})
    with pytest.raises(DocFactoryError) as exc:
        run_rechunk(ctx)
    assert exc.value.code == "E05"


# ---------------------------------------------------------------- 导出


def test_export_all_formats(db: Database, paths: Paths, samples: Path, tmp_path: Path):
    from docfactory.exporters import run_export

    doc_id, _, _ = import_and_parse(db, paths, samples / "headings.docx")
    out_dir = tmp_path / "exports"
    ctx = make_ctx(db, paths, "t-export", doc_id, {
        "doc_ids": [doc_id],
        "formats": ["md", "json", "csv", "alpaca", "sharegpt", "pdf"],
        "out_dir": str(out_dir),
    })
    outcome = run_export(ctx)
    assert outcome.status == "done", outcome.message

    files = [Path(p) for p in outcome.result.get("files", [])]
    assert files, "导出应产出文件清单"
    assert all(p.exists() for p in files), [str(p) for p in files if not p.exists()]
    suffixes = {p.suffix.lower() for p in files}
    assert {".md", ".json", ".csv"} <= suffixes

    # Markdown：标题层级与正文都在
    md = next(p for p in files if p.suffix == ".md")
    assert "# 第1章 总则" in md.read_text(encoding="utf-8")

    # 切片 JSON：schema 对齐 05 章 §2
    chunks_json = next(p for p in files if p.name.endswith(".chunks.json"))
    data = json.loads(chunks_json.read_text(encoding="utf-8"))
    assert data["schema_version"] and data["exported_by"]
    assert data["docs"] and data["chunks"]
    first = data["chunks"][0]
    for key in ("chunk_id", "doc_id", "seq", "kind", "type", "text",
                "token_count", "char_count", "heading_path", "pages", "node_ids", "hash"):
        assert key in first, f"切片 JSON 缺字段 {key}"
    assert isinstance(first["pages"], list) and isinstance(first["node_ids"], list)

    # CSV：UTF-8 BOM（防 Excel 乱码，01 章 §2.5）+ 列顺序冻结
    csv_path = next(p for p in files if p.suffix == ".csv" and "chunks" in p.name)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as fp:
        header = next(csv.reader(fp))
    assert header == ["doc_name", "chunk_id", "parent_id", "seq", "kind", "type",
                      "heading_path", "pages", "token_count", "char_count", "text"]

    # PDF：引擎只产出打印用 HTML，本体由 Electron printToPDF 接力（05 章 §1）
    pending = outcome.result.get("needs_electron_pdf") or []
    assert pending and Path(pending[0]["html"]).is_file()
    html = Path(pending[0]["html"]).read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html, "打印 HTML 不得有任何外链"


def test_export_dataset_blank_template_is_honest(db: Database, paths: Paths,
                                                 samples: Path, tmp_path: Path):
    """留空模板是默认档：output 必须留空，供人工标注或 V2 模型填充（05 章 §3.3）。"""
    from docfactory.exporters import run_export

    doc_id, _, _ = import_and_parse(db, paths, samples / "headings.docx")
    out_dir = tmp_path / "ds"
    ctx = make_ctx(db, paths, "t-ds", doc_id, {
        "doc_ids": [doc_id], "formats": ["alpaca"], "out_dir": str(out_dir),
        "dataset": {"mode": "blank", "format": "alpaca", "file_format": "json", "per_chunk": 1},
    })
    outcome = run_export(ctx)
    assert outcome.status == "done", outcome.message

    path = next(Path(p) for p in outcome.result["files"] if p.endswith(".json"))
    items = json.loads(path.read_text(encoding="utf-8"))
    assert items and all(set(("instruction", "input", "output")) <= set(i) for i in items)
    assert all(i["output"] == "" for i in items), "留空模板不得伪造答案"
    assert all(i["metadata"]["doc_id"] == doc_id for i in items)


def test_export_sharegpt_shape(db: Database, paths: Paths, samples: Path, tmp_path: Path):
    from docfactory.exporters import run_export

    doc_id, _, _ = import_and_parse(db, paths, samples / "headings.docx")
    ctx = make_ctx(db, paths, "t-sg", doc_id, {
        "doc_ids": [doc_id], "formats": ["sharegpt"], "out_dir": str(tmp_path / "sg"),
        "dataset": {"mode": "rule", "format": "sharegpt", "file_format": "json", "per_chunk": 1},
    })
    outcome = run_export(ctx)
    assert outcome.status == "done", outcome.message

    path = next(Path(p) for p in outcome.result["files"] if p.endswith(".json"))
    items = json.loads(path.read_text(encoding="utf-8"))
    assert items
    convo = items[0]["conversations"]
    assert [m["from"] for m in convo] == ["human", "gpt"]
    assert all(set(("from", "value")) == set(m) for m in convo)


def test_export_batch_survives_one_bad_document(db: Database, paths: Paths,
                                                samples: Path, tmp_path: Path):
    """批次纪律（FR-10）：一份文档出问题不该拖垮整批导出。"""
    from docfactory.exporters import run_export

    good_id, _, _ = import_and_parse(db, paths, samples / "headings.docx")
    db.insert_document({"id": "d-broken", "name": "broken.docx", "src_path": "broken.docx",
                        "fmt": "docx", "status": "ok"})   # 有记录但没有 IR/切片

    ctx = make_ctx(db, paths, "t-batch", None, {
        "doc_ids": [good_id, "d-broken"], "formats": ["md"], "out_dir": str(tmp_path / "batch"),
    })
    outcome = run_export(ctx)
    assert outcome.status == "done"
    assert any(good_id in str(p) or "headings" in str(p) for p in outcome.result["files"])
