"""PDF 解析器 —— M1 只做数字 PDF（03 章 §2/§5.1 的逐页降级链）。

```
L0 Docling 精解析 ──不可用/失败/超时──▶ L1 pdfplumber 文本行+启发式表格 ──失败──▶ L2 pypdfium2 纯文本
```

M1 的实现边界（刻意留白，不是遗漏）：

- ``_try_docling()``：L0 接入点。docling 未安装即返回 None 跳到 L1，**不在 M1 装 docling**
  （模型 ~270MB，属于 M2 的打包范畴）。
- ``_ocr_page()``：扫描页接入点，M2 接 **RapidOCR + onnxruntime CPU EP**（03 章 §3）。
  M1 返回 None：该页记 DGR-L2 降级 + E04 变体 warning 占位，让用户知道「这页是图，
  文字还没识别」，而不是悄悄产出一个空页。

逐页流式处理：一次只持有一页的对象与位图（03 章 §8 内存约束，禁止全文档位图驻留）；
每页结束轮询 ``ctx.cancelled()``。

页快照（preview/p{n}.png）用 pypdfium2 渲染，限宽 1600px；渲染失败只记日志**不阻断解析** ——
预览是锦上添花，正文才是产品价值。
"""

from __future__ import annotations

import re
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from docfactory.errors import DocFactoryError
from docfactory.ir import IRNode, NodeContent, Prov, TableCell, TableContent
from docfactory.parsers import PageTimeout, ParseEnv, run_with_timeout, visible_chars

# 文本层字符数 < 该阈值 → 判定为扫描页（03 章 §1）
SCANNED_CHAR_MIN = 10

# 页快照限宽（像素）：1600 足够 UI 里放大看清，再大只是浪费磁盘与渲染时间
PREVIEW_MAX_WIDTH = 1600
PREVIEW_MAX_SCALE = 3.0

# 标题启发式：字号相对正文的倍率 → section level
_HEADING_RATIOS = ((1.55, 1), (1.30, 2), (1.12, 3))

# 正文字号采样页数（前几页足以代表全篇，全扫一遍不值当）
_SIZE_SAMPLE_PAGES = 3

# 段落切分：行间距超过「行高 × 该系数」视为换段
_PARA_GAP_FACTOR = 1.6

_CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")


# ---------------------------------------------------------------- 打开与探测


def _open_pdfium(src: Path) -> Any:
    try:
        return pdfium.PdfDocument(str(src))
    except Exception as exc:
        text = str(exc).lower()
        if "password" in text or "encrypt" in text:
            raise DocFactoryError("E02", f"「{src.name}」受密码保护，请先解除密码再导入") from exc
        raise DocFactoryError("E01", f"打开 PDF 失败：{src.name}（{type(exc).__name__}: {exc}）") from exc


def _page_text(pdf_page: Any) -> str:
    """pypdfium2 原始文本层（覆盖率分母 + 扫描页判定，两处都要它，所以每页必取）。"""
    textpage = None
    try:
        textpage = pdf_page.get_textpage()
        return textpage.get_text_range() or ""
    except Exception:
        return ""
    finally:
        if textpage is not None:
            with suppress(Exception):
                textpage.close()


def _page_has_image(pdf_page: Any) -> bool:
    """该页是否含位图对象 —— 用来把「扫描页」和「真·空白页」分开。

    两者的文本层同样是空的，含义却相反：扫描页的文字锁在位图里等着 OCR，
    空白页（章节分隔页、双面排版的背面、落版页）本来就没有内容。
    只看文本层长度会把两者一起报成「扫描图像，文字未提取」，后果是
    **任何一份带空白页的数字 PDF 都被压成 warning**，用户还会照着提示
    去预览里找根本不存在的文字 —— 警示一旦经常误报就不再有人看。
    探测失败时返回 True（按扫描页处理）：宁可多提示，也不漏掉真要 OCR 的页。
    """
    try:
        return any(True for _ in pdf_page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)))
    except Exception:
        return True


def _try_docling(src: Path, env: ParseEnv) -> Any | None:
    """L0 接入点（M2）：返回 Docling 文档对象，不可用时返回 None 让流程落到 L1。

    这里刻意只做「能不能用」的探测：docling 属于 M2 的打包范畴（layout RT-DETR +
    TableFormer 约 270MB 模型），M1 的发布包里没有它，import 失败是**预期路径**。
    接入时把 DoclingDocument → IR 的单向映射写在这个函数里即可（04 章 §1），
    降级链、指标口径、取消检查点都不用动。
    """
    try:
        import docling  # noqa: F401  # 仅探测可用性
    except ImportError:
        return None
    env.info("检测到 Docling，但 L0 精解析接入将在 M2 提供，本次按 L1 解析")
    return None


def _ocr_page(pdf_page: Any, page_no: int, env: ParseEnv) -> list[dict[str, Any]] | None:
    """扫描页 OCR 接入点（M2）：返回与 ``_extract_page_items`` 同构的条目列表。

    M2 接 **RapidOCR（Apache-2.0）+ onnxruntime CPU EP**，默认 PP-OCRv5 mobile 中英模型
    （03 章 §3）：整页渲染成位图 → 识别 → 坐标回填 prov.bbox → 逐字符置信写
    ``env.ocr_confidences``（页均置信 < 0.6 记 E04，不判失败）。
    M1 返回 None：本模块绝不引入 OCR 依赖，也绝不联网下载模型。
    """
    if env.settings.ocr_mode == "off":
        return None
    return None


# ---------------------------------------------------------------- 版面启发式


def _body_size(plumber: Any, n_pages: int) -> float:
    """估算正文字号：取前几页所有字符字号的众数。

    用众数而不是均值 —— 标题、页码、脚注都是少数派，均值会被它们拽偏，
    而正文字符在任何一页都占绝对多数。
    """
    counter: Counter = Counter()
    for i in range(min(n_pages, _SIZE_SAMPLE_PAGES)):
        try:
            page = plumber.pages[i]
            for char in page.chars:
                size = char.get("size")
                if size:
                    counter[round(float(size), 1)] += 1
            page.close()
        except Exception:
            continue
    if not counter:
        return 10.0
    return float(counter.most_common(1)[0][0])


def _heading_level(size: float, body: float) -> int | None:
    if body <= 0:
        return None
    ratio = size / body
    for threshold, level in _HEADING_RATIOS:
        if ratio >= threshold:
            return level
    return None


def _join(prev: str, nxt: str) -> str:
    """跨行拼接：中日韩文本直接相连，西文补空格（PDF 的换行不是词边界）。"""
    if not prev:
        return nxt
    if prev.endswith("-") and not _CJK.search(prev[-2:] or ""):
        return prev[:-1] + nxt          # 西文连字符断词，去掉连字符
    if _CJK.search(prev[-1]) or _CJK.search(nxt[:1]):
        return prev + nxt
    return f"{prev} {nxt}"


def _in_bbox(obj: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (float(obj.get("x0", 0)) + float(obj.get("x1", 0))) / 2
    cy = (float(obj.get("top", 0)) + float(obj.get("bottom", 0))) / 2
    return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in boxes)


# ---------------------------------------------------------------- 单页抽取（L1）


def _extract_page_items(page: Any, body: float) -> list[dict[str, Any]]:
    """L1：把一页拆成有序条目（heading/para/table），**不碰 IRBuilder**。

    刻意返回纯数据而不是直接建节点：本函数可能被超时看门狗丢在后台线程里自生自灭
    （见 parsers.run_with_timeout），让它去写共享的 builder 会留下半截节点。
    """
    tables: list[dict[str, Any]] = []
    boxes: list[tuple[float, float, float, float]] = []
    try:
        for found in page.find_tables():
            rows = found.extract()
            if rows and any(any(cell for cell in row) for row in rows):
                x0, top, x1, bottom = found.bbox
                boxes.append((x0, top, x1, bottom))
                tables.append({"kind": "table", "top": float(top), "rows": rows})
    except Exception:
        tables, boxes = [], []

    # 表格区域内的字符不再参与正文行提取，否则表格内容会被重复成段
    target = page
    if boxes:
        try:
            target = page.filter(lambda obj: not _in_bbox(obj, boxes))
        except Exception:
            target = page

    try:
        lines = target.extract_text_lines(layout=False, strip=True, return_chars=True)
    except Exception:
        lines = []

    items: list[dict[str, Any]] = list(tables)
    buffer: dict[str, Any] | None = None
    prev_bottom: float | None = None
    for line in lines:
        text = (line.get("text") or "").strip()
        if not text:
            continue
        top = float(line.get("top", 0.0))
        bottom = float(line.get("bottom", top))
        height = max(1.0, bottom - top)
        chars = line.get("chars") or []
        sizes = [float(c["size"]) for c in chars if c.get("size")]
        size = sum(sizes) / len(sizes) if sizes else body
        level = _heading_level(size, body)

        if level is not None:
            if buffer is not None:
                items.append(buffer)
                buffer = None
            items.append({"kind": "heading", "level": level, "top": top, "text": text})
            prev_bottom = bottom
            continue

        gap = (top - prev_bottom) if prev_bottom is not None else 0.0
        if buffer is None or gap > height * _PARA_GAP_FACTOR:
            if buffer is not None:
                items.append(buffer)
            buffer = {"kind": "para", "top": top, "text": text}
        else:
            buffer["text"] = _join(buffer["text"], text)
        prev_bottom = bottom

    if buffer is not None:
        items.append(buffer)

    items.sort(key=lambda it: it.get("top", 0.0))
    return items


def _text_items(text: str) -> list[dict[str, Any]]:
    """L2：纯文本按空行切段（唯一还能用的结构线索）。"""
    items: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text):
        cleaned = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if cleaned:
            items.append({"kind": "para", "top": 0.0, "text": cleaned})
    return items


# ---------------------------------------------------------------- 节点产出


class _DocState:
    """跨页的章节栈：PDF 的标题层级是全文连续的，不能每页重来。"""

    def __init__(self) -> None:
        self.stack: list[tuple[int, IRNode]] = []

    def parent(self) -> IRNode | None:
        return self.stack[-1][1] if self.stack else None

    def push(self, level: int, node: IRNode) -> None:
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, node))


def _emit_items(items: list[dict[str, Any]], page: int, state: _DocState, env: ParseEnv) -> None:
    prov = [Prov(page=page)]
    for item in items:
        kind = item["kind"]
        if kind == "heading":
            node = env.builder.add(
                "section", parent=state.parent(), level=item["level"],
                content=NodeContent(text=item["text"]), prov=prov,
            )
            state.push(item["level"], node)
        elif kind == "para":
            env.builder.add(
                "paragraph", parent=state.parent(),
                content=NodeContent(text=item["text"]), prov=prov,
            )
        elif kind == "table":
            content = _table_content(item["rows"])
            if content.cells:
                env.builder.add(
                    "table", parent=state.parent(),
                    content=NodeContent(table=content), prov=prov,
                )


def _table_content(rows: list[list[Any]]) -> TableContent:
    """pdfplumber 表格 → IR cells。

    启发式表格没有合并单元格信息（rowspan/colspan 恒 1）；首行当表头是 PDF 表格的
    通用惯例，真正的表头判定要等 L0 TableFormer 的表头行标记。
    """
    cells: list[TableCell] = []
    for r, row in enumerate(rows):
        for c, raw in enumerate(row):
            text = (str(raw) if raw is not None else "").replace("\n", " ").strip()
            if not text:
                continue
            cells.append(TableCell(r=r, c=c, text=text, is_header=r == 0))
    return TableContent(cells=cells)


# ---------------------------------------------------------------- 页快照


def _render_preview(pdf_page: Any, out_path: Path) -> None:
    """渲染页快照到 preview/p{n}.png（失败由调用方吞掉）。"""
    width = float(pdf_page.get_width() or 0) or PREVIEW_MAX_WIDTH
    scale = min(PREVIEW_MAX_SCALE, PREVIEW_MAX_WIDTH / width)
    bitmap = pdf_page.render(scale=max(0.2, scale))
    try:
        image = bitmap.to_pil()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(out_path)
        finally:
            image.close()
    finally:
        with suppress(Exception):
            bitmap.close()


# ---------------------------------------------------------------- 主流程


def parse(src: Path, env: ParseEnv) -> None:
    src = Path(src)
    pdf = _open_pdfium(src)
    plumber: Any = None
    state = _DocState()
    raw_total = 0
    preview_failed = 0
    timeout_s = float(env.settings.page_timeout_s or 0)

    try:
        n_pages = len(pdf)
        if n_pages <= 0:
            raise DocFactoryError("E05", f"「{src.name}」没有任何页面")
        env.page_count = n_pages

        _try_docling(src, env)  # L0 接入点（M1 恒不可用，走 L1）

        try:
            plumber = pdfplumber.open(str(src))
        except Exception as exc:
            # pdfplumber 整体打不开：全篇退到 L2 纯文本，仍能产出内容
            env.info(f"pdfplumber 不可用，全篇按 L2 纯文本解析：{type(exc).__name__}: {exc}")
            plumber = None

        body = _body_size(plumber, n_pages) if plumber is not None else 10.0

        for index in range(n_pages):
            page_no = index + 1
            pdf_page = pdf[index]
            try:
                raw_text = _page_text(pdf_page)
                # 覆盖率分母与分子同口径去空白（见 parsers.visible_chars 的说明）：
                # 文本层每行都带 \r\n，按原始长度算会让完美解析也只得 0.9 分
                raw_total += visible_chars(raw_text)
                level, items = _parse_one_page(
                    plumber, pdf_page, page_no, raw_text, body, timeout_s, env
                )
                _emit_items(items, page_no, state, env)
                env.mark_level(page_no, level)

                try:
                    _render_preview(pdf_page, env.paths.doc_preview(env.doc_id) / f"p{page_no}.png")
                except Exception:
                    preview_failed += 1
            finally:
                with suppress(Exception):
                    pdf_page.close()

            env.progress(page_no, n_pages)
            env.check_cancel()

        env.raw_char_total = raw_total
        if preview_failed:
            env.info(f"{preview_failed} 页的预览快照渲染失败（不影响解析结果）")

        # 整篇扫描件：全无文本层 + 每页都因「无 OCR」降级。此时上层只会看到「IR 为空」，
        # 抛出的 E05 文案是「未能提取到有效内容 / 确认文件内容」—— 对着一份内容分明的
        # 扫描件说这句话毫无帮助。这里提前给出真正的原因与出路（03 章 §5.2「无任何产出 → failed」）。
        if raw_total == 0 and len(env.degraded_pages) >= n_pages:
            raise DocFactoryError(
                "E05",
                f"「{src.name}」共 {n_pages} 页全部是扫描图像，没有文本层可提取；"
                f"当前版本尚未内置 OCR，请先用带 OCR 的工具转成可搜索 PDF 后重新导入",
            )
    finally:
        for closable in (plumber, pdf):
            if closable is not None:
                with suppress(Exception):
                    closable.close()


def _parse_one_page(
    plumber: Any, pdf_page: Any, page_no: int, raw_text: str, body: float,
    timeout_s: float, env: ParseEnv,
) -> tuple[str, list[dict[str, Any]]]:
    """单页降级链，返回 (最终级别, 条目列表)。

    降级的判定与执行分开写：``env.degrade()`` 在 strict 策略下会抛错，
    放进 except 块里会变成「在处理异常时又抛异常」，栈信息与语义都会糊掉。
    """
    # 扫描页：文本层几乎为空 → 该走 OCR（M1 未接入，记 DGR-L2 + E04 占位）
    if len(raw_text.strip()) < SCANNED_CHAR_MIN:
        if not _page_has_image(pdf_page):
            # 空白页：既没有文本层也没有位图，没有任何内容可丢 —— 不是降级，不该告警
            return ("L1" if plumber is not None else "L2"), []
        items = _ocr_page(pdf_page, page_no, env)
        if items is not None:
            env.ocr_pages += 1
            return "L2", items
        env.degrade(page_no, "L2", "scanned_no_ocr", "该页无文本层，需 OCR")
        env.note_warning(
            "E04",
            f"第 {page_no} 页是扫描图像，当前版本尚未内置 OCR，该页文字未提取",
            page=page_no,
        )
        return "L2", _text_items(raw_text)

    if plumber is None:
        env.degrade(page_no, "L2", "engine_unavailable", "pdfplumber 不可用")
        return "L2", _text_items(raw_text)

    items = None
    reason, detail = "empty_result", "L1 未提取到内容"
    try:
        page = plumber.pages[page_no - 1]
        try:
            items = run_with_timeout(lambda: _extract_page_items(page, body), timeout_s)
        finally:
            with suppress(Exception):
                page.close()   # 释放该页字符缓存，保证逐页流式内存占用
    except PageTimeout:
        reason, detail = "timeout", f"超过 {timeout_s:.0f}s"
    except Exception as exc:
        reason, detail = "exception", f"{type(exc).__name__}: {exc}"

    if items:
        return "L1", items
    env.degrade(page_no, "L2", reason, detail)
    return "L2", _text_items(raw_text)


def parse_text_fallback(src: Path, env: ParseEnv) -> None:
    """整篇兜底：只用 pypdfium2 文本层（03 章 §5.1 的 L2）。"""
    pdf = _open_pdfium(Path(src))
    try:
        n_pages = len(pdf)
        env.page_count = n_pages
        raw_total = 0
        for index in range(n_pages):
            pdf_page = pdf[index]
            try:
                text = _page_text(pdf_page)
            finally:
                with suppress(Exception):
                    pdf_page.close()
            raw_total += visible_chars(text)     # 同 L1 路径的去空白口径
            for item in _text_items(text):
                env.builder.add(
                    "paragraph", content=NodeContent(text=item["text"]),
                    prov=[Prov(page=index + 1)],
                )
            env.mark_level(index + 1, "L2")
            env.progress(index + 1, max(1, n_pages))
            env.check_cancel()
        env.raw_char_total = raw_total
    finally:
        with suppress(Exception):
            pdf.close()
