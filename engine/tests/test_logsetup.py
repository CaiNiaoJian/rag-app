"""日志格式契约测试（08 章 §1）。

锁定「引擎日志 = 扁平九字段 JSONL」这条跨端契约：字段集合、src、level 归一（小写）、
业务字段透传（task_id/doc_id/code/page）、异常进 detail、每行都是合法单行 JSON。
此前引擎写的是 loguru serialize=True 的私有嵌套结构（无 src），与 Electron 侧纯文本
两边都不符契约，诊断包无法合并时间线——本测试防止回退。
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

from loguru import logger

import docfactory.logsetup as ls
from docfactory.config import Paths
from docfactory.logsetup import _jsonl_line

_NINE_FIELDS = {"ts", "level", "src", "task_id", "doc_id", "code", "page", "msg", "detail"}
_TZ8 = timezone(timedelta(hours=8))


def _fake_record(**over):
    rec = {
        "time": datetime(2026, 8, 1, 18, 30, 0, 123000, tzinfo=_TZ8),
        "level": types.SimpleNamespace(name="INFO"),
        "message": "hello",
        "extra": {},
        "exception": None,
    }
    rec.update(over)
    return rec


def test_jsonl_line_has_exactly_nine_flat_fields():
    obj = json.loads(_jsonl_line(_fake_record()))
    assert set(obj) == _NINE_FIELDS
    # 全是扁平标量/None，没有嵌套对象（loguru 私有结构那种 record.{...} 不允许再现）
    assert all(not isinstance(v, (dict, list)) for v in obj.values())


def test_jsonl_line_maps_fields():
    rec = _fake_record(
        level=types.SimpleNamespace(name="WARNING"),
        message="低置信",
        extra={"task_id": "t9", "doc_id": "d9", "code": "E04", "page": 3},
    )
    assert json.loads(_jsonl_line(rec)) == {
        "ts": "2026-08-01T18:30:00.123+08:00",
        "level": "warning",  # 归一为小写，与 task_events 的 info|warning|error 同域
        "src": "engine",
        "task_id": "t9",
        "doc_id": "d9",
        "code": "E04",
        "page": 3,
        "msg": "低置信",
        "detail": None,
    }


def test_jsonl_line_unbound_business_fields_are_null():
    obj = json.loads(_jsonl_line(_fake_record()))
    assert obj["task_id"] is None
    assert obj["doc_id"] is None
    assert obj["code"] is None
    assert obj["page"] is None


def test_jsonl_line_exception_goes_to_detail():
    exc = types.SimpleNamespace(type=ValueError, value=ValueError("boom"), traceback=None)
    rec = _fake_record(level=types.SimpleNamespace(name="ERROR"), message="failed", exception=exc)
    obj = json.loads(_jsonl_line(rec))
    assert obj["level"] == "error"
    assert obj["detail"] == "ValueError: boom"


def test_engine_log_file_is_line_delimited_json(tmp_path):
    """端到端：真跑 setup_logging，落盘的每一行都能被 json.loads 解析且字段齐全。"""
    paths = Paths(root=tmp_path)
    paths.ensure()

    # setup_logging 幂等（_configured 全局）：测试内强制重配，结束再复位，避免污染其他测试
    ls._configured = False
    logger.remove()
    try:
        ls.setup_logging(paths)
        logger.bind(task_id="t1", doc_id="d1").info("解析完成")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("任务异常")
    finally:
        logger.remove()  # enqueue=True：remove() 会 flush 队列并关闭文件句柄
        ls._configured = False

    files = list(paths.logs.glob("engine-*.jsonl"))
    assert files, "未产出 engine-*.jsonl 日志文件"
    lines = [
        line
        for f in files
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines, "日志文件为空"

    parsed = []
    for line in lines:
        obj = json.loads(line)  # 每行必须是合法 JSON（非法即抛，测试失败）
        assert set(obj) == _NINE_FIELDS
        assert obj["src"] == "engine"
        assert obj["level"] in ("info", "warning", "error", "debug", "critical", "trace", "success")
        parsed.append(obj)

    info = next(o for o in parsed if o["msg"] == "解析完成")
    assert info["task_id"] == "t1" and info["doc_id"] == "d1" and info["level"] == "info"

    err = next(o for o in parsed if o["msg"] == "任务异常")
    assert err["level"] == "error" and err["detail"] and "ValueError" in err["detail"]
