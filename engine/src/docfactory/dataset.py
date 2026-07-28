"""Q&A 生成与数据集构建两个任务 runner（05 章 §3，scheduler.RUNNERS 指向这里）。

拆成两步而不是一步到底，是为了让「问答对」成为可检视、可人工修订的中间产物：

    qa_generate  → {name}.qa.json（问答对，含 generated_by 成色标记）
    dataset_build → {name}.alpaca.json / {name}.sharegpt.json（喂给训练框架的成品）

dataset_build 既能直接吃切片（一步到位），也能吃上一步的 .qa.json（payload.qa_path），
后者正是 V2 的路径：模型生成问答 → 人工抽查修订 → 再构建数据集。

三档生成方式（05 章 §3.3 + 06 章 §4.2）：
- blank / rule：本地纯规则，永远可用；
- model：走 providers.get_provider()。V1 默认的 NullProvider 会明确抛
  MODEL_NOT_INSTALLED —— **给出「未安装模型模组」的友好提示并指路降级选项**，
  而不是静默产出一堆空 output 让用户以为模型跑过了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from docfactory import APP_NAME, ENGINE_VERSION
from docfactory.config import DatasetSettings
from docfactory.db import Database
from docfactory.errors import MODEL_NOT_INSTALLED, DocFactoryError
from docfactory.exporters import merge_settings, resolve_doc_ids, resolve_out_dir
from docfactory.exporters.chunks import CHUNKS_SCHEMA_VERSION
from docfactory.exporters.dataset import (
    MODE_BLANK,
    MODE_MODEL,
    MODE_RULE,
    build_qa_pairs,
    make_pair,
    select_dataset_chunks,
    write_pairs,
)
from docfactory.exporters.markdown import safe_filename
from docfactory.providers import ModelProvider, get_provider
from docfactory.taskspec import (
    EVENT_PROGRESS,
    EVENT_STAGE_CHANGE,
    TaskCancelled,
    TaskContext,
    TaskOutcome,
)

_STAGE = "export"

# 未安装模型模组时的用户可读提示（06 章 §4.3 的 UI 文案 + 明确的降级出路）
_NO_MODEL_HINT = (
    "未安装模型模组，无法使用「模型生成」。"
    "请先安装 llm-runtime 模组与本地模型，或改用「留空模板」/「规则生成」。"
)

# 模型生成的单条上下文上限（字符）：本地小模型上下文有限，超长切片先截断再送
_MODEL_CONTEXT_CHARS = 4000


# ---------------------------------------------------------------- 公共辅助


def _resolve_dataset(ctx: TaskContext) -> tuple[DatasetSettings, str]:
    """合并数据集设置，并单独解析生成档位。

    DatasetSettings.mode 是冻结契约，只认 blank/rule；「模型生成」是 V2 能力，
    经 payload 传入（payload.mode 或 dataset.mode = "model"），
    校验前先摘出去，免得 pydantic 直接把整个 payload 判死。
    """
    override = ctx.payload.get("dataset")
    override = dict(override) if isinstance(override, dict) else {}
    mode = str(ctx.payload.get("mode") or override.get("mode") or ctx.settings.dataset.mode).lower()
    if mode not in (MODE_BLANK, MODE_RULE, MODE_MODEL):
        mode = MODE_BLANK
    override["mode"] = MODE_RULE if mode == MODE_RULE else MODE_BLANK
    ds: DatasetSettings = merge_settings(ctx.settings.dataset, override, DatasetSettings)
    return ds, mode


def _source_chunks(db: Database, doc_id: str, kind: str | None) -> list[dict[str, Any]]:
    """取生成用切片：payload 指定 kind 时照办，否则按 parent → child 的优先级挑。"""
    if kind in ("parent", "child"):
        return db.get_chunks(doc_id, kind=kind)
    return select_dataset_chunks(db.get_chunks(doc_id))


def _dataset_suffix(ds: DatasetSettings) -> str:
    return f".{ds.format}.{ds.file_format}"


def _doc_name(row: dict[str, Any] | None, doc_id: str) -> str:
    return str((row or {}).get("name") or doc_id)


def _base_name(row: dict[str, Any] | None, doc_id: str) -> str:
    return safe_filename(Path(_doc_name(row, doc_id)).stem)


# ---------------------------------------------------------------- 模型生成


def _ensure_model_ready(provider: ModelProvider) -> None:
    """模型档位的前置闸门：不可用就立刻报 MODEL_NOT_INSTALLED，别让用户白等一批任务。"""
    try:
        caps = provider.capabilities()
        ready = bool(caps.get("chat")) and provider.health()
    except Exception as exc:  # provider 实现异常一律按不可用处理
        raise DocFactoryError(MODEL_NOT_INSTALLED, f"{_NO_MODEL_HINT}（{exc}）") from exc
    if not ready:
        raise DocFactoryError(MODEL_NOT_INSTALLED, _NO_MODEL_HINT)


def _model_prompt(text: str, n: int, doc_name: str) -> list[dict[str, str]]:
    context = text[:_MODEL_CONTEXT_CHARS]
    return [
        {
            "role": "system",
            "content": "你是中文文档问答数据集的标注助手。只依据用户给出的资料作答，不得编造资料之外的信息。",
        },
        {
            "role": "user",
            "content": (
                f"资料出自《{doc_name}》。请基于下面的资料生成 {n} 组问答，"
                "以 JSON 数组输出，每项包含 question 与 answer 两个字段，不要输出任何其他文字。\n\n"
                f"资料：\n{context}"
            ),
        },
    ]


def _parse_model_json(reply: str) -> list[dict[str, Any]]:
    """从模型回复里抠出 JSON 数组。本地小模型常带前后缀寒暄，容错优先。"""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(reply[start : end + 1])
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _model_pairs(
    provider: ModelProvider, chunk: dict[str, Any], ds: DatasetSettings, doc_name: str
) -> list[dict[str, Any]]:
    text = str(chunk.get("text") or "").strip()
    if not text:
        return []
    n = max(1, int(ds.per_chunk or 1))
    reply = provider.chat(_model_prompt(text, n, doc_name))
    if not isinstance(reply, str):     # 流式实现返回迭代器，这里只要完整文本
        reply = "".join(reply)
    pairs: list[dict[str, Any]] = []
    for item in _parse_model_json(reply)[:n]:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question:
            pairs.append(
                make_pair(chunk, question=question, answer=answer, context="",
                          generated_by=MODE_MODEL)
            )
    return pairs


# ---------------------------------------------------------------- runner：qa_generate


def _write_qa_file(pairs: list[dict[str, Any]], out_path: Path, mode: str) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CHUNKS_SCHEMA_VERSION,
        "exported_by": f"{APP_NAME} {ENGINE_VERSION}",
        "mode": mode,
        "count": len(pairs),
        "pairs": pairs,
    }
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return out_path


def run_qa_generate(ctx: TaskContext) -> TaskOutcome:
    """qa_generate 任务入口：切片 → 问答对中间产物 `{name}.qa.json`。

    payload::

        {"doc_ids": [...], "mode": "blank"|"rule"|"model", "kind": "parent"|"child"|None,
         "dataset": {...可选覆盖}, "out_dir": str|None, "merge": bool}
    """
    doc_ids = resolve_doc_ids(ctx)
    ds, mode = _resolve_dataset(ctx)
    kind = str(ctx.payload.get("kind") or "").strip() or None
    merge = bool(ctx.payload.get("merge"))

    provider: ModelProvider | None = None
    if mode == MODE_MODEL:
        provider = get_provider()
        _ensure_model_ready(provider)      # 未安装模型：整批直接失败，给明确指路

    ctx.progress(EVENT_STAGE_CHANGE, {"stage": _STAGE})
    total = len(doc_ids)
    files: list[str] = []
    failed: list[dict[str, Any]] = []
    merged_pairs: list[dict[str, Any]] = []
    pair_count = 0

    for index, doc_id in enumerate(doc_ids, 1):
        if ctx.cancelled():
            raise TaskCancelled()
        try:
            row = ctx.db.get_document(doc_id)
            chunks = _source_chunks(ctx.db, doc_id, kind)
            doc_name = _doc_name(row, doc_id)
            pairs: list[dict[str, Any]] = []
            for chunk in chunks:
                if ctx.cancelled():        # 模型生成逐片很慢，切片粒度轮询取消
                    raise TaskCancelled()
                if provider is not None:
                    try:
                        pairs.extend(_model_pairs(provider, chunk, ds, doc_name))
                    except DocFactoryError:
                        raise
                    except Exception as exc:
                        # 单片模型调用失败不该毁掉整篇：记 warning 后继续
                        logger.warning(f"模型生成问答失败，已跳过该切片：{exc}")
                        ctx.db.log_event(
                            level="warning", task_id=ctx.task_id, doc_id=doc_id, stage=_STAGE,
                            message=f"模型生成问答失败，已跳过一个切片：{exc}",
                        )
                else:
                    pairs.extend(build_qa_pairs(chunk, ds, doc_name=doc_name))
            pair_count += len(pairs)
            if merge:
                merged_pairs.extend(pairs)
            else:
                out_path = resolve_out_dir(ctx, doc_id) / f"{_base_name(row, doc_id)}.qa.json"
                files.append(str(_write_qa_file(pairs, out_path, mode)))
        except TaskCancelled:
            raise
        except Exception as exc:
            _record(ctx, failed, doc_id, exc, action="生成问答")
        finally:
            ctx.progress(EVENT_PROGRESS, {"page": index, "total": total, "stage": _STAGE})

    if merge:
        out_path = resolve_out_dir(ctx, None) / f"{_merge_name(ctx, doc_ids)}.qa.json"
        files.append(str(_write_qa_file(merged_pairs, out_path, mode)))

    result = {
        "files": files,
        "failed": failed,
        "mode": mode,
        "counts": {"docs": len(doc_ids), "pairs": pair_count, "failed": len(failed)},
    }
    if not files:
        code = str(failed[0]["error_code"]) if failed else "E05"
        return TaskOutcome(status="failed", error_code=code,
                           message=str(failed[0]["message"]) if failed else "没有可生成的切片内容",
                           result=result)
    return TaskOutcome(status="done", message=f"已生成 {pair_count} 组问答", result=result)


# ---------------------------------------------------------------- runner：dataset_build


def _load_qa_pairs(qa_path: Path) -> list[dict[str, Any]]:
    """读取 qa_generate 产物；兼容 {"pairs": [...]} 与裸数组两种形态。"""
    if not qa_path.is_file():
        raise DocFactoryError("E03", f"未找到问答文件：{qa_path}")
    try:
        obj = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DocFactoryError("E01", f"问答文件无法读取：{exc}") from exc
    pairs = obj.get("pairs") if isinstance(obj, dict) else obj
    if not isinstance(pairs, list):
        raise DocFactoryError("E05", f"问答文件里没有 pairs 数组：{qa_path.name}")
    return [p for p in pairs if isinstance(p, dict)]


def run_dataset_build(ctx: TaskContext) -> TaskOutcome:
    """dataset_build 任务入口：切片（或 .qa.json）→ Alpaca / ShareGPT 数据集文件。

    payload::

        {"doc_ids": [...], "dataset": {...可选覆盖}, "out_dir": str|None,
         "merge": bool, "kind": "parent"|"child"|None, "qa_path": str|None}
    """
    ds, mode = _resolve_dataset(ctx)
    out_suffix = _dataset_suffix(ds)

    # 分支一：直接把已有问答对装配成数据集（人工修订后再构建的典型路径）
    qa_path = str(ctx.payload.get("qa_path") or "").strip()
    if qa_path:
        ctx.progress(EVENT_STAGE_CHANGE, {"stage": _STAGE})
        pairs = _load_qa_pairs(Path(qa_path))
        base = safe_filename(Path(qa_path).name.removesuffix(".qa.json") or "dataset")
        written = write_pairs(pairs, resolve_out_dir(ctx, None) / f"{base}{out_suffix}", ds)
        ctx.progress(EVENT_PROGRESS, {"page": 1, "total": 1, "stage": _STAGE})
        return TaskOutcome(
            status="done", message=f"已构建 {len(pairs)} 条数据集样本",
            result={"files": [str(written)], "failed": [],
                    "counts": {"pairs": len(pairs), "files": 1, "failed": 0}},
        )

    # 分支二：从切片直接构建
    doc_ids = resolve_doc_ids(ctx)
    kind = str(ctx.payload.get("kind") or "").strip() or None
    merge = bool(ctx.payload.get("merge"))

    provider: ModelProvider | None = None
    if mode == MODE_MODEL:
        provider = get_provider()
        _ensure_model_ready(provider)

    ctx.progress(EVENT_STAGE_CHANGE, {"stage": _STAGE})
    total = len(doc_ids)
    files: list[str] = []
    failed: list[dict[str, Any]] = []
    merged_pairs: list[dict[str, Any]] = []
    pair_count = 0

    for index, doc_id in enumerate(doc_ids, 1):
        if ctx.cancelled():
            raise TaskCancelled()
        try:
            row = ctx.db.get_document(doc_id)
            chunks = _source_chunks(ctx.db, doc_id, kind)
            doc_name = _doc_name(row, doc_id)
            pairs: list[dict[str, Any]] = []
            for chunk in chunks:
                if ctx.cancelled():
                    raise TaskCancelled()
                if provider is not None:
                    pairs.extend(_model_pairs(provider, chunk, ds, doc_name))
                else:
                    pairs.extend(build_qa_pairs(chunk, ds, doc_name=doc_name))
            pair_count += len(pairs)
            if merge:
                merged_pairs.extend(pairs)
            else:
                out_path = resolve_out_dir(ctx, doc_id) / f"{_base_name(row, doc_id)}{out_suffix}"
                files.append(str(write_pairs(pairs, out_path, ds)))
        except TaskCancelled:
            raise
        except Exception as exc:
            _record(ctx, failed, doc_id, exc, action="构建数据集")
        finally:
            ctx.progress(EVENT_PROGRESS, {"page": index, "total": total, "stage": _STAGE})

    if merge:
        out_path = resolve_out_dir(ctx, None) / f"{_merge_name(ctx, doc_ids)}{out_suffix}"
        files.append(str(write_pairs(merged_pairs, out_path, ds)))

    result = {
        "files": files,
        "failed": failed,
        "format": ds.format,
        "counts": {"docs": len(doc_ids), "pairs": pair_count, "files": len(files),
                   "failed": len(failed)},
    }
    if not files:
        code = str(failed[0]["error_code"]) if failed else "E05"
        return TaskOutcome(status="failed", error_code=code,
                           message=str(failed[0]["message"]) if failed else "没有可用于构建数据集的切片",
                           result=result)
    return TaskOutcome(
        status="done",
        message=f"已构建 {pair_count} 条 {ds.format} 样本，共 {len(files)} 个文件",
        result=result,
    )


# ---------------------------------------------------------------- 失败与命名


def _record(
    ctx: TaskContext, failed: list[dict[str, Any]], doc_id: str, exc: Exception, *, action: str
) -> None:
    """批次纪律（FR-10）：单篇失败只记录，不中断整批。"""
    code = exc.code if isinstance(exc, DocFactoryError) else "E06"
    detail = exc.detail if isinstance(exc, DocFactoryError) else f"{type(exc).__name__}: {exc}"
    failed.append({"doc_id": doc_id, "error_code": code, "message": detail})
    try:
        ctx.db.log_event(
            level="error", task_id=ctx.task_id, doc_id=doc_id, code=code, stage=_STAGE,
            message=f"{action}失败：{detail}",
        )
    except Exception as log_exc:   # 日志故障不该把「继续跑完整批」的纪律带崩
        logger.warning(f"{action}失败事件落库失败：{log_exc}")
    logger.bind(task_id=ctx.task_id, doc_id=doc_id).warning(f"{action}失败：{detail}")


def _merge_name(ctx: TaskContext, doc_ids: list[str]) -> str:
    """合并产物名：单篇沿用文档名，多篇用统一的「数据集合集」+ 任务号（可追溯到日志）。"""
    if len(doc_ids) == 1:
        row = ctx.db.get_document(doc_ids[0])
        return _base_name(row, doc_ids[0])
    return safe_filename(f"数据集合集-{ctx.task_id[:8]}")
