"""LibreOffice 归一化层：doc/ppt/xls → docx/pptx/xlsx（03 章 §4）。

工程约束直接决定了这个文件的形状：

- **LibreOffice 非线程安全**，同一台机器上并发跑多个 soffice 会互相踩用户配置文件。
  引擎的 worker 是多线程的（scheduler.parallel_tasks），所以这里用**进程级全局锁**
  把所有转换串成单实例队列；转换本身是 IO/CPU 混合的秒级操作，串行不是瓶颈。
- **独立 user profile**（``-env:UserInstallation``）：不碰用户自己安装的 LibreOffice 配置，
  也避免「上次异常退出留下的恢复对话框」把 headless 进程卡住。profile 用完即删。
- **120s 超时 kill 重试 1 次**：soffice 卡死是已知常态（字体扫描、损坏文档），
  kill 必须连子进程一起（soffice.exe 只是启动器，真正干活的是 soffice.bin）。
- 转换成功写 ``convert_chain``（形如 ``doc->docx(libreoffice)``），失败一律 E03。

许可：LibreOffice 为 MPL-2.0，**以独立进程调用**（非链接），不触发 copyleft 义务；
随包附版权声明与源码获取声明（03 章 §4）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from contextlib import suppress
from pathlib import Path

from loguru import logger

from docfactory.errors import DocFactoryError

# 全局单实例锁：整个引擎进程内同一时刻只允许一个 soffice 在跑
_CONVERT_LOCK = threading.Lock()

CONVERT_TIMEOUT_S = 120.0
_MAX_ATTEMPTS = 2  # 首次 + 超时后重试 1 次

# 环境变量覆盖（开发机 / 用户自带安装位置）
_ENV_KEY = "DOCFACTORY_SOFFICE"

# 安装目录内置位置：engine/src/docfactory/parsers/ → 上溯到应用根，再进 resources/
_BUNDLED_RELATIVE = Path("resources") / "libreoffice" / "program" / "soffice.exe"

_IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def _bundled_candidates() -> list[Path]:
    """内置 LibreOffice 的候选位置。

    打包后（PyInstaller onedir）引擎 exe 在 ``resources/engine/`` 下，
    LibreOffice 在 ``resources/libreoffice/``；开发态则从仓库根往下找，
    两种布局都试一遍，避免开发/生产两套逻辑。
    """
    roots: list[Path] = []
    exe_dir = Path(sys.executable).resolve().parent
    roots.extend([exe_dir, *exe_dir.parents[:3]])
    here = Path(__file__).resolve()
    roots.extend(here.parents[:6])
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        cand = root / _BUNDLED_RELATIVE
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def find_soffice() -> Path | None:
    """按「环境变量 → 内置目录 → PATH」顺序查找 soffice 可执行文件。

    顺序是刻意的：开发机可以用环境变量指到任意版本；生产优先用随包裁剪版
    （版本可控、已剔除 GPL 组件）；最后才退到用户自装的 LibreOffice。
    """
    env_path = os.environ.get(_ENV_KEY, "").strip().strip('"')
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    for cand in _bundled_candidates():
        if cand.is_file():
            return cand

    from shutil import which

    for name in ("soffice.exe", "soffice", "soffice.com"):
        hit = which(name)
        if hit:
            return Path(hit)
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """连子进程一起杀：soffice.exe 会派生 soffice.bin，只 kill 父进程会留下僵尸。"""
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass  # taskkill 不可用（极少见）时退回下面的 kill
    with suppress(OSError):
        proc.kill()


def _run_soffice(args: list[str], timeout_s: float) -> tuple[int, str]:
    """跑一次 soffice，返回 (退出码, 合并输出)；超时抛 TimeoutExpired（已 kill 进程树）。"""
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, (out or "").strip()
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        with suppress(subprocess.TimeoutExpired, ValueError, OSError):
            proc.communicate(timeout=10)  # 回收管道，避免句柄泄漏
        raise


def convert_to_ooxml(
    src: Path,
    *,
    out_dir: Path,
    target_ext: str,
    timeout_s: float = CONVERT_TIMEOUT_S,
) -> tuple[Path, str]:
    """把旧格式文件转成 OOXML，返回 (转换后文件路径, convert_chain 条目)。

    ``out_dir`` 由调用方提供（parse_document 用 staging 下的临时目录，解析结束即删），
    user profile 建在 out_dir 内，跟着一起清理，不会在数据目录里长期堆垃圾。
    """
    src = Path(src)
    src_ext = src.suffix.lower().lstrip(".")
    soffice = find_soffice()
    if soffice is None:
        produced = _fallback_convert(src, out_dir, src_ext, target_ext)
        if produced is not None:
            return produced
        raise DocFactoryError(
            "E03",
            f"未内置 LibreOffice，无法转换 .{src_ext} 旧格式文件。"
            f"请在原程序中另存为 .{target_ext} 后重新导入，"
            f"或设置环境变量 {_ENV_KEY} 指向 soffice 可执行文件",
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        # 每次尝试都用全新 profile：上一次卡死可能已经把 profile 写坏了
        profile = out_dir / f"lo-profile-{uuid.uuid4().hex[:8]}"
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            str(soffice),
            "--headless",
            "--norestore",      # 不弹「文档恢复」向导（headless 下会静默卡住）
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--invisible",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to",
            target_ext,
            "--outdir",
            str(out_dir),
            str(src.resolve()),
        ]
        try:
            with _CONVERT_LOCK:  # 单实例队列
                code, output = _run_soffice(args, timeout_s)
        except subprocess.TimeoutExpired:
            last_error = f"转换超时（>{timeout_s:.0f}s）"
            logger.warning(f"LibreOffice 转换超时，第 {attempt}/{_MAX_ATTEMPTS} 次：{src.name}")
            continue
        except OSError as exc:
            raise DocFactoryError("E03", f"无法启动 LibreOffice：{exc}") from exc

        produced = _find_output(out_dir, src.stem, target_ext)
        if produced is not None:
            return produced, f"{src_ext}->{target_ext}(libreoffice)"

        last_error = f"退出码 {code}；输出：{output[:300] or '(无)'}"
        logger.warning(f"LibreOffice 未产出目标文件，第 {attempt}/{_MAX_ATTEMPTS} 次：{last_error}")

    # soffice 两次都没成：.xls 还有 xlrd 这条纯 Python 退路可试。
    # 兜底自身的报错不能掩盖主路径的失败原因，所以这里只接「读得出来」的情况。
    try:
        produced = _fallback_convert(src, out_dir, src_ext, target_ext)
    except DocFactoryError:
        produced = None
    if produced is not None:
        logger.warning(f"LibreOffice 转换失败，已用 xlrd 兜底完成：{src.name}")
        return produced

    raise DocFactoryError(
        "E03",
        f"旧格式转换失败：{src.name} → .{target_ext}（{last_error}）。"
        f"可在原程序中另存为 .{target_ext} 后重新导入",
    )


def _fallback_convert(
    src: Path, out_dir: Path, src_ext: str, target_ext: str
) -> tuple[Path, str] | None:
    """无 LibreOffice 时的纯 Python 备选路径；当前仅覆盖 .xls（xlrd，BSD-3）。

    .doc/.ppt 没有等价的可商用纯 Python 方案，返回 None 走原 E03 报错。
    convert_chain 的 ``(xlrd)`` 后缀是溯源标记：图片/图表不经此路保留（模块 docstring）。
    """
    if src_ext != "xls" or target_ext != "xlsx":
        return None
    from docfactory.parsers.xls_compat import convert_xls_to_xlsx

    produced = convert_xls_to_xlsx(src, Path(out_dir))
    return produced, f"{src_ext}->{target_ext}(xlrd)"


def _find_output(out_dir: Path, stem: str, target_ext: str) -> Path | None:
    """定位转换产物：优先同名文件，其次目录内任意同扩展名文件。

    LibreOffice 对特殊字符文件名会做转义，同名匹配不一定命中；
    out_dir 是本次转换专用的临时目录，兜底扫描不会误取到别的文件。
    """
    exact = out_dir / f"{stem}.{target_ext}"
    if exact.is_file() and exact.stat().st_size > 0:
        return exact
    for cand in sorted(out_dir.glob(f"*.{target_ext}")):
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    return None
