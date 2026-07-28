"""SQLite 数据层（02 章 §4 六表 + §5 迁移策略）。"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from docfactory import SCHEMA_VERSION
from docfactory.config import Paths
from docfactory.db import Database


def _doc(**over):
    base = {
        "id": str(uuid.uuid4()),
        "name": "合同v3.docx",
        "src_path": r"C:\tmp\合同v3.docx",
        "fmt": "docx",
        "size": 1024,
        "hash": "a" * 64,
        "status": "imported",
    }
    base.update(over)
    return base


def test_migrate_is_idempotent(paths: Paths):
    db = Database(paths.db_path)
    assert db.schema_version() == 0
    assert db.migrate(backup_dir=paths.backup) == SCHEMA_VERSION
    # 二次迁移无待办：版本不变，且不产生新备份
    assert db.migrate(backup_dir=paths.backup) == SCHEMA_VERSION
    assert db.schema_version() == SCHEMA_VERSION


def test_six_tables_exist(db: Database):
    with db.connect() as conn:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"documents", "tasks", "chunks", "task_events", "metrics_daily", "modules"} <= names


def test_document_crud_and_hash_lookup(db: Database):
    doc = _doc()
    db.insert_document(doc)
    got = db.get_document(doc["id"])
    assert got is not None and got["name"] == "合同v3.docx"
    assert got["degraded_pages"] == 0        # 默认值由 DAO 补齐
    assert got["created_at"]                  # 时间戳自动写入

    db.update_document(doc["id"], status="ok", text_coverage=0.98)
    assert db.get_document(doc["id"])["status"] == "ok"

    assert db.find_document_by_hash(doc["hash"])["id"] == doc["id"]
    assert db.find_document_by_hash("b" * 64) is None


def test_list_documents_filters_and_paging(db: Database):
    for i in range(7):
        db.insert_document(_doc(name=f"文件{i}.pdf", fmt="pdf", status="ok" if i % 2 else "failed"))
    db.insert_document(_doc(name="表格.xlsx", fmt="xlsx", status="ok"))

    ok_rows, ok_total = db.list_documents(status="ok")
    assert ok_total == len(ok_rows) == 4

    pdf_rows, pdf_total = db.list_documents(fmt="pdf", page_size=3)
    assert pdf_total == 7 and len(pdf_rows) == 3

    hit, total = db.list_documents(q="表格")
    assert total == 1 and hit[0]["fmt"] == "xlsx"


def test_claim_next_queued_is_atomic_and_ordered(db: Database):
    for i in range(3):
        db.create_task(f"t{i}", "parse", None, {"n": i})
    first = db.claim_next_queued()
    assert first is not None and first["id"] == "t0" and first["status"] == "running"
    # 已领取的不会被重复领取
    assert db.claim_next_queued()["id"] == "t1"
    assert db.claim_next_queued()["id"] == "t2"
    assert db.claim_next_queued() is None


def test_mark_interrupted_only_touches_running(db: Database):
    # created_at 是秒级，同秒内 claim 按 id 兜底排序 → 用 t1/t2 保证领取顺序确定
    db.create_task("t1", "parse", None, {})
    db.create_task("t2", "parse", None, {})
    assert db.claim_next_queued()["id"] == "t1"
    assert db.mark_interrupted() == 1
    assert db.get_task("t1")["status"] == "interrupted"
    assert db.get_task("t2")["status"] == "queued"


def test_replace_chunks_overwrites_whole_document(db: Database):
    doc = _doc()
    db.insert_document(doc)
    rows = [
        {
            "id": f"c-{i:04d}", "doc_id": doc["id"], "seq": i, "parent_id": None,
            "kind": "child", "type": "text", "text": f"段落{i}",
            "token_count": 10, "char_count": 3, "heading_path": "第1章",
            "pages": "[1]", "node_ids": '["n1"]', "meta_json": "{}", "hash": "sha256:x",
        }
        for i in range(3)
    ]
    assert db.replace_chunks(doc["id"], rows) == 3
    assert len(db.get_chunks(doc["id"])) == 3
    # 重切语义：整体覆盖而非追加（04 章 §3.4）
    assert db.replace_chunks(doc["id"], rows[:1]) == 1
    assert len(db.get_chunks(doc["id"])) == 1


def test_delete_document_cascades(db: Database):
    doc = _doc()
    db.insert_document(doc)
    db.create_task("t1", "parse", doc["id"], {})
    db.log_event(level="info", message="x", task_id="t1", doc_id=doc["id"])
    db.replace_chunks(doc["id"], [{
        "id": "c-0001", "doc_id": doc["id"], "seq": 0, "parent_id": None, "kind": "child",
        "type": "text", "text": "t", "token_count": 1, "char_count": 1,
        "heading_path": None, "pages": "[]", "node_ids": "[]", "meta_json": "{}", "hash": "h",
    }])

    db.delete_document(doc["id"])
    assert db.get_document(doc["id"]) is None
    assert db.get_chunks(doc["id"]) == []
    assert db.get_task("t1") is None
    assert db.query_events(doc_id=doc["id"])[1] == 0


def test_query_events_filters(db: Database):
    # task_events.task_id 有外键约束：事件必须挂在真实任务上（这正是我们要的纪律）
    db.create_task("t1", "parse", None, {})
    db.create_task("t2", "parse", None, {})
    db.log_event(level="error", code="E01", message="文件损坏", task_id="t1")
    db.log_event(level="warning", code="DGR-L1", message="第 3 页降级", task_id="t1", page=3)
    db.log_event(level="info", message="任务开始", task_id="t2")

    assert db.query_events(level="error")[1] == 1
    assert db.query_events(task_id="t1")[1] == 2
    assert db.query_events(q="降级")[1] == 1
    assert db.query_events(q="E01")[1] == 1     # q 同时匹配 code


def test_bump_metrics_accumulates_and_ignores_unknown(db: Database):
    db.bump_metrics(imported=2, parsed_ok=1)
    db.bump_metrics(imported=3, ocr_pages=5, 不存在的列=9)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM metrics_daily").fetchone()
    assert row["imported"] == 5 and row["parsed_ok"] == 1 and row["ocr_pages"] == 5


def test_module_version_pointer(db: Database):
    db.upsert_module(id="ocr-hp", name="高精度 OCR", type="ocr", version="2.1", manifest={"id": "ocr-hp"})
    db.upsert_module(id="ocr-hp", name="高精度 OCR", type="ocr", version="2.3",
                     manifest={"id": "ocr-hp"}, prev_version="2.1")
    row = db.get_module("ocr-hp")
    assert row["version"] == "2.3" and row["prev_version"] == "2.1"

    db.set_module_version("ocr-hp", "2.1", None)
    row = db.get_module("ocr-hp")
    assert row["version"] == "2.1" and row["prev_version"] is None
    assert [m["id"] for m in db.list_modules()] == ["ocr-hp"]


def test_migration_failure_restores_backup(paths: Paths, monkeypatch: pytest.MonkeyPatch):
    """迁移失败必须恢复备份（02 章 §5）：先建好库，再注入一个会炸的伪迁移。"""
    db = Database(paths.db_path)
    db.migrate(backup_dir=paths.backup)
    db.insert_document(_doc(id="keep-me"))

    monkeypatch.setattr(
        Database, "_load_migrations",
        staticmethod(lambda: [(SCHEMA_VERSION + 1, "CREATE TABLE bad (;")]),
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(backup_dir=paths.backup)

    # 数据仍在，版本未推进
    assert db.get_document("keep-me") is not None
    assert db.schema_version() == SCHEMA_VERSION
