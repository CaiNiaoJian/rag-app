"""解析层入口：格式路由 + 降级链编排 + 指标汇总（03 章 §1/§5，04 章 §2）。

对外只有一个函数 ``parse_document()``，输入源文件、输出 IRDocument；
所有格式差异被关在各 ``*_parser.py`` 里，上层（pipeline/切片/导出）永远只面对 IR。

编排上的三个约定：

1. **旧格式先归一化**：doc/ppt/xls 一律先经 LibreOffice 转成 OOXML（03 章 §4），
   转换轨迹写进 ``ir.doc.convert_chain``，之后走与新格式完全相同的代码路径 ——
   降级链、指标口径、单测覆盖都只需要维护一套。
2. **降级由 ParseEnv 统一出口**：解析器不直接碰 ctx/db，只调 ``env.degrade()``/
   ``env.progress()``。这样「strict 策略不降级直接报错」「降级同时写 SSE + task_events」
   这类横切规则改一处即可，也让解析器在没有 ctx 的单测里能裸跑。
3. **Office 只有两级**（03 章 §5.1）：结构化直解失败 → 整篇纯文本兜底（L2）；
   PDF 才有逐页 L0→L1→L2 的三级链，实现在 pdf_parser 内部。

延迟 import 各解析器子模块：某个第三方库缺失/异常时只影响对应格式，
不至于让整个解析层 import 失败（与 scheduler.RUNNERS 的思路一致）。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

from docfactory import ENGINE_VERSION
from docfactory.config import Paths, Settings
from docfactory.errors import DGR_L1, DGR_L2, DocFactoryError
from docfactory.ir import IRBuilder, IRDocMeta, IRDocument
from docfactory.taskspec import (
    EVENT_DEGRADE,
    EVENT_PROGRESS,
    EVENT_STAGE_CHANGE,
    TaskCancelled,
    TaskContext,
)

# 格式 → 解析器模块（延迟 import）。旧格式先归一化，不出现在本表里。
_PARSERS: dict[str, str] = {
    "docx": "docfactory.parsers.docx_parser",
    "pptx": "docfactory.parsers.pptx_parser",
    "xlsx": "docfactory.parsers.xlsx_parser",
    "pdf": "docfactory.parsers.pdf_parser",
}

# 旧格式 → 归一化目标格式（03 章 §1）
LEGACY_TARGET: dict[str, str] = {"doc": "docx", "ppt": "pptx", "xls": "xlsx"}

# OOXML 是 zip；OLE 复合文档头（老 Office 格式，也是加密 OOXML 的外壳）
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class PageTimeout(Exception):
    """单页解析超时（settings.page_timeout_s）；由降级链捕获转为 DGR 事件。"""


# ---------------------------------------------------------------- 解析环境


@dataclass
class ParseEnv:
    """解析器与流水线之间的唯一接触面：进度、降级、资源落盘、指标累加。

    解析器拿到 env 就够了，不需要知道 TaskContext/Database 的存在；
    ``ctx=None``（单测直接调 parse_document）时所有上报自动降为 no-op。
    """

    doc_id: str
    paths: Paths
    settings: Settings
    meta: IRDocMeta
    builder: IRBuilder
    ctx: TaskContext | None = None

    page_count: int = 0                              # 解析器回填（PDF 页数 / slide 数 / sheet 数）
    raw_char_total: int | None = None                # 原始文本层字符数（覆盖率分母，仅 PDF 有）
    page_levels: Counter = field(default_factory=Counter)   # 级别 → 页数
    degraded_pages: set[int] = field(default_factory=set)
    ocr_pages: int = 0
    ocr_confidences: list[float] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)  # E04/E05 变体（影响文档状态）
    _assets: dict[str, str] = field(default_factory=dict)         # 内容哈希 → 相对路径（去重）
    _asset_seq: int = 0

    # ---- 进度与取消 ----

    def stage(self, stage: str) -> None:
        if self.ctx is not None:
            self.ctx.progress(EVENT_STAGE_CHANGE, {"stage": stage})

    def progress(self, page: int, total: int, stage: str = "parse") -> None:
        if self.ctx is not None:
            self.ctx.progress(EVENT_PROGRESS, {"page": page, "total": total, "stage": stage})

    def check_cancel(self) -> None:
        """页/文件粒度检查点：解析器每处理完一页调一次。"""
        if self.ctx is not None and self.ctx.cancelled():
            raise TaskCancelled()

    # ---- 降级与告警 ----

    def mark_level(self, page: int, level: str) -> None:
        """登记某页最终使用的解析级别（用于 parse_level 主级别统计）。"""
        self.page_levels[level] += 1

    def degrade(self, page: int, level: str, reason: str, detail: str = "") -> None:
        """记录一次逐页降级：SSE degrade 事件 + warning 级 task_events（03 章 §5.1）。

        ``degrade_policy == "strict"`` 时不降级 —— 用户明确要求「宁可失败也不要不完整结果」，
        这里直接抛 E06 把整篇解析中止，比产出一份悄悄缺内容的文档诚实。
        """
        if self.settings.degrade_policy == "strict":
            raise DocFactoryError(
                "E06",
                f"第 {page} 页解析失败（{reason}{('：' + detail) if detail else ''}），"
                f"当前降级策略为「严格」，已中止解析",
                page=page,
            )
        self.degraded_pages.add(page)
        if self.ctx is None:
            return
        self.ctx.progress(EVENT_DEGRADE, {"page": page, "level": level, "reason": reason})
        self.ctx.db.log_event(
            level="warning",
            task_id=self.ctx.task_id,
            doc_id=self.doc_id,
            code=DGR_L1 if level == "L1" else DGR_L2,
            stage="parse",
            page=page,
            message=f"第 {page} 页降级到 {level}（{reason}）",
            detail={"reason": reason, "detail": detail},
        )

    def note_warning(self, code: str, message: str, *, page: int | None = None,
                     detail: dict[str, Any] | None = None) -> None:
        """记录不致命但需让用户看见的问题（E04 低质量扫描 / E05 超大表截断等）。

        这些告警会把文档最终状态压到 ``warning``（03 章 §5.2），但不中断解析。
        """
        self.warnings.append({"code": code, "message": message, "page": page})
        if self.ctx is not None:
            self.ctx.db.log_event(
                level="warning",
                task_id=self.ctx.task_id,
                doc_id=self.doc_id,
                code=code,
                stage="parse",
                page=page,
                message=message,
                detail=detail,
            )

    def info(self, message: str, *, code: str | None = None,
             detail: dict[str, Any] | None = None) -> None:
        if self.ctx is not None:
            self.ctx.db.log_event(
                level="info", task_id=self.ctx.task_id, doc_id=self.doc_id,
                code=code, stage="parse", message=message, detail=detail,
            )

    # ---- 资源落盘 ----

    def save_asset(self, data: bytes, ext: str) -> str:
        """图片等二进制资源落 ``workspace/{docId}/assets/``，返回 IR 用的相对路径。

        按内容哈希去重：Office 文档里同一张图（页眉 logo、水印）常被引用几十次，
        去重后 assets 目录不会爆，IR 里多个 figure 指向同一文件也完全正常。
        """
        digest = hashlib.sha256(data).hexdigest()
        hit = self._assets.get(digest)
        if hit:
            return hit
        ext = (ext or ".bin").lower()
        if not ext.startswith("."):
            ext = "." + ext
        self._asset_seq += 1
        assets_dir = self.paths.doc_assets(self.doc_id)
        assets_dir.mkdir(parents=True, exist_ok=True)
        name = f"img{self._asset_seq:03d}{ext}"
        (assets_dir / name).write_bytes(data)
        ref = f"assets/{name}"
        self._assets[digest] = ref
        return ref


# ---------------------------------------------------------------- 工具


def run_with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """在 daemon 线程里执行单页解析并施加超时（settings.page_timeout_s）。

    为什么是裸线程而不是 ThreadPoolExecutor：Python 杀不掉线程，超时后只能**弃用**它；
    ThreadPoolExecutor 的工作线程会在解释器退出时被 join，一个卡死的 pdfplumber 页
    足以把 10s 的优雅停机拖成永久挂起。daemon 线程则随进程退出，代价只是短暂的内存占用。
    ``timeout_s <= 0`` 视为不限时，直接同步执行（单测里最省事）。
    """
    if not timeout_s or timeout_s <= 0:
        return fn()

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样搬到主线程重抛
            box["error"] = exc

    worker = threading.Thread(target=_target, name="df-page", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise PageTimeout(f"单页处理超过 {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def assert_ooxml_readable(src: Path) -> None:
    """OOXML（docx/pptx/xlsx）落地前的头部体检，把两类高频问题分辨清楚。

    加密的 OOXML 在磁盘上是 OLE 复合文档（内含加密流），zip 库只会报「不是 zip」，
    直接透传给用户就是一句莫名其妙的技术错误；这里提前识别成 E02「文件受密码保护」。
    """
    try:
        with open(src, "rb") as f:
            head = f.read(8)
    except PermissionError as exc:
        raise DocFactoryError("E01", f"文件被占用或无读取权限：{src.name}") from exc
    except OSError as exc:
        raise DocFactoryError("E01", f"读取文件失败：{src.name}（{exc.strerror or exc}）") from exc
    if head.startswith(_ZIP_MAGIC):
        return
    if head.startswith(_OLE_MAGIC):
        raise DocFactoryError(
            "E02", f"「{src.name}」无法作为 OOXML 打开：文件可能受密码保护，或是被改了扩展名的旧格式"
        )
    raise DocFactoryError("E01", f"「{src.name}」不是有效的 Office 文档（文件头异常）")


_WHITESPACE = re.compile(r"\s+")


def visible_chars(text: str) -> int:
    """去掉全部空白后的字符数 —— text_coverage 分子分母的**统一口径**。

    为什么不数原始长度：PDF 文本层里每行都带 ``\\r\\n``，而解析产物按语义重排后
    这些换行不会原样保留。若分母含空白、分子被 strip，一份**内容零丢失**的 PDF
    也只能得到 0.9 左右的分数——覆盖率是产品的卖点指标（仪表盘「平均文本覆盖率」）
    且门禁为 ≥0.97，口径歪一点就会长期误报并向用户展示错误的低分。
    这个指标衡量的是「文字内容有没有丢」，不是「空白排版有没有保留」，故两边都去空白。
    """
    return len(_WHITESPACE.sub("", text))


def ir_char_total(ir: IRDocument) -> int:
    """IR 承载的有效字符总数（text_coverage 的分子，03 章 §5.2）。"""
    total = 0
    for node in ir.nodes:
        c = node.content
        for text in (c.text, c.title, c.notes, c.caption, c.ocr_text, c.name):
            if text:
                total += visible_chars(text)
        if c.table is not None:
            total += sum(visible_chars(cell.text) for cell in c.table.cells)
    return total


def _load(module_path: str) -> ModuleType:
    try:
        return import_module(module_path)
    except ImportError as exc:  # 依赖缺失：报成「格式暂不支持」比抛 ImportError 有用
        raise DocFactoryError("E03", f"该格式的解析组件不可用：{exc}") from exc


# ---------------------------------------------------------------- 主入口


def parse_document(
    *,
    src: Path,
    doc_id: str,
    fmt: str,
    paths: Paths,
    settings: Settings,
    ctx: TaskContext | None = None,
) -> IRDocument:
    """把任意受支持格式解析为 IRDocument（不落盘，落盘由 pipeline 负责）。

    ``ctx`` 为 None 时不上报进度、不写 task_events，供单测直接调用。
    """
    src = Path(src)
    fmt = (fmt or src.suffix).lower().lstrip(".")
    if not src.is_file():
        raise DocFactoryError("E01", f"源文件不存在：{src}")

    meta = IRDocMeta(
        id=doc_id,
        source_file=src.name,
        source_format=fmt,
        engine_version=ENGINE_VERSION,
    )
    env = ParseEnv(
        doc_id=doc_id, paths=paths, settings=settings, meta=meta,
        builder=IRBuilder(meta), ctx=ctx,
    )

    work_dir: Path | None = None
    try:
        # ---- ① 旧格式归一化（03 章 §4）----
        parse_src = src
        target = LEGACY_TARGET.get(fmt)
        if target:
            env.stage("convert")
            env.check_cancel()
            from docfactory.parsers.office_convert import convert_to_ooxml

            work_dir = paths.staging / f"convert-{uuid.uuid4().hex}"
            parse_src, chain = convert_to_ooxml(src, out_dir=work_dir, target_ext=target)
            meta.convert_chain.append(chain)
            env.info(f"已归一化：{chain}", detail={"target": target})
            fmt = target

        module_path = _PARSERS.get(fmt)
        if module_path is None:
            raise DocFactoryError("E03", f"暂不支持的格式：.{fmt}")
        module = _load(module_path)

        # ---- ② 结构化解析（失败则整篇纯文本兜底，Office 两级降级）----
        env.stage("parse")
        env.check_cancel()
        try:
            module.parse(parse_src, env)
        except (DocFactoryError, TaskCancelled):
            raise
        except Exception as exc:
            # 整篇结构化解析崩了：重建 builder（节点重新编号），退到纯文本
            env.degrade(1, "L2", "exception", f"{type(exc).__name__}: {exc}")
            env.builder = IRBuilder(meta)
            env.page_levels.clear()
            module.parse_text_fallback(parse_src, env)

        ir = env.builder.build()
        if not ir.nodes:
            raise DocFactoryError("E05", f"未能从「{src.name}」中提取到任何内容")

        _fill_metrics(ir, env)
        return ir
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def _fill_metrics(ir: IRDocument, env: ParseEnv) -> None:
    """汇总完整性指标（03 章 §5.2）写回 ir.doc。"""
    m = ir.doc.metrics
    m.degraded_pages = len(env.degraded_pages)

    # 主级别 = 占比最高的级别；解析器没登记过级别时按 L0（规则直解）算
    ir.doc.parse_level = env.page_levels.most_common(1)[0][0] if env.page_levels else "L0"  # type: ignore[assignment]

    # 覆盖率：有原始文本层分母（PDF）时按定义算；规则直解（Office）无损，恒 1.0
    if env.raw_char_total is not None:
        denom = env.raw_char_total
        m.text_coverage = min(1.0, ir_char_total(ir) / denom) if denom > 0 else 1.0
    else:
        m.text_coverage = 1.0

    # 表格置信：M1 全部走规则路径（IR 契约规定规则路径恒 1.0）；
    # 接入 L0 TableFormer 后由模型 cell 置信均值覆盖。无表格则留空，UI 显示「—」。
    has_table = any(n.type in ("table", "sheet_region") for n in ir.nodes)
    m.table_confidence = 1.0 if has_table else None

    m.ocr_confidence = (
        sum(env.ocr_confidences) / len(env.ocr_confidences) if env.ocr_confidences else None
    )


__all__ = [
    "LEGACY_TARGET",
    "PageTimeout",
    "ParseEnv",
    "assert_ooxml_readable",
    "ir_char_total",
    "parse_document",
    "run_with_timeout",
]
