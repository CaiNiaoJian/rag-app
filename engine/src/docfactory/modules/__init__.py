"""模组系统（.kmod）：读取校验（kmod）与安装/回滚/启动自检（manager）。

契约来源：docs/06 §1~§3、docs/02 §6。
对外暴露最常用入口，便于 api 层与调度器延迟 import。
"""

from docfactory.modules.kmod import KmodManifest, verify_kmod
from docfactory.modules.manager import install_kmod, rollback, run_install, startup_check

__all__ = [
    "KmodManifest",
    "verify_kmod",
    "install_kmod",
    "rollback",
    "run_install",
    "startup_check",
]
