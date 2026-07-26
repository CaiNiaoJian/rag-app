"""IR v1.0 契约（04 章）：构造、树遍历、序列化往返、表格网格展开。"""

from __future__ import annotations

from pathlib import Path

from docfactory import IR_VERSION
from docfactory.ir import (
    IRBuilder,
    IRDocMeta,
    IRDocument,
    NodeContent,
    Prov,
    TableCell,
    TableContent,
    table_has_merged_cells,
    table_to_grid,
)


def _meta() -> IRDocMeta:
    return IRDocMeta(id="doc-1", source_file="合同v3.doc", source_format="doc")


def test_builder_numbers_nodes_and_links_parents():
    b = IRBuilder(_meta())
    ch1 = b.add("section", level=1, content=NodeContent(text="第1章"))
    p1 = b.add("paragraph", parent=ch1, content=NodeContent(text="正文一"))
    ch11 = b.add("section", parent=ch1, level=2, content=NodeContent(text="1.1 小节"))
    p2 = b.add("paragraph", parent=ch11, content=NodeContent(text="正文二"))
    ir = b.build()

    assert [n.id for n in ir.nodes] == ["n1", "n2", "n3", "n4"]
    assert ch1.children == [p1.id, ch11.id]     # 父节点的 children 被双向维护
    assert p2.parent == ch11.id
    assert [n.id for n in ir.roots()] == ["n1"]
    assert [n.id for n in ir.walk()] == ["n1", "n2", "n3", "n4"]  # 先序


def test_walk_covers_multiple_roots_in_order():
    b = IRBuilder(_meta())
    s1 = b.add("slide", content=NodeContent(title="第一页"))
    b.add("paragraph", parent=s1, content=NodeContent(text="要点"))
    b.add("slide", content=NodeContent(title="第二页"))
    ir = b.build()
    assert [n.id for n in ir.walk()] == ["n1", "n2", "n3"]


def test_save_load_roundtrip_drops_none_but_keeps_semantics(tmp_path: Path):
    b = IRBuilder(_meta())
    b.add(
        "section", level=2, content=NodeContent(text="2.3 交付条款"),
        prov=[Prov(page=3, bbox=[72.0, 340.2, 523.5, 368.9], charspan=[120, 384])],
        confidence=0.93,
    )
    ir = b.build()
    p = tmp_path / "doc.ir.json"
    ir.save(p)

    raw = p.read_text(encoding="utf-8")
    assert '"image_ref"' not in raw          # exclude_none 生效，联合面里未用的字段不落盘
    assert '"ir_version":"1.0"' in raw.replace(" ", "")

    back = IRDocument.load(p)
    assert back.ir_version == IR_VERSION
    node = back.nodes[0]
    assert node.content.text == "2.3 交付条款"
    assert node.prov[0].page == 3 and node.confidence == 0.93
    assert node.content.image_ref is None     # 缺省回填为 None，读取侧无需判存在


def test_iter_children_skips_dangling_ids():
    b = IRBuilder(_meta())
    parent = b.add("section", level=1, content=NodeContent(text="章"))
    child = b.add("paragraph", parent=parent, content=NodeContent(text="段"))
    ir = b.build()
    parent.children.append("n-does-not-exist")   # 模拟外部数据轻微损坏
    assert [n.id for n in ir.iter_children(parent)] == [child.id]


def test_table_to_grid_places_merged_cells_at_top_left():
    table = TableContent(cells=[
        TableCell(r=0, c=0, colspan=2, text="项目", is_header=True),
        TableCell(r=0, c=2, text="金额", is_header=True),
        TableCell(r=1, c=0, rowspan=2, text="设备"),
        TableCell(r=1, c=1, text="服务器"),
        TableCell(r=1, c=2, text="100"),
        TableCell(r=2, c=1, text="交换机"),
        TableCell(r=2, c=2, text="200"),
    ])
    grid = table_to_grid(table)
    assert grid == [
        ["项目", "", "金额"],
        ["设备", "服务器", "100"],
        ["", "交换机", "200"],
    ]
    assert table_has_merged_cells(table) is True


def test_table_to_grid_handles_empty():
    assert table_to_grid(TableContent()) == []
    assert table_has_merged_cells(TableContent()) is False
