""".kmod 包校验与安装/回滚（06 章）。

信任链在测试里用「临时密钥对 + 环境变量追加公钥」搭起来（kmod.ENV_EXTRA_PUBKEY），
不碰编译进主程序的正式公钥。每个用例自带一副密钥，互不影响。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from docfactory import API_VERSION, KMOD_MANIFEST_VERSION
from docfactory.config import Paths
from docfactory.db import Database
from docfactory.errors import DocFactoryError
from docfactory.modules.kmod import (
    ENV_EXTRA_PUBKEY,
    MANIFEST_NAME,
    PAYLOAD_PREFIX,
    SIGNATURE_NAME,
    assert_safe_relpath,
    canonical_manifest_bytes,
    check_compatibility,
    parse_manifest,
    parse_semver,
    verify_kmod,
)
from docfactory.modules.manager import (
    install_kmod,
    module_dir_ok,
    rollback,
    startup_check,
)


@contextmanager
def raises_detail(needle: str) -> Iterator[None]:
    """断言业务异常的**具体原因**。

    ``DocFactoryError.__str__`` 展示的是错误码注册表里的人话文案（面向用户），
    E03 下二十来种不同原因看起来都一样；具体原因在 ``.detail`` 上，测试必须盯它，
    否则「签名无效」和「版本不兼容」的用例会互相通过。
    """
    with pytest.raises(DocFactoryError) as excinfo:
        yield
    detail = excinfo.value.detail or ""
    assert needle in detail, f"期望 detail 含 {needle!r}，实际为 {detail!r}"


@pytest.fixture
def signer(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    pub_hex = key.public_key().public_bytes_raw().hex()
    monkeypatch.setenv(ENV_EXTRA_PUBKEY, pub_hex)
    return key


def build_kmod(
    dest: Path,
    key: Ed25519PrivateKey | None,
    *,
    payload: dict[str, bytes] | None = None,
    manifest_over: dict[str, Any] | None = None,
    extra_members: dict[str, bytes] | None = None,
    tamper_payload: str | None = None,
    omit: tuple[str, ...] = (),
) -> Path:
    """造一个 .kmod。tamper_payload 指定的成员在**签名之后**被改写，用来模拟篡改。"""
    payload = payload or {"model.onnx": b"fake-onnx-bytes"}
    files = [
        {"path": f"{PAYLOAD_PREFIX}{name}", "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in payload.items()
    ]
    manifest: dict[str, Any] = {
        "manifest_version": KMOD_MANIFEST_VERSION,
        "id": "ocr-hp",
        "name": "高精度 OCR",
        "type": "ocr",
        "version": "2.3",
        "api_version": API_VERSION,
        "min_app_version": "0.1.0",
        "files": files,
    }
    if manifest_over:
        manifest.update(manifest_over)
        if "files" in manifest_over:
            files = manifest_over["files"]

    signature = key.sign(canonical_manifest_bytes(manifest)) if key else b"\x00" * 64

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        if MANIFEST_NAME not in omit:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
        if SIGNATURE_NAME not in omit:
            zf.writestr(SIGNATURE_NAME, signature)
        for name, data in payload.items():
            member = f"{PAYLOAD_PREFIX}{name}"
            zf.writestr(member, b"TAMPERED" if tamper_payload == name else data)
        for name, data in (extra_members or {}).items():
            zf.writestr(name, data)
    return dest


# ---------------------------------------------------------------- 纯函数


def test_canonical_bytes_are_key_order_independent():
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1}
    assert canonical_manifest_bytes(a) == canonical_manifest_bytes(b)
    # 无空格、UTF-8 原样保留（签名双方逐字节一致的前提）
    assert canonical_manifest_bytes({"名": "值"}) == '{"名":"值"}'.encode()


@pytest.mark.parametrize("s,expected", [("1", (1, 0, 0)), ("1.2", (1, 2, 0)),
                                        ("1.2.3", (1, 2, 3)), ("1.2.3-rc1", (1, 2, 3)),
                                        ("1.2.3+build5", (1, 2, 3))])
def test_parse_semver(s: str, expected: tuple[int, int, int]):
    assert parse_semver(s) == expected


@pytest.mark.parametrize("s", ["", "a.b", "1.2.3.4", "1..2", "v1.2"])
def test_parse_semver_rejects_garbage(s: str):
    with pytest.raises(ValueError):
        parse_semver(s)


@pytest.mark.parametrize("p", ["/abs/x", "..", "a/../b", "C:/x", "a\\b", "a//b", "a/./b", ""])
def test_zip_slip_paths_rejected(p: str):
    with pytest.raises(DocFactoryError) as exc:
        assert_safe_relpath(p)
    assert exc.value.code == "E03"


def test_safe_paths_accepted():
    assert_safe_relpath("payload/models/det.onnx")


# ---------------------------------------------------------------- manifest 字段校验


def test_manifest_requires_all_fields():
    with raises_detail("缺少字段"):
        parse_manifest({"id": "x"})


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", ".hidden", "with space"])
def test_manifest_rejects_unsafe_module_id(bad_id: str):
    obj = {
        "manifest_version": KMOD_MANIFEST_VERSION, "id": bad_id, "name": "n", "type": "ocr",
        "version": "1.0", "api_version": API_VERSION, "min_app_version": "0.1.0",
        "files": [{"path": "payload/a", "sha256": "0" * 64}],
    }
    with pytest.raises(DocFactoryError):
        parse_manifest(obj)


def test_manifest_rejects_files_outside_payload():
    obj = {
        "manifest_version": KMOD_MANIFEST_VERSION, "id": "m", "name": "n", "type": "ocr",
        "version": "1.0", "api_version": API_VERSION, "min_app_version": "0.1.0",
        "files": [{"path": "sneaky.exe", "sha256": "0" * 64}],
    }
    with raises_detail("payload/"):
        parse_manifest(obj)


def test_compatibility_checks(signer, tmp_path: Path):
    good = parse_manifest(json.loads(
        zipfile.ZipFile(build_kmod(tmp_path / "a.kmod", signer)).read(MANIFEST_NAME)
    ))
    check_compatibility(good)  # 不抛即通过

    future = parse_manifest({**good.raw, "min_app_version": "99.0.0"})
    with raises_detail("请先升级应用"):
        check_compatibility(future)

    other_api = parse_manifest({**good.raw, "api_version": "9.0"})
    with raises_detail("接口版本不兼容"):
        check_compatibility(other_api)


# ---------------------------------------------------------------- 端到端验签


def test_verify_valid_kmod(signer, tmp_path: Path):
    manifest = verify_kmod(build_kmod(tmp_path / "ok.kmod", signer))
    assert manifest.id == "ocr-hp" and manifest.version == "2.3" and manifest.type == "ocr"


def test_verify_rejects_untrusted_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用一副**未登记**的密钥签名：签名结构合法但不匹配任何受信公钥。"""
    monkeypatch.delenv(ENV_EXTRA_PUBKEY, raising=False)
    rogue = Ed25519PrivateKey.generate()
    with raises_detail("签名无效"):
        verify_kmod(build_kmod(tmp_path / "rogue.kmod", rogue))


def test_verify_rejects_tampered_payload(signer, tmp_path: Path):
    """签名只覆盖 manifest，payload 完整性完全靠逐文件哈希兜住。"""
    path = build_kmod(tmp_path / "bad.kmod", signer, tamper_payload="model.onnx")
    with raises_detail("哈希不匹配"):
        verify_kmod(path)


def test_verify_rejects_unsigned_extra_payload_file(signer, tmp_path: Path):
    path = build_kmod(tmp_path / "sneak.kmod", signer,
                      extra_members={f"{PAYLOAD_PREFIX}backdoor.dll": b"evil"})
    with raises_detail("未签名文件"):
        verify_kmod(path)


@pytest.mark.parametrize("missing,pattern", [(MANIFEST_NAME, "manifest"), (SIGNATURE_NAME, "签名")])
def test_verify_rejects_missing_members(signer, tmp_path: Path, missing: str, pattern: str):
    path = build_kmod(tmp_path / "incomplete.kmod", signer, omit=(missing,))
    with raises_detail(pattern):
        verify_kmod(path)


def test_verify_rejects_non_zip(tmp_path: Path):
    path = tmp_path / "fake.kmod"
    path.write_bytes(b"this is definitely not a zip file")
    with raises_detail("zip"):
        verify_kmod(path)


def test_verify_rejects_missing_file(tmp_path: Path):
    with raises_detail("未找到"):
        verify_kmod(tmp_path / "nope.kmod")


# ---------------------------------------------------------------- 安装 / 回滚 / 自检


def test_install_extracts_and_registers(signer, tmp_path: Path, db: Database, paths: Paths):
    steps: list[tuple[int, str]] = []
    result = install_kmod(db, paths, build_kmod(tmp_path / "v23.kmod", signer),
                          on_step=lambda n, m: steps.append((n, m)))

    assert result["module_id"] == "ocr-hp" and result["version"] == "2.3"
    assert result["restart_required"] is True and result["prev_version"] is None
    assert [n for n, _ in steps] == [1, 2, 3, 4, 5]

    mdir = paths.module_dir("ocr-hp", "2.3")
    assert (mdir / MANIFEST_NAME).is_file()
    assert (mdir / PAYLOAD_PREFIX / "model.onnx").read_bytes() == b"fake-onnx-bytes"
    assert module_dir_ok(paths, "ocr-hp", "2.3")
    assert db.get_module("ocr-hp")["version"] == "2.3"
    assert list(paths.staging.iterdir()) == []           # staging 不留尸体


def test_install_new_version_sets_rollback_pointer(signer, tmp_path: Path, db: Database, paths: Paths):
    install_kmod(db, paths, build_kmod(tmp_path / "a.kmod", signer, manifest_over={"version": "2.1"}))
    result = install_kmod(db, paths, build_kmod(tmp_path / "b.kmod", signer, manifest_over={"version": "2.3"}))

    assert result["prev_version"] == "2.1"
    assert db.get_module("ocr-hp")["prev_version"] == "2.1"
    assert module_dir_ok(paths, "ocr-hp", "2.1")         # 多版本并存，旧目录保留


def test_rollback_returns_to_previous_and_clears_pointer(signer, tmp_path: Path, db: Database, paths: Paths):
    install_kmod(db, paths, build_kmod(tmp_path / "a.kmod", signer, manifest_over={"version": "2.1"}))
    install_kmod(db, paths, build_kmod(tmp_path / "b.kmod", signer, manifest_over={"version": "2.3"}))

    result = rollback(db, paths, "ocr-hp")
    assert result["version"] == "2.1" and result["rolled_back_from"] == "2.3"
    row = db.get_module("ocr-hp")
    # 清空 prev_version 防止在新旧两版间打乒乓
    assert row["version"] == "2.1" and row["prev_version"] is None

    with raises_detail("没有可回滚"):
        rollback(db, paths, "ocr-hp")


def test_rollback_unknown_module(db: Database, paths: Paths):
    with raises_detail("模组不存在"):
        rollback(db, paths, "ghost")


def test_failed_install_leaves_existing_version_untouched(signer, tmp_path: Path, db: Database, paths: Paths):
    install_kmod(db, paths, build_kmod(tmp_path / "good.kmod", signer, manifest_over={"version": "2.1"}))
    with pytest.raises(DocFactoryError):
        install_kmod(db, paths, build_kmod(tmp_path / "bad.kmod", signer,
                                           manifest_over={"version": "2.3"},
                                           tamper_payload="model.onnx"))
    assert db.get_module("ocr-hp")["version"] == "2.1"
    assert not paths.module_dir("ocr-hp", "2.3").exists()
    assert list(paths.staging.iterdir()) == []


def test_startup_check_auto_rolls_back_broken_pointer(signer, tmp_path: Path, db: Database, paths: Paths):
    import shutil

    install_kmod(db, paths, build_kmod(tmp_path / "a.kmod", signer, manifest_over={"version": "2.1"}))
    install_kmod(db, paths, build_kmod(tmp_path / "b.kmod", signer, manifest_over={"version": "2.3"}))
    shutil.rmtree(paths.module_dir("ocr-hp", "2.3"))     # 模拟目录被破坏

    issues = startup_check(db, paths)
    assert issues == [{"module_id": "ocr-hp", "action": "rolled_back", "from": "2.3", "to": "2.1"}]
    assert db.get_module("ocr-hp")["version"] == "2.1"


def test_startup_check_marks_unavailable_when_no_fallback(signer, tmp_path: Path, db: Database, paths: Paths):
    import shutil

    install_kmod(db, paths, build_kmod(tmp_path / "a.kmod", signer))
    shutil.rmtree(paths.module_dir("ocr-hp", "2.3"))

    issues = startup_check(db, paths)
    assert issues[0]["action"] == "unavailable"


def test_startup_check_is_quiet_when_healthy(signer, tmp_path: Path, db: Database, paths: Paths):
    install_kmod(db, paths, build_kmod(tmp_path / "a.kmod", signer))
    assert startup_check(db, paths) == []
