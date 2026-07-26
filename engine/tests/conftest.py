"""pytest 公共装置。

纪律：
- 每个测试用独立 tmp 数据根（DOCFACTORY_DATA_DIR 语义），互不污染；
- 测试进程内**关闭离线闸**（DOCFACTORY_DISABLE_OFFLINE_GUARD=1），否则 httpx/TestClient
  连本机随机端口时会被误伤——离线闸本身的行为由 test_offline_guard.py 单独覆盖；
- 不触碰用户真实数据目录（%LOCALAPPDATA%\\DocFactory）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# 必须早于任何 docfactory 模块导入：offline_guard 在 import 期就可能被 main 装上
os.environ.setdefault("DOCFACTORY_DISABLE_OFFLINE_GUARD", "1")

from docfactory.app import SettingsHolder  # noqa: E402
from docfactory.config import Paths, Settings  # noqa: E402
from docfactory.db import Database  # noqa: E402
from docfactory.scheduler import Scheduler  # noqa: E402


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path / "DocFactory")
    p.ensure()
    return p


@pytest.fixture
def db(paths: Paths) -> Database:
    database = Database(paths.db_path)
    database.migrate(backup_dir=paths.backup)
    return database


@pytest.fixture
def settings_holder(paths: Paths) -> SettingsHolder:
    # 并发度压到 1：测试要的是确定性，不是吞吐
    return SettingsHolder(paths, Settings(parallel_tasks=1))


@pytest.fixture
def scheduler(db: Database, paths: Paths, settings_holder: SettingsHolder) -> Iterator[Scheduler]:
    sched = Scheduler(db, paths, settings_holder.get)
    yield sched
    sched.stop(timeout=5.0)


TEST_TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def token() -> str:
    return TEST_TOKEN


@pytest.fixture
def app(db: Database, paths: Paths, settings_holder: SettingsHolder, scheduler: Scheduler):
    """装配好的 FastAPI 实例；**不启动调度器** —— API 测试关心的是端点契约，
    任务是否真跑由 test_scheduler.py 覆盖，混在一起只会让用例变慢且不确定。"""
    from docfactory.app import create_app

    shutdown_calls: list[int] = []
    application = create_app(
        db=db, paths=paths, settings=settings_holder, scheduler=scheduler,
        token=TEST_TOKEN, request_shutdown=lambda: shutdown_calls.append(1),
    )
    application.state.shutdown_calls = shutdown_calls
    return application


@pytest.fixture
def client(app) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        yield c


@pytest.fixture
def anon_client(app) -> Iterator[Any]:
    """不带凭据的客户端，用于验证鉴权闸。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
