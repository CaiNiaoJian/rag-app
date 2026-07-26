"""结构感知切片层（04 章 §3 全节）。

职责：把 IR 文档树切成检索粒度的 child 块与章节粒度的 parent 块，直接产出
``db.replace_chunks`` 需要的行 dict；并提供 rechunk 任务入口。

**为什么在 IR 上切而不是在 Markdown 上切**：IR 里还留着「这是表格 / 这是 PPT 第 3 页 /
这段属于哪个标题」的结构信息，规则才有的可依；一旦渲染成 Markdown 就只剩标点，
再想做结构感知只能靠猜。

规则优先级（§3.1，从高到低）：
    ① 表格原子 → ② PPT 按 slide → ③ Excel 按 sheet_region
    → ④ 文本按最深 section 聚合 → ⑤ 孤儿内容归虚拟根块

**①②相遇时的裁决**：slide 里的表格既要「不拆断」又要「同页一片」，做法是把表格当成
slide 片内的**不可拆分单元** —— 页装得下就是一片，装不下才在结构边界切开，两条规则
都不违背。

**chunk id**：``chunks.id`` 是全库主键，而 05 章样例的 ``c-0012`` 只在单文档内唯一，
故落库用 ``{docId}:c-0012``，样例形式的短号写在 ``meta_json.local_id``（详见 _chunk_id）。

**parent 的边界**（§3.3）：parent = child 所在**章节自己直属**的全文（不含子章节 ——
子章节有自己的 parent，否则每层都复制一遍正文，库会膨胀成 O(深度×正文)）。
parent 天然可能超过 max_tokens：它是「上下文供给单元」不是「检索单元」，长度上限只
约束 child；但仍有一条防退化的宽松天花板，见 _parent_batches。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from docfactory.config import ChunkSettings
from docfactory.errors import DocFactoryError
from docfactory.ir import IRDocument, IRNode, TableContent, table_has_merged_cells, table_to_grid
from docfactory.taskspec import (
    EVENT_PROGRESS,
    EVENT_STAGE_CHANGE,
    TaskCancelled,
    TaskContext,
    TaskOutcome,
)
from docfactory.tokenizer import configure_models_dir, count_tokens

__all__ = ["chunk_document", "count_tokens", "run_rechunk"]

# 能承载 heading_path 与 parent 归属的容器节点类型
_CONTAINERS = ("section", "slide", "sheet")

_ROOT_KEY = "__root__"           # 虚拟根块（§3.1 规则⑤：无标题归属的孤儿内容）
_FOOTNOTE_KEY = "__footnotes__"  # 脚注归尾块
_FOOTNOTE_HEADING = "脚注"

_HEADING_SEP = ">"
_MAX_HEADING_SEG = 60            # 单级标题截断（防止把整段正文误判成标题后撑爆列）
_MAX_HEADING_PATH = 240          # 整条路径截断（chunks.heading_path 是索引与展示用，不是正文）

_PARENT_BUDGET_RATIO = 8         # parent 相对 max_tokens 的软上限（见 _parent_batches）

_MAX_HEADER_ROWS = 3             # 大表分片时最多复制几行表头
_MIN_FILL_RATIO = 0.5            # 当前片不足目标一半时，允许超目标吸收下一个单元（避免碎片）
_SOFT_LIMIT_RATIO = 1.25         # 文本单元超目标 25% 才拆（给「保住一个完整段落」留余量）
_BLOCK_SEP = "\n\n"

# 句子边界：中文句末标点（含其后的闭合引号/括号）、英文句末标点（要求其后是空白，
# 避开 e.g. / 3.14 这类误切）、以及换行本身。
_SENTENCE_END = re.compile(
    r"[。！？；…]+[」』”’）】》]*[ \t]*"
    r"|[.!?;]+[\"')\]]*(?=[ \t\n]|$)[ \t]*"
    r"|\n+"
)


# ---------------------------------------------------------------- 内部数据结构


@dataclass
class _Unit:
    """切片装箱的最小单位：一个 IR 内容节点渲染出的文本块。"""

    text: str
    node_ids: list[str]
    pages: list[int]
    tokens: int
    table: IRNode | None = None   # 表格单元：超上限时按「行组」切而不是按句子切
    title: str = ""               # 表格标题（分片时每片都要复制）
    heading_only: bool = False     # section 自己的标题行（无正文时不单独成块）


@dataclass
class _Bucket:
    """一段连续的、切片规则相同的内容（文本流 / 一张表 / 一页 PPT / 一个数据区域）。"""

    type: str                     # text|table|slide|sheet_region（与 chunks.type 同域）
    group_key: str                # parent 归属键（容器节点 id / 虚拟根 / 脚注）
    heading_path: str
    units: list[_Unit] = field(default_factory=list)
    merged_flag: bool | None = None   # 合并单元格判定的惰性缓存（见 _bucket_has_merged_cells）

    @property
    def allow_overlap(self) -> bool:
        # §3.2：重叠只作用于文本块 —— 表格重叠会产生半张表，slide 重叠破坏「一页一片」
        return self.type == "text"

    def full_text(self) -> str:
        return _BLOCK_SEP.join(u.text for u in self.units if u.text)


@dataclass
class _Piece:
    """装箱结果：一个 child 块的内容。"""

    text: str
    node_ids: list[str]
    pages: list[int]
    part: int = 1
    parts: int = 1
    overlap_chars: int = 0


# ---------------------------------------------------------------- 公开入口


def chunk_document(ir: IRDocument, cs: ChunkSettings, *, doc_id: str) -> list[dict[str, Any]]:
    """按 §3 规则把 IR 切成 chunks 表行（parent 与 child 混排，seq 全文档连续递增）。

    返回的行可直接交给 ``db.replace_chunks``；pages/node_ids/meta_json 已是 JSON 字符串。

    纯函数、不碰磁盘与数据库，解析流水线可直接调用。若希望 token 计数走本地 Qwen
    tokenizer（而不是启发式估算），调用前先执行一次
    ``docfactory.tokenizer.configure_models_dir(paths.models)``；不调也能正常工作，
    只是 token_count 是估算值。
    """
    cs = _sanitize(cs)
    buckets, footnotes = _collect_buckets(ir, cs)
    if footnotes:
        # §3.2 footnote_to_end：脚注汇总成独立块挂在文档最后（正文里不再夹杂脚注噪声）
        buckets.append(
            _Bucket(type="text", group_key=_FOOTNOTE_KEY,
                    heading_path=_FOOTNOTE_HEADING, units=footnotes)
        )

    groups: dict[str, list[_Bucket]] = {}
    for bucket in buckets:
        groups.setdefault(bucket.group_key, []).append(bucket)

    rows: list[dict[str, Any]] = []
    seq = parent_no = child_no = 0
    for key, group in groups.items():          # dict 保序 → 组的先后即文档先后
        for batch in _parent_batches(group, cs):
            pieces: list[tuple[_Bucket, _Piece]] = []
            for bucket in batch:
                for piece in _split_bucket(bucket, cs):
                    pieces.append((bucket, piece))
            if not pieces:
                continue

            parent_no += 1
            parent_local = f"p-{parent_no:04d}"
            parent_id = _chunk_id(doc_id, parent_local)
            parent_text = _BLOCK_SEP.join(b.full_text() for b in batch if b.units)
            types = {b.type for b in batch}
            seq += 1
            rows.append(_chunk_row(
                chunk_id=parent_id,
                doc_id=doc_id,
                seq=seq,
                parent_id=None,
                kind="parent",
                ctype=types.pop() if len(types) == 1 else "text",
                text=parent_text,
                heading_path=batch[0].heading_path,
                node_ids=_merge_ids(u.node_ids for b in batch for u in b.units),
                pages=_merge_pages(u.pages for b in batch for u in b.units),
                meta={
                    "local_id": parent_local,
                    "children": len(pieces),
                    "blocks": len(batch),
                    "virtual_root": key == _ROOT_KEY,
                    "footnotes": key == _FOOTNOTE_KEY,
                },
            ))

            for bucket, piece in pieces:
                child_no += 1
                seq += 1
                local_id = f"c-{child_no:04d}"
                meta: dict[str, Any] = {
                    "local_id": local_id,
                    "parent_local_id": parent_local,
                    "part": piece.part,
                    "parts": piece.parts,
                }
                if piece.overlap_chars:
                    meta["overlap_chars"] = piece.overlap_chars
                if bucket.type in ("table", "sheet_region"):
                    meta["merged_cells"] = _bucket_has_merged_cells(bucket)
                rows.append(_chunk_row(
                    chunk_id=_chunk_id(doc_id, local_id),
                    doc_id=doc_id,
                    seq=seq,
                    parent_id=parent_id,
                    kind="child",
                    ctype=bucket.type,
                    text=piece.text,
                    heading_path=bucket.heading_path,
                    node_ids=piece.node_ids,
                    pages=piece.pages,
                    meta=meta,
                ))
    return rows


def run_rechunk(ctx: TaskContext) -> TaskOutcome:
    """rechunk 任务入口（§3.4）：读已存 IR → 重新切片 → 整体覆盖 chunks，**不重新解析**。

    payload: ``{"doc_id": str, "chunk": {可选的 ChunkSettings 覆盖}}``。
    覆盖参数只在本次生效，不写回 settings.json —— 导出中心调参是「这一票」的行为，
    要长期改默认值该走 PUT /settings。
    """
    doc_id = str(ctx.payload.get("doc_id") or ctx.doc_id or "").strip()
    if not doc_id:
        raise DocFactoryError("E03", "缺少参数 doc_id")

    ir_path = ctx.paths.doc_ir_path(doc_id)
    if not ir_path.is_file():
        raise DocFactoryError("E05", f"未找到该文档的解析结果（{ir_path.name}），请先重新解析")

    configure_models_dir(ctx.paths.models)     # token 计数优先用本地 Qwen tokenizer
    cs = _merge_chunk_settings(ctx.settings.chunk, ctx.payload.get("chunk"))

    ctx.progress(EVENT_STAGE_CHANGE, {"stage": "chunk"})
    if ctx.cancelled():
        raise TaskCancelled()

    try:
        ir = IRDocument.load(ir_path)
    except (OSError, ValueError, ValidationError) as exc:
        raise DocFactoryError("E05", f"解析结果文件损坏，无法重切：{exc}") from exc

    chunks = chunk_document(ir, cs, doc_id=doc_id)
    # 落库前的最后一个取消检查点：replace_chunks 是整体覆盖，中途放弃不会留半成品
    if ctx.cancelled():
        raise TaskCancelled()
    count = ctx.db.replace_chunks(doc_id, chunks)

    ctx.progress(EVENT_PROGRESS, {"page": 1, "total": 1, "stage": "chunk"})
    ctx.db.log_event(
        level="info",
        task_id=ctx.task_id,
        doc_id=doc_id,
        stage="chunk",
        message=f"重切完成：{count} 个切片",
        detail={"chunk_count": count, "chunk_settings": cs.model_dump()},
    )
    # 不 bump_metrics(chunk_cnt=…)：重切是同一批内容的再加工，累计进日指标会把仪表盘刷虚高
    if count == 0:
        ctx.db.log_event(
            level="warning", task_id=ctx.task_id, doc_id=doc_id, stage="chunk",
            code="E05", message="重切后没有产生任何切片，请检查解析结果是否为空",
        )
    return TaskOutcome(
        status="done",
        message=f"重切完成，共 {count} 个切片",
        result={"doc_id": doc_id, "chunk_count": count},
    )


# ---------------------------------------------------------------- 参数


def _sanitize(cs: ChunkSettings) -> ChunkSettings:
    """兜底夹紧：上限不得小于目标、重叠不得吃掉半块，避免病态参数把切片逻辑逼进死角。"""
    target = max(16, int(cs.target_tokens))
    hard = max(target, int(cs.max_tokens))
    overlap = min(0.5, max(0.0, float(cs.overlap)))
    if (target, hard, overlap) == (cs.target_tokens, cs.max_tokens, cs.overlap):
        return cs
    return cs.model_copy(update={"target_tokens": target, "max_tokens": hard, "overlap": overlap})


def _merge_chunk_settings(base: ChunkSettings, override: Any) -> ChunkSettings:
    if not isinstance(override, dict) or not override:
        return _sanitize(base)
    data = base.model_dump()
    unknown = [k for k in override if k not in ChunkSettings.model_fields]
    if unknown:
        raise DocFactoryError("E03", f"未知的切片参数：{'、'.join(unknown)}")
    data.update(override)
    try:
        return _sanitize(ChunkSettings.model_validate(data))
    except ValidationError as exc:
        raise DocFactoryError("E03", f"切片参数不合法：{exc}") from exc


# ---------------------------------------------------------------- 收集：IR → buckets


def _collect_buckets(ir: IRDocument, cs: ChunkSettings) -> tuple[list[_Bucket], list[_Unit]]:
    """遍历 IR，把内容节点归入 bucket；返回 (buckets, 待归尾的脚注单元)。"""
    nmap = ir.node_map()
    chains = _chain_index(ir, nmap)
    buckets: list[_Bucket] = []
    footnotes: list[_Unit] = []
    pending_notes: dict[str, _Unit] = {}   # slide 备注要排到该页所有内容之后
    current: _Bucket | None = None

    def close_current() -> None:
        nonlocal current
        if current is None:
            return
        note = pending_notes.pop(current.group_key, None)
        if note is not None:
            current.units.append(note)
        # 只有一行标题、没有正文的桶直接丢弃：标题已经写进各级 heading_path，
        # 单独成块只会产生一个永远命不中的噪声切片（有正文时标题仍在块首）
        if current.units and not (len(current.units) == 1 and current.units[0].heading_only):
            buckets.append(current)
        current = None

    def open_bucket(btype: str, group_key: str, heading_path: str) -> _Bucket:
        nonlocal current
        if current is not None and (current.group_key != group_key or current.type != btype):
            close_current()
        if current is None:
            current = _Bucket(type=btype, group_key=group_key, heading_path=heading_path)
        return current

    for node in _ordered_nodes(ir):
        ntype = node.type
        if ntype == "sheet":
            continue                                  # 容器本身不产内容（名字进 heading_path）
        if ntype in ("header", "footer") and cs.drop_header_footer:
            continue                                  # §3.2 drop_header_footer
        if ntype == "footnote" and cs.footnote_to_end:
            unit = _make_unit(node, _node_text(node))
            if unit is not None:
                footnotes.append(unit)
            continue

        chain = chains.get(node.id, ())
        owner = _owner_of(chain, cs.split_by_heading)
        group_key = owner.id if owner is not None else _ROOT_KEY
        heading_path = _heading_path(chain)
        # slide 的直属内容一律留在 slide 桶里（规则②「每 slide 一片」）
        text_type = "slide" if owner is not None and owner.type == "slide" else "text"

        if ntype in ("table", "sheet_region"):
            unit = _make_table_unit(node, heading_path)
            if unit is None:
                continue
            if not cs.table_atomic:
                unit.table = None                     # 关掉原子性：当普通文本单元参与聚合
                open_bucket(text_type, group_key, heading_path).units.append(unit)
            elif text_type == "slide":
                open_bucket("slide", group_key, heading_path).units.append(unit)
            else:
                close_current()                       # 规则①：表格独立成片，前后文本各自成块
                buckets.append(_Bucket(type=ntype, group_key=group_key,
                                       heading_path=heading_path, units=[unit]))
            continue

        if ntype == "slide":
            title = _container_label(node)
            bucket = open_bucket("slide", group_key, heading_path)
            if title:
                bucket.units.append(_Unit(text=title, node_ids=[node.id],
                                          pages=_node_pages(node), tokens=count_tokens(title)))
            notes = _clean(node.content.notes)
            if notes:
                note_text = f"备注：{notes}"
                pending_notes[group_key] = _Unit(
                    text=note_text, node_ids=[node.id], pages=_node_pages(node),
                    tokens=count_tokens(note_text),
                )
            continue

        unit = _make_unit(node, _node_text(node))
        if unit is None:
            continue
        unit.heading_only = ntype == "section"
        open_bucket(text_type, group_key, heading_path).units.append(unit)

    close_current()
    # 收尾：还没排出去的 slide 备注（该 slide 后面没有别的内容触发换桶）
    for key, note in list(pending_notes.items()):
        for bucket in reversed(buckets):
            if bucket.group_key == key:
                bucket.units.append(note)
                break
    return buckets, footnotes


def _ordered_nodes(ir: IRDocument) -> list[IRNode]:
    """文档顺序的节点序列；父引用断裂的孤儿节点补在末尾（宁可位置不佳也不静默丢内容）。"""
    ordered = list(ir.walk())
    if len(ordered) == len(ir.nodes):
        return ordered
    seen = {n.id for n in ordered}
    ordered.extend(n for n in ir.nodes if n.id not in seen)
    return ordered


def _chain_index(ir: IRDocument, nmap: dict[str, IRNode]) -> dict[str, tuple[IRNode, ...]]:
    """每个节点的容器链（根→深，含自身）。按 id 记忆化，深树也只算一遍。"""
    cache: dict[str, tuple[IRNode, ...]] = {}
    for node in ir.nodes:
        if node.id in cache:
            continue
        # 先把「自身→根」的路径摊平，再从最靠近根的一端回填，避免递归栈深
        path: list[IRNode] = []
        cur: IRNode | None = node
        seen: set[str] = set()
        while cur is not None and cur.id not in seen and cur.id not in cache:
            seen.add(cur.id)
            path.append(cur)
            cur = nmap.get(cur.parent) if cur.parent else None
        base = cache.get(cur.id, ()) if cur is not None else ()
        for item in reversed(path):
            base = (*base, item) if item.type in _CONTAINERS else base
            cache[item.id] = base
    return cache


def _owner_of(chain: tuple[IRNode, ...], split_by_heading: bool) -> IRNode | None:
    """parent 归属的容器：默认取最深 section（§3.1 规则④）。

    split_by_heading=False 时取**最外层**容器 —— 语义是「不在标题边界切」，于是同一章下
    的各级小节文本连成一条流按长度切；但仍以章为单位收口，免得 parent 变成整篇文档。
    """
    if not chain:
        return None
    return chain[-1] if split_by_heading else chain[0]


# ---------------------------------------------------------------- 渲染：IR 节点 → 文本


def _clean(text: str | None) -> str:
    return (text or "").strip()


def _node_text(node: IRNode) -> str:
    """内容节点的纯文本表示（figure 用图注 + 图内 OCR 文字，无文字则不产块）。"""
    c = node.content
    if node.type == "figure":
        parts = [_clean(c.caption), _clean(c.ocr_text)]
        return "\n".join(p for p in parts if p)
    if node.type == "footnote":
        text = _clean(c.text)
        return f"[脚注] {text}" if text else ""
    return _clean(c.text)


def _container_label(node: IRNode) -> str:
    if node.type == "slide":
        title = _clean(node.content.title)
        if title:
            return title
        page = _first_page(node)
        return f"第 {page} 页" if page else "幻灯片"
    if node.type == "sheet":
        return _clean(node.content.name) or "工作表"
    return _clean(node.content.text)


def _first_page(node: IRNode) -> int | None:
    return node.prov[0].page if node.prov else None


def _node_pages(node: IRNode) -> list[int]:
    return sorted({p.page for p in node.prov if p.page})


def _heading_path(chain: tuple[IRNode, ...]) -> str:
    """形如 "第2章>2.3 交付条款"；逐级与整体都做长度截断保护。"""
    segs: list[str] = []
    for node in chain:
        seg = re.sub(r"\s+", " ", _container_label(node)).strip()
        if not seg:
            continue
        if len(seg) > _MAX_HEADING_SEG:
            seg = seg[: _MAX_HEADING_SEG - 1] + "…"
        segs.append(seg)
    path = _HEADING_SEP.join(segs)
    if len(path) <= _MAX_HEADING_PATH:
        return path
    # 超长时从左侧丢：越深的层级对定位越有用，优先保留
    keep: list[str] = []
    total = 1
    for seg in reversed(segs):
        if total + len(seg) + 1 > _MAX_HEADING_PATH:
            break
        keep.append(seg)
        total += len(seg) + 1
    return "…" + _HEADING_SEP + _HEADING_SEP.join(reversed(keep))


def _make_unit(node: IRNode, text: str) -> _Unit | None:
    if not text:
        return None
    return _Unit(text=text, node_ids=[node.id], pages=_node_pages(node), tokens=count_tokens(text))


def _make_table_unit(node: IRNode, heading_path: str) -> _Unit | None:
    """表格 / sheet_region → 带标题的 Markdown 表格单元。"""
    table = node.content.table
    grid = table_to_grid(table) if table is not None else []
    if not grid:
        return None
    title = _table_title(node, heading_path)
    text = _render_table(grid, _header_row_count(table, grid), title)
    return _Unit(
        text=text, node_ids=[node.id], pages=_node_pages(node),
        tokens=count_tokens(text), table=node, title=title,
    )


def _table_title(node: IRNode, heading_path: str) -> str:
    """表标题：解析器给的题注优先；Excel 用「工作表 区域」；否则退到所属标题。"""
    caption = _clean(node.content.caption) or _clean(node.content.text)
    if caption:
        return caption
    if node.type == "sheet_region":
        rng = _clean(node.content.range)
        base = heading_path.rsplit(_HEADING_SEP, 1)[-1] if heading_path else "数据区域"
        return f"{base} {rng}".strip()
    tail = heading_path.rsplit(_HEADING_SEP, 1)[-1] if heading_path else ""
    return f"{tail} 表格".strip() if tail else "表格"


def _header_row_count(table: TableContent | None, grid: list[list[str]]) -> int:
    """连续的前若干行只要含表头单元格就算表头（分片时逐片复制）。"""
    if table is None or not table.cells:
        return 0
    flags: dict[int, bool] = {}
    for cell in table.cells:
        flags[cell.r] = flags.get(cell.r, False) or cell.is_header
    n = 0
    for r in range(min(len(grid), _MAX_HEADER_ROWS)):
        if not flags.get(r):
            break
        n = r + 1
    return n


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join((c or "").replace("|", "\\|").replace("\n", " ").strip()
                             for c in cells) + " |"


def _render_table(grid: list[list[str]], header_rows: int, title: str,
                  *, part: int = 0, parts: int = 0) -> str:
    """渲染为 Markdown 表格；part/parts 非零时在标题上标注「续 k/n」。"""
    head = grid[:header_rows]
    body = grid[header_rows:]
    lines: list[str] = []
    if title:
        lines.append(f"表：{title}（续 {part}/{parts}）" if parts > 1 else f"表：{title}")
    lines.extend(_md_row(r) for r in head)
    if head:
        lines.append("| " + " | ".join("---" for _ in head[0]) + " |")
    lines.extend(_md_row(r) for r in body)
    return "\n".join(lines)


def _bucket_has_merged_cells(bucket: _Bucket) -> bool:
    """该桶的表格是否含合并单元格（写进每个 child 的 meta）。

    结果按桶缓存：判定要扫全部 cell，而一张大表会切出成百上千个 child，每个 child 都
    重扫一遍就是 O(片数×单元格数) —— 实测 5 万行 × 8 列的工作表，光这一项就占了 171s
    切片总耗时的八成。表格内容在切片期间不会变，缓存一次即可。
    """
    if bucket.merged_flag is None:
        bucket.merged_flag = any(
            u.table is not None and u.table.content.table is not None
            and table_has_merged_cells(u.table.content.table)
            for u in bucket.units
        )
    return bucket.merged_flag


# ---------------------------------------------------------------- 归组：group → parent 批次


def _bucket_tokens(bucket: _Bucket) -> int:
    return sum(u.tokens for u in bucket.units)


def _parent_batches(group: list[_Bucket], cs: ChunkSettings) -> list[list[_Bucket]]:
    """把一个归属组切成若干 parent 批次，防止 parent 退化成「整篇文档」。

    §3.3 说 parent 是章节全文、长度不受 max_tokens 约束 —— 前提是文档**真有标题层级**。
    可现实里「没用标题样式的 docx」「标题识别不出的扫描版 PDF」很常见：它们的全部正文都
    落进同一个虚拟根组，parent 就成了整篇文档，后果是一行几 MB 落库、
    ``GET /documents/{id}/chunks`` 单页就吐出 MB 级 JSON、QA 数据集整篇只出一条样本
    （dataset 层优先取 parent 粒度）、上下文供给单元也塞不进任何模型窗口。

    所以给 parent 留一条**宽松**上限（max_tokens 的 8 倍，默认 8192 token）：正常章节远在
    其下、切片结果逐字节不变；只有上述退化场景才会被切成多个 parent。
    """
    budget = max(int(cs.max_tokens) * _PARENT_BUDGET_RATIO, int(cs.target_tokens))
    batches: list[list[_Bucket]] = []
    cur: list[_Bucket] = []
    cur_tokens = 0
    for bucket in _split_oversized_buckets(group, budget):
        tokens = _bucket_tokens(bucket)
        if cur and cur_tokens + tokens > budget:
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(bucket)
        cur_tokens += tokens
    if cur:
        batches.append(cur)
    return batches


def _split_oversized_buckets(group: list[_Bucket], budget: int) -> list[_Bucket]:
    """单个文本桶自身就超预算时，按**单元（段落）边界**拆成同型桶。

    只拆 text 桶：table/sheet_region/slide 桶拆开就破坏了「表格原子」「一页一片」
    （§3.1 规则①②优先级高于 parent 体积），它们的 child 分片本来就有长度约束，
    真正超大的只有 parent 一行，属于可接受代价。
    """
    out: list[_Bucket] = []
    for bucket in group:
        if bucket.type != "text" or _bucket_tokens(bucket) <= budget:
            out.append(bucket)
            continue
        cur: list[_Unit] = []
        cur_tokens = 0
        for unit in bucket.units:
            if cur and cur_tokens + unit.tokens > budget:
                out.append(_Bucket(type=bucket.type, group_key=bucket.group_key,
                                   heading_path=bucket.heading_path, units=cur))
                cur, cur_tokens = [], 0
            cur.append(unit)
            cur_tokens += unit.tokens
        if cur:
            out.append(_Bucket(type=bucket.type, group_key=bucket.group_key,
                               heading_path=bucket.heading_path, units=cur))
    return out


# ---------------------------------------------------------------- 装箱：buckets → pieces


def _split_bucket(bucket: _Bucket, cs: ChunkSettings) -> list[_Piece]:
    target, hard = cs.target_tokens, cs.max_tokens
    pieces = _pack(bucket.units, target, hard)
    pieces = _enforce_limit(pieces, hard)            # 兜底：真 tokenizer 与估算有出入时的保险
    if bucket.allow_overlap and cs.overlap > 0:
        _apply_overlap(pieces, cs.overlap, hard)
    total = len(pieces)
    for i, piece in enumerate(pieces, start=1):
        piece.part, piece.parts = i, total
    return pieces


def _pack(units: list[_Unit], target: int, hard: int) -> list[_Piece]:
    """贪心装箱：靠拢 target，绝不越过 hard。"""
    pieces: list[_Piece] = []
    cur: list[_Unit] = []
    cur_tokens = 0

    def flush() -> None:
        nonlocal cur, cur_tokens
        if cur:
            pieces.append(_piece_of(cur))
            cur, cur_tokens = [], 0

    for unit in _expand(units, target, hard):
        cost = unit.tokens + (1 if cur else 0)       # 块间分隔符也占 token，一并算进预算
        overflow = bool(cur) and cur_tokens + cost > target
        # 当前片还很空时宁可超一点目标也别切出碎片（只要不越过硬上限）
        elastic = cur_tokens < target * _MIN_FILL_RATIO and cur_tokens + cost <= hard
        if overflow and not elastic:
            flush()
            cost = unit.tokens
        cur.append(unit)
        cur_tokens += cost
    flush()
    return pieces


def _expand(units: list[_Unit], target: int, hard: int) -> list[_Unit]:
    """超限单元先自行拆开再参与装箱 —— 拆出的尾巴还能与后面的内容并成一块，不留碎片。

    文本单元的拆分阈值取 target×1.25 而非硬上限：既让块长贴着目标走（不然 target 形同
    虚设，512/1024 的配置会切出一堆 900+ 的块），又给「保住一个完整段落」留 25% 余量。
    表格单元只在超过硬上限时才拆（§3.1 表格原子优先级最高）。
    """
    out: list[_Unit] = []
    for unit in units:
        limit = hard if unit.table is not None else int(target * _SOFT_LIMIT_RATIO)
        if unit.tokens > limit:
            out.extend(_split_unit(unit, target, hard))
        else:
            out.append(unit)
    return out


def _piece_of(units: list[_Unit]) -> _Piece:
    return _Piece(
        text=_BLOCK_SEP.join(u.text for u in units),
        node_ids=_merge_ids(u.node_ids for u in units),
        pages=_merge_pages(u.pages for u in units),
    )


def _split_unit(unit: _Unit, target: int, hard: int) -> list[_Unit]:
    """单个超限单元的拆分：表格走行组，文本走段内句子边界，都不行才按字符硬切。"""
    if unit.table is not None:
        return _split_table_unit(unit, target, hard)
    return [
        _Unit(text=text, node_ids=list(unit.node_ids), pages=list(unit.pages), tokens=tokens)
        for text, tokens in _pack_texts(_split_sentences(unit.text), target, hard)
    ]


def _split_sentences(text: str) -> list[str]:
    """按句子边界切；保留原分隔符与空白，拼回去与原文完全一致。"""
    parts: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        if m.end() > start:
            parts.append(text[start:m.end()])
            start = m.end()
    if start < len(text):
        parts.append(text[start:])
    merged: list[str] = []
    for part in parts:
        if not part:
            continue
        if not part.strip() and merged:            # 纯空白不单独成句，粘回上一句尾部
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def _pack_texts(segments: list[str], target: int, hard: int) -> list[tuple[str, int]]:
    """把句子（或字符片段）装箱成 (文本, token 数) 列表，每片不超过 hard。"""
    out: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_tokens = 0
    for seg in segments:
        tokens = count_tokens(seg)
        if tokens > hard:                          # 单句就超限：只能按字符硬切
            if cur:
                out.append(("".join(cur), cur_tokens))
                cur, cur_tokens = [], 0
            out.extend(_hard_split(seg, hard))
            continue
        if cur and cur_tokens + tokens > target:
            out.append(("".join(cur), cur_tokens))
            cur, cur_tokens = [], 0
        cur.append(seg)
        cur_tokens += tokens
    if cur:
        out.append(("".join(cur), cur_tokens))
    return out


def _hard_split(text: str, hard: int) -> list[tuple[str, int]]:
    """最后手段：按 token 密度估算窗口逐段收缩，保证每片不超上限（会切断句子）。"""
    out: list[tuple[str, int]] = []
    rest = text
    while rest:
        tokens = count_tokens(rest)
        if tokens <= hard:
            out.append((rest, tokens))
            break
        size = max(1, int(len(rest) * hard / max(1, tokens)))
        while size > 1 and count_tokens(rest[:size]) > hard:
            size = max(1, int(size * 0.85))
        out.append((rest[:size], count_tokens(rest[:size])))
        rest = rest[size:]
    return out


def _split_table_unit(unit: _Unit, target: int, hard: int) -> list[_Unit]:
    """大表按**行组**切分，每片复制表头行 + 表标题（§3.1 规则①）。"""
    node = unit.table
    table = node.content.table if node is not None else None
    grid = table_to_grid(table) if table is not None else []
    if node is None or not grid:
        return [unit]

    header_rows = _header_row_count(table, grid)
    head, body = grid[:header_rows], grid[header_rows:]
    title = unit.title
    # 表头 + 标题本身就撑满上限时逐级放弃（先弃表头再弃标题），保证正文行还装得下
    base = count_tokens(_render_table(head, len(head), title))
    if base > hard:
        head = []
        base = count_tokens(_render_table([], 0, title))
    if base > hard:
        title = ""
        base = 0
    goal = target if base < target else hard

    groups: list[list[list[str]]] = []
    cur: list[list[str]] = []
    cur_tokens = 0
    for row in body:
        tokens = count_tokens(_md_row(row))
        if cur and base + cur_tokens + tokens > goal:
            groups.append(cur)
            cur, cur_tokens = [], 0
        cur.append(row)
        cur_tokens += tokens
    if cur:
        groups.append(cur)
    if not groups:
        groups = [[]]

    parts = len(groups)
    out: list[_Unit] = []
    for i, rows in enumerate(groups, start=1):
        text = _render_table(head + rows, len(head), title, part=i, parts=parts)
        tokens = count_tokens(text)
        if tokens > hard:
            # 单行本身就超限（长文本单元格）：退化成字符硬切，标题仍逐片保留
            for frag, frag_tokens in _hard_split(text, hard):
                out.append(_Unit(text=frag, node_ids=list(unit.node_ids),
                                 pages=list(unit.pages), tokens=frag_tokens, title=title))
            continue
        out.append(_Unit(text=text, node_ids=list(unit.node_ids),
                         pages=list(unit.pages), tokens=tokens, title=title))
    return out


def _enforce_limit(pieces: list[_Piece], hard: int) -> list[_Piece]:
    """最终防线：任何 child 块都不得超过 max_tokens（估算与真 tokenizer 有出入时兜底）。"""
    out: list[_Piece] = []
    for piece in pieces:
        if count_tokens(piece.text) <= hard:
            out.append(piece)
            continue
        for text, _ in _pack_texts(_split_sentences(piece.text), hard, hard):
            out.append(_Piece(text=text, node_ids=list(piece.node_ids), pages=list(piece.pages)))
    return out


def _apply_overlap(pieces: list[_Piece], overlap: float, hard: int) -> None:
    """相邻文本块重叠：把前一块尾部按句子边界取一段，前置到后一块（§3.2）。

    取自前一块的**原始文本**（不含它自己的重叠前缀），避免重叠内容像滚雪球一样顺着
    链条累积；前置后仍超上限就逐句缩短，实在放不下就放弃这次重叠 —— 长度硬上限优先。
    """
    if len(pieces) < 2:
        return
    originals = [p.text for p in pieces]
    for i in range(1, len(pieces)):
        want = max(1, int(round(count_tokens(originals[i - 1]) * overlap)))
        tail = _tail_sentences(originals[i - 1], want)
        while tail and count_tokens(tail) + count_tokens(pieces[i].text) + 1 > hard:
            sentences = _split_sentences(tail)
            tail = "".join(sentences[1:]) if len(sentences) > 1 else ""
        if not tail.strip():
            continue
        pieces[i].text = tail.strip() + _BLOCK_SEP + pieces[i].text
        pieces[i].overlap_chars = len(tail.strip())


def _tail_sentences(text: str, want_tokens: int) -> str:
    """从文本尾部按句子取够 want_tokens；最多取到半块，免得重叠喧宾夺主。"""
    sentences = _split_sentences(text)
    if not sentences:
        return ""
    limit = max(1, len(sentences) // 2) if len(sentences) > 1 else 1
    picked: list[str] = []
    tokens = 0
    for seg in reversed(sentences[-limit:]):
        picked.append(seg)
        tokens += count_tokens(seg)
        if tokens >= want_tokens:
            break
    return "".join(reversed(picked))


# ---------------------------------------------------------------- 行构造


def _merge_ids(groups: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ids in groups:
        for nid in ids:
            if nid not in seen:
                seen.add(nid)
                out.append(nid)
    return out


def _merge_pages(groups: Any) -> list[int]:
    pages: set[int] = set()
    for item in groups:
        pages.update(item)
    return sorted(pages)


def _chunk_id(doc_id: str, local_id: str) -> str:
    """chunks.id 的全局唯一形式。

    05 章 §2 的样例 ``"chunk_id": "c-0012"`` 只在**单文档内**唯一，而 02 章 §4 的
    ``chunks.id`` 是**全库主键** —— 两篇文档都从 c-0001 起编号必然撞 UNIQUE 约束
    （已实测：第二篇文档入库即 IntegrityError）。这里取「文档号:短号」保证全局唯一，
    同时把 ``c-0012`` 原样写进 ``meta_json.local_id``：导出/UI 想完全对齐样例，读它即可。
    """
    return f"{doc_id}:{local_id}"


def _chunk_row(
    *,
    chunk_id: str,
    doc_id: str,
    seq: int,
    parent_id: str | None,
    kind: str,
    ctype: str,
    text: str,
    heading_path: str,
    node_ids: list[str],
    pages: list[int],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """组装 chunks 表行：JSON 列在这里统一序列化，db 层直接写 TEXT。"""
    return {
        "id": chunk_id,
        "doc_id": doc_id,
        "seq": seq,
        "parent_id": parent_id,
        "kind": kind,
        "type": ctype,
        "text": text,
        "token_count": count_tokens(text),
        "char_count": len(text),
        "heading_path": heading_path,
        "pages": json.dumps(pages, ensure_ascii=False),
        "node_ids": json.dumps(node_ids, ensure_ascii=False),
        "meta_json": json.dumps(meta, ensure_ascii=False),
        "hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
