"""数据目录布局（02 章 §3）与用户设置（05 章 §5 处理选项面）。

根目录：%LOCALAPPDATA%\\DocFactory\\；开发/测试用环境变量 DOCFACTORY_DATA_DIR 覆盖。
设置持久化为 data\\settings.json（原子写）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


def default_data_root() -> Path:
    env = os.environ.get("DOCFACTORY_DATA_DIR")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "DocFactory"


@dataclass(frozen=True)
class Paths:
    """磁盘目录布局（冻结契约，02 章 §3）。"""

    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data / "app.db"

    @property
    def settings_path(self) -> Path:
        return self.data / "settings.json"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def modules(self) -> Path:
        return self.root / "modules"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def backup(self) -> Path:
        return self.root / "backup"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    # ---- 每文档 workspace 子目录 ----

    def doc_dir(self, doc_id: str) -> Path:
        return self.workspace / doc_id

    def doc_parsed(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "parsed"

    def doc_ir_path(self, doc_id: str) -> Path:
        return self.doc_parsed(doc_id) / "doc.ir.json"

    def doc_md_path(self, doc_id: str) -> Path:
        return self.doc_parsed(doc_id) / "doc.md"

    def doc_assets(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "assets"

    def doc_preview(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "preview"

    def doc_exports(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "exports"

    def module_dir(self, module_id: str, version: str) -> Path:
        return self.modules / module_id / version

    def ensure(self) -> None:
        for p in (self.data, self.workspace, self.models, self.modules,
                  self.staging, self.backup, self.logs):
            p.mkdir(parents=True, exist_ok=True)


# ---------------- 用户设置（05 章 §5，默认值即最佳实践） ----------------


class ChunkSettings(BaseModel):
    """切片参数（04 章 §3.2）。"""

    target_tokens: int = 512
    max_tokens: int = 1024
    overlap: float = 0.12                # 相邻文本块重叠比例（表格/slide 不重叠）
    split_by_heading: bool = True
    table_atomic: bool = True
    drop_header_footer: bool = True      # 剔除页眉页脚
    footnote_to_end: bool = True         # 脚注归尾


class PdfExportSettings(BaseModel):
    font_size: int = 12
    header_footer: bool = True           # 内置页眉页脚模板


class DatasetSettings(BaseModel):
    """微调数据集导出默认（05 章 §3）。"""

    format: Literal["alpaca", "sharegpt"] = "alpaca"
    file_format: Literal["json", "csv"] = "json"
    mode: Literal["blank", "rule"] = "blank"   # 留空模板（默认）/ 规则生成（实验性）
    per_chunk: int = 1


def _default_parallel() -> int:
    return max(1, min((os.cpu_count() or 4) // 2, 4))


class Settings(BaseModel):
    """全部用户可调项（FR-16）。主流程零配置：默认值即最佳实践。"""

    ocr_mode: Literal["on", "off", "high"] = "on"
    degrade_policy: Literal["auto", "strict"] = "auto"
    page_timeout_s: int = 30
    parallel_tasks: int = Field(default_factory=_default_parallel)
    output_dir: str | None = None        # None → workspace/{docId}/exports
    chunk: ChunkSettings = Field(default_factory=ChunkSettings)
    pdf_export: PdfExportSettings = Field(default_factory=PdfExportSettings)
    dataset: DatasetSettings = Field(default_factory=DatasetSettings)


def load_settings(paths: Paths) -> Settings:
    try:
        raw = json.loads(paths.settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except Exception:
        # 文件损坏（半写入/编码错乱）时回退默认值（不阻断启动），由调用方记日志
        return Settings()
    try:
        return Settings.model_validate(raw)
    except Exception:
        # 跨版本兼容（覆盖安装后新版本读旧 settings.json）：个别字段非法
        # ——比如新版收紧/更名了枚举值——不该把整份设置打回默认。
        # 逐个顶层字段抢救：合法的保留，非法的才用默认值（组内字段一荣俱荣，
        # 组粒度足够：用户真正的自定义集中在 output_dir 与 chunk 组）。
        return _salvage_settings(raw)


def _salvage_settings(raw: object) -> Settings:
    settings = Settings()
    if not isinstance(raw, dict):
        return settings
    data = settings.model_dump()
    for name in Settings.model_fields:
        if name not in raw:
            continue
        try:
            settings = Settings.model_validate({**data, name: raw[name]})
            data = settings.model_dump()
        except Exception:
            continue  # 该字段（或该组）非法：保持默认，不影响其余字段
    return settings


def save_settings(paths: Paths, settings: Settings) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    tmp = paths.settings_path.with_suffix(".json.tmp")
    tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, paths.settings_path)
