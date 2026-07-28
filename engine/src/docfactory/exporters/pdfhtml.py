"""PDF 导出的引擎侧半程：打印用 HTML + 专用打印 CSS（05 章 §1）。

分工：**引擎产出 `.print.html`，Electron 用离屏 BrowserWindow 的 `printToPDF()` 渲染成 PDF**
（02 章架构图）。这样 PDF 引擎就是 Chromium 本身——完全离线、零外部二进制依赖，
中文排版与 Web 一致；引擎侧不引入任何 GPL 系的 PDF 库。

离线红线：CSS 与 HTML 中**不出现任何 http(s) URL**。字体走系统字体族
（"Microsoft YaHei","Segoe UI",sans-serif），图片走 `file://` 绝对路径。

打印细节（对应 05 章「专用打印 CSS」）：
- `@page` 设定纸张与页边距；
- `thead{display:table-header-group}` 让长表格每页重复表头；
- 标题 `break-after:avoid` 防止标题掉在页底成孤行；
- 图片/表格行 `break-inside:avoid` 防止被切成两半；
- 每个标题带 `id`，供 Chromium 生成 PDF 书签与内部锚点跳转。

页码不在 CSS 里做：Chromium 不支持 `@page` 的 margin box（`@bottom-center`），
`counter(page)` 拿不到值。页眉页脚交给 Electron 的 `displayHeaderFooter` +
`headerTemplate/footerTemplate`（引擎把文档标题写进 `<meta>` 供其取用），
同时保留一份固定定位的页眉页脚 div 作为直接浏览该 HTML 时的兜底。
"""

from __future__ import annotations

import html
from pathlib import Path

from docfactory import APP_NAME, ENGINE_VERSION
from docfactory.config import ChunkSettings, PdfExportSettings
from docfactory.exporters.markdown import (
    normalize_image_ref,
    render_table_html,
    safe_filename,
)
from docfactory.ir import IRDocument, IRNode

# 字号护栏：过小无法阅读、过大排版崩坏；用户设置越界时夹到区间内而不是报错
_FONT_MIN, _FONT_MAX = 8, 24

# 页眉页脚固定块的高度（mm），与 body 的上下留白配套
_MARGIN_BLOCK_MM = 10


def _esc(text: str | None) -> str:
    return html.escape(str(text or ""))


def _image_uri(rel: str, out_html: Path) -> str:
    """图片相对引用 → `file://` 绝对 URI。

    优先在导出目录旁找（run_export 会把 assets 复制过来，产物自包含），
    其次退回文档 workspace 的常见相对位置；都不存在时仍给出 assets 下的绝对路径，
    使 HTML 里留有可诊断的线索而不是空 src。
    """
    inner = rel[len("assets/"):] if rel.startswith("assets/") else rel
    parts = inner.split("/")
    base = out_html.parent
    candidates = [
        base / "assets" / Path(*parts),
        base / Path(*parts),
        base.parent / "assets" / Path(*parts),
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve().as_uri()
    fallback = candidates[0]
    try:
        return fallback.resolve().as_uri()
    except (OSError, ValueError):
        return "assets/" + inner


class _HtmlRenderer:
    """IR → 打印用 HTML 片段。与 Markdown 渲染共用表格实现，保证两种产物结构一致。"""

    def __init__(self, ir: IRDocument, cs: ChunkSettings, out_html: Path):
        self.ir = ir
        self.cs = cs
        self.out_html = out_html
        self.nodes = ir.node_map()
        self.parts: list[str] = []
        self.footnotes: list[str] = []
        self.visited: set[str] = set()

    def render(self) -> str:
        for root in self.ir.roots():
            self._node(root, depth=0)
        for node in self.ir.nodes:          # 兜底：父指针悬空的节点也要出现在 PDF 里
            if node.id not in self.visited:
                self._node(node, depth=0)
        if self.footnotes:
            items = "".join(f"<li>{_esc(t)}</li>" for t in self.footnotes)
            self.parts.append(
                f'<section class="footnotes"><h2 id="df-footnotes">脚注</h2><ol>{items}</ol></section>'
            )
        return "\n".join(self.parts)

    def _children(self, node: IRNode) -> list[IRNode]:
        return [self.nodes[cid] for cid in node.children if cid in self.nodes]

    def _node(self, node: IRNode, *, depth: int) -> None:
        if node.id in self.visited:
            return
        self.visited.add(node.id)
        handler = getattr(self, f"_on_{node.type}", None)
        (handler or self._on_paragraph)(node, depth=depth)

    def _descend(self, node: IRNode, *, depth: int) -> None:
        for child in self._children(node):
            self._node(child, depth=depth)

    # ---- 各节点类型 ----

    def _on_section(self, node: IRNode, *, depth: int) -> None:
        level = max(1, min(6, node.level or (depth + 1)))
        text = (node.content.text or "").strip()
        if text:
            # id 用节点号：既是 PDF 书签锚点，也让 UI 能从 IR 树直接跳到打印稿位置
            self.parts.append(f'<h{level} id="{_esc(node.id)}">{_esc(text)}</h{level}>')
        self._descend(node, depth=depth + 1)

    def _on_paragraph(self, node: IRNode, *, depth: int) -> None:
        text = (node.content.text or "").strip()
        if text:
            self.parts.append(f"<p>{_esc(text)}</p>")
        self._descend(node, depth=depth)

    def _on_table(self, node: IRNode, *, depth: int) -> None:
        if node.content.table is not None:
            table = render_table_html(node.content.table)
            if table:
                self.parts.append(f'<div class="table-wrap">{table}</div>')
        self._descend(node, depth=depth)

    def _on_figure(self, node: IRNode, *, depth: int) -> None:
        rel = normalize_image_ref(node.content.image_ref)
        caption = (node.content.caption or "").strip()
        if rel:
            src = _image_uri(rel, self.out_html)
            cap = f"<figcaption>{_esc(caption)}</figcaption>" if caption else ""
            self.parts.append(
                f'<figure><img src="{_esc(src)}" alt="{_esc(caption or "图片")}">{cap}</figure>'
            )
        elif node.content.ocr_text:
            self.parts.append(f'<p class="ocr">{_esc(node.content.ocr_text.strip())}</p>')
        self._descend(node, depth=depth)

    def _on_formula(self, node: IRNode, *, depth: int) -> None:
        text = (node.content.text or "").strip()
        if text:
            self.parts.append(f'<p class="formula">{_esc(text)}</p>')
        self._descend(node, depth=depth)

    def _on_slide(self, node: IRNode, *, depth: int) -> None:
        title = (node.content.title or node.content.text or "").strip()
        self.parts.append(f'<h2 id="{_esc(node.id)}">{_esc(title or "（无标题幻灯片）")}</h2>')
        self._descend(node, depth=depth + 1)
        notes = (node.content.notes or "").strip()
        if notes:
            self.parts.append(f'<div class="notes"><strong>备注</strong><p>{_esc(notes)}</p></div>')

    def _on_sheet(self, node: IRNode, *, depth: int) -> None:
        name = (node.content.name or node.content.text or "").strip()
        self.parts.append(f'<h2 id="{_esc(node.id)}">工作表：{_esc(name or "（未命名）")}</h2>')
        self._descend(node, depth=depth + 1)

    def _on_sheet_region(self, node: IRNode, *, depth: int) -> None:
        rng = (node.content.range or "").strip()
        if rng:
            self.parts.append(f'<p class="range">区域 {_esc(rng)}</p>')
        self._on_table(node, depth=depth)

    def _on_header(self, node: IRNode, *, depth: int) -> None:
        self._marginal(node, depth=depth)

    def _on_footer(self, node: IRNode, *, depth: int) -> None:
        self._marginal(node, depth=depth)

    def _marginal(self, node: IRNode, *, depth: int) -> None:
        if self.cs.drop_header_footer:
            return                          # 子树一并跳过（页眉页脚不会有实质子内容）
        text = (node.content.text or "").strip()
        if text:
            self.parts.append(f'<p class="marginal">{_esc(text)}</p>')
        self._descend(node, depth=depth)

    def _on_footnote(self, node: IRNode, *, depth: int) -> None:
        text = (node.content.text or "").strip()
        if not text:
            return
        if self.cs.footnote_to_end:
            self.footnotes.append(text)
        else:
            self.parts.append(f'<p class="footnote-inline">{_esc(text)}</p>')

    def _on_list(self, node: IRNode, *, depth: int) -> None:
        self.parts.append(self._list_html(node))

    def _list_html(self, node: IRNode) -> str:
        tag = "ol" if node.content.ordered else "ul"
        items: list[str] = []
        for child in self._children(node):
            if child.id in self.visited:
                continue
            self.visited.add(child.id)
            if child.type == "list":
                items.append(f"<li>{self._list_html(child)}</li>")
                continue
            if child.type == "table" and child.content.table is not None:
                items.append(f"<li>{render_table_html(child.content.table)}</li>")
                continue
            text = (child.content.text or "").strip()
            if text:
                items.append(f"<li>{_esc(text)}</li>")
        return f"<{tag}>{''.join(items)}</{tag}>"


def build_print_css(pdf_settings: PdfExportSettings) -> str:
    """打印专用 CSS（全部本地资源，无任何 http(s) URL）。"""
    font_size = max(_FONT_MIN, min(_FONT_MAX, int(getattr(pdf_settings, "font_size", 12) or 12)))
    small = max(_FONT_MIN, font_size - 1)
    header_footer = bool(getattr(pdf_settings, "header_footer", True))
    # 开启页眉页脚时把正文上下留白撑开，避免与固定块重叠
    body_pad = f"{_MARGIN_BLOCK_MM + 4}mm" if header_footer else "0"
    marginal = (
        """
.print-header, .print-footer {
  position: fixed; left: 0; right: 0; color: #666; font-size: 9pt;
}
.print-header { top: 0; border-bottom: 1px solid #ddd; padding-bottom: 2mm; }
.print-footer { bottom: 0; border-top: 1px solid #ddd; padding-top: 2mm; text-align: center; }
"""
        if header_footer
        else ".print-header, .print-footer { display: none; }"
    )
    return f"""
/* 纸张与页边距：A4 纵向，页边距给页眉页脚留出空间 */
@page {{ size: A4; margin: 18mm 16mm; }}

html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{
  font-family: "Microsoft YaHei", "Segoe UI", sans-serif;   /* 系统字体，零外链 */
  font-size: {font_size}pt; line-height: 1.75; color: #1a1a1a;
  margin: 0; padding-top: {body_pad}; padding-bottom: {body_pad};
}}

/* 标题：不与后文分离（避免页底孤行），自身也不跨页 */
h1, h2, h3, h4, h5, h6 {{
  font-weight: 600; line-height: 1.4; margin: 1.1em 0 0.5em;
  break-after: avoid; page-break-after: avoid;
  break-inside: avoid; page-break-inside: avoid;
}}
h1 {{ font-size: {font_size + 8}pt; }}
h2 {{ font-size: {font_size + 5}pt; }}
h3 {{ font-size: {font_size + 3}pt; }}
h4, h5, h6 {{ font-size: {font_size + 1}pt; }}

p {{ margin: 0.5em 0; orphans: 3; widows: 3; text-align: justify; }}
ul, ol {{ margin: 0.5em 0 0.5em 1.6em; padding: 0; }}
li {{ margin: 0.2em 0; break-inside: avoid; page-break-inside: avoid; }}

/* 表格：表头每页重复；单行不跨页；长表允许整体跨页 */
.table-wrap {{ margin: 0.8em 0; }}
table {{ width: 100%; border-collapse: collapse; font-size: {small}pt; }}
thead {{ display: table-header-group; }}
tfoot {{ display: table-footer-group; }}
tr {{ break-inside: avoid; page-break-inside: avoid; }}
th, td {{ border: 1px solid #999; padding: 4px 6px; vertical-align: top; text-align: left; }}
th {{ background: #f2f2f2; font-weight: 600; }}

/* 图片：整体不跨页，宽度不溢出版心 */
figure {{ margin: 0.9em 0; text-align: center; break-inside: avoid; page-break-inside: avoid; }}
figure img {{ max-width: 100%; height: auto; break-inside: avoid; page-break-inside: avoid; }}
figcaption {{ margin-top: 0.3em; color: #555; font-size: {small}pt; }}

.formula {{ text-align: center; font-family: "Cambria Math", "Segoe UI", serif; margin: 0.8em 0; }}
.notes {{ margin: 0.6em 0; padding: 0.4em 0.8em; border-left: 3px solid #ccc; color: #555; }}
.range {{ color: #555; font-size: {small}pt; margin: 0.6em 0 0.2em; }}
.marginal, .ocr {{ color: #777; font-size: {small}pt; }}
.footnotes {{ margin-top: 1.6em; border-top: 1px solid #ccc; padding-top: 0.8em; font-size: {small}pt; }}
.footnote-inline {{ color: #555; font-size: {small}pt; }}
{marginal}
"""


def build_print_html(
    ir: IRDocument, out_html: Path, *, pdf_settings: PdfExportSettings
) -> str:
    """组装完整 HTML 文本（不落盘，供单测与预览直接取用）。"""
    cs = ChunkSettings()   # 打印稿沿用默认版面处理：剔页眉页脚、脚注归尾
    title = Path(ir.doc.source_file or ir.doc.id or "document").stem or "文档"
    body = _HtmlRenderer(ir, cs, Path(out_html)).render()
    header_footer = bool(getattr(pdf_settings, "header_footer", True))
    marginal_html = (
        f'<div class="print-header">{_esc(title)}</div>\n'
        f'<div class="print-footer">由 {_esc(APP_NAME)} 导出 · 保留逻辑结构，非原始版面复刻</div>\n'
        if header_footer
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="generator" content="{_esc(APP_NAME)} {_esc(ENGINE_VERSION)}">
<!-- 供 Electron printToPDF 的 headerTemplate/footerTemplate 取用（Chromium 不支持 @page margin box） -->
<meta name="df-doc-title" content="{_esc(title)}">
<meta name="df-header-footer" content="{'on' if header_footer else 'off'}">
<title>{_esc(title)}</title>
<style>{build_print_css(pdf_settings)}</style>
</head>
<body>
{marginal_html}<main>
{body}
</main>
</body>
</html>
"""


def export_pdf_html(ir: IRDocument, out_html: Path, *, pdf_settings) -> Path:
    """写出打印用 HTML（UTF-8 无 BOM），返回实际路径；PDF 由 Electron 接力渲染。

    调用方约定：把该文档的 assets 复制到 `out_html.parent/assets/` 后再调用，
    图片才会解析成 `file://` 绝对路径（run_export 已代为处理）。
    """
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    text = build_print_html(ir, out_html, pdf_settings=pdf_settings)
    with open(out_html, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return out_html


def default_html_name(doc_name: str) -> str:
    """`合同v3.docx` → `合同v3.print.html`（run_export 与 Electron 侧共用的命名约定）。"""
    return f"{safe_filename(Path(doc_name or 'document').stem)}.print.html"
