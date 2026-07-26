"""kmod_tool —— .kmod 开发工具（keygen / pack / verify）。

仅供开发测试与未来发布签名流程使用，不进发布包（06 章 §3）：
- 正式私钥离线保管（不入库、不上 CI；密码保护 + 两处物理备份），发布时人工在离线机签名；
- 本工具生成的测试密钥只用于本地自测，公钥经环境变量 DOCFACTORY_KMOD_PUBKEY_EXTRA 注入验证。

用法：
    python scripts/kmod_tool.py keygen --out-dir keys [--name dev]
    python scripts/kmod_tool.py pack --payload-dir ./payload --manifest manifest.json \
        --key keys/dev.key --out ocr-server-zh-2.3.0.kmod
    python scripts/kmod_tool.py verify --kmod xxx.kmod [--pubkey <hex> | --pubkey-file keys/dev.pub]

pack 的 manifest 模板须含：id/name/type/version/api_version/min_app_version（changelog 可选）；
manifest_version、files（逐文件 SHA-256）、size_bytes 由工具计算填充后签名。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

# 允许在未安装 docfactory 包的情况下直接运行（scripts/ 与 src/ 平级）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from docfactory import KMOD_MANIFEST_VERSION  # noqa: E402
from docfactory.errors import DocFactoryError  # noqa: E402
from docfactory.modules.kmod import (  # noqa: E402
    ENV_EXTRA_PUBKEY,
    MANIFEST_NAME,
    PAYLOAD_PREFIX,
    SIGNATURE_NAME,
    canonical_manifest_bytes,
    verify_kmod,
)

_CHUNK = 1024 * 1024


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while chunk := fp.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- keygen


def cmd_keygen(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / f"{args.name}.key"
    pub_path = out_dir / f"{args.name}.pub"
    if key_path.exists() and not args.force:
        print(f"[keygen] 已存在 {key_path}，加 --force 覆盖", file=sys.stderr)
        return 2

    priv = Ed25519PrivateKey.generate()
    key_path.write_text(priv.private_bytes_raw().hex() + "\n", encoding="ascii")
    pub_path.write_text(priv.public_key().public_bytes_raw().hex() + "\n", encoding="ascii")
    print(f"[keygen] 私钥: {key_path}（务必离线保管，勿提交仓库/CI）")
    print(f"[keygen] 公钥: {pub_path}")
    print(f"[keygen] 公钥 hex: {pub_path.read_text(encoding='ascii').strip()}")
    print(f"[keygen] 测试时注入引擎：set {ENV_EXTRA_PUBKEY}=<公钥hex>")
    return 0


# ---------------------------------------------------------------- pack


def _build_files_entry(payload_dir: Path) -> tuple[list[dict[str, str]], int]:
    """遍历 payload 目录，产出 manifest.files（路径统一 payload/ 前缀 + POSIX 分隔符）。"""
    files: list[dict[str, str]] = []
    total = 0
    for p in sorted(payload_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(payload_dir).as_posix()
        files.append({"path": f"{PAYLOAD_PREFIX}{rel}", "sha256": _sha256_file(p)})
        total += p.stat().st_size
    if not files:
        raise SystemExit(f"[pack] payload 目录为空：{payload_dir}")
    return files, total


def cmd_pack(args: argparse.Namespace) -> int:
    payload_dir = Path(args.payload_dir)
    if not payload_dir.is_dir():
        print(f"[pack] payload 目录不存在：{payload_dir}", file=sys.stderr)
        return 2
    template = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    required = ("id", "name", "type", "version", "api_version", "min_app_version")
    missing = [k for k in required if not template.get(k)]
    if missing:
        print(f"[pack] manifest 模板缺少字段：{', '.join(missing)}", file=sys.stderr)
        return 2

    files, total = _build_files_entry(payload_dir)
    manifest = dict(template)
    manifest["manifest_version"] = KMOD_MANIFEST_VERSION
    manifest["files"] = files
    manifest["size_bytes"] = total

    # 签名对象 = 规范化后的完整 manifest（与引擎验签逐字节一致）
    priv_hex = Path(args.key).read_text(encoding="ascii").strip()
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    canonical = canonical_manifest_bytes(manifest)
    signature = priv.sign(canonical)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # manifest 直接写规范化字节：包内内容与被签内容逐字节一致，便于人工核对
        zf.writestr(MANIFEST_NAME, canonical)
        zf.writestr(SIGNATURE_NAME, signature)
        for entry in files:
            src = payload_dir / entry["path"][len(PAYLOAD_PREFIX):]
            zf.write(src, entry["path"])

    # 自检：用刚才的公钥走引擎同一条验证链路，确保产物可安装
    os.environ[ENV_EXTRA_PUBKEY] = priv.public_key().public_bytes_raw().hex()
    try:
        m = verify_kmod(out)
    except DocFactoryError as exc:
        print(f"[pack] 产物自检失败：{exc.detail or exc}", file=sys.stderr)
        return 1
    print(f"[pack] 完成: {out}")
    print(f"[pack] 模组: {m.id} v{m.version}（{m.type}），文件 {len(m.files)} 个，payload {total} 字节")
    print(f"[pack] 包 SHA-256: {_sha256_file(out)}（发布时同步公布供人工核验）")
    return 0


# ---------------------------------------------------------------- verify


def cmd_verify(args: argparse.Namespace) -> int:
    pubkey = args.pubkey
    if not pubkey and args.pubkey_file:
        pubkey = Path(args.pubkey_file).read_text(encoding="ascii").strip()
    if pubkey:
        os.environ[ENV_EXTRA_PUBKEY] = pubkey
    try:
        m = verify_kmod(Path(args.kmod))
    except DocFactoryError as exc:
        print(f"[verify] 失败: {exc.detail or exc}", file=sys.stderr)
        return 1
    print("[verify] 通过（签名 / 逐文件哈希 / 兼容性）")
    print(json.dumps(
        {
            "id": m.id, "name": m.name, "type": m.type, "version": m.version,
            "api_version": m.api_version, "min_app_version": m.min_app_version,
            "files": len(m.files),
        },
        ensure_ascii=False, indent=2,
    ))
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kmod_tool", description=".kmod 开发工具（keygen/pack/verify）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="生成 Ed25519 测试密钥对（hex 文本文件）")
    k.add_argument("--out-dir", required=True, help="输出目录")
    k.add_argument("--name", default="dev", help="密钥文件名前缀（默认 dev）")
    k.add_argument("--force", action="store_true", help="覆盖已存在的密钥文件")
    k.set_defaults(fn=cmd_keygen)

    p = sub.add_parser("pack", help="打包目录 + manifest 模板 → 签名 .kmod")
    p.add_argument("--payload-dir", required=True, help="模组内容目录（打包为 payload/）")
    p.add_argument("--manifest", required=True, help="manifest 模板 JSON（含 id/name/type/version/…）")
    p.add_argument("--key", required=True, help="Ed25519 私钥文件（hex）")
    p.add_argument("--out", required=True, help="输出 .kmod 路径")
    p.set_defaults(fn=cmd_pack)

    v = sub.add_parser("verify", help="本地验证 .kmod（默认用内置公钥，可附加测试公钥）")
    v.add_argument("--kmod", required=True, help=".kmod 文件路径")
    v.add_argument("--pubkey", help="附加测试公钥 hex")
    v.add_argument("--pubkey-file", help="附加测试公钥文件（.pub）")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
