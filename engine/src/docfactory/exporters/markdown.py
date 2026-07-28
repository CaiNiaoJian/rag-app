"""Markdown 导出（05 章 §1「结构完整还原」）。

本模块同时是导出层的**渲染基座**：文件名净化、assets 归一与复制、表格渲染
（MD 表 / HTML 表两条路）都定义在这里，pdfhtml.py 与 run_export 直接复用，
保证同一份 IR 在 Markdown 与打印 HTML 里渲染出一致的结构。

设计取舍：
- 表格默认渲染为 MD 表（可读、可 diff）；**含合并单元格时退化为 HTML <table>** ——
  MD 表语法表达不了 rowspan/colspan，硬塞会静默丢结构，宁可换一种语法保住语义。
- 脚注归尾（cs.footnote_to_end）用有序列表而非 `[^1]` 定义语法：IR 的 footnote 节点
  没有与正文引用点的绑定关系，凭空造引用标记等于伪造溯源。
- 图片一律相对引用 `assets/xxx`，并在导出到任意目录时把资源复制过去，
  使 .md 与 assets/ 组成自包含的可迁移产物。
"""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

from loguru import logger

from docfactory.config import ChunkSettings
from docfactory.ir import (
    IRDocument,
    IRNode,
    TableContent,
    table_has_merged_cells,
    table_to_grid,
)

# Windows 文件名非法字符 + 控制字符（\x00-\x1f 在 NTFS 上同样非法）
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留设备名（大小写不敏感，带扩展名也保留）：撞上时加前缀避开
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_FALLBACK_NAME = "未命名"

# 文件名长度上限（字符）：Windows MAX_PATH 260，留足目录前缀与 .sharegpt.json 之类的后缀
_NAME_MAX_LEN = 100


def safe_filename(name: str, *, max_len: int = _NAME_MAX_LEN) -> str:
    """把文档名净化为安全的 Windows 文件名（保留中文）。

    非法字符替换为 `_`；去掉首尾空白与结尾的点（资源管理器会静默吃掉）；
    截断到 max_len 字符防长路径失败；撞保留设备名时加下划线前缀。
    """
    cleaned = _ILLEGAL_CHARS.sub("_", str(name or "")).strip()
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return _FALLBACK_NAME
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .") or _FALLBACK_NAME
    if cleaned.split(".", 1)[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def normalize_image_ref(ref: str | None) -> str:
    """IR 的 image_ref 归一为 `assets/xxx` 形式的相对 POSIX 路径。

    解析器写入的可能是 `p1_img1.png` 也可能是 `assets/p1_img1.png`，
    导出侧统一补齐前缀；顺手剔除 `..` 段，防止导出时越出目标目录。
    """
    raw = str(ref or "").replace("\\", "/").lstrip("/")
    parts = [seg for seg in raw.split("/") if seg not in ("", ".", "..")]
    if parts and parts[0] == "assets":
        parts = parts[1:]
    return "assets/" + "/".join(parts) if parts else ""


def collect_image_refs(ir: IRDocument) -> list[str]:
    """文档引用到的全部图片（归一后的相对路径，去重保序）。"""
    refs: list[str] = []
    seen: set[str] = set()
    for node in ir.nodes:
        if node.type != "figure":
            continue
        rel = normalize_image_ref(node.content.image_ref)
        if rel and rel not in seen:
            seen.add(rel)
            refs.append(rel)
    return refs


def copy_assets(refs: list[str], assets_dir: Path | None, out_dir: Path) -> int:
    """把引用到的图片复制到 `out_dir/assets/`，返回成功复制的文件数。

    导出目录与 workspace 资源目录同源时直接跳过（就地导出无需搬运）；
    单个文件缺失只记 warning —— 图片丢失不该让整篇文档导出失败（批次纪律 FR-10）。
    """
    if assets_dir is None:
        return 0
    src_root = Path(assets_dir)
    dst_root = Path(out_dir) / "assets"
    try:
        if src_root.resolve() == dst_root.resolve():
            return 0
    except OSError:  # 路径不可解析（盘符失效等）时按需要复制处理
        pass

    copied = 0
    for rel in refs:
        rel_inner = rel[len("assets/"):] if rel.startswith("assets/") else rel
        src = src_root / Path(*rel_inner.split("/"))
        if not src.is_file():
            logger.warning(f"导出时未找到图片资源，已跳过：{src}")
            continue
        dst = dst_root / Path(*rel_inner.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied += 1
        except OSError as exc:
            logger.warning(f"复制图片资源失败，已跳过：{src} → {dst}（{exc}）")
    return copied


# ---------------------------------------------------------------- 正文文本转义

# 长得像 HTML 标签开头的 `<`：`<p`、`</td`、`<!--`、`<?xml`
_TAG_LIKE = re.compile(r"<(?=[A-Za-z/!?])")


def escape_tag_like(text: str | None) -> str:
    """把文档正文里长得像 HTML 标签的 `<` 转义成 `&lt;`。

    正文是**数据**，不该被下游当标记读。本模块唯一有权产出 HTML 的地方是
    ``render_table_html``；其余文本一旦能伪装成标签，一段以 ``<table>`` 开头的正文就会
    被渲染层当成表格结构吃掉——内容不是显示错了，是直接消失了（Markdown 生态的其他
    渲染器同样会把它当原始 HTML 吞掉）。

    只转义标签形态的 `<`：``a < b``、``<未填写>`` 这类自然文本保持原样，
    不会把正常正文改得面目全非。
    """
    return _TAG_LIKE.sub("&lt;", str(text or ""))


# ---------------------------------------------------------------- 表格渲染


def _md_cell(text: str) -> str:
    """MD 表单元格转义：竖线会截断列，换行会截断行。"""
    escaped = escape_tag_like(text)
    return escaped.replace("|", r"\|").replace("\r\n", "\n").replace("\n", "<br>").strip()


def render_table_markdown(table: TableContent) -> str:
    """简单表 → MD 表。首行固定当表头（MD 语法要求恰好一行表头）。"""
    grid = table_to_grid(table)
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    rows = [[_md_cell(row[i]) if i < len(row) else "" for i in range(width)] for row in grid]
    head, body = rows[0], rows[1:]
    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def render_table_html(table: TableContent) -> str:
    """任意表 → HTML `<table>`（保留 rowspan/colspan）。

    复杂表在 Markdown 里的退化目标，也是打印 HTML 的唯一表格渲染路径 —— 两处共用同一实现，
    避免「MD 里对、PDF 里错」这类只有用户才发现的分叉。
    """
    if not table.cells:
        return ""
    by_row: dict[int, list] = {}
    for cell in sorted(table.cells, key=lambda c: (c.r, c.c)):
        by_row.setdefault(cell.r, []).append(cell)

    ordered_rows = sorted(by_row)
    # 前导的整行表头进 <thead>：打印时配合 thead{display:table-header-group} 实现跨页重复
    head_rows: list[int] = []
    for r in ordered_rows:
        if by_row[r] and all(c.is_header for c in by_row[r]):
            head_rows.append(r)
        else:
            break

    def _row_html(r: int) -> str:
        cells = []
        for cell in by_row[r]:
            tag = "th" if cell.is_header else "td"
            attrs = ""
            if cell.rowspan > 1:
                attrs += f' rowspan="{cell.rowspan}"'
            if cell.colspan > 1:
                attrs += f' colspan="{cell.colspan}"'
            text = html.escape(str(cell.text or "")).replace("\n", "<br>")
            cells.append(f"<{tag}{attrs}>{text}</{tag}>")
        return "<tr>" + "".join(cells) + "</tr>"

    parts = ["<table>"]
    if head_rows:
        parts.append("<thead>")
        parts.extend(_row_html(r) for r in head_rows)
        parts.append("</thead>")
    body_rows = [r for r in ordered_rows if r not in head_rows]
    if body_rows:
        parts.append("<tbody>")
        parts.extend(_row_html(r) for r in body_rows)
        parts.append("</tbody>")
    parts.append("</table>")
    return "\n".join(parts)


def render_table(table: TableContent) -> str:
    """按是否含合并单元格自动选择 MD 表 / HTML 表。"""
    if table_has_merged_cells(table):
        return render_table_html(table)
    return render_table_markdown(table)


# ---------------------------------------------------------------- 文档渲染


class _MarkdownRenderer:
    """IR → Markdown。递归下降而非 walk() 平铺：list/slide/sheet 这类容器
    需要控制自己子节点的渲染形态（列表项、备注块、区域表），平铺拿不到上下文。"""

    def __init__(self, ir: IRDocument, cs: ChunkSettings):
        self.ir = ir
        self.cs = cs
        self.nodes = ir.node_map()
        self.blocks: list[str] = []
        self.footnotes: list[str] = []
        self.visited: set[str] = set()

    # ---- 输出原语 ----

    def _emit(self, block: str) -> None:
        if block:
            self.blocks.append(block)

    def _children(self, node: IRNode) -> list[IRNode]:
        return [self.nodes[cid] for cid in node.children if cid in self.nodes]

    @staticmethod
    def _text(value: str | None) -> str:
        """节点文本的统一取法：先转义标签形态的 `<`，再去首尾空白。

        所有自由文本都走这一道门，正文才不可能伪装成结构化标记（见 escape_tag_like）。
        例外是 ``_on_formula``：公式是另一套子语言，转义会把 LaTeX 改坏。
        """
        return escape_tag_like(value).strip()

    # ---- 主流程 ----

    def render(self) -> str:
        for root in self.ir.roots():
            self._node(root, depth=0)
        # 兜底：parent 指针悬空导致未被树遍历覆盖的节点，按原始顺序补渲染，绝不静默丢内容
        for node in self.ir.nodes:
            if node.id not in self.visited:
                self._node(node, depth=0)
        if self.footnotes:
            self._emit("## 脚注")
            self._emit("\n".join(f"{i}. {t}" for i, t in enumerate(self.footnotes, 1)))
        body = "\n\n".join(b.strip("\n") for b in self.blocks if b.strip())
        return body + "\n" if body else ""

    def _node(self, node: IRNode, *, depth: int) -> None:
        if node.id in self.visited:      # 同时挡住脏数据里的父子环
            return
        self.visited.add(node.id)
        handler = getattr(self, f"_on_{node.type}", None)
        if handler is None:
            self._on_paragraph(node, depth=depth)
            return
        handler(node, depth=depth)

    def _descend(self, node: IRNode, *, depth: int) -> None:
        for child in self._children(node):
            self._node(child, depth=depth)

    # ---- 各节点类型 ----

    def _on_section(self, node: IRNode, *, depth: int) -> None:
        level = node.level or (depth + 1)
        level = max(1, min(6, level))
        text = self._text(node.content.text)
        if text:
            self._emit("#" * level + " " + text)
        self._descend(node, depth=depth + 1)

    def _on_paragraph(self, node: IRNode, *, depth: int) -> None:
        self._emit(self._text(node.content.text))
        self._descend(node, depth=depth)

    def _on_table(self, node: IRNode, *, depth: int) -> None:
        if node.content.table is not None:
            self._emit(render_table(node.content.table))
        self._descend(node, depth=depth)

    def _on_figure(self, node: IRNode, *, depth: int) -> None:
        rel = normalize_image_ref(node.content.image_ref)
        caption = self._text(node.content.caption)
        if rel:
            alt = _md_cell(caption) or "图片"
            self._emit(f"![{alt}]({rel})")
        if caption:
            self._emit(f"*{caption}*")
        elif not rel and node.content.ocr_text:
            # 图片文件缺失但有 OCR 文本时，至少把文字留下来
            self._emit(self._text(node.content.ocr_text))
        self._descend(node, depth=depth)

    def _on_formula(self, node: IRNode, *, depth: int) -> None:
        text = (node.content.text or "").strip()
        if text:
            self._emit(f"$$\n{text}\n$$")
        self._descend(node, depth=depth)

    def _on_slide(self, node: IRNode, *, depth: int) -> None:
        title = self._text(node.content.title or node.content.text)
        self._emit(f"## {title or '（无标题幻灯片）'}")
        self._descend(node, depth=depth + 1)
        notes = self._text(node.content.notes)
        if notes:
            quoted = "\n".join(f"> {line}" for line in notes.splitlines())
            self._emit(f"> **备注**\n>\n{quoted}")

    def _on_sheet(self, node: IRNode, *, depth: int) -> None:
        name = self._text(node.content.name or node.content.text)
        self._emit(f"## 工作表：{name or '（未命名）'}")
        self._descend(node, depth=depth + 1)

    def _on_sheet_region(self, node: IRNode, *, depth: int) -> None:
        rng = self._text(node.content.range)
        if rng:
            self._emit(f"**区域 {rng}**")
        if node.content.table is not None:
            self._emit(render_table(node.content.table))
        self._descend(node, depth=depth)

    def _on_header(self, node: IRNode, *, depth: int) -> None:
        self._marginal(node, depth=depth)

    def _on_footer(self, node: IRNode, *, depth: int) -> None:
        self._marginal(node, depth=depth)

    def _marginal(self, node: IRNode, *, depth: int) -> None:
        if self.cs.drop_header_footer:
            self.visited.update(self._collect_subtree(node))
            return
        text = self._text(node.content.text)
        if text:
            self._emit(f"*{text}*")
        self._descend(node, depth=depth)

    def _on_footnote(self, node: IRNode, *, depth: int) -> None:
        text = self._text(node.content.text)
        if not text:
            return
        if self.cs.footnote_to_end:
            self.footnotes.append(text.replace("\n", " "))
        else:
            self._emit(f"> {text}")

    def _on_list(self, node: IRNode, *, depth: int) -> None:
        lines = self._list_lines(node, indent=0)
        self._emit("\n".join(lines))

    def _list_lines(self, node: IRNode, *, indent: int) -> list[str]:
        ordered = bool(node.content.ordered)
        pad = "  " * indent
        lines: list[str] = []
        index = 1
        for child in self._children(node):
            if child.id in self.visited:
                continue
            self.visited.add(child.id)
            if child.type == "list":
                lines.extend(self._list_lines(child, indent=indent + 1))
                continue
            text = self._text(child.content.text)
            # 表格是本模块唯一有权产出 HTML 的地方，所以放在转义之后覆盖
            if child.type == "table" and child.content.table is not None:
                text = render_table(child.content.table)
            if not text:
                continue
            marker = f"{index}." if ordered else "-"
            body = text.replace("\n", "\n" + pad + "   ")
            lines.append(f"{pad}{marker} {body}")
            index += 1
            # 列表项自身还有子节点（嵌套列表以外的情况）时缩进续排
            for grand in self._children(child):
                if grand.type == "list":
                    lines.extend(self._list_lines(grand, indent=indent + 1))
                    self.visited.add(grand.id)
        return lines

    def _collect_subtree(self, node: IRNode) -> list[str]:
        out = [node.id]
        for child in self._children(node):
            out.extend(self._collect_subtree(child))
        return out


def render_markdown(ir: IRDocument, cs: ChunkSettings) -> str:
    """IR → Markdown 文本（不落盘，供预览与单测直接调用）。"""
    return _MarkdownRenderer(ir, cs).render()


def default_md_name(ir: IRDocument) -> str:
    """IR → 默认 Markdown 文件名（`合同v3.docx` → `合同v3.md`）。

    单独暴露是为了让调用方**在写盘之前**就知道目标文件名：批量导出同名文档到同一目录时，
    必须先算出唯一名再写，否则第二篇会先把第一篇的文件覆盖掉再改名（内容已经丢了）。
    """
    stem = Path(ir.doc.source_file or ir.doc.id or "document").stem
    return f"{safe_filename(stem)}.md"


def write_markdown(
    ir: IRDocument, out_path: Path, *, cs: ChunkSettings, assets_dir: Path | None
) -> Path:
    """按指定路径写出 Markdown，并把引用到的图片复制到同级 `assets/`。

    编码固定 UTF-8 **无 BOM**（Markdown 生态默认，BOM 会让部分解析器把首个 `#` 当正文）；
    换行统一 `\\n`（newline="" 关闭 Windows 平台的 CRLF 转换，跨平台 diff 稳定）。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(ir, cs)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    copy_assets(collect_image_refs(ir), assets_dir, out_path.parent)
    return out_path


def export_markdown(
    ir: IRDocument, out_dir: Path, *, cs: ChunkSettings, assets_dir: Path | None
) -> Path:
    """导出 `{doc}.md` 到 out_dir，并把引用到的图片复制到 `out_dir/assets/`。"""
    return write_markdown(
        ir, Path(out_dir) / default_md_name(ir), cs=cs, assets_dir=assets_dir
    )
