"""HTTP 端点契约（02 章 §2.1）。

覆盖鉴权闸、健康探活、设置读写、任务生命周期端点、文档库、日志与仪表盘。
调度器**不启动**：这里验的是端点形状与错误语义，任务执行由 test_scheduler.py 负责。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from docfactory import API_VERSION, ENGINE_VERSION, IR_VERSION, SCHEMA_VERSION
from docfactory.config import Paths
from docfactory.db import Database
from docfactory.ir import IRBuilder, IRDocMeta, NodeContent


def _make_doc(db: Database, **over: Any) -> str:
    doc_id = over.pop("id", None) or str(uuid.uuid4())
    db.insert_document({
        "id": doc_id, "name": "合同v3.docx", "src_path": r"C:\tmp\合同v3.docx",
        "fmt": "docx", "size": 2048, "hash": "a" * 64, "status": "ok",
        "page_cnt": 5, "parse_level": "L0", "text_coverage": 0.98,
        "ir_version": IR_VERSION, **over,
    })
    return doc_id


# ---------------------------------------------------------------- 鉴权


def test_health_is_public(anon_client):
    """主进程要在拿到凭据之前探活，/health 必须免鉴权（02 章 §1.1）。"""
    res = anon_client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["engine_version"] == ENGINE_VERSION
    assert body["api_version"] == API_VERSION
    assert body["ir_version"] == IR_VERSION and body["schema_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("path", ["/settings", "/tasks", "/documents", "/logs", "/modules"])
def test_endpoints_require_bearer(anon_client, path: str):
    assert anon_client.get(path).status_code == 401


@pytest.mark.parametrize("header_tpl", ["", "Bearer wrong-token", "Basic {token}", "{token}"])
def test_malformed_credentials_rejected(anon_client, token: str, header_tpl: str):
    """错误 token、错误 scheme、裸 token 都得挡住——只有 `Bearer <正确值>` 放行。"""
    header = header_tpl.format(token=token)
    res = anon_client.get("/settings", headers={"Authorization": header} if header else {})
    assert res.status_code == 401


# ---------------------------------------------------------------- 设置


def test_get_settings_returns_full_snapshot(client):
    body = client.get("/settings").json()
    assert body["ocr_mode"] == "on" and body["degrade_policy"] == "auto"
    assert body["chunk"]["target_tokens"] == 512
    assert body["dataset"]["format"] == "alpaca"


def test_put_settings_is_a_partial_merge(client, paths: Paths):
    res = client.put("/settings", json={"ocr_mode": "high", "chunk": {"target_tokens": 800}})
    assert res.status_code == 200
    body = res.json()
    assert body["ocr_mode"] == "high"
    assert body["chunk"]["target_tokens"] == 800
    assert body["chunk"]["overlap"] == 0.12          # 未提及的兄弟字段不被抹掉
    assert body["degrade_policy"] == "auto"

    # 落盘：重启引擎后仍生效
    saved = json.loads(paths.settings_path.read_text(encoding="utf-8"))
    assert saved["ocr_mode"] == "high" and saved["chunk"]["target_tokens"] == 800


def test_put_settings_rejects_invalid_value(client):
    res = client.put("/settings", json={"ocr_mode": "超高精度"})
    assert res.status_code == 400
    body = res.json()
    # 错误三级呈现结构（FR-13）
    assert body["error_code"] == "E06"
    assert body["user_message"] and body["suggestion"]
    assert "ocr_mode" in body["detail"]
    # 非法请求不该污染已有设置
    assert client.get("/settings").json()["ocr_mode"] == "on"


# ---------------------------------------------------------------- 退出


def test_shutdown_replies_before_stopping(client, app):
    res = client.post("/shutdown")
    assert res.status_code == 200 and res.json() == {"ok": True}
    # BackgroundTask 在响应发送完毕后执行；TestClient 会等它跑完
    assert app.state.shutdown_calls == [1]


# ---------------------------------------------------------------- 任务


def test_create_task_returns_id_and_enqueues(client, db: Database):
    doc_id = _make_doc(db)
    res = client.post("/tasks", json={"type": "parse", "payload": {"doc_id": doc_id}})
    assert res.status_code == 200
    task_id = res.json()["task_id"]

    row = db.get_task(task_id)
    assert row["status"] == "queued" and row["type"] == "parse" and row["doc_id"] == doc_id
    assert json.loads(row["payload_json"]) == {"doc_id": doc_id}


def test_create_task_rejects_unknown_type(client):
    res = client.post("/tasks", json={"type": "mine_bitcoin", "payload": {}})
    assert res.status_code == 400 and res.json()["error_code"] == "E03"


def test_create_task_rejects_missing_document(client):
    res = client.post("/tasks", json={"type": "parse", "payload": {"doc_id": "ghost"}})
    assert res.status_code == 400 and res.json()["error_code"] == "E03"


def test_list_tasks_paginates_and_filters(client, db: Database):
    for i in range(5):
        db.create_task(f"t{i}", "parse", None, {})
    db.update_task("t0", status="done")

    body = client.get("/tasks", params={"page_size": 2}).json()
    assert body["total"] == 5 and len(body["items"]) == 2

    done = client.get("/tasks", params={"status": "done"}).json()
    assert done["total"] == 1 and done["items"][0]["id"] == "t0"


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 9999}])
def test_list_tasks_validates_paging(client, params: dict[str, int]):
    assert client.get("/tasks", params=params).status_code == 422


def test_task_detail_includes_timeline(client, db: Database):
    db.create_task("t-detail", "parse", None, {"src": "a.pdf"})
    db.update_task("t-detail", status="failed", stage="ocr", error_code="E04")
    db.log_event(level="info", task_id="t-detail", stage="convert", message="转换完成")
    db.log_event(level="warning", task_id="t-detail", stage="parse", code="DGR-L1",
                 page=3, message="第 3 页降级到 L1")
    db.log_event(level="error", task_id="t-detail", stage="ocr", code="E04",
                 message="扫描质量较低")

    body = client.get("/tasks/t-detail").json()
    assert body["status"] == "failed" and body["error_code"] == "E04"
    assert body["payload"] == {"src": "a.pdf"}

    stages = [s["stage"] for s in body["timeline"]]
    assert stages == ["convert", "parse", "ocr"]        # 正序推进，UI 直接当步骤条渲染
    by_stage = {s["stage"]: s for s in body["timeline"]}
    assert by_stage["parse"]["level"] == "warning" and by_stage["parse"]["code"] == "DGR-L1"
    assert by_stage["ocr"]["level"] == "error" and by_stage["ocr"]["code"] == "E04"
    assert len(body["events"]) == 3                     # 最近事件倒序给 UI


def test_task_detail_404(client):
    assert client.get("/tasks/nope").status_code == 404


def test_cancel_queued_task(client, db: Database):
    db.create_task("t-cancel", "parse", None, {})
    body = client.post("/tasks/t-cancel/cancel").json()
    assert body["canceled"] is True
    assert db.get_task("t-cancel")["status"] == "canceled"


def test_cancel_unknown_task(client):
    res = client.post("/tasks/ghost/cancel")
    assert res.status_code == 400 and res.json()["error_code"] == "E03"


def test_sse_stream_replays_terminal_state(client, db: Database):
    """引擎重启后订阅历史任务：总线是空的，端点应从库里补一条终态再收流。"""
    db.create_task("t-old", "parse", None, {})
    db.update_task("t-old", status="failed", error_code="E02")

    with client.stream("GET", "/tasks/t-old/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        text = "".join(res.iter_text())

    assert text.startswith(": ok\n\n")          # 先推头，客户端立刻确认订阅成功
    assert "event: failed" in text
    payload = json.loads(text.split("data: ")[1].split("\n")[0])
    assert payload["error_code"] == "E02"
    assert payload["user_message"] == "文件受密码保护"


# ---------------------------------------------------------------- 文档库


def test_list_documents_filters(client, db: Database):
    _make_doc(db, name="报表.xlsx", fmt="xlsx", status="ok")
    _make_doc(db, name="扫描件.pdf", fmt="pdf", status="warning")
    _make_doc(db, name="旧合同.doc", fmt="doc", status="failed")

    assert client.get("/documents").json()["total"] == 3
    assert client.get("/documents", params={"status": "ok"}).json()["total"] == 1
    assert client.get("/documents", params={"fmt": "pdf"}).json()["total"] == 1
    assert client.get("/documents", params={"q": "合同"}).json()["total"] == 1


def test_document_detail_and_ir_paths(client, db: Database, paths: Paths):
    doc_id = _make_doc(db)
    builder = IRBuilder(IRDocMeta(id=doc_id, source_file="合同v3.docx", source_format="docx"))
    builder.add("section", level=1, content=NodeContent(text="第1章"))
    builder.add("paragraph", content=NodeContent(text="正文"))
    builder.build().save(paths.doc_ir_path(doc_id))

    detail = client.get(f"/documents/{doc_id}").json()
    assert detail["id"] == doc_id and detail["text_coverage"] == 0.98

    ir_info = client.get(f"/documents/{doc_id}/ir").json()
    # 大内容一律传路径不传内容（02 章 §2.2）
    assert ir_info["ir_version"] == IR_VERSION and ir_info["node_count"] == 2
    assert Path(ir_info["path"]) == paths.doc_ir_path(doc_id)
    assert "nodes" not in ir_info


def test_document_ir_missing(client, db: Database):
    doc_id = _make_doc(db, status="imported")
    assert client.get(f"/documents/{doc_id}/ir").status_code in (400, 404)


def test_document_chunks(client, db: Database):
    doc_id = _make_doc(db)
    db.replace_chunks(doc_id, [
        {"id": f"c-{i:04d}", "doc_id": doc_id, "seq": i, "parent_id": "p-0001",
         "kind": "child", "type": "text", "text": f"块{i}", "token_count": 10,
         "char_count": 2, "heading_path": "第1章", "pages": "[1]",
         "node_ids": '["n1"]', "meta_json": "{}", "hash": "sha256:x"}
        for i in range(3)
    ])
    body = client.get(f"/documents/{doc_id}/chunks").json()
    assert body["total"] == 3 and body["items"][0]["seq"] == 0


def test_document_preview_missing_page_404(client, db: Database):
    doc_id = _make_doc(db)
    assert client.get(f"/documents/{doc_id}/preview/1").status_code == 404


def test_delete_document_removes_workspace(client, db: Database, paths: Paths):
    doc_id = _make_doc(db)
    doc_dir = paths.doc_dir(doc_id)
    (doc_dir / "parsed").mkdir(parents=True, exist_ok=True)
    (doc_dir / "parsed" / "doc.md").write_text("# 标题", encoding="utf-8")

    assert client.delete(f"/documents/{doc_id}").status_code in (200, 204)
    assert db.get_document(doc_id) is None
    assert not doc_dir.exists()


def test_delete_unknown_document(client):
    assert client.delete("/documents/ghost").status_code in (400, 404)


# ---------------------------------------------------------------- 日志与仪表盘


def test_logs_query(client, db: Database):
    db.create_task("t1", "parse", None, {})
    db.log_event(level="error", code="E01", message="文件似乎已损坏", task_id="t1")
    db.log_event(level="info", message="任务开始", task_id="t1")

    assert client.get("/logs").json()["total"] == 2
    assert client.get("/logs", params={"level": "error"}).json()["total"] == 1
    assert client.get("/logs", params={"q": "损坏"}).json()["total"] == 1
    assert client.get("/logs", params={"task_id": "t1"}).json()["total"] == 2


def test_diagnostics_bundle_is_written(client, paths: Paths):
    import zipfile

    (paths.logs / "engine-20260727.jsonl").write_text('{"msg":"hi"}\n', encoding="utf-8")
    res = client.post("/logs/diagnostics")
    assert res.status_code == 200
    zip_path = Path(res.json()["path"])
    assert zip_path.is_file() and zipfile.is_zipfile(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("engine-20260727.jsonl" in n for n in names)
    assert any("system" in n.lower() or "info" in n.lower() for n in names)


def test_dashboard_shape(client, db: Database):
    _make_doc(db, fmt="pdf", status="ok", parse_level="L0", text_coverage=0.99)
    _make_doc(db, fmt="docx", status="warning", parse_level="L1", text_coverage=0.80)
    _make_doc(db, fmt="pdf", status="failed", parse_level=None, text_coverage=None)
    db.create_task("t1", "parse", None, {})
    db.log_event(level="error", code="E01", message="损坏", task_id="t1")
    db.bump_metrics(imported=3, parsed_ok=1, ocr_pages=7)

    body = client.get("/stats/dashboard").json()
    # 前端 DashboardStats 的字段面（types.ts）必须齐全，缺字段会让仪表盘整块空白
    for key in ("cards", "fmt_dist", "status_dist", "level_dist", "chunk_hist",
                "fail_top", "duration", "trend"):
        assert key in body, f"仪表盘缺少 {key}"
    assert body["cards"]
