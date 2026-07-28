"""golden corpus 回归打分（08 章 §3.1，里程碑合入门禁）。

批量解析 `corpus/samples/` 下的全部样本，与 `corpus/expected/*.expected.json` 的标注
比对打分，输出趋势报告。**任一样本不达标即退出码 1**。

设计取舍：
- 标注写的是**下限与不变量**（节点数下限、必现标题、必现文本），不是精确快照。
  精确快照会让每次解析器改进都要重刷标注，最终没人维护。
- 没有标注的样本仍然会被解析并计入「解析成功率」，只是不做结构断言 ——
  这样往 samples/ 里丢一批真实文档就能立刻得到崩溃率数据，标注可以后补。
- 表格相似度用**网格覆盖率**而非完整 TEDS：TEDS 需要树编辑距离实现，M2 接入正式指标前
  网格覆盖率已足以拦住「表格整个丢了」「行列错位」这类真正的回归。

用法见 corpus/README.md。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = CORPUS_DIR / "samples"
EXPECTED_DIR = CORPUS_DIR / "expected"

# 引擎源码目录（脚本可能被直接 python 调用，不依赖已安装的包）
ENGINE_SRC = CORPUS_DIR.parent / "engine" / "src"
if ENGINE_SRC.is_dir() and str(ENGINE_SRC) not in sys.path:
    sys.path.insert(0, str(ENGINE_SRC))

# corpus 跑的是解析质量，不该被离线闸干扰（解析本身不联网，但闸会 patch 全局 socket）
import os  # noqa: E402

os.environ.setdefault("DOCFACTORY_DISABLE_OFFLINE_GUARD", "1")


@dataclass
class SampleResult:
    name: str
    fmt: str
    ok: bool = True
    parsed: bool = False
    seconds: float = 0.0
    error_code: str | None = None
    error: str | None = None
    text_coverage: float | None = None
    parse_level: str | None = None
    node_counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    annotated: bool = False


# ---------------------------------------------------------------- 解析


def _parse_one(src: Path, workdir: Path) -> tuple[Any, float]:
    """解析单个样本，返回 (IRDocument, 耗时秒)。异常原样上抛给调用方分类。"""
    from docfactory.config import Paths, Settings
    from docfactory.parsers import parse_document

    paths = Paths(root=workdir)
    paths.ensure()
    doc_id = src.stem.replace(".", "_")
    started = time.perf_counter()
    ir = parse_document(
        src=src, doc_id=doc_id, fmt=src.suffix.lstrip(".").lower(),
        paths=paths, settings=Settings(), ctx=None,
    )
    return ir, time.perf_counter() - started


def _ir_text(ir: Any) -> str:
    """IR 全文（含表格单元格），供 text_contains 断言。"""
    from docfactory.ir import table_to_grid

    parts: list[str] = []
    for node in ir.nodes:
        content = node.content
        for value in (content.text, content.title, content.notes,
                      content.caption, content.ocr_text, content.name):
            if value:
                parts.append(str(value))
        if content.table is not None:
            for row in table_to_grid(content.table):
                parts.extend(cell for cell in row if cell)
    return "\n".join(parts)


def _node_counts(ir: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in ir.nodes:
        counts[node.type] = counts.get(node.type, 0) + 1
    return counts


# ---------------------------------------------------------------- 断言


def _check_tables(ir: Any, specs: list[dict[str, Any]], result: SampleResult) -> None:
    from docfactory.ir import table_has_merged_cells, table_to_grid

    tables = [n for n in ir.nodes if n.type in ("table", "sheet_region") and n.content.table]
    if len(tables) < len(specs):
        result.failures.append(f"表格数不足：期望 ≥{len(specs)}，实际 {len(tables)}")
        return

    for i, spec in enumerate(specs):
        table = tables[i].content.table
        grid = table_to_grid(table)
        rows, cols = len(grid), (len(grid[0]) if grid else 0)
        if "rows" in spec and rows < spec["rows"]:
            result.failures.append(f"表格[{i}] 行数不足：期望 ≥{spec['rows']}，实际 {rows}")
        if "cols" in spec and cols < spec["cols"]:
            result.failures.append(f"表格[{i}] 列数不足：期望 ≥{spec['cols']}，实际 {cols}")
        if "has_merged" in spec:
            actual = table_has_merged_cells(table)
            if actual != bool(spec["has_merged"]):
                result.failures.append(
                    f"表格[{i}] 合并单元格判定不符：期望 {spec['has_merged']}，实际 {actual}"
                )
        flat = "\n".join(cell for row in grid for cell in row)
        for needle in spec.get("cells_contain", []):
            if needle not in flat:
                result.failures.append(f"表格[{i}] 缺少单元格内容：{needle!r}")


def _check_expectations(ir: Any, spec: dict[str, Any], result: SampleResult) -> None:
    metrics = ir.doc.metrics
    result.text_coverage = metrics.text_coverage
    result.parse_level = ir.doc.parse_level
    result.node_counts = _node_counts(ir)

    min_cov = spec.get("min_text_coverage")
    if min_cov is not None:
        if metrics.text_coverage is None:
            result.warnings.append("未产出 text_coverage 指标（无法核对覆盖率门禁）")
        elif metrics.text_coverage < min_cov:
            result.failures.append(
                f"文本覆盖率不达标：期望 ≥{min_cov}，实际 {metrics.text_coverage:.3f}"
            )

    max_s = spec.get("max_seconds")
    if max_s is not None and result.seconds > max_s:
        # 机器性能差异大：耗时超限记 warning 看趋势，不作为门禁
        result.warnings.append(f"耗时 {result.seconds:.1f}s 超过期望 {max_s}s")

    expect = spec.get("expect") or {}
    for node_type, minimum in (expect.get("node_counts") or {}).items():
        actual = result.node_counts.get(node_type, 0)
        if actual < minimum:
            result.failures.append(f"{node_type} 节点数不足：期望 ≥{minimum}，实际 {actual}")

    if expect.get("headings"):
        heading_texts = [
            (n.content.text or n.content.title or "")
            for n in ir.nodes if n.type in ("section", "slide")
        ]
        blob = "\n".join(heading_texts)
        for heading in expect["headings"]:
            if heading not in blob:
                result.failures.append(f"缺少标题：{heading!r}")

    if expect.get("text_contains"):
        full = _ir_text(ir)
        for needle in expect["text_contains"]:
            if needle not in full:
                result.failures.append(f"正文缺少内容：{needle!r}")

    if expect.get("tables"):
        _check_tables(ir, expect["tables"], result)


# ---------------------------------------------------------------- 单样本流程


def run_sample(src: Path, spec: dict[str, Any] | None, workdir: Path) -> SampleResult:
    from docfactory.errors import DocFactoryError

    result = SampleResult(name=src.name, fmt=src.suffix.lstrip(".").lower())
    result.annotated = spec is not None
    spec = spec or {}
    expected_code = spec.get("expect_error_code")
    must_not_fail = spec.get("must_not_fail", expected_code is None)

    try:
        ir, seconds = _parse_one(src, workdir)
        result.parsed = True
        result.seconds = seconds
    except DocFactoryError as exc:
        result.error_code = exc.code
        result.error = exc.detail or str(exc)
        if expected_code:
            # 优雅报错也是通过：损坏/密码样本要的就是「明确分类报错，不崩溃」
            if exc.code != expected_code:
                result.failures.append(f"错误码不符：期望 {expected_code}，实际 {exc.code}")
        elif must_not_fail:
            result.failures.append(f"解析失败 [{exc.code}]：{result.error}")
        result.ok = not result.failures
        return result
    except Exception as exc:
        # 未分类异常永远是失败：01 章「解析成功率」的定义是「有产出或明确分类报错，不崩溃」
        result.error = f"{type(exc).__name__}: {exc}"
        result.failures.append(f"未分类异常（应映射为 E01~E07）：{result.error}")
        result.warnings.append(traceback.format_exc(limit=3))
        result.ok = False
        return result

    if expected_code:
        result.failures.append(f"期望以 {expected_code} 报错，实际解析成功")

    _check_expectations(ir, spec, result)
    result.ok = not result.failures
    return result


# ---------------------------------------------------------------- 报告


def _summary(results: list[SampleResult]) -> dict[str, Any]:
    total = len(results)
    graceful = sum(1 for r in results if r.parsed or r.error_code)
    coverages = [r.text_coverage for r in results if r.text_coverage is not None]
    durations = [r.seconds for r in results if r.parsed]
    levels: dict[str, int] = {}
    for r in results:
        if r.parse_level:
            levels[r.parse_level] = levels.get(r.parse_level, 0) + 1
    return {
        "样本数": total,
        "通过": sum(1 for r in results if r.ok),
        "失败": sum(1 for r in results if not r.ok),
        "解析成功率": f"{graceful / total:.1%}" if total else "-",
        "平均文本覆盖率": f"{sum(coverages) / len(coverages):.3f}" if coverages else "-",
        "平均耗时": f"{sum(durations) / len(durations):.2f}s" if durations else "-",
        "解析级别分布": levels or "-",
    }


def render_report(results: list[SampleResult], summary: dict[str, Any]) -> str:
    lines = ["# golden corpus 回归报告", "", "## 汇总", ""]
    for k, v in summary.items():
        lines.append(f"- **{k}**：{v}")
    lines += ["", "## 明细", "",
              "| 样本 | 结果 | 级别 | 覆盖率 | 耗时 | 节点 | 备注 |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        status = "✅ 通过" if r.ok else "❌ 失败"
        if not r.annotated:
            status += "（未标注）"
        cov = f"{r.text_coverage:.3f}" if r.text_coverage is not None else "-"
        nodes = sum(r.node_counts.values()) or "-"
        note = "；".join(r.failures) or (r.error_code or "")
        lines.append(
            f"| {r.name} | {status} | {r.parse_level or '-'} | {cov} | "
            f"{r.seconds:.2f}s | {nodes} | {note} |"
        )

    problems = [r for r in results if r.failures or r.warnings]
    if problems:
        lines += ["", "## 问题详情", ""]
        for r in problems:
            lines.append(f"### {r.name}")
            for f in r.failures:
                lines.append(f"- ❌ {f}")
            for w in r.warnings:
                lines.append(f"- ⚠️ {w}")
            lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 入口


def load_spec(name: str) -> dict[str, Any] | None:
    path = EXPECTED_DIR / f"{Path(name).stem}.expected.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"标注文件无法解析 {path.name}：{exc}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="golden corpus 回归打分")
    parser.add_argument("--generate", action="store_true", help="先生成程序化 fixtures")
    parser.add_argument("--only", help="只跑文件名含该子串的样本")
    parser.add_argument("--report", help="报告输出路径（默认 corpus/report.md）")
    parser.add_argument("--dump", action="store_true", help="打印每个样本解析出的结构（写标注时用）")
    parser.add_argument("--workdir", help="解析工作目录（默认临时目录）")
    args = parser.parse_args(argv)

    if args.generate:
        sys.path.insert(0, str(CORPUS_DIR / "fixtures"))
        from make_fixtures import generate

        generate(SAMPLES_DIR)

    if not SAMPLES_DIR.is_dir():
        print(f"样本目录不存在：{SAMPLES_DIR}（先跑 --generate）", file=sys.stderr)
        return 2

    samples = sorted(p for p in SAMPLES_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    if args.only:
        samples = [p for p in samples if args.only in p.name]
    if not samples:
        print("没有样本可跑（corpus/samples/ 为空？先跑 --generate）", file=sys.stderr)
        return 2

    import tempfile

    with tempfile.TemporaryDirectory(prefix="corpus-") as tmp:
        workdir = Path(args.workdir) if args.workdir else Path(tmp)
        results = []
        for src in samples:
            spec = load_spec(src.name)
            result = run_sample(src, spec, workdir)
            results.append(result)
            mark = "✅" if result.ok else "❌"
            print(f"{mark} {src.name:<28} {result.seconds:>6.2f}s  {result.parse_level or '-':<3} "
                  f"{'; '.join(result.failures) or result.error_code or ''}")
            if args.dump and result.node_counts:
                print(f"    节点分布：{result.node_counts}")

    summary = _summary(results)
    print("\n" + "  ".join(f"{k}={v}" for k, v in summary.items()))

    report_path = Path(args.report) if args.report else CORPUS_DIR / "report.md"
    report_path.write_text(render_report(results, summary), encoding="utf-8")
    print(f"报告已写出：{report_path}")

    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
