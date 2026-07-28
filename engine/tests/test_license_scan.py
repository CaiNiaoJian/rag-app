"""许可证红线扫描的回归锁（scripts/license_scan.py）。

背景：pyinstaller / pyinstaller-hooks-contrib 是 GPL-2.0（带官方 Bootloader
Exception），曾把整条 CI 染红并连带挡住 package job。修复口径：红线只管
「随发布包分发的组件」，纯构建期工具登记进 BUILD_ONLY_PACKAGES 排除集。
本文件锁两件事：排除集生效（不再误报），且被点名禁用的组件不能借道豁免。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "license_scan.py"
_spec = importlib.util.spec_from_file_location("license_scan", _SCRIPT)
assert _spec is not None and _spec.loader is not None
license_scan = importlib.util.module_from_spec(_spec)
sys.modules["license_scan"] = license_scan
_spec.loader.exec_module(license_scan)


def test_build_only_packages_do_not_violate() -> None:
    """dev 环境装着 pyinstaller（GPLv2），扫描不得因它报违规。"""
    rows, violations = license_scan.scan()
    offenders = [v for v in violations if "pyinstaller" in v.lower()]
    assert offenders == [], f"构建期依赖被误判为红线：{offenders}"
    # 环境里确实装了它（dev 组），排除是「跳过判定」而非「没扫到」
    installed = {license_scan._normalize(r["name"]) for r in rows}
    assert "pytest" in installed, "扫描应能看到 dev 环境的其他包"


def test_build_only_packages_stay_out_of_notices() -> None:
    """NOTICES 只声明随包分发的组件：构建期工具不得出现在清单里。"""
    rows, _ = license_scan.scan()
    names = {license_scan._normalize(r["name"]) for r in rows}
    for pkg in license_scan.BUILD_ONLY_PACKAGES:
        assert pkg not in names, f"{pkg} 不进发布包，不应写入 THIRD-PARTY-NOTICES"


def test_banned_package_cannot_hide_behind_build_only() -> None:
    """禁用清单优先级高于构建期豁免：两边同时登记时必须仍然拦截。

    scan() 读的是真实环境，无法安全地装一个 AGPL 包来验证，
    这里直接锁源码里的判定顺序：BANNED 分支在 BUILD_ONLY 分支之前。
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    assert src.index("in BANNED_PACKAGES") < src.index("in BUILD_ONLY_PACKAGES:"), (
        "BANNED_PACKAGES 的判定必须先于 BUILD_ONLY_PACKAGES，禁用组件不能借构建期豁免"
    )


def test_scan_passes_on_current_environment() -> None:
    """当前锁定环境整体过闸——这正是 CI 那道门禁的口径。"""
    _, violations = license_scan.scan()
    assert violations == [], "\n".join(violations)
