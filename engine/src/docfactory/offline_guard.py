"""运行时离线闸（02 章 §7 引擎侧强制）。

约束：「不能联网」= 阻断一切非回环 socket 连接；127.0.0.0/8、::1、localhost 放行，
供 UI↔引擎↔未来本地模型服务（均走本机回环）使用。
实现：monkeypatch socket 连接类调用（connect / connect_ex / sendto），目标非回环
直接抛 OSError（前缀固定 "offline guard: blocked"）——白名单机制，宁可误杀不可漏放。
进程入口应在任何网络组件初始化之前调用 install()。

环境变量 DOCFACTORY_DISABLE_OFFLINE_GUARD=1 可跳过安装，**但仅在开发态生效**：
打包产物（PyInstaller frozen）里这条通道整体失效，见 install()。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from typing import Any

# 已登记的本地服务端口（allow_port 供未来本地模型运行时注册监听端口）。
# 回环地址本就整体放行，此表当前仅作登记；为将来「回环也按端口收紧」预留扩展点。
_allowed_ports: set[int] = set()
_installed: bool = False

ENV_DISABLE = "DOCFACTORY_DISABLE_OFFLINE_GUARD"


def _is_frozen() -> bool:
    """是否为打包产物（与 modules/kmod.py、routes_logs.py 的判定口径一致）。"""
    return bool(getattr(sys, "frozen", False))


def allow_port(port: int) -> None:
    """登记本地服务端口（如未来 llama.cpp 运行时的 127.0.0.1:{port}）。"""
    _allowed_ports.add(int(port))


def allowed_ports() -> set[int]:
    """当前登记端口的快照（诊断用）。"""
    return set(_allowed_ports)


def _host_is_loopback(host: str) -> bool:
    """判定主机是否回环：IP 字面量看地址段；非 IP 主机名仅认 localhost（离线环境不做 DNS）。"""
    h = host.strip().strip("[]").split("%", 1)[0].lower()
    if h in ("", "localhost") or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # 非 IP 主机名一律视为外网目标：解析它本身就意味着要出网
        return False
    if ip.is_loopback or ip.is_unspecified:
        return True
    # IPv4-mapped IPv6（::ffff:127.0.0.1）的 is_loopback 为 False，需拆出内层判定
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped is not None and (mapped.is_loopback or mapped.is_unspecified))


def _address_allowed(address: Any) -> bool:
    # AF_UNIX / 文件路径套接字：本机内通信，放行
    if isinstance(address, (str, bytes, os.PathLike)):
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            try:
                host = host.decode("utf-8", "ignore")
            except Exception:
                return False
        if isinstance(host, str):
            return _host_is_loopback(host)
        return False
    # 结构未知：按最严处理（离线优先于兼容）
    return False


def install(allowed_ports: set[int] | None = None) -> None:
    """安装离线闸（幂等）。allowed_ports 会并入本地端口登记表。"""
    global _installed
    if allowed_ports:
        _allowed_ports.update(int(p) for p in allowed_ports)
    # 逃生门**只在开发态存在**。原先仅靠「生产构建不设置该变量」这个约定，可它拦不住
    # 任何人：环境变量是用户可写的，而主进程 spawn 引擎时把 {...process.env} 整个传下去
    # （app/src/main/engine-supervisor.ts），于是在目标机器上设一个系统环境变量就能让
    # FR-17「代码层面强制禁止外联」这条硬约束彻底失效——而这正是本产品面向涉密内网的立身之本。
    # 打包产物里直接无视该变量：测试要关闸，跑的是源码，不受影响。
    if not _is_frozen() and os.environ.get(ENV_DISABLE) == "1":
        return
    if _installed:
        return
    _installed = True

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto

    def _check(address: Any) -> None:
        if not _address_allowed(address):
            raise OSError(f"offline guard: blocked -> {address!r}")

    def guarded_connect(self: socket.socket, address: Any) -> None:
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    def guarded_sendto(self: socket.socket, *args: Any) -> int:
        # sendto(data[, flags], address)：地址恒为最后一个参数；UDP 无连接发送同样受闸
        if args:
            _check(args[-1])
        return real_sendto(self, *args)

    socket.socket.connect = guarded_connect        # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = guarded_sendto          # type: ignore[method-assign]
