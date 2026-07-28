"""任务调度器与事件总线（02 章 §2.2）。

覆盖：队列派发、进度落库节流、终态映射、取消（排队中/运行中两条路径）、
异常 → 错误码映射、SSE 事件序列（含晚订阅补看历史）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from docfactory import scheduler as sched_mod
from docfactory.db import Database
from docfactory.errors import DocFactoryError
from docfactory.scheduler import EventBus, Scheduler
from docfactory.taskspec import (
    EVENT_DONE,
    EVENT_FAILED,
    EVENT_PROGRESS,
    TaskCancelled,
    TaskContext,
    TaskOutcome,
)

Runner = Callable[[TaskContext], TaskOutcome]


@pytest.fixture
def use_runner(monkeypatch: pytest.MonkeyPatch):
    """把任务类型统一劫持到指定 runner，避免测试依赖尚未落地的业务模块。"""

    def _apply(runner: Runner) -> None:
        monkeypatch.setattr(sched_mod, "_resolve_runner", lambda task_type: runner)

    return _apply


def _wait_status(db: Database, task_id: str, expected: set[str], timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = db.get_task(task_id)
        if row and row["status"] in expected:
            return row["status"]
        time.sleep(0.02)
    row = db.get_task(task_id)
    pytest.fail(f"任务 {task_id} 未在 {timeout}s 内到达 {expected}，当前 {row and row['status']}")


# ---------------------------------------------------------------- EventBus


def test_event_bus_replays_history_for_late_subscriber():
    bus = EventBus()
    bus.publish("t1", EVENT_PROGRESS, {"page": 1, "total": 3})
    bus.publish("t1", EVENT_PROGRESS, {"page": 2, "total": 3})

    events, cursor, closed = bus.read("t1", -1)
    assert [e["data"]["page"] for e in events] == [1, 2]
    assert cursor == 1 and closed is False

    bus.publish("t1", EVENT_DONE, {"status": "done"})
    fresh, cursor, closed = bus.read("t1", cursor)
    assert len(fresh) == 1 and closed is True


def test_event_bus_trims_progress_but_keeps_semantic_events(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sched_mod, "_MAX_EVENTS_PER_TASK", 10)
    bus = EventBus()
    bus.publish("t1", "stage_change", {"stage": "parse"})
    for i in range(30):
        bus.publish("t1", EVENT_PROGRESS, {"page": i, "total": 30})

    events, _, _ = bus.read("t1", -1)
    kinds = [e["event"] for e in events]
    assert "stage_change" in kinds            # 语义事件永不丢
    assert len(events) <= 10
    assert events[-1]["data"]["page"] == 29   # 保留的是最近的进度


def test_event_bus_unknown_task():
    bus = EventBus()
    assert bus.read("nope", -1) == ([], -1, False)
    assert bus.is_known("nope") is False


# ---------------------------------------------------------------- 正常执行


def test_task_runs_to_done_with_progress(db: Database, scheduler: Scheduler, use_runner):
    seen: list[int] = []

    def runner(ctx: TaskContext) -> TaskOutcome:
        for page in range(1, 4):
            ctx.progress(EVENT_PROGRESS, {"page": page, "total": 3, "stage": "parse"})
            seen.append(page)
        return TaskOutcome(status="done", message="ok", result={"pages": 3})

    use_runner(runner)
    db.create_task("t-ok", "parse", None, {})
    scheduler.start()
    scheduler.notify()

    assert _wait_status(db, "t-ok", {"done"}) == "done"
    assert seen == [1, 2, 3]
    row = db.get_task("t-ok")
    assert row["progress"] == 1.0 and row["ended_at"] and row["started_at"]

    events, _, closed = scheduler.bus.read("t-ok", -1)
    assert closed is True
    assert events[-1]["event"] == EVENT_DONE
    assert events[-1]["data"]["result"] == {"pages": 3}


def test_queue_is_drained_in_creation_order(db: Database, scheduler: Scheduler, use_runner):
    order: list[str] = []
    lock = threading.Lock()

    def runner(ctx: TaskContext) -> TaskOutcome:
        with lock:
            order.append(ctx.task_id)
        return TaskOutcome(status="done")

    use_runner(runner)
    for i in range(4):
        db.create_task(f"q{i}", "parse", None, {})
        time.sleep(0.01)  # created_at 是秒级，靠 id 兜底排序；这里只求稳定
    scheduler.start()

    for i in range(4):
        _wait_status(db, f"q{i}", {"done"})
    assert order == ["q0", "q1", "q2", "q3"]


# ---------------------------------------------------------------- 失败与取消


def test_business_error_maps_to_registry_code(db: Database, scheduler: Scheduler, use_runner):
    def runner(ctx: TaskContext) -> TaskOutcome:
        raise DocFactoryError("E02", "文件加密标志位为真")

    use_runner(runner)
    # tasks.doc_id 有外键约束，任务必须挂在真实文档上
    db.insert_document({"id": "doc-1", "name": "a.pdf", "src_path": "a.pdf",
                        "fmt": "pdf", "status": "imported"})
    db.create_task("t-e02", "parse", "doc-1", {})
    scheduler.start()

    assert _wait_status(db, "t-e02", {"failed"}) == "failed"
    assert db.get_task("t-e02")["error_code"] == "E02"

    events, _, _ = scheduler.bus.read("t-e02", -1)
    failed = [e for e in events if e["event"] == EVENT_FAILED][-1]
    # 错误三级呈现结构（FR-13）
    assert failed["data"]["error_code"] == "E02"
    assert failed["data"]["user_message"] == "文件受密码保护"
    assert failed["data"]["suggestion"]

    rows, total = db.query_events(level="error", task_id="t-e02")
    assert total == 1 and rows[0]["code"] == "E02"


def test_unexpected_exception_maps_to_e06(db: Database, scheduler: Scheduler, use_runner):
    def runner(ctx: TaskContext) -> TaskOutcome:
        raise ZeroDivisionError("boom")

    use_runner(runner)
    db.create_task("t-boom", "parse", None, {})
    scheduler.start()

    assert _wait_status(db, "t-boom", {"failed"}) == "failed"
    assert db.get_task("t-boom")["error_code"] == "E06"


def test_cancel_queued_task_without_occupying_worker(db: Database, scheduler: Scheduler, use_runner):
    """排队中取消：不启动 worker 也应立即落终态。"""
    use_runner(lambda ctx: TaskOutcome(status="done"))
    db.create_task("t-queued", "parse", None, {})

    result = scheduler.cancel("t-queued")
    assert result["canceled"] is True
    assert db.get_task("t-queued")["status"] == "canceled"


def test_cancel_running_task_at_checkpoint(db: Database, scheduler: Scheduler, use_runner):
    started = threading.Event()

    def runner(ctx: TaskContext) -> TaskOutcome:
        started.set()
        for page in range(1, 200):
            if ctx.cancelled():
                raise TaskCancelled()
            ctx.progress(EVENT_PROGRESS, {"page": page, "total": 200, "stage": "parse"})
            time.sleep(0.01)
        return TaskOutcome(status="done")

    use_runner(runner)
    db.create_task("t-run", "parse", None, {})
    scheduler.start()
    assert started.wait(10), "runner 未在 10s 内启动"

    assert scheduler.cancel("t-run")["canceled"] is True
    assert _wait_status(db, "t-run", {"canceled"}) == "canceled"


def test_cancel_unresponsive_runner_forced_after_grace(
    db: Database, scheduler: Scheduler, use_runner, monkeypatch: pytest.MonkeyPatch
):
    """取消宽限强制兜底（02 章 §2.1）：runner 卡在无检查点的第三方调用里时，
    宽限超时必须 ① 强标 canceled ② 记 warning ③ 补位 worker（并发槽不泄漏）
    ④ 弃用线程迟到返回后结果被丢弃。压缩宽限到 0.2s 走同一条代码路径。"""
    monkeypatch.setattr(sched_mod, "_CANCEL_GRACE_S", 0.2)
    started = threading.Event()
    release = threading.Event()
    late_done = threading.Event()

    def runner(ctx: TaskContext) -> TaskOutcome:
        if ctx.task_id != "t-stuck":
            return TaskOutcome(status="done")
        started.set()
        release.wait(30)          # 模拟卡死：没有任何 ctx.cancelled() 检查点
        late_done.set()
        return TaskOutcome(status="done", message="迟到的成功")

    use_runner(runner)
    db.create_task("t-stuck", "parse", None, {})
    scheduler.start()             # conftest 并发度=1：t-stuck 占住唯一的 worker
    assert started.wait(10), "runner 未启动"

    assert scheduler.cancel("t-stuck")["canceled"] is True
    # ①② 宽限超时后强制终态 + warning 事件（含 E06 码）。
    # 写入顺序契约：warning 先于终态可见，所以观察到 canceled 后事件必然已在库里
    assert _wait_status(db, "t-stuck", {"canceled"}, timeout=5.0) == "canceled"
    rows, total = db.query_events(level="warning", task_id="t-stuck")
    assert total >= 1 and any(r["code"] == "E06" for r in rows)
    # SSE 终态事件在库终态之后一拍才广播：这里对总线做短轮询而不是立即断言
    deadline = time.monotonic() + 3.0
    events: list[dict[str, Any]] = []
    closed = False
    while time.monotonic() < deadline and not closed:
        events, _, closed = scheduler.bus.read("t-stuck", -1)
        if not closed:
            time.sleep(0.02)
    assert closed and events[-1]["event"] == EVENT_DONE
    assert events[-1]["data"]["status"] == "canceled"

    # ③ 唯一的原 worker 还卡着，但补位的新 worker 必须能跑后续任务
    db.create_task("t-after", "parse", None, {})
    scheduler.notify()
    assert _wait_status(db, "t-after", {"done"}, timeout=5.0) == "done"

    # ④ 放开卡死调用：迟到结果被丢弃，终态不被改写
    release.set()
    assert late_done.wait(10)
    time.sleep(0.3)  # 给弃用线程走完 finally 的时间
    assert db.get_task("t-stuck")["status"] == "canceled", "迟到的 done 不得改写强制终态"


def test_cancel_responsive_runner_does_not_trigger_force_path(
    db: Database, scheduler: Scheduler, use_runner, monkeypatch: pytest.MonkeyPatch
):
    """宽限期内在检查点自行停下：不得出现强制路径的 warning（计时器被撤掉）。"""
    monkeypatch.setattr(sched_mod, "_CANCEL_GRACE_S", 5.0)
    started = threading.Event()

    def runner(ctx: TaskContext) -> TaskOutcome:
        started.set()
        for _ in range(500):
            if ctx.cancelled():
                raise TaskCancelled()
            time.sleep(0.01)
        return TaskOutcome(status="done")

    use_runner(runner)
    db.create_task("t-polite", "parse", None, {})
    scheduler.start()
    assert started.wait(10)

    scheduler.cancel("t-polite")
    assert _wait_status(db, "t-polite", {"canceled"}) == "canceled"
    _, total = db.query_events(level="warning", task_id="t-polite")
    assert total == 0, "检查点响应的取消不该记强制 warning"
    assert scheduler.snapshot()["abandoned"] == []


def test_cancel_finished_task_is_noop(db: Database, scheduler: Scheduler, use_runner):
    use_runner(lambda ctx: TaskOutcome(status="done"))
    db.create_task("t-fin", "parse", None, {})
    scheduler.start()
    _wait_status(db, "t-fin", {"done"})

    result = scheduler.cancel("t-fin")
    assert result["canceled"] is False and result["status"] == "done"


def test_cancel_missing_task(db: Database, scheduler: Scheduler):
    with pytest.raises(DocFactoryError) as excinfo:
        scheduler.cancel("does-not-exist")
    assert excinfo.value.code == "E03"


# ---------------------------------------------------------------- 队列暂停


def test_queue_pause_gates_dispatch_and_persists(
    db: Database, scheduler: Scheduler, use_runner, settings_holder
):
    """暂停 = 暂停派发：排队任务**原地保留**（不取消不重建，task_id 稳定）；
    开关持久化到 meta——新调度器实例（模拟引擎重启）必须恢复暂停态。"""
    done = threading.Event()

    def runner(ctx: TaskContext) -> TaskOutcome:
        done.set()
        return TaskOutcome(status="done")

    use_runner(runner)
    scheduler.set_paused(True)
    db.create_task("t-held", "parse", None, {})
    scheduler.start()
    scheduler.notify()

    assert not done.wait(0.8), "暂停期间不得派发任务"
    assert db.get_task("t-held")["status"] == "queued", "排队任务应原地保留而非被取消"

    restarted = Scheduler(db, scheduler.paths, settings_holder.get)
    assert restarted.is_paused() is True, "暂停态必须在引擎重启后延续（meta 持久化）"

    scheduler.set_paused(False)
    assert _wait_status(db, "t-held", {"done"}) == "done"
    assert done.is_set()


def test_queue_pause_exempts_module_install(db: Database, scheduler: Scheduler, use_runner):
    """模组安装不受暂停约束：用户点「验签并安装」期望立即有反馈。"""
    def runner(ctx: TaskContext) -> TaskOutcome:
        return TaskOutcome(status="done")

    use_runner(runner)
    scheduler.set_paused(True)
    db.create_task("t-parse-held", "parse", None, {})
    db.create_task("t-mod", "module_install", None, {})
    scheduler.start()

    assert _wait_status(db, "t-mod", {"done"}) == "done"
    assert db.get_task("t-parse-held")["status"] == "queued"

    scheduler.set_paused(False)
    assert _wait_status(db, "t-parse-held", {"done"}) == "done"


# ---------------------------------------------------------------- runner 解析


def test_missing_runner_module_fails_that_type_only(db: Database, scheduler: Scheduler):
    """执行器模块缺失只让该类型任务失败，不牵连引擎与其他任务（并行开发期的纪律）。"""
    original = dict(sched_mod.RUNNERS)
    sched_mod.RUNNERS["parse"] = "docfactory.definitely_not_a_module:run"
    try:
        db.create_task("t-missing", "parse", None, {})
        scheduler.start()
        assert _wait_status(db, "t-missing", {"failed"}) == "failed"
        assert db.get_task("t-missing")["error_code"] == "E06"
    finally:
        sched_mod.RUNNERS.clear()
        sched_mod.RUNNERS.update(original)


def test_unknown_task_type_maps_to_e03(db: Database, scheduler: Scheduler):
    db.create_task("t-weird", "no_such_type", None, {})
    scheduler.start()
    assert _wait_status(db, "t-weird", {"failed"}) == "failed"
    assert db.get_task("t-weird")["error_code"] == "E03"


# ---------------------------------------------------------------- SSE 异步流


@pytest.mark.asyncio
async def test_events_stream_yields_until_terminal(db: Database, scheduler: Scheduler, use_runner):
    def runner(ctx: TaskContext) -> TaskOutcome:
        for page in range(1, 4):
            ctx.progress(EVENT_PROGRESS, {"page": page, "total": 3, "stage": "parse"})
            time.sleep(0.05)
        return TaskOutcome(status="done")

    use_runner(runner)
    db.create_task("t-sse", "parse", None, {})
    scheduler.start()

    collected: list[dict[str, Any]] = []
    async def drain() -> None:
        async for item in scheduler.events("t-sse"):
            collected.append(item)

    await asyncio.wait_for(drain(), timeout=20)
    assert collected[-1]["event"] == EVENT_DONE
    assert [e["data"]["page"] for e in collected if e["event"] == EVENT_PROGRESS] == [1, 2, 3]


@pytest.mark.asyncio
async def test_events_stream_replays_historical_task_after_restart(db: Database, scheduler: Scheduler):
    """引擎重启后总线是空的：订阅历史任务应从库里补一条终态而不是空转。"""
    db.create_task("t-old", "parse", None, {})
    db.update_task("t-old", status="interrupted")

    collected = [item async for item in scheduler.events("t-old")]
    assert len(collected) == 1
    assert collected[0]["event"] == EVENT_FAILED
    assert collected[0]["data"]["error_code"] == "E06"


@pytest.mark.asyncio
async def test_events_stream_ends_for_unknown_task(scheduler: Scheduler):
    assert [item async for item in scheduler.events("never-existed")] == []


# ---------------------------------------------------------------- SSE 文本编码


def test_sse_format_block():
    block = "".join(sched_mod.sse_format({"seq": 7, "event": "progress", "data": {"page": 2, "总": "中文"}}))
    assert block.startswith("event: progress\n")
    assert "id: 7\n" in block
    assert '"总": "中文"' in block or '"总":"中文"' in block  # ensure_ascii=False
    assert block.endswith("\n\n")                            # SSE 事件块以空行结束
