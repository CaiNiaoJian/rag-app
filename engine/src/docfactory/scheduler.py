"""任务调度器（02 章 §2.2 状态机与队列语义）。

职责边界（与 taskspec.py 的契约互补）：
- **队列**：唯一事实来源是 SQLite ``tasks`` 表 —— 引擎重启不丢任务；worker 线程
  经 ``claim_next_queued()`` 原子领取（``queued`` → ``running``）。
- **派发**：按 ``type`` 延迟 import 对应 runner（见 RUNNERS），建 TaskContext 交给它。
- **收口**：捕获异常映射 error_code（DocFactoryError 用自带码，其余一律 E06），
  写 tasks/task_events，推终态 SSE 事件。
- **进度**：runner 调 ``ctx.progress(event, data)`` → 进事件总线（SSE）+ 节流落库。
- **取消**：置标志位，runner 在检查点轮询；``queued`` 任务直接置 canceled 不占 worker。

线程模型：worker 是普通线程（解析为 CPU/IO 混合同步代码，不进 asyncio 事件循环）；
SSE 端点在事件循环里**轮询**事件序列，不做跨线程 asyncio 唤醒——少一层易错的并发面，
代价是最多 ``_POLL_S`` 的延迟（远低于 NFR 的 1s 进度刷新要求）。
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from loguru import logger

from docfactory.config import Paths, Settings
from docfactory.db import Database, now_iso
from docfactory.errors import DocFactoryError, error_payload
from docfactory.taskspec import (
    EVENT_DEGRADE,
    EVENT_DONE,
    EVENT_FAILED,
    EVENT_PROGRESS,
    EVENT_STAGE_CHANGE,
    TaskCancelled,
    TaskContext,
    TaskOutcome,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# 任务类型 → runner 位置（"模块:函数"）。延迟 import：某个 runner 模块尚未落地
# 或 import 失败时，只让该类型任务报错，不牵连引擎启动与其他任务类型。
RUNNERS: dict[str, str] = {
    "parse": "docfactory.pipeline:run_parse",
    "export": "docfactory.exporters:run_export",
    "rechunk": "docfactory.chunking:run_rechunk",
    "module_install": "docfactory.modules.manager:run_install",
    "qa_generate": "docfactory.dataset:run_qa_generate",
    "dataset_build": "docfactory.dataset:run_dataset_build",
}

_POLL_S = 0.2              # 空队列轮询与 SSE 拉取间隔
_PROGRESS_WRITE_S = 0.5    # tasks.progress 落库节流（阶段变化不受此限，立即写）
_EVENT_RETAIN_S = 120.0    # 任务终态后事件序列保留时长（供晚到的 SSE 订阅补看）
_MAX_EVENTS_PER_TASK = 4000  # 单任务事件上限（超出丢弃最早的 progress，保留语义事件）
# 取消宽限（02 章 §2.1）：runner 若卡在无检查点的第三方调用（LibreOffice/大 PDF 读页）里，
# 取消信号永远等不到响应。超过宽限即走强制路径：记 warning + 强标 canceled +
# 弃用该 worker 线程（Python 杀不掉线程，与 run_with_timeout 同款取舍）+ 补位新 worker。
_CANCEL_GRACE_S = 10.0

# 队列暂停的持久化键（meta 表）：暂停状态与队列本体（tasks 表）同库共存亡，
# 刷新页面、重开窗口、引擎重启都不丢——此前「暂停」只活在渲染进程内存里，
# 还要靠「取消再重建」模拟，task_id 一变追溯链就断。
_META_QUEUE_PAUSED = "queue_paused"
# 暂停不拦模组安装：用户点「验签并安装」期望立即有反馈，且它不占解析资源
_PAUSE_EXEMPT_TYPES: tuple[str, ...] = ("module_install",)


class _TaskStream:
    """单任务的事件序列。追加与读取都加锁；终态后打时间戳供 GC。"""

    __slots__ = ("events", "closed", "closed_at")

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False
        self.closed_at = 0.0


class EventBus:
    """任务事件总线：worker 线程写入，SSE 端点按序号增量读取。

    不用 asyncio.Queue —— 订阅可能晚于事件产生（UI 建任务后才发起 SSE 请求），
    序列 + 游标天然支持「补看历史」，重连也不丢事件。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: dict[str, _TaskStream] = {}

    def publish(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            stream = self._streams.get(task_id)
            if stream is None:
                stream = self._streams[task_id] = _TaskStream()
            seq = len(stream.events)
            stream.events.append({"seq": seq, "event": event, "data": data})
            if len(stream.events) > _MAX_EVENTS_PER_TASK:
                self._trim(stream)
            if event in (EVENT_DONE, EVENT_FAILED):
                stream.closed = True
                stream.closed_at = time.monotonic()
            self._gc_locked()

    @staticmethod
    def _trim(stream: _TaskStream) -> None:
        """超限时优先丢弃最早的 progress（可再生的噪声），语义事件全部保留。"""
        keep = [e for e in stream.events if e["event"] != EVENT_PROGRESS]
        progress = [e for e in stream.events if e["event"] == EVENT_PROGRESS]
        room = max(0, _MAX_EVENTS_PER_TASK // 2)
        merged = keep + progress[-room:]
        merged.sort(key=lambda e: e["seq"])
        stream.events = merged

    def read(self, task_id: str, cursor: int) -> tuple[list[dict[str, Any]], int, bool]:
        """取 seq > cursor 的事件；返回 (事件列表, 新游标, 是否已终结)。"""
        with self._lock:
            stream = self._streams.get(task_id)
            if stream is None:
                return [], cursor, False
            fresh = [e for e in stream.events if e["seq"] > cursor]
            new_cursor = fresh[-1]["seq"] if fresh else cursor
            return fresh, new_cursor, stream.closed

    def is_known(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._streams

    def drop(self, task_id: str) -> None:
        with self._lock:
            self._streams.pop(task_id, None)

    def _gc_locked(self) -> None:
        now = time.monotonic()
        stale = [
            tid for tid, s in self._streams.items()
            if s.closed and now - s.closed_at > _EVENT_RETAIN_S
        ]
        for tid in stale:
            self._streams.pop(tid, None)


class Scheduler:
    """SQLite 队列 + 固定 worker 线程池。``settings_provider`` 每次取当前设置，
    保证 PUT /settings 改并发度/超时后新任务立即生效（已在跑的任务沿用旧值）。"""

    def __init__(
        self,
        db: Database,
        paths: Paths,
        settings_provider: Callable[[], Settings],
        *,
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.paths = paths
        self._settings_provider = settings_provider
        self.bus = bus or EventBus()

        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._cancel_lock = threading.Lock()
        self._cancelled: set[str] = set()          # 已请求取消的 task_id
        self._running: dict[str, float] = {}       # task_id → 开始时间（monotonic）
        self._progress_ts: dict[str, float] = {}   # task_id → 上次 progress 落库时刻
        self._abandoned: set[str] = set()          # 取消超时被强制终态、线程已弃用的 task_id
        self._watchdogs: dict[str, threading.Timer] = {}  # task_id → 取消宽限计时器

        # 队列暂停开关：从 meta 恢复（引擎重启后暂停态延续，用户不会看到「凭空恢复派发」）
        self._paused = threading.Event()
        try:
            if (db.get_meta(_META_QUEUE_PAUSED) or "0") == "1":
                self._paused.set()
        except Exception:
            pass  # meta 表尚未就绪（空库未迁移）：按未暂停处理，migrate 后由 set_paused 落库

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        n = max(1, int(self._settings_provider().parallel_tasks))
        for _ in range(n):
            self._spawn_worker()
        logger.info(f"任务调度器启动：{n} 个 worker")
        self.notify()

    def _spawn_worker(self) -> None:
        with self._cancel_lock:
            seq = len(self._threads)
            t = threading.Thread(target=self._worker_loop, name=f"df-worker-{seq}", daemon=True)
            self._threads.append(t)
        t.start()

    def stop(self, timeout: float = 10.0) -> None:
        """请求停机：唤醒所有 worker 退出循环；在跑的任务会收到取消信号。"""
        self._stopping.set()
        with self._cancel_lock:
            self._cancelled.update(self._running.keys())
            watchdogs = list(self._watchdogs.values())
            self._watchdogs.clear()
            threads = list(self._threads)
        for timer in watchdogs:
            timer.cancel()
        self._wake.set()
        deadline = time.monotonic() + timeout
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def notify(self) -> None:
        """有新任务入队时唤醒 worker（POST /tasks 与 /modules/install 调用）。"""
        self._wake.set()

    # ------------------------------------------------------------ 队列暂停

    def set_paused(self, paused: bool) -> dict[str, Any]:
        """暂停/恢复队列派发（持久化到 meta，重启不丢）。

        语义是「暂停派发」而非「取消重建」：排队任务原地保留（task_id 不变，
        按 ID 追溯一次导入的链路不断），正在跑的任务不打断；
        模组安装不受暂停约束（_PAUSE_EXEMPT_TYPES）。
        """
        changed = paused != self._paused.is_set()
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
        self.db.set_meta(_META_QUEUE_PAUSED, "1" if paused else "0")
        if changed:
            self.db.log_event(
                level="info",
                message="队列已暂停派发（排队任务原地等待）" if paused else "队列已恢复派发",
            )
        if not paused:
            self.notify()
        return {"paused": paused}

    def is_paused(self) -> bool:
        return self._paused.is_set()

    # ------------------------------------------------------------ 取消

    def cancel(self, task_id: str) -> dict[str, Any]:
        """取消任务。queued → 直接置 canceled；running → 置标志等 runner 检查点响应。"""
        row = self.db.get_task(task_id)
        if row is None:
            raise DocFactoryError("E03", f"任务不存在：{task_id}")
        status = row["status"]
        if status in ("done", "failed", "canceled", "interrupted"):
            return {"task_id": task_id, "status": status, "canceled": False}

        with self._cancel_lock:
            self._cancelled.add(task_id)
            running = task_id in self._running

        if not running:
            # 尚未被领取：直接落终态，worker 领到时也会因标志位立刻退出
            self.db.update_task(task_id, status="canceled", ended_at=now_iso())
            self.bus.publish(task_id, EVENT_DONE, {"status": "canceled", "message": "任务已取消"})
            self.db.log_event(level="info", task_id=task_id, doc_id=row.get("doc_id"),
                              message="任务已取消（排队中）")
            return {"task_id": task_id, "status": "canceled", "canceled": True}

        self._arm_cancel_watchdog(task_id, row.get("doc_id"))
        return {"task_id": task_id, "status": "running", "canceled": True,
                "message": "已发出取消信号，任务将在下一个检查点停止"}

    def is_cancelled(self, task_id: str) -> bool:
        with self._cancel_lock:
            return task_id in self._cancelled

    def _arm_cancel_watchdog(self, task_id: str, doc_id: str | None) -> None:
        """取消宽限计时：到点仍在跑就走 `_force_cancel`。重复取消不叠加计时器。"""
        with self._cancel_lock:
            if task_id in self._watchdogs or self._stopping.is_set():
                return
            timer = threading.Timer(_CANCEL_GRACE_S, self._force_cancel, args=(task_id, doc_id))
            timer.daemon = True
            self._watchdogs[task_id] = timer
        timer.start()

    def _force_cancel(self, task_id: str, doc_id: str | None) -> None:
        """宽限超时的强制兜底：runner 卡在无检查点的第三方调用里，取消信号永远悬空。

        Python 杀不掉线程，能做的是：① 任务强标 canceled（用户意图已达成，不再显示
        永远的 running）；② 广播终态事件（UI/SSE 拿到确定性收尾）；③ 弃用该 worker
        线程——其迟到的结果会被丢弃（见 `_execute`），取消标志保持置位，卡死的调用
        一旦返回就会在下个检查点自行退出；④ 立刻补位一个新 worker，并发槽不泄漏。
        """
        with self._cancel_lock:
            self._watchdogs.pop(task_id, None)
            if self._stopping.is_set() or task_id not in self._running:
                return  # 已在宽限期内自行停下，或引擎正在停机
            self._abandoned.add(task_id)

        # 写入顺序是契约：解释性 warning 先于终态落库，终态先于 SSE 终态事件——
        # 任何一侧（轮询 tasks 表 / 订阅 SSE）看到「已取消」时，原因日志必然已可查。
        try:
            self.db.log_event(
                level="warning", task_id=task_id, doc_id=doc_id, code="E06",
                message=f"取消信号超过 {_CANCEL_GRACE_S:.0f}s 未被响应，已强制标记为已取消",
                detail={"note": "工作线程已弃用并补位；被卡住的第三方调用返回后其结果将被丢弃"},
            )
            self.db.update_task(task_id, status="canceled", ended_at=now_iso())
        except Exception as exc:  # 强制路径不能因落库失败而半途而废
            logger.exception(f"强制取消落库失败 task={task_id}：{exc}")
        self.bus.publish(task_id, EVENT_DONE, {"status": "canceled", "message": "任务已取消（超时强制）"})
        logger.warning(f"任务 {task_id} 取消超时：worker 线程已弃用，已补位新 worker")
        self._spawn_worker()

    # ------------------------------------------------------------ SSE

    async def events(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """SSE 事件异步流：补看历史 → 增量推送 → 终态后收尾。

        任务已在库里是终态而总线无记录（引擎重启后的历史任务）时，直接补一条终态事件。
        """
        cursor = -1
        idle_since = time.monotonic()
        while True:
            fresh, cursor, closed = self.bus.read(task_id, cursor)
            for item in fresh:
                yield item
                idle_since = time.monotonic()
            if closed:
                return
            if not fresh:
                # 总线无该任务：可能是重启前的历史任务，查库补终态后结束
                if not self.bus.is_known(task_id):
                    row = self.db.get_task(task_id)
                    if row is None:
                        return
                    if row["status"] in ("done", "failed", "canceled", "interrupted"):
                        yield {"seq": 0, "event": _terminal_event(row["status"]),
                               "data": _terminal_payload(row)}
                        return
                # 长时间静默也保持连接（uvicorn 侧无超时），由客户端断开
                if time.monotonic() - idle_since > 3600:
                    return
            await asyncio.sleep(_POLL_S)

    # ------------------------------------------------------------ worker

    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            task = None
            try:
                # 暂停期间只放行豁免类型（模组安装），解析/导出等留在队列原地等待
                only = _PAUSE_EXEMPT_TYPES if self._paused.is_set() else None
                task = self.db.claim_next_queued(only_types=only)
            except Exception as exc:  # 数据库瞬时故障不该打死 worker
                logger.exception(f"领取任务失败：{exc}")
            if task is None:
                self._wake.wait(_POLL_S)
                self._wake.clear()
                continue
            # 还有任务时立刻放行其他 worker（wait 的那个会马上再抢一轮）
            self._wake.set()
            try:
                if self._execute(task):
                    return  # 本线程曾被取消宽限判定为卡死并已补位：迟到返回后安静退役
            except Exception as exc:  # 兜底：_execute 内部已收口，这里只防意外
                logger.exception(f"任务执行框架异常 task={task.get('id')}：{exc}")

    def _execute(self, task: dict[str, Any]) -> bool:
        """执行一个任务；返回 True 表示本线程已被弃用（调用方应退出循环）。"""
        task_id: str = task["id"]
        doc_id: str | None = task.get("doc_id")
        task_type: str = task["type"]
        payload = _decode_payload(task.get("payload_json"))

        with self._cancel_lock:
            if task_id in self._cancelled:
                self.db.update_task(task_id, status="canceled", ended_at=now_iso())
                self.bus.publish(task_id, EVENT_DONE, {"status": "canceled", "message": "任务已取消"})
                return False
            self._running[task_id] = time.monotonic()

        log = logger.bind(task_id=task_id, doc_id=doc_id)
        log.info(f"任务开始：type={task_type}")
        self.bus.publish(task_id, EVENT_STAGE_CHANGE, {"stage": None, "status": "running"})

        ctx = TaskContext(
            db=self.db,
            paths=self.paths,
            settings=self._settings_provider(),
            task_id=task_id,
            doc_id=doc_id,
            payload=payload,
            progress=lambda event, data: self._on_progress(task_id, event, data),
            cancelled=lambda: self.is_cancelled(task_id),
        )

        outcome: TaskOutcome
        try:
            runner = _resolve_runner(task_type)
            outcome = runner(ctx)
            if not isinstance(outcome, TaskOutcome):  # runner 契约兜底
                outcome = TaskOutcome(status="done")
        except TaskCancelled:
            outcome = TaskOutcome(status="canceled", message="任务已取消")
        except DocFactoryError as exc:
            log.warning(f"任务失败 [{exc.code}]：{exc.detail}")
            self.db.log_event(level="error", task_id=task_id, doc_id=doc_id, code=exc.code,
                              page=exc.page, message=str(exc), detail={"detail": exc.detail})
            outcome = TaskOutcome(status="failed", error_code=exc.code, message=exc.detail)
        except Exception as exc:
            log.exception(f"任务异常：{exc}")
            self.db.log_event(level="error", task_id=task_id, doc_id=doc_id, code="E06",
                              message=f"引擎异常：{type(exc).__name__}",
                              detail={"exception": f"{type(exc).__name__}: {exc}"})
            outcome = TaskOutcome(status="failed", error_code="E06", message=str(exc))
        finally:
            with self._cancel_lock:
                abandoned = task_id in self._abandoned
                self._abandoned.discard(task_id)
                self._running.pop(task_id, None)
                self._cancelled.discard(task_id)
                timer = self._watchdogs.pop(task_id, None)
            if timer is not None:
                timer.cancel()  # 宽限期内自行停下：撤掉计时器，别再走强制路径
            self._progress_ts.pop(task_id, None)

        if abandoned:
            # 终态早已由 _force_cancel 落库并广播；这里的结果是弃用线程的迟到产物，
            # 一律丢弃——并发槽已被补位，本线程继续领任务只会超出并发度上限
            logger.bind(task_id=task_id).warning(
                f"被弃用的任务线程最终返回，迟到结果已丢弃（{outcome.status}）"
            )
            return True

        self._finish(task_id, doc_id, outcome)
        return False

    def _finish(self, task_id: str, doc_id: str | None, outcome: TaskOutcome) -> None:
        fields: dict[str, Any] = {"status": outcome.status, "ended_at": now_iso()}
        if outcome.error_code:
            fields["error_code"] = outcome.error_code
        if outcome.status == "done":
            fields["progress"] = 1.0
        try:
            self.db.update_task(task_id, **fields)
        except Exception as exc:
            logger.exception(f"任务终态落库失败 task={task_id}：{exc}")

        if outcome.status == "failed":
            payload = error_payload(outcome.error_code or "E06", outcome.message)
            self.bus.publish(task_id, EVENT_FAILED, payload)
        else:
            self.bus.publish(task_id, EVENT_DONE, {
                "status": outcome.status,
                "message": outcome.message,
                "result": outcome.result,
            })
        logger.bind(task_id=task_id, doc_id=doc_id).info(f"任务结束：{outcome.status}")

    # ------------------------------------------------------------ 进度

    def _on_progress(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        """runner 的进度回调：进事件总线（实时）+ 节流落库（供刷新页面后仍看得到）。"""
        with self._cancel_lock:
            if task_id in self._abandoned:
                return  # 弃用线程的迟到进度：任务已被强制终态，不再污染事件流与库
        self.bus.publish(task_id, event, data)

        fields: dict[str, Any] = {}
        stage = data.get("stage")
        if event == EVENT_STAGE_CHANGE and stage:
            fields["stage"] = stage
        elif event == EVENT_PROGRESS:
            total = data.get("total") or 0
            page = data.get("page") or 0
            if total:
                fields["progress"] = max(0.0, min(1.0, float(page) / float(total)))
            if stage:
                fields["stage"] = stage
            last = self._progress_ts.get(task_id, 0.0)
            now = time.monotonic()
            if now - last < _PROGRESS_WRITE_S:
                return  # 节流：页粒度进度不必每页写库，SSE 已经实时
            self._progress_ts[task_id] = now
        elif event == EVENT_DEGRADE:
            return  # 降级事件只走 SSE + task_events（由 runner 自己记录），不动进度

        if fields:
            try:
                self.db.update_task(task_id, **fields)
            except Exception as exc:
                logger.warning(f"进度落库失败 task={task_id}：{exc}")

    # ------------------------------------------------------------ 诊断

    def snapshot(self) -> dict[str, Any]:
        with self._cancel_lock:
            return {
                "workers": len(self._threads),
                "paused": self._paused.is_set(),
                "running": sorted(self._running),
                "cancelling": sorted(self._cancelled),
                "abandoned": sorted(self._abandoned),
            }


# ---------------------------------------------------------------- 辅助


def _decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        import json

        try:
            obj = json.loads(raw)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


def _resolve_runner(task_type: str) -> Callable[[TaskContext], TaskOutcome]:
    spec = RUNNERS.get(task_type)
    if spec is None:
        raise DocFactoryError("E03", f"未知任务类型：{task_type}")
    mod_name, _, fn_name = spec.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise DocFactoryError("E06", f"任务类型 {task_type} 的执行器不可用：{exc}") from exc
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise DocFactoryError("E06", f"任务类型 {task_type} 的执行器缺少入口 {fn_name}")
    return fn


def _terminal_event(status: str) -> str:
    return EVENT_DONE if status in ("done", "canceled") else EVENT_FAILED


def _terminal_payload(row: dict[str, Any]) -> dict[str, Any]:
    status = row["status"]
    if status in ("failed", "interrupted"):
        detail = "任务在引擎重启前被中断" if status == "interrupted" else None
        return error_payload(row.get("error_code") or "E06", detail)
    return {"status": status, "message": ""}


def sse_format(item: dict[str, Any]) -> Iterator[str]:
    """事件字典 → SSE 文本块（供 routes_tasks 直接拼装 StreamingResponse）。"""
    import json

    yield f"event: {item['event']}\n"
    yield f"id: {item['seq']}\n"
    yield f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
