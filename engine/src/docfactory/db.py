"""SQLite 数据层（02 章 §4 六表 + §5 迁移策略）。

- WAL 模式；多线程各自短连接 + busy_timeout 排队写。
- 迁移：启动时读 meta.schema_version，按序执行包内 migrations/000N_*.sql；
  迁移前自动备份 app.db 到 backup\\；失败恢复备份并抛错。
- 所有业务模块经本文件 DAO 访问数据库；复杂聚合（仪表盘）可用 connect() 自写 SQL。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """本地时区 ISO 时间戳（秒级），全库统一。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._write_lock = threading.Lock()

    # ------------------------------------------------ 连接与迁移

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                return int(row["value"]) if row else 0
            except sqlite3.OperationalError:
                return 0

    def migrate(self, backup_dir: Path | None = None) -> int:
        """执行待应用迁移，返回最终 schema_version。失败时恢复备份后抛出。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        current = self.schema_version()
        migrations = self._load_migrations()
        pending = [(n, sql) for n, sql in migrations if n > current]
        if not pending:
            return current

        backup_path: Path | None = None
        if backup_dir is not None and self.db_path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = backup_dir / f"app-{stamp}.db"
            shutil.copy2(self.db_path, backup_path)

        try:
            with self._write_lock, self.connect() as conn:
                for n, sql in pending:
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(n),),
                    )
        except Exception:
            if backup_path is not None:
                shutil.copy2(backup_path, self.db_path)
            raise
        return pending[-1][0]

    @staticmethod
    def _load_migrations() -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        pkg = resources.files("docfactory") / "migrations"
        for entry in sorted(pkg.iterdir(), key=lambda e: e.name):
            m = re.match(r"^(\d{4})_.*\.sql$", entry.name)
            if m:
                out.append((int(m.group(1)), entry.read_text(encoding="utf-8")))
        return out

    # ------------------------------------------------ meta（引擎级持久开关/标记）

    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ------------------------------------------------ documents

    def insert_document(self, doc: dict[str, Any]) -> None:
        doc = {"degraded_pages": 0, "created_at": now_iso(), **doc}
        cols = (
            "id", "name", "src_path", "fmt", "size", "hash", "status", "page_cnt",
            "parse_level", "text_coverage", "table_confidence", "ocr_confidence",
            "degraded_pages", "ir_version", "created_at", "parsed_at",
        )
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"INSERT INTO documents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(doc.get(c) for c in cols),
            )

    def update_document(self, doc_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id)
            )

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row_to_dict(
                conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            )

    def find_document_by_hash(self, sha256: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row_to_dict(
                conn.execute(
                    "SELECT * FROM documents WHERE hash=? ORDER BY created_at DESC LIMIT 1",
                    (sha256,),
                ).fetchone()
            )

    def list_documents(
        self,
        *,
        status: str | None = None,
        fmt: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if fmt:
            where.append("fmt=?")
            args.append(fmt)
        if q:
            where.append("name LIKE ?")
            args.append(f"%{q}%")
        cond = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM documents {cond}", args
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM documents {cond} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        return [dict(r) for r in rows], total

    def delete_document(self, doc_id: str) -> None:
        """级联删各表记录（workspace 目录由调用方删除）。"""
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM task_events WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM tasks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    # ------------------------------------------------ tasks

    def create_task(
        self, task_id: str, type: str, doc_id: str | None, payload: dict[str, Any]
    ) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, doc_id, type, status, progress, payload_json, created_at) "
                "VALUES (?, ?, ?, 'queued', 0, ?, ?)",
                (task_id, doc_id, type, json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._write_lock, self.connect() as conn:
            conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row_to_dict(
                conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

    def list_tasks(
        self, *, status: str | None = None, page: int = 1, page_size: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = ("WHERE status=?", [status]) if status else ("", [])
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM tasks {where}", args
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        return [dict(r) for r in rows], total

    def claim_next_queued(self, only_types: tuple[str, ...] | None = None) -> dict[str, Any] | None:
        """原子领取最早的 queued 任务（置 running），无任务返回 None。

        ``only_types`` 限定可领取的类型——队列暂停时只放行不受暂停约束的类型
        （scheduler._PAUSE_EXEMPT_TYPES），其余任务留在队列里原地等待。
        """
        type_where = ""
        args: list[Any] = []
        if only_types:
            type_where = f" AND type IN ({','.join('?' * len(only_types))})"
            args = list(only_types)
        with self._write_lock, self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM tasks WHERE status='queued'{type_where} ORDER BY created_at, id LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?",
                (now_iso(), row["id"]),
            )
            task = dict(row)
            task["status"] = "running"
            return task

    def mark_interrupted(self) -> int:
        """引擎启动自检：running 任务标记 interrupted（02 章 §1.1）。"""
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='interrupted', ended_at=? WHERE status='running'",
                (now_iso(),),
            )
            return cur.rowcount

    # ------------------------------------------------ chunks

    def replace_chunks(self, doc_id: str, chunks: list[dict[str, Any]]) -> int:
        """整体覆盖该文档 chunks（解析入库与重切共用，04 章 §3.4）。"""
        cols = (
            "id", "doc_id", "seq", "parent_id", "kind", "type", "text",
            "token_count", "char_count", "heading_path", "pages", "node_ids",
            "meta_json", "hash",
        )
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.executemany(
                f"INSERT INTO chunks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(c.get(k) for k in cols) for c in chunks],
            )
        return len(chunks)

    def get_chunks(self, doc_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE doc_id=?" + (" AND kind=?" if kind else "")
        args = (doc_id, kind) if kind else (doc_id,)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM chunks {where} ORDER BY seq", args
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------ task_events（结构化日志）

    def log_event(
        self,
        *,
        level: str,
        message: str,
        task_id: str | None = None,
        doc_id: str | None = None,
        code: str | None = None,
        stage: str | None = None,
        page: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, doc_id, level, code, stage, page, message, detail_json, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, doc_id, level, code, stage, page, message,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    now_iso(),
                ),
            )

    def query_events(
        self,
        *,
        level: str | None = None,
        task_id: str | None = None,
        doc_id: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if level:
            where.append("level=?")
            args.append(level)
        if task_id:
            where.append("task_id=?")
            args.append(task_id)
        if doc_id:
            where.append("doc_id=?")
            args.append(doc_id)
        if q:
            where.append("(message LIKE ? OR code LIKE ?)")
            args.extend([f"%{q}%", f"%{q}%"])
        cond = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM task_events {cond}", args
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM task_events {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        return [dict(r) for r in rows], total

    # ------------------------------------------------ metrics_daily

    def bump_metrics(self, **increments: int) -> None:
        """当日指标增量更新（UPSERT 累加），如 bump_metrics(imported=3, ocr_pages=12)。"""
        allowed = (
            "imported", "parsed_ok", "parsed_warn", "parsed_fail",
            "chunk_cnt", "ocr_pages", "total_ms",
        )
        inc = {k: v for k, v in increments.items() if k in allowed and v}
        if not inc:
            return
        cols = ", ".join(inc)
        placeholders = ", ".join("?" * len(inc))
        updates = ", ".join(f"{k}={k}+excluded.{k}" for k in inc)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                f"INSERT INTO metrics_daily (day, {cols}) VALUES (?, {placeholders}) "
                f"ON CONFLICT(day) DO UPDATE SET {updates}",
                (today(), *inc.values()),
            )

    # ------------------------------------------------ modules

    def upsert_module(
        self,
        *,
        id: str,
        name: str,
        type: str,
        version: str,
        manifest: dict[str, Any],
        prev_version: str | None = None,
    ) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO modules (id, name, type, version, enabled, prev_version, manifest_json, installed_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type, "
                "version=excluded.version, prev_version=excluded.prev_version, "
                "manifest_json=excluded.manifest_json, installed_at=excluded.installed_at",
                (
                    id, name, type, version, prev_version,
                    json.dumps(manifest, ensure_ascii=False), now_iso(),
                ),
            )

    def get_module(self, module_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row_to_dict(
                conn.execute("SELECT * FROM modules WHERE id=?", (module_id,)).fetchone()
            )

    def list_modules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM modules ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def set_module_version(
        self, module_id: str, version: str, prev_version: str | None
    ) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "UPDATE modules SET version=?, prev_version=?, installed_at=? WHERE id=?",
                (version, prev_version, now_iso(), module_id),
            )
