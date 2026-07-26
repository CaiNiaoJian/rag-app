"""中间表示 IR v1.0（04 章，冻结契约）。

所有格式的解析结果归一为本文档树；切片、导出、质量评估、LLM 模组只面向 IR。
序列化位置：workspace/{docId}/parsed/doc.ir.json。
minor 版本只增字段（向后兼容读取）；major 破坏性变更需评审。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from docfactory import IR_VERSION

# 节点类型枚举（冻结）。list 的子节点为 list_item 语义的 paragraph 节点。
NodeType = Literal[
    "section",       # 标题/章节（树形嵌套，level=1..6）
    "paragraph",     # 正文段落
    "table",         # 表格（原子节点，跨页表合并为一个）
    "figure",        # 图片
    "formula",       # 公式
    "slide",         # PPT 页
    "sheet",         # Excel 工作表
    "sheet_region",  # 连续数据区域
    "header",        # 页眉
    "footer",        # 页脚
    "footnote",      # 脚注
    "list",          # 列表
]

ParseLevel = Literal["L0", "L1", "L2"]


class TableCell(BaseModel):
    r: int
    c: int
    rowspan: int = 1
    colspan: int = 1
    text: str = ""
    is_header: bool = False


class TableContent(BaseModel):
    cells: list[TableCell] = Field(default_factory=list)


class Prov(BaseModel):
    """溯源：page 从 1 计；bbox 为 PDF 坐标系（Office 无 bbox 时省略）；
    charspan 为原始文本层字符区间（覆盖率计算用）。"""

    page: int
    bbox: list[float] | None = None       # [x0, y0, x1, y1]
    charspan: list[int] | None = None     # [start, end]


class NodeContent(BaseModel):
    """content 字段联合面（按 type 取用，序列化 exclude_none）。"""

    text: str | None = None               # section/paragraph/formula/header/footer/footnote
    table: TableContent | None = None     # table/sheet_region
    image_ref: str | None = None          # figure：assets 相对路径
    caption: str | None = None            # figure
    ocr_text: str | None = None           # figure：图内文字 OCR 结果
    ocr: bool | None = None               # figure：是否经过 OCR
    title: str | None = None              # slide
    notes: str | None = None              # slide：演讲者备注
    name: str | None = None               # sheet
    range: str | None = None              # sheet_region："A1:F120"
    ordered: bool | None = None           # list


class IRNode(BaseModel):
    id: str
    type: NodeType
    parent: str | None = None
    children: list[str] = Field(default_factory=list)
    level: int | None = None              # section 专用 1..6
    content: NodeContent = Field(default_factory=NodeContent)
    prov: list[Prov] = Field(default_factory=list)
    confidence: float = 1.0               # 规则路径恒 1.0；模型路径 0~1
    extensions: dict[str, Any] = Field(default_factory=dict)  # 保留域，解析器不写入


class IRMetrics(BaseModel):
    """完整性指标（03 章 §5.2）。"""

    text_coverage: float | None = None
    table_confidence: float | None = None
    ocr_confidence: float | None = None
    disordered_pages: int = 0
    degraded_pages: int = 0


class IRDocMeta(BaseModel):
    id: str
    source_file: str
    source_format: str                    # doc|docx|pdf|ppt|pptx|xls|xlsx
    convert_chain: list[str] = Field(default_factory=list)   # 如 "doc->docx(libreoffice 24.8)"
    parse_level: ParseLevel = "L0"        # 整体主级别（占比最高级）
    engine_version: str = ""
    metrics: IRMetrics = Field(default_factory=IRMetrics)


class IRDocument(BaseModel):
    ir_version: str = IR_VERSION
    doc: IRDocMeta
    nodes: list[IRNode] = Field(default_factory=list)

    # ---- 便捷访问 ----

    def node_map(self) -> dict[str, IRNode]:
        return {n.id: n for n in self.nodes}

    def roots(self) -> list[IRNode]:
        return [n for n in self.nodes if n.parent is None]

    def iter_children(self, node: IRNode) -> Iterator[IRNode]:
        m = self.node_map()
        for cid in node.children:
            child = m.get(cid)
            if child is not None:
                yield child

    def walk(self) -> Iterator[IRNode]:
        """按树形先序遍历（roots 按 nodes 中出现顺序）。"""
        m = self.node_map()

        def _walk(n: IRNode) -> Iterator[IRNode]:
            yield n
            for cid in n.children:
                child = m.get(cid)
                if child is not None:
                    yield from _walk(child)

        for r in self.roots():
            yield from _walk(r)

    # ---- 序列化 ----

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(exclude_none=True, indent=None), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> "IRDocument":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class IRBuilder:
    """解析器构造 IR 的辅助：自动编号 n1..nN、维护父子双向引用。"""

    def __init__(self, meta: IRDocMeta):
        self._doc = IRDocument(doc=meta)
        self._seq = 0

    def add(
        self,
        type: NodeType,
        *,
        parent: IRNode | None = None,
        level: int | None = None,
        content: NodeContent | None = None,
        prov: list[Prov] | None = None,
        confidence: float = 1.0,
    ) -> IRNode:
        self._seq += 1
        node = IRNode(
            id=f"n{self._seq}",
            type=type,
            parent=parent.id if parent else None,
            level=level,
            content=content or NodeContent(),
            prov=prov or [],
            confidence=confidence,
        )
        self._doc.nodes.append(node)
        if parent is not None:
            parent.children.append(node.id)
        return node

    def build(self) -> IRDocument:
        return self._doc


def table_to_grid(table: TableContent) -> list[list[str]]:
    """把 cells 展开为二维文本网格（合并单元格取值归左上格，其余为空串）。
    供导出与切片模块共用，保证渲染一致。"""
    if not table.cells:
        return []
    n_rows = max(c.r + c.rowspan for c in table.cells)
    n_cols = max(c.c + c.colspan for c in table.cells)
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for cell in table.cells:
        if 0 <= cell.r < n_rows and 0 <= cell.c < n_cols:
            grid[cell.r][cell.c] = cell.text
    return grid


def table_has_merged_cells(table: TableContent) -> bool:
    return any(c.rowspan > 1 or c.colspan > 1 for c in table.cells)
