"""父进程守望（08 章 §3.3 崩溃恢复）。

真实场景需要一个「会死掉的父进程」，所以这里用子进程模拟：起一个 python 子进程，
在它内部调 watch() 监视一个由测试掌控生死的 PID，看回调有没有按预期触发。
纯函数 _parent_alive 则直接测。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

from docfactory.parent_watch import _parent_alive, watch


def test_self_is_alive():
    assert _parent_alive(os.getpid()) is True


def test_dead_pid_reported_dead():
    """起一个立刻退出的进程，拿它已回收的 PID 判定。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    # 句柄已被 Popen 关闭，PID 此时要么不存在、要么已被复用；
    # 复用是小概率且只会让断言更严（复用后返回 True 才算失败）
    assert _parent_alive(proc.pid) is False


def test_invalid_pid_reported_dead():
    # PID 0 在 Windows 上是 System Idle Process，负数一定无效
    assert _parent_alive(-12345) is False


def test_watch_skipped_without_parent():
    """--parent-pid 缺省（0）时不启动守望：开发态手工拉引擎就是这种情况。"""
    called: list[int] = []
    assert watch(0, lambda: called.append(1)) is None
    assert not called


def test_watch_fires_immediately_when_parent_already_gone():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    fired = threading.Event()
    thread = watch(proc.pid, fired.set)
    assert thread is None  # 不值得进循环
    assert fired.is_set()


def test_watch_fires_when_parent_exits():
    """父进程活着时不动，父进程一死就触发回调。"""
    parent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        fired = threading.Event()
        thread = watch(parent.pid, fired.set)
        assert thread is not None
        # 父进程还活着：不该误触发（守望是 2s 一轮，等够一轮多）
        assert not fired.wait(3.0), "父进程仍存活时不应触发孤儿回调"
        parent.terminate()
        parent.wait(timeout=20)
        # 轮询间隔 2s，给足两轮余量
        assert fired.wait(8.0), "父进程退出后应触发孤儿回调"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


def test_callback_exception_does_not_escape():
    """回调抛异常不能把守望线程崩掉（它是 daemon 线程，崩了没人知道）。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        def boom() -> None:
            raise RuntimeError("回调故意炸")

        thread = watch(proc.pid, boom)
        assert thread is not None
        proc.terminate()
        proc.wait(timeout=20)
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_engine_accepts_parent_pid_flag():
    """CLI 参数真的存在且能解析——防止改 main.py 时把它漏掉。"""
    code = textwrap.dedent("""
        from docfactory.main import _parse_args
        args = _parse_args(["--parent-pid", "4321"])
        print(args.parent_pid)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        env={**os.environ, "DOCFACTORY_DISABLE_OFFLINE_GUARD": "1"},
    )
    assert proc.stdout.strip() == "4321", proc.stderr


def test_watch_thread_is_daemon():
    """守望线程必须是 daemon：引擎自己正常退出时不该被它拖住。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        thread = watch(proc.pid, lambda: None)
        assert thread is not None
        assert thread.daemon is True
    finally:
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0)
