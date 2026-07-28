"""运行时离线闸（02 章 §7）。

判定逻辑用纯函数直接覆盖；``install()`` 会永久 monkeypatch socket 类方法，
放在测试进程里会污染后续用例（httpx/uvicorn 都要连本机），故改用**子进程**验证真实拦截效果。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from docfactory.offline_guard import _address_allowed, _host_is_loopback, allow_port, allowed_ports


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.10.20.30", "::1", "localhost", "LOCALHOST", "app.localhost",
     "0.0.0.0", "::", "::ffff:127.0.0.1", "[::1]", "::1%0", ""],
)
def test_loopback_hosts_allowed(host: str):
    assert _host_is_loopback(host) is True


@pytest.mark.parametrize(
    "host",
    ["8.8.8.8", "192.168.1.10", "10.0.0.1", "api.anthropic.com", "example.com",
     "2001:4860:4860::8888", "::ffff:8.8.8.8", "169.254.1.1"],
)
def test_external_hosts_blocked(host: str):
    """内网地址同样拦截：白名单机制宁可误杀不可漏放。"""
    assert _host_is_loopback(host) is False


def test_address_shapes():
    assert _address_allowed(("127.0.0.1", 8080)) is True
    assert _address_allowed((b"127.0.0.1", 8080)) is True
    assert _address_allowed(("8.8.8.8", 53)) is False
    assert _address_allowed("/tmp/app.sock") is True          # AF_UNIX：本机内通信
    assert _address_allowed(12345) is False                    # 结构未知 → 按最严处理
    assert _address_allowed(()) is False


def test_allow_port_registry():
    allow_port(51234)
    assert 51234 in allowed_ports()
    # 返回的是快照，外部改动不影响内部表
    snapshot = allowed_ports()
    snapshot.add(999)
    assert 999 not in allowed_ports()


def _run_guarded(body: str) -> subprocess.CompletedProcess:
    code = textwrap.dedent(f"""
        import os, socket
        os.environ.pop("DOCFACTORY_DISABLE_OFFLINE_GUARD", None)
        from docfactory import offline_guard
        offline_guard.install()
        {body}
    """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_install_blocks_external_connect():
    proc = _run_guarded("""
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("8.8.8.8", 53))
            print("NOT_BLOCKED")
        except OSError as exc:
            print("BLOCKED" if "offline guard: blocked" in str(exc) else f"OTHER:{exc}")
    """)
    assert "BLOCKED" in proc.stdout, proc.stderr


def test_install_allows_loopback_connect():
    """回环必须放行——UI↔引擎↔未来本地模型服务全走 127.0.0.1。"""
    proc = _run_guarded("""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        c = socket.socket()
        c.settimeout(3)
        try:
            c.connect(("127.0.0.1", port))
            print("CONNECTED")
        except OSError as exc:
            print(f"BLOCKED_WRONGLY:{exc}")
        finally:
            c.close(); srv.close()
    """)
    assert "CONNECTED" in proc.stdout, proc.stderr


def test_install_blocks_udp_sendto():
    proc = _run_guarded("""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"x", ("8.8.8.8", 53))
            print("NOT_BLOCKED")
        except OSError as exc:
            print("BLOCKED" if "offline guard: blocked" in str(exc) else f"OTHER:{exc}")
    """)
    assert "BLOCKED" in proc.stdout, proc.stderr


def test_disable_env_var_skips_installation():
    """测试逃生门必须真的生效，否则整个测试套件跑不起来。"""
    code = textwrap.dedent("""
        import os, socket
        os.environ["DOCFACTORY_DISABLE_OFFLINE_GUARD"] = "1"
        from docfactory import offline_guard
        before = socket.socket.connect
        offline_guard.install()
        print("UNPATCHED" if socket.socket.connect is before else "PATCHED")
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert "UNPATCHED" in proc.stdout, proc.stderr


def test_disable_env_var_ignored_when_frozen():
    """打包产物里逃生门必须失效。

    这条守的是 FR-17「代码层面强制禁止外联」：环境变量是用户可写的，而主进程
    spawn 引擎时会把整个 process.env 传下去，所以「约定生产不设这个变量」根本
    不是防线。sys.frozen 由 PyInstaller 的 bootloader 注入，这里手动置上以模拟
    打包态——闸必须照装不误。
    """
    code = textwrap.dedent("""
        import os, socket, sys
        os.environ["DOCFACTORY_DISABLE_OFFLINE_GUARD"] = "1"
        sys.frozen = True          # 模拟 PyInstaller 打包产物
        from docfactory import offline_guard
        before = socket.socket.connect
        offline_guard.install()
        if socket.socket.connect is before:
            print("ESCAPED")      # 逃生门在打包态仍生效 —— 硬约束失守
        else:
            s = socket.socket(); s.settimeout(3)
            try:
                s.connect(("8.8.8.8", 53)); print("NOT_BLOCKED")
            except OSError as exc:
                print("BLOCKED" if "offline guard: blocked" in str(exc) else f"OTHER:{exc}")
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert "BLOCKED" in proc.stdout, proc.stderr
