"""任务端点：创建 / 列表 / 详情 / SSE 进度流 / 取消（02 章 §2.1、§2.2）。

- POST /tasks              body ``{type, payload}``；文件一律传绝对路径不传内容。
                           入库后必须 ``scheduler.notify()``，否则要等 worker 的 0.2s 轮询才起跑。
- GET  /tasks              分页 + status 过滤，统一返回 ``{"items": [...], "total": N}``。
- GET  /tasks/{id}         任务行 + **阶段时间线** + 解析后的 payload/result。
                           时间线是 UI「失败定位」抽屉的主体（07 章 §2 流程 B）：
                           把 task_events 按 stage 聚合成「读取→转换→OCR→切片」的步骤条，
                           失败步骤带错误码，UI 直接高亮那一格。
- GET  /tasks/{id}/events  SSE。事件序列由 scheduler.events() 产出（含补看历史与终态补发），
                           这里只负责按 SSE 文本格式拼块 + 关掉沿途缓冲。
- POST /tasks/{id}/cancel  置取消标志；queued 任务直接落终态（语义见 scheduler.cancel）。
- GET  /queue              队列状态：暂停开关 + queued/running 计数（工作台数据源）。
- POST /queue/pause        body ``{paused}``。**引擎侧持久暂停**（meta 表，重启不丢）：
                           排队任务原地保留不取消不重建，task_id 稳定、追溯链不断；
                           正在跑的任务不打断；模组安装不受暂停约束。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, get_args

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from docfactory.errors import DocFactoryError
from docfactory.scheduler import sse_format
from docfactory.taskspec import TaskType

router = APIRouter()

# 合法任务类型直接取自冻结的 Literal，避免路由里再抄一份枚举而与 taskspec 漂移
_TASK_TYPES: frozenset[str] = frozenset(get_args(TaskType))

# 时间线聚合读取的事件上限：单任务事件再多，步骤条也只需要各阶段的首尾与最坏级别
_TIMELINE_SCAN = 1000
# 详情里附带的最近事件条数（07 章 §2：技术详情折叠里给「相关日志 50 行」）
_RECENT_EVENTS = 50

_LEVEL_RANK = {"info": 0, "warning": 1, "error": 2}

# SSE 响应头：Cache-Control 防中间层缓存整条流；X-Accel-Buffering 关掉可能存在的反代缓冲；
# 二者都是「即使今天没有代理，明天有人加了也不会静默坏掉」的保险。
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


class CreateTaskBody(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/tasks")
def create_task(request: Request, body: CreateTaskBody) -> dict[str, Any]:
    """建任务入队；返回 {task_id}，进度经 GET /tasks/{id}/events 订阅。"""
    if body.type not in _TASK_TYPES:
        raise DocFactoryError("E03", f"未知任务类型：{body.type}")

    db = request.app.state.db
    doc_id = body.payload.get("doc_id")
    if doc_id is not None and not isinstance(doc_id, str):
        raise DocFactoryError("E03", "payload.doc_id 必须是字符串")
    if doc_id and db.get_document(doc_id) is None:
        # 与 scheduler.cancel 对「任务不存在」的处理保持一致，同用 E03
        raise DocFactoryError("E03", f"文档不存在：{doc_id}")

    task_id = str(uuid.uuid4())
    db.create_task(task_id, body.type, doc_id, body.payload)
    request.app.state.scheduler.notify()
    return {"task_id": task_id}


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """任务列表（创建时间倒序）。"""
    items, total = request.app.state.db.list_tasks(
        status=status, page=page, page_size=page_size
    )
    return {"items": items, "total": total}


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> dict[str, Any]:
    """任务详情：原始行 + payload/result 解析值 + 阶段时间线 + 最近事件。"""
    db = request.app.state.db
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    events, _ = db.query_events(task_id=task_id, page=1, page_size=_TIMELINE_SCAN)
    # query_events 按 id 倒序返回；时间线要正序推进，最近事件保持倒序给 UI 直接渲染
    ascending = list(reversed(events))

    return {
        **row,
        "payload": _load_json(row.get("payload_json")),
        # tasks 表没有结果列（大结果一律落盘、摘要走 SSE done 事件）；
        # 未来若加 result_json 列，这里自动跟着返回，UI 侧类型已预留该字段。
        "result": _load_json(row.get("result_json")),
        "timeline": _build_timeline(ascending, row),
        "events": events[:_RECENT_EVENTS],
    }


@router.get("/tasks/{task_id}/events")
async def task_event_stream(request: Request, task_id: str) -> StreamingResponse:
    """SSE 进度流：progress / stage_change / degrade / done / failed。"""
    scheduler = request.app.state.scheduler

    async def stream() -> AsyncIterator[str]:
        # 先发一个 SSE 注释行把响应头推出去：客户端能立刻确认订阅成功，
        # 也顺带打穿任何按缓冲区大小刷新的中间层（注释行按规范被客户端忽略）。
        yield ": ok\n\n"
        async for item in scheduler.events(task_id):
            for chunk in sse_format(item):
                yield chunk

    # 客户端断开由 Starlette 取消生成器，这里不主动轮询 is_disconnected（会与其抢 receive 通道）
    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(request: Request, task_id: str) -> dict[str, Any]:
    """取消任务；running 任务在下一个检查点响应（10s 宽限见 scheduler）。"""
    return request.app.state.scheduler.cancel(task_id)


class QueuePauseBody(BaseModel):
    paused: bool


@router.get("/queue")
def queue_state(request: Request) -> dict[str, Any]:
    """队列状态：暂停开关（引擎侧持久）+ queued/running 计数。"""
    db = request.app.state.db
    _, queued = db.list_tasks(status="queued", page=1, page_size=1)
    _, running = db.list_tasks(status="running", page=1, page_size=1)
    return {
        "paused": request.app.state.scheduler.is_paused(),
        "queued": queued,
        "running": running,
    }


@router.post("/queue/pause")
def queue_pause(request: Request, body: QueuePauseBody) -> dict[str, Any]:
    """暂停/恢复队列派发。持久化到 meta：刷新页面、重开窗口、引擎重启都不丢。"""
    return request.app.state.scheduler.set_paused(body.paused)


# ---------------------------------------------------------------- 辅助


def _load_json(raw: Any) -> Any:
    """宽容解析入库的 JSON 文本：脏数据只让该字段为 None，不该让整个详情端点 500。"""
    if isinstance(raw, dict | list):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _build_timeline(events: list[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    """按 stage 聚合事件为步骤条：首次出现即入列，级别取该阶段最坏值（失败步骤一眼可见）。"""
    order: list[str] = []
    agg: dict[str, dict[str, Any]] = {}
    for ev in events:
        stage = ev.get("stage")
        if not stage:
            continue
        item = agg.get(stage)
        if item is None:
            item = agg[stage] = {
                "stage": stage,
                "started_at": ev.get("ts"),
                "ended_at": ev.get("ts"),
                "level": "info",
                "code": None,
                "message": ev.get("message"),
                "events": 0,
            }
            order.append(stage)
        item["ended_at"] = ev.get("ts")
        item["events"] += 1
        level = ev.get("level") or "info"
        if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(item["level"], 0):
            item.update(level=level, code=ev.get("code"), message=ev.get("message"))

    timeline = [agg[s] for s in order]
    if not timeline and row.get("stage"):
        # runner 只上报了 stage_change（进 tasks.stage）而没写 task_events：至少给出当前阶段
        timeline.append({
            "stage": row["stage"],
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "level": "error" if row.get("status") == "failed" else "info",
            "code": row.get("error_code"),
            "message": None,
            "events": 0,
        })
    return timeline
