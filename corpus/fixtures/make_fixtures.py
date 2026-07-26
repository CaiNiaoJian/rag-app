"""程序化生成 golden corpus 的基础样本（08 章 §3.1）。

为什么不用真实文档：真实语料有版权与体积问题，不能入库；但 CI 必须有可跑的最小回归集。
这里用 python-docx / python-pptx / openpyxl / reportlab 造出**结构确定**的样本 ——
每个样本刻意包含一个已知的失败模式（合并单元格、跨级标题、公式缓存值、超大表截断……），
标注就是生成脚本的意图，永远不会与样本漂移。

真实语料放 corpus/samples/（.gitignore 忽略），与这里的产物一起被 run_corpus.py 扫到。

用法：python corpus/fixtures/make_fixtures.py [输出目录]
"""

from __future__ import annotations

import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "samples"


# ---------------------------------------------------------------- docx


def make_headings_docx(path: Path) -> None:
    """多级标题 + 列表：验证层级还原与 list 节点。"""
    from docx import Document

    doc = Document()
    doc.add_heading("第1章 总则", level=1)
    doc.add_paragraph("本合同由甲乙双方于平等自愿基础上签订，具备完全法律效力。")
    doc.add_heading("1.1 定义", level=2)
    doc.add_paragraph("本合同中「交付物」指乙方按约定向甲方提供的全部成果。")
    doc.add_heading("1.1.1 交付物范围", level=3)
    doc.add_paragraph("交付物包括但不限于：", style=None)
    for item in ("源代码及构建脚本", "部署文档与运维手册", "验收测试报告"):
        doc.add_paragraph(item, style="List Bullet")
    doc.add_heading("第2章 交付条款", level=1)
    doc.add_paragraph("交付期限为合同签订后 90 天。逾期按日千分之三支付违约金。")
    doc.save(path)


def make_merged_table_docx(path: Path) -> None:
    """含合并单元格的表格：验证 rowspan/colspan 与取值归左上格。"""
    from docx import Document

    doc = Document()
    doc.add_heading("采购清单", level=1)
    table = doc.add_table(rows=4, cols=3)
    table.style = "Table Grid"
    headers = ("项目", "规格", "金额")
    for i, text in enumerate(headers):
        table.cell(0, i).text = text
    rows = [
        ("服务器", "2U 机架式", "120000"),
        ("交换机", "48 口千兆", "18000"),
        ("合计", "", "138000"),
    ]
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    # 「合计」行横跨前两列：这是解析器最容易漏掉的一类结构
    table.cell(3, 0).merge(table.cell(3, 1))
    doc.add_paragraph("以上金额均为含税价。")
    doc.save(path)


def make_empty_docx(path: Path) -> None:
    """几乎空的文档：验证 E05「未能提取到有效内容」的优雅报错路径。"""
    from docx import Document

    Document().save(path)


# ---------------------------------------------------------------- pptx


def make_slides_pptx(path: Path) -> None:
    """图文混排 PPT + 演讲者备注：验证 slide 节点与 notes 并入。"""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "季度经营回顾"
    slide.placeholders[1].text = "营收同比增长 23%\n毛利率提升 4 个百分点"
    slide.notes_slide.notes_text_frame.text = "此处强调增长主要来自政企客户。"

    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "区域分布"
    table = slide2.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(6), Inches(2)).table
    table.cell(0, 0).text = "区域"
    table.cell(0, 1).text = "占比"
    table.cell(1, 0).text = "华东"
    table.cell(1, 1).text = "45%"
    table.cell(2, 0).text = "华南"
    table.cell(2, 1).text = "31%"
    prs.save(path)


# ---------------------------------------------------------------- xlsx


def make_multisheet_xlsx(path: Path) -> None:
    """多 sheet + 合并单元格 + 公式缓存值 + 空 sheet：Excel 契约（03 章 §7）的主要分支。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "预算"
    ws["A1"] = "部门预算表"
    ws.merge_cells("A1:C1")
    for col, text in zip("ABC", ("科目", "预算", "实际"), strict=True):
        ws[f"{col}2"] = text
    data = [("人力", 1200000, 1180000), ("市场", 500000, 620000), ("研发", 2000000, 1950000)]
    for i, (name, budget, actual) in enumerate(data, start=3):
        ws[f"A{i}"], ws[f"B{i}"], ws[f"C{i}"] = name, budget, actual
    # 公式单元格：openpyxl 写入时没有缓存值，解析器应回退公式原文并记 warning
    ws["B6"] = "=SUM(B3:B5)"
    ws["A6"] = "合计"

    detail = wb.create_sheet("明细")
    detail["A1"] = "日期"
    detail["B1"] = "金额"
    for i in range(2, 12):
        detail[f"A{i}"] = f"2026-07-{i - 1:02d}"
        detail[f"B{i}"] = i * 1000

    wb.create_sheet("空表")  # 空 sheet 应被跳过
    wb.save(path)


def make_wide_xlsx(path: Path) -> None:
    """较多行的表：验证切片层按行组切分并复制表头（04 章 §3.1 规则 1）。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "流水"
    ws.append(["序号", "客户", "品类", "数量", "单价", "金额"])
    for i in range(1, 601):
        ws.append([i, f"客户{i % 37}", ["硬件", "软件", "服务"][i % 3], i % 9 + 1,
                   round(100 + i * 0.7, 2), round((i % 9 + 1) * (100 + i * 0.7), 2)])
    wb.save(path)


# ---------------------------------------------------------------- pdf


def make_text_pdf(path: Path) -> None:
    """数字 PDF（有文本层）：验证文本覆盖率与标题启发式。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    # 中文字体：Windows 自带黑体；缺失时退回 Helvetica 只写英文（保证脚本在任何机器可跑）
    font_name = "Helvetica"
    for candidate in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if Path(candidate).exists():
            try:
                pdfmetrics.registerFont(TTFont("CJK", candidate))
                font_name = "CJK"
                break
            except Exception:
                continue

    cn = font_name == "CJK"
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4

    def page(title: str, body: list[str]) -> None:
        c.setFont(font_name, 18)
        c.drawString(60, height - 80, title)
        c.setFont(font_name, 11)
        y = height - 120
        for line in body:
            c.drawString(60, y, line)
            y -= 18
        c.showPage()

    if cn:
        page("技术方案说明书", [
            "本方案面向内网环境下的文档知识库建设。",
            "系统完全离线运行，不依赖任何外部服务。",
            "解析引擎支持七种常见办公文档格式。",
        ])
        page("第二章 实施计划", [
            "第一阶段完成环境准备与数据迁移。",
            "第二阶段完成解析入库与质量验收。",
            "交付期限为合同签订后 90 天。",
        ])
    else:
        page("Technical Proposal", [
            "This proposal targets offline knowledge base construction.",
            "The system runs fully offline with no external services.",
            "The parsing engine supports seven office document formats.",
        ])
        page("Chapter 2 Implementation", [
            "Phase one covers environment setup and data migration.",
            "Phase two covers parsing, ingestion and acceptance.",
            "Delivery deadline is 90 days after contract signing.",
        ])
    c.save()


# ---------------------------------------------------------------- 异常样本


def make_corrupt_docx(path: Path) -> None:
    """损坏文件：字节是 zip 但内部不是 OOXML —— 必须报 E01 而不是崩溃。"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("not-a-document.txt", "这不是一个有效的 docx")


def make_truncated_pdf(path: Path) -> None:
    """截断的 PDF：只有文件头，验证 E01 优雅报错。"""
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog")


def make_unsupported_file(path: Path) -> None:
    """非支持格式：导入阶段就该被 probe_file 挡下（E03）。"""
    path.write_text("plain text, not an office document", encoding="utf-8")


# ---------------------------------------------------------------- 注册表

FIXTURES: dict[str, Callable[[Path], None]] = {
    "headings.docx": make_headings_docx,
    "merged_table.docx": make_merged_table_docx,
    "empty.docx": make_empty_docx,
    "slides.pptx": make_slides_pptx,
    "multisheet.xlsx": make_multisheet_xlsx,
    "wide_table.xlsx": make_wide_xlsx,
    "text_document.pdf": make_text_pdf,
    "corrupt.docx": make_corrupt_docx,
    "truncated.pdf": make_truncated_pdf,
    "unsupported.txt": make_unsupported_file,
}


def generate(out_dir: Path = DEFAULT_OUT, *, force: bool = False) -> list[Path]:
    """生成全部 fixtures，返回产出路径。已存在且非 force 时跳过（真实语料不会被覆盖）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, fn in FIXTURES.items():
        target = out_dir / name
        if target.exists() and not force:
            continue
        try:
            fn(target)
            written.append(target)
        except ImportError as exc:
            print(f"跳过 {name}：缺少依赖（{exc}）", file=sys.stderr)
        except Exception as exc:
            print(f"生成 {name} 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    return written


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    force = "--force" in args
    positional = [a for a in args if not a.startswith("--")]
    out_dir = Path(positional[0]) if positional else DEFAULT_OUT

    written = generate(out_dir, force=force)
    print(f"已生成 {len(written)} 个样本到 {out_dir}")
    for p in written:
        print(f"  + {p.name}")
    if not written:
        print("（样本已存在，加 --force 可重新生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
