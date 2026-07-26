"""模组安装 / 回滚 / 启动自检（06 章 §2，02 章 §6）。

安装七步（拖入 .kmod 后）：
    ① 复制到 staging\\ → ② 验证（验签 + 逐文件哈希 + 兼容性，见 kmod.py）
    → ③ 解包到 modules\\{id}\\{newVer}\\（旧版本目录保留，多版本并存）
    → ④ modules 表 upsert（prev_version=旧版本，作回滚指针）
    → ⑤ 清理 staging → ⑥ 提示「重启引擎生效」（V1 不做热替换，换取确定性）
    → ⑦ 下次启动 startup_check 自检，失败自动回滚指针

失败纪律：任一步失败 → 清理 staging 与半成品目录，报具体原因，不影响现有版本。
"""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docfactory.config import Paths
from docfactory.db import Database
from docfactory.errors import DocFactoryError
from docfactory.modules.kmod import (
    MANIFEST_NAME,
    PAYLOAD_PREFIX,
    SIGNATURE_NAME,
    KmodManifest,
    assert_safe_relpath,
    verify_kmod,
)
from docfactory.taskspec import EVENT_PROGRESS, TaskCancelled, TaskContext, TaskOutcome

# 安装步骤总数（进度上报用；⑥⑦分别是提示与下次启动的事，不占进度格）
_TOTAL_STEPS = 5

# 磁盘预检系数：staging 副本 + 解包产物 ≈ 2 倍包大小，×3 留余量（E07 事前预防精神，02 章 §3）
_DISK_FACTOR = 3


def _rmtree_quiet(path: Path) -> None:
    """尽力删除目录/文件，失败不抛（清理路径永远不该掩盖真正的错误）。"""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _extract_kmod(kmod_path: Path, dest: Path) -> None:
    """解包 manifest.json + signature.bin + payload/ 到模组版本目录。

    保留包内原始布局，使安装目录可被再次完整验证（startup_check 读 manifest 即基于此）。
    路径安全已在 verify 阶段整体把关，这里对每个成员再防一手 zip-slip。
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(kmod_path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if name not in (MANIFEST_NAME, SIGNATURE_NAME) and not name.startswith(PAYLOAD_PREFIX):
                continue  # 包内其他杂项一律不落盘
            assert_safe_relpath(name)
            target = dest / Path(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)


def _check_cancel(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise TaskCancelled()


def install_kmod(
    db: Database,
    paths: Paths,
    kmod_path: Path,
    *,
    on_step: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """执行安装七步流程（同步）；返回安装摘要（含「重启引擎生效」提示）。

    on_step(step, 描述)：步骤粒度进度回调；cancelled：取消检查点（步骤间轮询）。
    """
    kmod_path = Path(kmod_path)
    if not kmod_path.is_file():
        raise DocFactoryError("E03", f"未找到 .kmod 文件：{kmod_path}")

    def step(n: int, message: str) -> None:
        if on_step is not None:
            on_step(n, message)

    # 磁盘预检（E07 事前预防）：staging 副本 + 解包产物都在数据根目录所在盘
    size = kmod_path.stat().st_size
    paths.staging.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.root).free
    if free < size * _DISK_FACTOR:
        raise DocFactoryError(
            "E07",
            f"安装模组需要约 {size * _DISK_FACTOR // (1024 * 1024)} MB 空间，"
            f"当前剩余 {free // (1024 * 1024)} MB",
        )

    # ① 复制到 staging（唯一文件名，避免并发/重名互踩）
    step(1, "复制安装包")
    staged = paths.staging / f"{uuid.uuid4().hex}-{kmod_path.name}"
    extract_tmp: Path | None = None
    try:
        shutil.copy2(kmod_path, staged)
        _check_cancel(cancelled)

        # ② 验证：验签 → 逐文件哈希 → 兼容性（任一失败抛 DocFactoryError）
        step(2, "验证签名与完整性")
        manifest: KmodManifest = verify_kmod(staged)
        _check_cancel(cancelled)

        # ③ 解包到 modules/{id}/{version}/（先落临时目录，成功后原子换名；旧版本目录保留）
        step(3, "解包模组文件")
        final_dir = paths.module_dir(manifest.id, manifest.version)
        pre_existing = final_dir.exists()
        extract_tmp = paths.modules / manifest.id / f".tmp-{uuid.uuid4().hex}"
        _extract_kmod(staged, extract_tmp)
        _check_cancel(cancelled)
        if pre_existing:
            # 重装同版本：换出旧目录再换入新目录（重启生效前不会被读取，窗口安全）
            _rmtree_quiet(final_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        extract_tmp.replace(final_dir)
        extract_tmp = None

        # ④ modules 表 upsert：prev_version 指向被替换的旧版本（同版本重装则沿用原指针）
        step(4, "登记模组版本")
        old = db.get_module(manifest.id)
        if old and old.get("version") and old["version"] != manifest.version:
            prev_version: str | None = old["version"]
        elif old:
            prev_version = old.get("prev_version")
        else:
            prev_version = None
        try:
            db.upsert_module(
                id=manifest.id,
                name=manifest.name,
                type=manifest.type,
                version=manifest.version,
                manifest=manifest.raw,
                prev_version=prev_version,
            )
        except Exception:
            # 登记失败：若版本目录是本次新建的，撤掉半成品，保证"不影响现有版本"
            if not pre_existing:
                _rmtree_quiet(final_dir)
            raise

        # ⑤ 清理 staging
        step(5, "清理临时文件")
        _rmtree_quiet(staged)

        # ⑥ 重启引擎生效（V1 不热替换）；⑦ 由下次启动的 startup_check 兜底
        return {
            "module_id": manifest.id,
            "name": manifest.name,
            "type": manifest.type,
            "version": manifest.version,
            "prev_version": prev_version,
            "restart_required": True,
            "message": "安装完成，重启引擎后生效",
        }
    finally:
        # 无论成败，staging 与解包临时目录都不留尸体
        _rmtree_quiet(staged)
        if extract_tmp is not None:
            _rmtree_quiet(extract_tmp)


# ---------------------------------------------------------------- 回滚


def rollback(db: Database, paths: Paths, module_id: str) -> dict[str, Any]:
    """手动回滚：版本指针退回 prev_version（旧目录仍在磁盘，无需搬文件）。

    回滚后 prev_version 清空——避免再次回滚在新旧两版间打乒乓；
    如需回到新版，重新安装对应 .kmod 即可（目录还在，仅重新登记，秒完成）。
    """
    row = db.get_module(module_id)
    if row is None:
        raise DocFactoryError("E03", f"模组不存在：{module_id}")
    prev = row.get("prev_version")
    if not prev:
        raise DocFactoryError("E03", f"模组 {module_id} 没有可回滚的上一版本")
    if not module_dir_ok(paths, module_id, prev):
        raise DocFactoryError("E03", f"模组 {module_id} 的上一版本 v{prev} 目录已缺失，无法回滚")
    db.set_module_version(module_id, prev, None)
    db.log_event(
        level="info",
        message=f"模组 {module_id} 已回滚：v{row.get('version')} → v{prev}，重启引擎后生效",
        detail={"module_id": module_id, "from": row.get("version"), "to": prev},
    )
    return {
        "module_id": module_id,
        "version": prev,
        "rolled_back_from": row.get("version"),
        "restart_required": True,
        "message": f"已回滚到 v{prev}，重启引擎后生效",
    }


# ---------------------------------------------------------------- 启动自检（安装第⑦步）


def module_dir_ok(paths: Paths, module_id: str, version: str) -> bool:
    """自检标准：版本目录存在 + manifest.json 可读可解析 + id 对得上。"""
    mdir = paths.module_dir(module_id, version)
    manifest_path = mdir / MANIFEST_NAME
    if not mdir.is_dir() or not manifest_path.is_file():
        return False
    try:
        obj = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(obj, dict) and obj.get("id") == module_id


def startup_check(db: Database, paths: Paths) -> list[dict[str, Any]]:
    """引擎启动自检：启用中的模组目录/清单校验，失败自动回滚指针并记 warning。

    返回处置清单（供启动日志汇总与 UI 提示）：
        {"module_id", "action": "rolled_back"|"unavailable", ...}
    """
    issues: list[dict[str, Any]] = []
    for row in db.list_modules():
        if not row.get("enabled"):
            continue
        mid, ver, prev = row["id"], row["version"], row.get("prev_version")
        if module_dir_ok(paths, mid, ver):
            continue
        if prev and module_dir_ok(paths, mid, prev):
            # 自动回滚指针（清 prev_version，防循环回滚）
            db.set_module_version(mid, prev, None)
            db.log_event(
                level="warning",
                message=f"模组 {mid} v{ver} 启动自检失败，已自动回滚到 v{prev}",
                detail={"module_id": mid, "from": ver, "to": prev, "action": "rolled_back"},
            )
            issues.append({"module_id": mid, "action": "rolled_back", "from": ver, "to": prev})
        else:
            db.log_event(
                level="warning",
                message=f"模组 {mid} v{ver} 启动自检失败且无可用回滚版本，该模组暂不可用，建议重新安装",
                detail={"module_id": mid, "version": ver, "action": "unavailable"},
            )
            issues.append({"module_id": mid, "action": "unavailable", "version": ver})
    return issues


# ---------------------------------------------------------------- 任务 runner（调度器按 type 延迟 import）


def run_install(ctx: TaskContext) -> TaskOutcome:
    """module_install 任务入口；payload: {"kmod_path": "绝对路径"}。

    失败清理由 install_kmod 内部兜底（staging/半成品目录），不影响现有版本；
    DocFactoryError 向上抛，由调度器统一映射 error_code 与 task_events。
    """
    kmod_path = str(ctx.payload.get("kmod_path") or "").strip()
    if not kmod_path:
        raise DocFactoryError("E03", "缺少参数 kmod_path（.kmod 文件的绝对路径）")

    def on_step(step: int, message: str) -> None:
        # 复用 SSE progress 事件形状 {page,total,stage}：步骤号当页号，stage 固定 install
        ctx.progress(EVENT_PROGRESS, {
            "page": step, "total": _TOTAL_STEPS, "stage": "install", "message": message,
        })

    result = install_kmod(
        ctx.db, ctx.paths, Path(kmod_path),
        on_step=on_step, cancelled=ctx.cancelled,
    )
    ctx.db.log_event(
        level="info",
        task_id=ctx.task_id,
        message=f"模组 {result['module_id']} v{result['version']} 安装完成，重启引擎后生效",
        detail=result,
    )
    return TaskOutcome(status="done", message=result["message"], result=result)
