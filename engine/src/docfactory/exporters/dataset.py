"""微调数据集导出：Alpaca / ShareGPT（05 章 §3，契约 V1 冻结）。

字段严格对齐 05 章 §3.1/§3.2 样例（instruction/input/output + metadata、
conversations[from,value] + metadata），覆盖 LLaMA-Factory / Axolotl 等主流框架。

V1 两种生成方式（05 章 §3.3），都是**诚实产物**、不假装有模型能力：
- `blank`（默认）：instruction 用通用模板、input 填切片文本、**output 留空**，
  供人工标注或 V2 模型填充；
- `rule`（实验性）：基于 heading_path 与表格结构生成简单 Q&A，答案就是切片原文，
  metadata 标 `generated_by="rule"` —— 让下游一眼看出这批数据的成色。

中间结构是「QA pair」而非直接生成目标格式：pair → alpaca / sharegpt 是两个纯函数，
V2 换成模型生成时只需换 pair 的生产者，两种目标格式的落盘逻辑一行不用动。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from docfactory.config import DatasetSettings

# 留空模板的 instruction 轮换池：per_chunk>1 时避免同一切片产出完全一样的条目
_BLANK_TEMPLATES: tuple[str, ...] = (
    "请阅读以下内容，并回答与之相关的问题。",
    "请根据以下内容，提炼其中的关键信息。",
    "请依据以下内容作答，不要引入内容之外的信息。",
    "请阅读以下资料，并用简洁的语言总结要点。",
)

# 生成方式标记（写进 metadata.generated_by，与 05 章 §3.3 一致）
MODE_BLANK = "blank"
MODE_RULE = "rule"
MODE_MODEL = "model"

_MD_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_MD_SEP_LINE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


def heading_tail(heading_path: str | None) -> str:
    """取标题路径的末段（`第2章>2.3 交付条款` → `2.3 交付条款`），作为提问的话题词。"""
    text = str(heading_path or "").strip()
    if not text:
        return ""
    return text.split(">")[-1].strip()


def _table_headers(text: str) -> list[str]:
    """从切片文本里嗅探 MD 表表头（规则生成「表中 X 的值」类问题用）。"""
    for line in text.splitlines():
        if not line.strip():
            continue
        if _MD_TABLE_LINE.match(line) and not _MD_SEP_LINE.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            return [c for c in cells if c]
        break  # 表格必然出现在切片开头，首个非空行不是表行就没有表
    return []


def _rule_questions(chunk: dict[str, Any], doc_name: str, limit: int) -> list[str]:
    """规则生成的问题池（05 章 §3.3：「X 章讲了什么」「表中 Y 的值」）。"""
    topic = heading_tail(chunk.get("heading_path"))
    text = str(chunk.get("text") or "")
    questions: list[str] = []
    if str(chunk.get("type")) == "table":
        subject = f"「{topic}」的表格" if topic else "文中的表格"
        questions.append(f"《{doc_name}》中{subject}包含哪些内容？")
        questions.extend(
            f"表中「{col}」这一列有哪些取值？" for col in _table_headers(text)[:limit]
        )
    if topic:
        questions.append(f"「{topic}」这一节讲了什么？")
        questions.append(f"请简要介绍《{doc_name}》中「{topic}」的主要内容。")
    else:
        questions.append(f"《{doc_name}》的这部分内容讲了什么？")
    # 去重保序后按需截断
    seen: set[str] = set()
    unique = [q for q in questions if not (q in seen or seen.add(q))]
    return unique[:limit] if limit > 0 else unique


def make_pair(
    chunk: dict[str, Any],
    *,
    question: str,
    answer: str,
    context: str,
    generated_by: str,
) -> dict[str, Any]:
    """统一的 QA pair 结构（落盘中间态，也是 qa_generate 任务的产物元素）。"""
    return {
        "doc_id": chunk.get("doc_id") or "",
        "chunk_id": chunk.get("chunk_id") or chunk.get("id") or "",
        "heading_path": chunk.get("heading_path") or "",
        "question": question,
        "answer": answer,
        "context": context,
        "generated_by": generated_by,
    }


def build_qa_pairs(
    chunk: dict[str, Any], ds: DatasetSettings, *, doc_name: str
) -> list[dict[str, Any]]:
    """按 ds.mode / ds.per_chunk 为单个切片生成 QA pair 列表。"""
    text = str(chunk.get("text") or "").strip()
    if not text:
        return []
    n = max(1, int(ds.per_chunk or 1))
    if ds.mode == MODE_RULE:
        # 规则生成：问题来自结构，答案就是原文（诚实——不编造模型才能给的概括）
        return [
            make_pair(chunk, question=q, answer=text, context="", generated_by=MODE_RULE)
            for q in _rule_questions(chunk, doc_name, n)
        ]
    return [
        make_pair(
            chunk,
            question=_BLANK_TEMPLATES[i % len(_BLANK_TEMPLATES)],
            answer="",
            context=text,
            generated_by=MODE_BLANK,
        )
        for i in range(n)
    ]


def select_dataset_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从混合 kind 的切片里挑出数据集生成用的一种粒度。

    优先 `parent`（04 章 §3.3 明确：parent 是 Q&A 数据集生成的天然单元），
    没有 parent 时退回 child，再退回原样 —— 两种 kind 同时喂进去会让同一段内容
    在数据集里出现两遍，训练时等于变相加权。
    """
    parents = [c for c in chunks if str(c.get("kind") or "") == "parent"]
    if parents:
        return parents
    children = [c for c in chunks if str(c.get("kind") or "") == "child"]
    return children or list(chunks)


def build_pairs_from_chunks(
    docs: list[dict[str, Any]], chunks: list[dict[str, Any]], ds: DatasetSettings
) -> list[dict[str, Any]]:
    """整批切片 → QA pair 列表（doc_name 由 docs 提供，找不到时留空不报错）。

    入口处统一做 select_dataset_chunks：调用方常常直接把 `db.get_chunks(doc_id)`
    的结果丢进来（parent + child 混在一起），在这里兜住比要求每个调用方都记得过滤可靠。

    粒度筛选**按文档分组**进行：合并导出时多篇文档的切片混在一个列表里，
    一旦全局筛选，只要有一篇产出了 parent，其余只有 child 的文档就会被整篇筛掉 ——
    用户看到的是数据集里凭空少了几篇，且没有任何失败记录。
    """
    name_of = {
        (d.get("doc_id") or d.get("id") or ""): (d.get("doc_name") or d.get("name") or "")
        for d in docs
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        grouped.setdefault(str(chunk.get("doc_id") or ""), []).append(chunk)

    pairs: list[dict[str, Any]] = []
    for doc_id, doc_chunks in grouped.items():          # dict 保序：产物顺序与输入一致
        doc_name = name_of.get(doc_id, "") or "本文档"
        for chunk in select_dataset_chunks(doc_chunks):
            pairs.extend(build_qa_pairs(chunk, ds, doc_name=doc_name))
    return pairs


# ---------------------------------------------------------------- pair → 目标格式


def _metadata(pair: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": pair.get("doc_id") or "",
        "chunk_id": pair.get("chunk_id") or "",
        "heading_path": pair.get("heading_path") or "",
        "generated_by": pair.get("generated_by") or MODE_BLANK,
    }


def pair_to_alpaca(pair: dict[str, Any]) -> dict[str, Any]:
    """QA pair → Alpaca 条目（05 章 §3.1）。"""
    return {
        "instruction": pair.get("question") or "",
        "input": pair.get("context") or "",
        "output": pair.get("answer") or "",
        "metadata": _metadata(pair),
    }


def pair_to_sharegpt(pair: dict[str, Any]) -> dict[str, Any]:
    """QA pair → ShareGPT 条目（05 章 §3.2）：参考内容并进 human 轮，符合样例形态。"""
    question = pair.get("question") or ""
    context = pair.get("context") or ""
    human = f"{question}\n\n参考内容：{context}" if context else question
    return {
        "conversations": [
            {"from": "human", "value": human},
            {"from": "gpt", "value": pair.get("answer") or ""},
        ],
        "metadata": _metadata(pair),
    }


# ---------------------------------------------------------------- 落盘


_ALPACA_CSV_COLUMNS = (
    "instruction", "input", "output", "doc_id", "chunk_id", "heading_path", "generated_by",
)
_SHAREGPT_CSV_COLUMNS = (
    "human", "gpt", "doc_id", "chunk_id", "heading_path", "generated_by",
)


def _with_suffix(out_path: Path, ext: str) -> Path:
    """按 file_format 校正扩展名：`x.alpaca.json` → `x.alpaca.csv`（只换最后一段）。"""
    out_path = Path(out_path)
    return out_path if out_path.suffix.lower() == ext else out_path.with_suffix(ext)


def _write_json(records: list[dict[str, Any]], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return out_path


def _write_csv(rows: list[list[Any]], columns: tuple[str, ...], out_path: Path) -> Path:
    """数据集 CSV 同样用 UTF-8 BOM —— 用户拿它在 Excel 里人工标注 output 列。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows(rows)
    return out_path


def write_pairs(pairs: list[dict[str, Any]], out_path: Path, ds: DatasetSettings) -> Path:
    """QA pairs → Alpaca/ShareGPT × JSON/CSV 四种组合之一，返回实际写入路径。"""
    if ds.format == "sharegpt":
        if ds.file_format == "csv":
            rows = []
            for pair in pairs:
                item = pair_to_sharegpt(pair)
                meta = item["metadata"]
                rows.append([
                    item["conversations"][0]["value"],
                    item["conversations"][1]["value"],
                    meta["doc_id"], meta["chunk_id"], meta["heading_path"], meta["generated_by"],
                ])
            return _write_csv(rows, _SHAREGPT_CSV_COLUMNS, _with_suffix(out_path, ".csv"))
        return _write_json([pair_to_sharegpt(p) for p in pairs], _with_suffix(out_path, ".json"))

    if ds.file_format == "csv":
        rows = []
        for pair in pairs:
            item = pair_to_alpaca(pair)
            meta = item["metadata"]
            rows.append([
                item["instruction"], item["input"], item["output"],
                meta["doc_id"], meta["chunk_id"], meta["heading_path"], meta["generated_by"],
            ])
        return _write_csv(rows, _ALPACA_CSV_COLUMNS, _with_suffix(out_path, ".csv"))
    return _write_json([pair_to_alpaca(p) for p in pairs], _with_suffix(out_path, ".json"))


def export_alpaca(
    docs: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    out_path: Path,
    ds: DatasetSettings,
) -> Path:
    """切片 → Alpaca 数据集文件（ds.file_format 决定 JSON/CSV 变体）。"""
    forced = ds.model_copy(update={"format": "alpaca"})
    return write_pairs(build_pairs_from_chunks(docs, chunks, forced), Path(out_path), forced)


def export_sharegpt(
    docs: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    out_path: Path,
    ds: DatasetSettings,
) -> Path:
    """切片 → ShareGPT 数据集文件（ds.file_format 决定 JSON/CSV 变体）。"""
    forced = ds.model_copy(update={"format": "sharegpt"})
    return write_pairs(build_pairs_from_chunks(docs, chunks, forced), Path(out_path), forced)
