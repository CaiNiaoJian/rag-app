"""settings.json 的加载韧性（02 章 §3；覆盖安装兼容旧配置，issue #4 评论）。

升级路径的核心承诺：新版本引擎读旧版本的 settings.json，个别字段对不上号
（新版收紧/更名了枚举值）时**只有该字段回默认**，用户其余的自定义全部保留；
只有文件整体损坏（半写入/非 JSON）才整份回默认。
"""

from __future__ import annotations

import json

from docfactory.config import Paths, Settings, load_settings, save_settings


def _write_raw(paths: Paths, obj: object) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text(
        obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False),
        encoding="utf-8",
    )


def test_missing_file_returns_defaults(paths: Paths):
    assert load_settings(paths) == Settings()


def test_corrupt_file_returns_defaults(paths: Paths):
    _write_raw(paths, '{"ocr_mode": "on", "chunk": {')  # 半写入
    assert load_settings(paths) == Settings()


def test_roundtrip(paths: Paths):
    s = Settings(ocr_mode="off", output_dir="D:/out")
    save_settings(paths, s)
    assert load_settings(paths) == s


def test_old_settings_with_unknown_fields_still_load(paths: Paths):
    """旧版本可能带有新版已删除的字段：直接忽略，不报错不重置。"""
    s = Settings(page_timeout_s=60)
    raw = json.loads(s.model_dump_json())
    raw["legacy_removed_option"] = {"whatever": 1}
    _write_raw(paths, raw)
    assert load_settings(paths) == s


def test_single_invalid_field_salvages_the_rest(paths: Paths):
    """一个字段非法（如旧枚举值被移除）只丢该字段，其余自定义原样保留。"""
    raw = json.loads(Settings().model_dump_json())
    raw["ocr_mode"] = "ultra"                 # 假想的已废弃枚举值
    raw["output_dir"] = "D:/my-exports"       # 用户自定义，必须活下来
    raw["chunk"]["target_tokens"] = 768       # 用户自定义，必须活下来
    _write_raw(paths, raw)

    loaded = load_settings(paths)
    assert loaded.ocr_mode == Settings().ocr_mode          # 非法字段回默认
    assert loaded.output_dir == "D:/my-exports"
    assert loaded.chunk.target_tokens == 768


def test_invalid_group_falls_back_alone(paths: Paths):
    """组内字段非法时该组整体回默认（组粒度契约），顶层其余字段不受牵连。"""
    raw = json.loads(Settings().model_dump_json())
    raw["dataset"] = {"format": "not-a-format"}
    raw["parallel_tasks"] = 2
    _write_raw(paths, raw)

    loaded = load_settings(paths)
    assert loaded.dataset == Settings().dataset
    assert loaded.parallel_tasks == 2
