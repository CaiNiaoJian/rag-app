"""切片 JSON / CSV 导出（05 章 §2）。

两种产物面向两类使用者：
- **JSON**：schema 冻结（schema_version 1.0），给下游 RAG 管线程序化消费；
  库里以 JSON 字符串存的 pages / node_ids 在这里反序列化回数组 —— 契约是数组，
  存储形态是 SQLite 的妥协，不该泄漏给消费者。
- **CSV**：给人用 Excel 抽查，故用 **UTF-8 BOM**（无 BOM 时 Excel 按 GBK 解码中文全乱），
  且 `newline=""` 交给 csv 模块统一写 CRLF，避免多字段文本里的换行把行数搞乱。

字段一律按 05 章样例取名（chunk_id / doc_id / …），与 chunks 表的列名（id / doc_id / …）
在此做一次映射：数据库列名是内部事实，导出字段名是对外契约，两者独立演进。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from docfactory import APP_NAME, ENGINE_VERSION

# 导出 schema 版本（05 章 §2 冻结）；与 IR_VERSION / SCHEMA_VERSION 相互独立
CHUNKS_SCHEMA_VERSION = "1.0"

# CSV 列顺序（05 章 §2 逐字段冻结，不得增删改序）
CSV_COLUMNS: tuple[str, ...] = (
    "doc_name", "chunk_id", "parent_id", "seq", "kind", "type",
    "heading_path", "pages", "token_count", "char_count", "text",
)


def exported_by() -> str:
    """产物出处标识（便于用户回溯是哪一版引擎导出的）。"""
    return f"{APP_NAME} {ENGINE_VERSION}"


def _json_array(raw: Any) -> list[Any]:
    """pages / node_ids 反序列化：兼容 JSON 字符串、已解析数组与 None。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            # 容错：历史数据可能是 "3,4" 这种朴素写法，别为此丢字段
            return [seg.strip() for seg in raw.split(",") if seg.strip()]
        return value if isinstance(value, list) else [value]
    return []


def normalize_doc(row: dict[str, Any]) -> dict[str, Any]:
    """documents 表行 → 导出 docs[] 条目（05 章 §2）。"""
    return {
        "doc_id": row.get("doc_id") or row.get("id") or "",
        "doc_name": row.get("doc_name") or row.get("name") or "",
        "source_format": row.get("source_format") or row.get("fmt") or "",
        "parse_level": row.get("parse_level") or "",
        "text_coverage": row.get("text_coverage"),
    }


def normalize_chunk(row: dict[str, Any]) -> dict[str, Any]:
    """chunks 表行 → 导出 chunks[] 条目（字段顺序对齐 05 章样例，便于人工比对）。"""
    return {
        "chunk_id": row.get("chunk_id") or row.get("id") or "",
        "parent_id": row.get("parent_id"),
        "doc_id": row.get("doc_id") or "",
        "seq": row.get("seq"),
        "kind": row.get("kind") or "",
        "type": row.get("type") or "",
        "text": row.get("text") or "",
        "token_count": row.get("token_count"),
        "char_count": row.get("char_count"),
        "heading_path": row.get("heading_path") or "",
        "pages": _json_array(row.get("pages")),
        "node_ids": _json_array(row.get("node_ids")),
        "hash": row.get("hash") or "",
    }


def build_chunks_payload(
    docs: list[dict[str, Any]], chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """组装切片 JSON 顶层结构（供导出与 API 预览共用）。"""
    return {
        "schema_version": CHUNKS_SCHEMA_VERSION,
        "exported_by": exported_by(),
        "docs": [normalize_doc(d) for d in docs],
        "chunks": [normalize_chunk(c) for c in chunks],
    }


def export_chunks_json(
    docs: list[dict[str, Any]], chunks: list[dict[str, Any]], out_path: Path
) -> Path:
    """导出切片 JSON（UTF-8 无 BOM，indent=2 便于人工核对与版本管理）。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_chunks_payload(docs, chunks)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return out_path


def _csv_pages(pages: list[Any]) -> str:
    """pages 数组 → CSV 单元格文本（逗号分隔，由 csv 模块负责加引号）。"""
    return ",".join(str(p) for p in pages)


def export_chunks_csv(
    docs: list[dict[str, Any]], chunks: list[dict[str, Any]], out_path: Path
) -> Path:
    """导出切片 CSV：列顺序冻结、UTF-8 **BOM**、newline="" 防换行错位。

    text 列原样写出（含换行由 csv 引号包裹）——数据工厂的产物以保真为先，
    不做「=+-@ 前缀加撇号」这类防 Excel 公式注入的改写，以免污染训练语料。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    name_of = {d["doc_id"]: d["doc_name"] for d in (normalize_doc(x) for x in docs)}
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for raw in chunks:
            c = normalize_chunk(raw)
            writer.writerow([
                name_of.get(c["doc_id"], ""),
                c["chunk_id"],
                c["parent_id"] or "",
                c["seq"] if c["seq"] is not None else "",
                c["kind"],
                c["type"],
                c["heading_path"],
                _csv_pages(c["pages"]),
                c["token_count"] if c["token_count"] is not None else "",
                c["char_count"] if c["char_count"] is not None else "",
                c["text"],
            ])
    return out_path
