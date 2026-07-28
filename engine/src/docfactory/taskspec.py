"""任务执行契约：调度器与各 runner（parse/export/rechunk/module_install/…）之间的统一签名。

所有任务 runner 实现：``def run(ctx: TaskContext) -> TaskOutcome``。
调度器负责：建 TaskContext、捕获异常映射错误码、写 tasks/task_events 表、推 SSE。
runner 负责：干活、调用 ctx.progress() 上报页粒度进度、在检查点轮询 ctx.cancelled()。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from docfactory.config import Paths, Settings
    from docfactory.db import Database

TaskType = Literal["parse", "export", "rechunk", "module_install", "qa_generate", "dataset_build"]
TaskStatus = Literal["queued", "running", "done", "failed", "canceled", "interrupted"]
Stage = Literal["convert", "parse", "ocr", "chunk", "export"]

# SSE 事件名（02 章 §2.1 /tasks/{id}/events）
EVENT_PROGRESS = "progress"          # {page, total, stage}
EVENT_STAGE_CHANGE = "stage_change"  # {stage}
EVENT_DEGRADE = "degrade"            # {page, level, reason}
EVENT_DONE = "done"
EVENT_FAILED = "failed"              # {error_code, user_message, suggestion}


@dataclass
class TaskContext:
    db: Database
    paths: Paths
    settings: Settings
    task_id: str
    doc_id: str | None
    payload: dict[str, Any]
    # progress(event_name, data)：调度器将其落库（阶段变化）并发布到 SSE 总线
    progress: Callable[[str, dict[str, Any]], None]
    # 取消检查点：runner 应在页/文件粒度轮询；返回 True 时尽快收尾抛 TaskCancelled
    cancelled: Callable[[], bool]


@dataclass
class TaskOutcome:
    status: Literal["done", "failed", "canceled"]
    error_code: str | None = None
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)   # 摘要（大结果一律落盘传路径）


class TaskCancelled(Exception):
    """runner 检测到取消标志后抛出；调度器捕获置 canceled 状态。"""
