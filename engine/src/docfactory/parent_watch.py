"""父进程守望：主进程被强杀时引擎自我了断（08 章 §3.3「崩溃恢复」）。

Electron 主进程正常退出会先 ``POST /shutdown``，引擎优雅收尾。但用户在任务管理器里
直接结束 DocFactory.exe 时没有这个机会——子进程会被系统过继给别人，engine.exe
连同它派生的 soffice.exe 一起变成常驻孤儿：占着内存、占着端口，用户也看不到它们是谁的。

Windows 没有 POSIX 那种 ``PR_SET_PDEATHSIG``。正规做法是主进程把子进程挂进 Job Object
（``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``），但那要在 Electron 侧调 Win32 API，
等于引入一个原生模块——而 ``electron-builder.yml`` 里 ``npmRebuild: false`` 与
``buildDependenciesFromSource: false`` 正是为了避开原生模块的重编译与跨版本 ABI 负担。
所以反过来做：由子进程盯着父进程。轮询成本可忽略（2s 一次、一次几微秒），且零新依赖。

**判定方向是刻意偏保守的**：只有在能确证父进程已不存在时才收工。查询失败、权限不足、
PID 被复用这些不确定情形一律当作「父进程还活着」——留下一个孤儿只是浪费内存，
而误判会把正在给用户干活的引擎自杀掉，那是数据损失。
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

from loguru import logger

_POLL_INTERVAL_S = 2.0

# Win32 常量。用 QUERY_LIMITED_INFORMATION 而非 QUERY_INFORMATION：
# 后者在某些受保护/提权进程上会被拒，而前者是为「只想知道它死没死」设计的最小权限。
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87  # OpenProcess 对不存在的 PID 返回这个


def _parent_alive(pid: int) -> bool:
    """父进程是否仍存活。任何不确定的情形都返回 True（见模块 docstring 的保守原则）。"""
    if sys.platform != "win32":
        try:
            import os

            os.kill(pid, 0)  # 信号 0 只做存在性与权限探测，不真的送信号
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True

    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 只有「参数无效」能确定是 PID 不存在；权限不足等一律按还活着处理
        return kernel32.GetLastError() != _ERROR_INVALID_PARAMETER
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 查不出来就别下结论
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def watch(parent_pid: int, on_orphaned: Callable[[], None]) -> threading.Thread | None:
    """起一个守望线程；父进程消失时调用 ``on_orphaned``（通常是请求优雅退出）。

    返回线程对象便于测试；``parent_pid <= 0`` 时不启动并返回 None
    （开发态手工拉起引擎就属于这种情况，没有父进程需要守望）。
    """
    if parent_pid <= 0:
        return None
    if not _parent_alive(parent_pid):
        # 启动瞬间父进程就没了：不值得进循环，直接收工
        logger.warning(f"父进程 {parent_pid} 在引擎启动时已不存在，立即退出")
        on_orphaned()
        return None

    def _loop() -> None:
        while True:
            if not _parent_alive(parent_pid):
                logger.warning(f"父进程 {parent_pid} 已退出，引擎作为孤儿进程主动收工")
                try:
                    on_orphaned()
                except Exception as exc:  # 守望线程不能把异常吞回主流程
                    logger.exception(f"孤儿退出回调异常：{exc}")
                return
            if _stop.wait(_POLL_INTERVAL_S):
                return

    _stop = threading.Event()
    thread = threading.Thread(target=_loop, name="parent-watch", daemon=True)
    # daemon=True：引擎自己正常退出时不该被这个线程拖住
    thread.start()
    logger.info(f"父进程守望已启动，监视 PID {parent_pid}")
    return thread
