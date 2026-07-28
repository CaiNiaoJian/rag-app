""".kmod 包读取与校验（06 章 §1，冻结契约）。

.kmod = 重命名的 zip：
    manifest.json      元数据（id/type/version/api_version/min_app_version/files 逐文件哈希）
    signature.bin      Ed25519 签名 —— 对 manifest.json「规范化字节」签名
    payload/           模组内容（模型文件/组件等）

验证顺序（任一步失败抛 DocFactoryError，复用 E03 + 具体中文 detail，绝不半信半疑放行）：
    ① 读 manifest → ② 规范化字节 Ed25519 验签（公钥编译进主程序，预留多公钥轮换）
    → ③ 逐文件 SHA-256 对照 manifest.files（并拒绝 payload/ 下未列入清单的"私货"文件）
    → ④ 兼容性检查（manifest_version / api_version / min_app_version）

安全要点：
- 签名只覆盖 manifest，因此 payload 完整性完全依赖 files 哈希清单 → zip 内多出的
  payload 文件视为未签名内容，直接拒绝。
- 所有路径做 zip-slip 防护（拒绝绝对路径 / ".." / 盘符 / 反斜杠）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from loguru import logger

from docfactory import API_VERSION, ENGINE_VERSION, KMOD_MANIFEST_VERSION
from docfactory.errors import DocFactoryError

# ---------------------------------------------------------------- 公钥（离线信任链的根，06 章 §3）

# 编译进主程序的受信公钥列表（hex，Ed25519 32 字节）。
# 轮换规程：新版本应用同时带新旧双公钥过渡 → 下下版移除旧钥，因此这里是 list。
# 注意：下面第一枚是【开发占位公钥】——对应私钥已销毁、无人能签；发布前必须替换为
# 正式公钥（正式私钥离线保管，不入库、不上 CI）。
_DEV_PLACEHOLDER_KEY = "2b7517039416bd8adaff0d28ef68749108d6bb3376c12d7b1896ad6a1c86e86e"

PUBLIC_KEYS: list[str] = [
    _DEV_PLACEHOLDER_KEY,
]

# 测试/开发允许经环境变量追加一枚公钥（kmod_tool.py 自签自验即走此通道），
# 每次验证时实时读取，便于测试用例按需设置。
#
# **仅开发态生效**：打包产物（PyInstaller frozen）里这条通道整体失效。否则它就是信任链上
# 的一个后门——能设一个环境变量的人即可自签模组、绕过全部验签，而验签是离线分发下
# 唯一能拦住篡改包的东西。开发态保留是因为 kmod_tool.py 要自签自验。
ENV_EXTRA_PUBKEY = "DOCFACTORY_KMOD_PUBKEY_EXTRA"


def _is_frozen() -> bool:
    """是否为打包产物（与 routes_logs 的判定口径一致）。"""
    return bool(getattr(sys, "frozen", False))

# zip 内固定成员名
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "signature.bin"
PAYLOAD_PREFIX = "payload/"

# 模组类型枚举（02 章 modules 表注释）
MODULE_TYPES = ("parser", "ocr", "converter", "llm-runtime", "llm-model")

_HASH_CHUNK = 1024 * 1024  # 流式哈希块大小，避免大模型文件整块进内存


@dataclass(frozen=True)
class KmodManifest:
    """解析后的 manifest（raw 保留原始 dict，入库 manifest_json 用）。"""

    manifest_version: int
    id: str
    name: str
    type: str
    version: str
    api_version: str
    min_app_version: str
    files: list[dict[str, str]]        # [{"path": "payload/…", "sha256": "…"}]
    raw: dict[str, Any]


# ---------------------------------------------------------------- 规范化与签名


def canonical_manifest_bytes(obj: dict[str, Any]) -> bytes:
    """manifest 规范化字节（签名/验签双方必须逐字节一致，冻结契约）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def load_public_keys() -> list[Ed25519PublicKey]:
    """内置公钥（+ 开发态经环境变量追加的测试公钥）；非法条目跳过而非崩溃（保证验证流程可诊断）。"""
    hexes = list(PUBLIC_KEYS)
    frozen = _is_frozen()
    if frozen and _DEV_PLACEHOLDER_KEY in hexes:
        # 发布检查清单漏项：占位私钥已销毁，因此不是可利用漏洞，但意味着正式公钥没换上，
        # 所有正式签名的模组都会装不上。留一条明确日志，别让它只在用户报障时才浮出来。
        logger.warning("打包产物仍内置【开发占位公钥】，正式签名的模组将无法通过验签，请替换 PUBLIC_KEYS")
    extra = os.environ.get(ENV_EXTRA_PUBKEY, "").strip()
    if extra and not frozen:
        hexes.append(extra)
    keys: list[Ed25519PublicKey] = []
    for h in hexes:
        try:
            keys.append(Ed25519PublicKey.from_public_bytes(bytes.fromhex(h)))
        except (ValueError, TypeError):
            continue  # 占位/配置错误的公钥不参与验证；全部无效时验签自然失败并给出明确报错
    return keys


def verify_signature(manifest_obj: dict[str, Any], signature: bytes) -> None:
    """多公钥逐一尝试，任一命中即通过（支持轮换过渡期新旧双钥并存）。"""
    keys = load_public_keys()
    if not keys:
        raise DocFactoryError("E03", "模组签名无法验证：本程序未内置有效公钥")
    data = canonical_manifest_bytes(manifest_obj)
    for key in keys:
        try:
            key.verify(signature, data)
            return
        except InvalidSignature:
            continue
    raise DocFactoryError("E03", "模组签名无效：不匹配任何受信公钥，安装包可能被篡改或来源不明")


# ---------------------------------------------------------------- 版本比较（自写 semver，不引库）


def parse_semver(s: str) -> tuple[int, int, int]:
    """宽松 semver：'1' / '1.2' / '1.2.3'，忽略 -预发布/+构建 后缀；非法则 ValueError。"""
    core = s.strip().split("+", 1)[0].split("-", 1)[0]
    segs = core.split(".")
    if not 1 <= len(segs) <= 3 or not all(seg.isdigit() for seg in segs):
        raise ValueError(f"非法版本号: {s!r}")
    nums = [int(seg) for seg in segs] + [0, 0]
    return nums[0], nums[1], nums[2]


def _api_major(api_version: str) -> int:
    """api_version 形如 '1.x' / '1.0' / '1'，取 major 段做兼容判定。"""
    head = api_version.strip().split(".", 1)[0]
    if not head.isdigit():
        raise ValueError(f"非法 api_version: {api_version!r}")
    return int(head)


def check_compatibility(manifest: KmodManifest) -> None:
    """兼容性三查：manifest_version / api_version(major 匹配) / min_app_version<=引擎版本。"""
    if manifest.manifest_version != KMOD_MANIFEST_VERSION:
        raise DocFactoryError(
            "E03",
            f"模组格式版本不兼容：包为 manifest_version={manifest.manifest_version}，"
            f"本程序支持 {KMOD_MANIFEST_VERSION}",
        )
    try:
        kmod_major = _api_major(manifest.api_version)
        engine_major = _api_major(API_VERSION)
    except ValueError as exc:
        raise DocFactoryError("E03", f"模组版本号无法识别：{exc}") from exc
    if kmod_major != engine_major:
        raise DocFactoryError(
            "E03",
            f"模组接口版本不兼容：包要求 api_version={manifest.api_version}，"
            f"引擎为 {API_VERSION}",
        )
    try:
        need = parse_semver(manifest.min_app_version)
        have = parse_semver(ENGINE_VERSION)
    except ValueError as exc:
        raise DocFactoryError("E03", f"模组版本号无法识别：{exc}") from exc
    if need > have:
        raise DocFactoryError(
            "E03",
            f"模组要求应用版本 ≥ {manifest.min_app_version}，当前为 {ENGINE_VERSION}，请先升级应用",
        )


# ---------------------------------------------------------------- 路径安全（zip-slip 防护）


def assert_safe_relpath(path: str) -> None:
    """拒绝一切可逃逸解包目录的路径写法（绝对路径 / .. / 盘符 / 反斜杠 / 空段）。"""
    if not path or "\\" in path or ":" in path or path.startswith("/"):
        raise DocFactoryError("E03", f"模组内文件路径非法：{path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise DocFactoryError("E03", f"模组内文件路径非法：{path!r}")


# ---------------------------------------------------------------- manifest 读取与逐文件校验


def _read_manifest_obj(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = zf.read(MANIFEST_NAME)
    except KeyError:
        raise DocFactoryError("E03", "模组包损坏：缺少 manifest.json") from None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DocFactoryError("E03", f"模组包损坏：manifest.json 无法解析（{exc}）") from exc
    if not isinstance(obj, dict):
        raise DocFactoryError("E03", "模组包损坏：manifest.json 顶层必须是对象")
    return obj


def parse_manifest(obj: dict[str, Any]) -> KmodManifest:
    """字段级校验：必填字段齐全、类型正确、files 清单结构合法且不重复。"""
    required = ("manifest_version", "id", "name", "type", "version",
                "api_version", "min_app_version", "files")
    missing = [k for k in required if k not in obj]
    if missing:
        raise DocFactoryError("E03", f"manifest.json 缺少字段：{', '.join(missing)}")
    if not isinstance(obj["manifest_version"], int):
        raise DocFactoryError("E03", "manifest_version 必须是整数")
    for k in ("id", "name", "type", "version", "api_version", "min_app_version"):
        if not isinstance(obj[k], str) or not obj[k].strip():
            raise DocFactoryError("E03", f"manifest 字段 {k} 必须是非空字符串")
    # 模组 id 会成为磁盘目录名：仅允许安全字符
    mid = obj["id"]
    if not all(c.isalnum() or c in "-_." for c in mid) or mid.startswith("."):
        raise DocFactoryError("E03", f"模组 id 含非法字符：{mid!r}")
    ver = obj["version"]
    if not all(c.isalnum() or c in "-_." for c in ver) or ver.startswith("."):
        raise DocFactoryError("E03", f"模组 version 含非法字符：{ver!r}")
    if obj["type"] not in MODULE_TYPES:
        raise DocFactoryError("E03", f"未知模组类型：{obj['type']!r}（支持 {'/'.join(MODULE_TYPES)}）")

    files_raw = obj["files"]
    if not isinstance(files_raw, list) or not files_raw:
        raise DocFactoryError("E03", "manifest.files 必须是非空数组")
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files_raw:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)):
            raise DocFactoryError("E03", "manifest.files 条目必须含 path 与 sha256 字符串")
        p = item["path"]
        assert_safe_relpath(p)
        if not p.startswith(PAYLOAD_PREFIX):
            raise DocFactoryError("E03", f"manifest.files 路径必须位于 payload/ 下：{p!r}")
        if p in seen:
            raise DocFactoryError("E03", f"manifest.files 路径重复：{p!r}")
        seen.add(p)
        files.append({"path": p, "sha256": item["sha256"].lower()})

    return KmodManifest(
        manifest_version=obj["manifest_version"],
        id=mid,
        name=obj["name"],
        type=obj["type"],
        version=ver,
        api_version=obj["api_version"],
        min_app_version=obj["min_app_version"],
        files=files,
        raw=obj,
    )


def _sha256_of_member(zf: zipfile.ZipFile, name: str) -> str:
    h = hashlib.sha256()
    with zf.open(name) as fp:
        while chunk := fp.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def verify_files(zf: zipfile.ZipFile, manifest: KmodManifest) -> None:
    """逐文件 SHA-256 对照清单；payload/ 下多出的未签名文件同样判损坏。"""
    names = set(zf.namelist())
    for item in manifest.files:
        path = item["path"]
        if path not in names:
            raise DocFactoryError("E03", f"模组文件损坏：清单文件缺失 {path}")
        actual = _sha256_of_member(zf, path)
        if actual != item["sha256"]:
            raise DocFactoryError("E03", f"模组文件损坏：{path} 哈希不匹配，安装包可能被篡改或下载不完整")
    listed = {item["path"] for item in manifest.files}
    for name in names:
        if name.startswith(PAYLOAD_PREFIX) and not name.endswith("/") and name not in listed:
            raise DocFactoryError("E03", f"模组文件损坏：payload 内存在未签名文件 {name}")


# ---------------------------------------------------------------- 总入口


def verify_kmod(kmod_path: Path) -> KmodManifest:
    """完整校验一个 .kmod，返回解析后的 manifest；失败抛 DocFactoryError（含具体原因）。"""
    if not kmod_path.is_file():
        raise DocFactoryError("E03", f"未找到 .kmod 文件：{kmod_path}")
    try:
        with zipfile.ZipFile(kmod_path) as zf:
            bad = zf.testzip()  # zip 级 CRC 快速体检，能提前发现截断/损坏
            if bad is not None:
                raise DocFactoryError("E03", f"模组文件损坏：{bad} 校验失败")
            obj = _read_manifest_obj(zf)
            try:
                signature = zf.read(SIGNATURE_NAME)
            except KeyError:
                raise DocFactoryError("E03", "模组包损坏：缺少 signature.bin 签名文件") from None
            verify_signature(obj, signature)
            manifest = parse_manifest(obj)
            verify_files(zf, manifest)
            check_compatibility(manifest)
            return manifest
    except zipfile.BadZipFile as exc:
        raise DocFactoryError("E03", f"不是有效的 .kmod 包（zip 解析失败：{exc}）") from exc
