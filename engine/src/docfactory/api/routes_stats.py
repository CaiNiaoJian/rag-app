"""仪表盘聚合（GET /stats/dashboard，07 章 §3 指标表）。

取数策略遵循 02 章 §4 的分工：
- **趋势类走 metrics_daily 预聚合**（近 30 天）：日粒度累计值任务完成时增量写入，查询恒定成本。
- **明细类实时 GROUP BY**（documents / chunks / task_events）：数据量 < 10 万行时无压力，
  且永远与真实数据一致——预聚合最怕的就是「漏加一次就永远对不上」。

响应形状对齐前端 ``DashboardStats``：``{cards, fmt_dist, status_dist, level_dist,
chunk_hist, chunk_per_doc, fail_top, duration, trend}``。分布类**一律是「对象数组」**，
且每项都带 ``label``/``count`` 两个通用键——渲染进程用同一个归一化函数喂所有图表，
嵌套对象或换个计数键名都会让那张图静默空白（比报错更难查）。
单值一律进 cards（Record<string, number|null>，缺数据时给 null 让 UI 显示占位而不是 0）。

耗时不在 SQL 里算：tasks 的时间戳是带时区偏移的 ISO 串，交给 Python 的
``datetime.fromisoformat`` 解析比依赖 SQLite 的日期函数行为更可控。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request

from docfactory.errors import ERRORS

router = APIRouter()

# 趋势窗口（07 章 §3：近 30 天）与「近期」卡片窗口（饼图的「近 7 日」）
_TREND_DAYS = 30
_RECENT_DAYS = 7

# 文档状态与解析级别的固定枚举：即使当前一条数据都没有也要出全，
# 否则环形图/堆叠条的图例会随数据抖动（UI 稳定性优先于响应体积）。
_DOC_STATUS = ("imported", "parsing", "ok", "warning", "failed")
_PARSE_LEVELS = ("L0", "L1", "L2")

# 图例文案在服务端给：图表组件是通用的，它只认 label 字段，不认业务枚举
_DOC_STATUS_LABEL = {
    "imported": "已导入", "parsing": "解析中", "ok": "成功", "warning": "警告", "failed": "失败",
}
_LEVEL_LABEL = {"L0": "L0 深度解析", "L1": "L1 基础解析", "L2": "L2 兜底提取", "unknown": "未分级"}
_TASK_TYPE_LABEL = {
    "parse": "解析", "export": "导出", "rechunk": "重切", "module_install": "模组安装",
    "qa_generate": "问答生成", "dataset_build": "数据集构建",
}

# 切片长度直方图分桶（token 口径，与 ChunkSettings 的 512/1024 目标值对齐）
_TOKEN_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0-128", 0, 128),
    ("128-256", 128, 256),
    ("256-512", 256, 512),
    ("512-1024", 512, 1024),
    ("1024+", 1024, None),
)

_TOP_DOCS = 20          # 「每文档切片数」只给切片最多的前 N 个（图表也画不下更多）
_FAIL_TOP = 5           # 失败原因 TOP5（07 章 §3）
_DURATION_SAMPLE = 2000  # 耗时统计的采样条数：够稳且不会把整表拉进内存


@router.get("/stats/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    today = date.today()
    trend_from = (today - timedelta(days=_TREND_DAYS - 1)).isoformat()
    recent_from = (today - timedelta(days=_RECENT_DAYS - 1)).isoformat()

    with db.connect() as conn:
        docs = _document_stats(conn, recent_from)
        chunks = _chunk_stats(conn)
        fail_top = _fail_top(conn)
        duration = _duration_stats(conn)
        trend, totals = _trend(conn, trend_from, today)

    pages_total = docs["pages_total"]
    ocr_pages = totals["ocr_pages"]
    parsed_total = sum(docs["status_map"].get(s, 0) for s in ("ok", "warning", "failed"))

    cards: dict[str, float | int | None] = {
        "docs_total": docs["total"],
        "docs_recent": docs["recent_total"],
        "pages_total": pages_total,
        "degraded_pages": docs["degraded_pages"],
        # 成功率把 warning 算作成功（E04 是警告不是失败，07 章 §4）
        "success_rate": _ratio(docs["status_map"].get("ok", 0) + docs["status_map"].get("warning", 0), parsed_total),
        "avg_text_coverage": _round(docs["avg_text_coverage"]),
        "avg_table_confidence": _round(docs["avg_table_confidence"]),
        "avg_ocr_confidence": _round(docs["avg_ocr_confidence"]),
        "chunks_total": chunks["total"],
        "avg_chunks_per_doc": _round(_ratio(chunks["total"], chunks["doc_count"]), 2),
        "avg_chunk_tokens": _round(chunks["avg_tokens"], 1),
        "ocr_pages": ocr_pages,
        "ocr_ratio": _ratio(ocr_pages, pages_total),
        "tasks_queued": docs["tasks_queued"],
        "tasks_running": docs["tasks_running"],
        "tasks_failed": docs["tasks_failed"],
        "avg_duration_ms": duration["overall"]["avg_ms"],
        "p50_duration_ms": duration["overall"]["p50_ms"],
        # 下面三个是同值别名：UI 的取数器按候选键名依次找，找不到就退化成占位。
        # 与其让两边为了命名来回改，不如在服务端把常见别名一并给出（成本三个整数）。
        "documents": docs["total"],
        "chunk_total": chunks["total"],
        "imported_7d": docs["recent_total"],
    }

    return {
        "cards": cards,
        "fmt_dist": docs["fmt_dist"],
        "status_dist": [
            {"status": s, "label": _DOC_STATUS_LABEL[s], "count": docs["status_map"].get(s, 0)}
            for s in _DOC_STATUS
        ],
        "level_dist": docs["level_dist"],
        "chunk_hist": chunks["hist"],
        "chunk_per_doc": chunks["per_doc"],
        "fail_top": fail_top,
        "duration": duration["by_type"],
        "trend": trend,
    }


# ---------------------------------------------------------------- 明细聚合


def _document_stats(conn: sqlite3.Connection, recent_from: str) -> dict[str, Any]:
    """documents / tasks 的实时聚合（类型分布、状态分布、分级占比、完整性均值）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(page_cnt), 0) AS pages, "
        "COALESCE(SUM(degraded_pages), 0) AS degraded, "
        "AVG(text_coverage) AS cov, AVG(table_confidence) AS tab, AVG(ocr_confidence) AS ocr "
        "FROM documents"
    ).fetchone()

    # created_at 是带时区偏移的 ISO 串，同一台机器上前缀比较等价于按日期比较
    recent_total = conn.execute(
        "SELECT COUNT(*) AS c FROM documents WHERE created_at >= ?", (recent_from,)
    ).fetchone()["c"]

    # 累计与近 7 日合成一张表：同一个饼图切换「累计/近 7 日」时不必再请求一次
    recent_by_fmt = {
        r["fmt"]: r["c"]
        for r in conn.execute(
            "SELECT fmt, COUNT(*) AS c FROM documents WHERE created_at >= ? GROUP BY fmt",
            (recent_from,),
        ).fetchall()
    }
    fmt_dist = [
        {
            "fmt": r["fmt"],
            "label": (r["fmt"] or "未知").upper(),
            "count": r["c"],
            "recent": recent_by_fmt.get(r["fmt"], 0),
        }
        for r in conn.execute(
            "SELECT fmt, COUNT(*) AS c FROM documents GROUP BY fmt ORDER BY c DESC"
        ).fetchall()
    ]

    status_map = {
        r["status"]: r["c"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM documents GROUP BY status"
        ).fetchall()
    }

    level_rows = {
        (r["parse_level"] or "unknown"): (r["docs"], r["pages"])
        for r in conn.execute(
            "SELECT parse_level, COUNT(*) AS docs, COALESCE(SUM(page_cnt), 0) AS pages "
            "FROM documents WHERE status IN ('ok', 'warning', 'failed') GROUP BY parse_level"
        ).fetchall()
    }
    leveled_pages = sum(pages for _, pages in level_rows.values())
    # 07 章 §3 要的是「页占比」；但 xlsx/pptx 这类不一定写得出 page_cnt，
    # 全是 0 时退回文档数——堆叠条画出来总比空白强，口径差异由 pages/docs 两列自证。
    levels = [*_PARSE_LEVELS, *(["unknown"] if "unknown" in level_rows else [])]
    level_dist = [
        {
            "level": lv,
            "label": _LEVEL_LABEL.get(lv, lv),
            "docs": level_rows.get(lv, (0, 0))[0],
            "pages": level_rows.get(lv, (0, 0))[1],
            "count": level_rows.get(lv, (0, 0))[1] if leveled_pages else level_rows.get(lv, (0, 0))[0],
            "page_ratio": _ratio(level_rows.get(lv, (0, 0))[1], leveled_pages),
        }
        for lv in levels
    ]

    task_status = {
        r["status"]: r["c"]
        for r in conn.execute("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status").fetchall()
    }

    return {
        "total": row["total"],
        "recent_total": recent_total,
        "pages_total": row["pages"],
        "degraded_pages": row["degraded"],
        "avg_text_coverage": row["cov"],
        "avg_table_confidence": row["tab"],
        "avg_ocr_confidence": row["ocr"],
        "fmt_dist": fmt_dist,
        "status_map": status_map,
        "level_dist": level_dist,
        "tasks_queued": task_status.get("queued", 0),
        "tasks_running": task_status.get("running", 0),
        "tasks_failed": task_status.get("failed", 0),
    }


def _chunk_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """切片规模：总量、覆盖文档数、长度直方图、每文档切片数 TOP N。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT doc_id) AS docs, AVG(token_count) AS avg_tokens FROM chunks"
    ).fetchone()

    buckets: list[dict[str, Any]] = []
    for label, lo, hi in _TOKEN_BUCKETS:
        if hi is None:
            cond, args = "token_count >= ?", (lo,)
        else:
            cond, args = "token_count >= ? AND token_count < ?", (lo, hi)
        count = conn.execute(f"SELECT COUNT(*) AS c FROM chunks WHERE {cond}", args).fetchone()["c"]
        buckets.append({"bucket": label, "label": label, "min": lo, "max": hi, "count": count})

    per_doc = [
        {"doc_id": r["doc_id"], "name": r["name"], "label": r["name"] or r["doc_id"],
         "chunks": r["c"], "count": r["c"], "tokens": r["tokens"] or 0}
        for r in conn.execute(
            "SELECT c.doc_id AS doc_id, d.name AS name, COUNT(*) AS c, SUM(c.token_count) AS tokens "
            "FROM chunks c LEFT JOIN documents d ON d.id = c.doc_id "
            "GROUP BY c.doc_id ORDER BY c DESC LIMIT ?",
            (_TOP_DOCS,),
        ).fetchall()
    ]

    return {
        "total": row["total"],
        "doc_count": row["docs"],
        "avg_tokens": row["avg_tokens"],
        "hist": buckets,        # 长度直方图（token 口径）
        "per_doc": per_doc,     # 每文档切片数 TOP N
    }


def _fail_top(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """失败原因 TOP5：按错误码聚合并附上人话文案，UI 点击可跳日志（07 章 §3）。

    另给 ``label``（错误码 + 人话文案）：条形图上光看 E01 谁也想不起是什么。
    """
    rows = conn.execute(
        "SELECT COALESCE(code, 'E06') AS code, COUNT(*) AS c FROM task_events "
        "WHERE level='error' GROUP BY COALESCE(code, 'E06') ORDER BY c DESC LIMIT ?",
        (_FAIL_TOP,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        info = ERRORS.get(r["code"])
        message = info.user_message if info else "发生未知错误"
        out.append({
            "code": r["code"],
            "label": f"{r['code']} {message}",
            "count": r["c"],
            "user_message": message,
            "suggestion": info.suggestion if info else "重试；反复出现请导出诊断包反馈",
        })
    return out


def _duration_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """处理耗时：整体与按任务类型的平均/中位（只统计正常完成的任务）。

    ``by_type`` 每项的 ``value`` 就是平均耗时（图表主指标），样本数叫 ``samples``
    而不是 ``count``——通用条形图默认把 ``count`` 当主指标，样本数画成柱子会误导人。
    整体值不单列一层，直接进 cards 的 avg/p50_duration_ms，少一处重复口径。
    """
    rows = conn.execute(
        "SELECT type, started_at, ended_at FROM tasks "
        "WHERE status='done' AND started_at IS NOT NULL AND ended_at IS NOT NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (_DURATION_SAMPLE,),
    ).fetchall()

    by_type: dict[str, list[float]] = {}
    for r in rows:
        ms = _elapsed_ms(r["started_at"], r["ended_at"])
        if ms is None:
            continue
        by_type.setdefault(r["type"], []).append(ms)

    all_ms = [ms for series in by_type.values() for ms in series]
    rows: list[dict[str, Any]] = []
    for t, series in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        summary = _summarize(series)
        rows.append({"type": t, "label": _TASK_TYPE_LABEL.get(t, t),
                     "value": summary["avg_ms"] or 0, **summary})
    return {"overall": _summarize(all_ms), "by_type": rows}


# ---------------------------------------------------------------- 趋势（预聚合）


def _trend(conn: sqlite3.Connection, since: str, today: date) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """近 30 天趋势：metrics_daily 预聚合 + 补零。

    额外挂两列实时值：``docs_created``（documents 按日计数）与 ``text_coverage``（当日解析均值）。
    前者让「导入趋势」在预聚合尚未写入时也有可信数据，后者是 metrics_daily 没有的卖点指标。
    """
    daily = {
        r["day"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM metrics_daily WHERE day >= ? ORDER BY day", (since,)
        ).fetchall()
    }
    created = {
        r["day"]: r["c"]
        for r in conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c FROM documents "
            "WHERE created_at >= ? GROUP BY day",
            (since,),
        ).fetchall()
    }
    coverage = {
        r["day"]: r["cov"]
        for r in conn.execute(
            "SELECT substr(parsed_at, 1, 10) AS day, AVG(text_coverage) AS cov FROM documents "
            "WHERE parsed_at >= ? AND text_coverage IS NOT NULL GROUP BY day",
            (since,),
        ).fetchall()
    }

    # 累计口径（OCR 触发占比等卡片用）：metrics_daily 全量，不受趋势窗口限制
    totals_row = conn.execute(
        "SELECT COALESCE(SUM(ocr_pages), 0) AS ocr_pages, COALESCE(SUM(imported), 0) AS imported, "
        "COALESCE(SUM(total_ms), 0) AS total_ms FROM metrics_daily"
    ).fetchone()

    series: list[dict[str, Any]] = []
    for offset in range(_TREND_DAYS):
        day = (today - timedelta(days=_TREND_DAYS - 1 - offset)).isoformat()
        row = daily.get(day, {})
        series.append({
            "day": day,
            "imported": row.get("imported", 0) or 0,
            "parsed_ok": row.get("parsed_ok", 0) or 0,
            "parsed_warn": row.get("parsed_warn", 0) or 0,
            "parsed_fail": row.get("parsed_fail", 0) or 0,
            "chunk_cnt": row.get("chunk_cnt", 0) or 0,
            "ocr_pages": row.get("ocr_pages", 0) or 0,
            "total_ms": row.get("total_ms", 0) or 0,
            "docs_created": created.get(day, 0),
            "text_coverage": _round(coverage.get(day)),
        })
    return series, dict(totals_row)


# ---------------------------------------------------------------- 辅助


def _ratio(numerator: float, denominator: float) -> float | None:
    """分母为 0 时给 None：让 UI 显示「—」而不是误导性的 0%。"""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _elapsed_ms(started: str, ended: str) -> float | None:
    try:
        return (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds() * 1000
    except (TypeError, ValueError):
        return None  # 时间戳格式异常只丢这一条样本，不影响整体统计


def _summarize(series: list[float]) -> dict[str, Any]:
    if not series:
        return {"samples": 0, "avg_ms": None, "p50_ms": None}
    ordered = sorted(series)
    mid = len(ordered) // 2
    p50 = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "samples": len(ordered),
        "avg_ms": round(sum(ordered) / len(ordered)),
        "p50_ms": round(p50),
    }
