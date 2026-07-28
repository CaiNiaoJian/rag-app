"""text_coverage 的分母：独立于结构化解析，从 OOXML 包直接统计源文件文本层字符（03 章 §5.2）。

为什么必须走旁路：分母若由解析器自己汇报，「解析器漏了什么」永远测不出来——
此前 docx/pptx/xlsx 一律恒写 1.0，docx 脚注静默丢字、SmartArt 全丢时仪表盘照样满分。
PDF 的分母是 pdfminer 文本层（pdf_parser 自算），docx/pptx 的分母在这里，
xlsx 由 xlsx_parser 在单元格扫描时顺路累计（openpyxl 重开一遍工作簿代价太高）。

口径（与 ``parsers.visible_chars`` 相同：去全部空白后计数）：

- **docx**：``document.xml`` 全部 ``w:t``（含文本框——mc:AlternateContent 双分支的重复
  两侧口径一致，不影响比值）+ ``footnotes.xml``/``endnotes.xml``（解析器目前不抽脚注，
  这正是让指标诚实的关键：丢了就要看得见）+ header/footer 部件（按「块文本」去重，
  与 docx_parser「同文只留一份」的规则对齐；页眉里的表格解析器不抽，同样计入分母）。
- **pptx**：``slides/slide*.xml`` 的 ``a:t``（剔除 ``a:fld`` 域文本：页码/日期，
  python-pptx 的 runs 不含它，解析器不会产出）+ ``notesSlides/*`` 中 body 占位符文本
  + ``diagrams/data*.xml``（SmartArt 文本，解析器目前完全不抽）。
- **图表 XML 不计入**：03 章 §7 明确不还原图形数据（只保留标题），
  属于有契约的取舍而非静默丢失，不该压低覆盖率。

任何异常返回 None：宁可让 UI 显示「覆盖率未知」，也不给一个假的 1.0。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from pathlib import Path

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

_WS = re.compile(r"\s+")

_HDR_FTR = re.compile(r"word/(?:header|footer)\d*\.xml")
_SLIDE = re.compile(r"ppt/slides/slide\d+\.xml")
_NOTES = re.compile(r"ppt/notesSlides/notesSlide\d+\.xml")
_DIAGRAM = re.compile(r"ppt/diagrams/data\d+\.xml")


def raw_char_total(src: Path, fmt: str) -> int | None:
    """返回 ``src`` 的文本层可见字符总数；不支持的格式或统计失败返回 None。"""
    try:
        if fmt == "docx":
            return _docx_chars(Path(src))
        if fmt == "pptx":
            return _pptx_chars(Path(src))
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- 公共小件


def _visible(text: str) -> int:
    return len(_WS.sub("", text))


def _texts(root: ET.Element, tag: str) -> Iterator[str]:
    for el in root.iter(tag):
        if el.text:
            yield el.text


def _chars(root: ET.Element, tag: str) -> int:
    return sum(_visible(t) for t in _texts(root, tag))


# ---------------------------------------------------------------- docx


def _docx_chars(src: Path) -> int:
    total = 0
    with zipfile.ZipFile(src) as zf:
        names = set(zf.namelist())
        for name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
            if name in names:
                total += _chars(ET.fromstring(zf.read(name)), _W + "t")

        # header/footer：同一段文字在多 section/多部件重复是常态（整篇统一页眉），
        # docx_parser 按文本去重只留一份 —— 分母用同一条规则，否则会凭空压低覆盖率。
        # 去重粒度是「顶层块」（段落/表格），与解析器逐段去重的口径对齐。
        seen: set[str] = set()
        for name in sorted(n for n in names if _HDR_FTR.fullmatch(n)):
            root = ET.fromstring(zf.read(name))
            for block in root:  # w:hdr / w:ftr 的直接子元素：w:p、w:tbl 等
                text = "".join(_texts(block, _W + "t"))
                key = _WS.sub("", text)
                if key and key not in seen:
                    seen.add(key)
                    total += len(key)
    return total


# ---------------------------------------------------------------- pptx


def _pptx_chars(src: Path) -> int:
    total = 0
    with zipfile.ZipFile(src) as zf:
        for name in sorted(zf.namelist()):
            if _SLIDE.fullmatch(name):
                root = ET.fromstring(zf.read(name))
                total += _chars(root, _A + "t") - _fld_chars(root)
            elif _NOTES.fullmatch(name):
                total += _notes_body_chars(ET.fromstring(zf.read(name)))
            elif _DIAGRAM.fullmatch(name):
                total += _chars(ET.fromstring(zf.read(name)), _A + "t")
    return total


def _fld_chars(root: ET.Element) -> int:
    """``a:fld``（幻灯片编号/日期域）内的文本量——解析器不产出，须从分母剔除。"""
    return sum(_chars(fld, _A + "t") for fld in root.iter(_A + "fld"))


def _notes_body_chars(root: ET.Element) -> int:
    """备注页只计 body 占位符：页面缩略图/页码占位符不是演讲者备注。"""
    total = 0
    for sp in root.iter(_P + "sp"):
        ph = next(iter(sp.iter(_P + "ph")), None)
        if ph is not None and ph.get("type") == "body":
            total += _chars(sp, _A + "t") - _fld_chars(sp)
    return total
