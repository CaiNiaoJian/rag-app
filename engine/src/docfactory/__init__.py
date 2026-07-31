"""DocFactory 引擎包 —— 全局版本常量（冻结契约的版本锚点）。

- ENGINE_VERSION：引擎实现版本（随发布迭代）
- API_VERSION：Electron↔引擎 HTTP API 契约版本（02 章）
- IR_VERSION：中间表示 JSON 契约版本（04 章，M2 结束冻结）
- SCHEMA_VERSION：SQLite schema 版本（02 章 §4，migrations 推进）
- KMOD_MANIFEST_VERSION：.kmod manifest 格式版本（06 章）
"""

APP_NAME = "DocFactory"
ENGINE_VERSION = "0.1.3"
API_VERSION = "1.0"
IR_VERSION = "1.0"
SCHEMA_VERSION = 1
KMOD_MANIFEST_VERSION = 1
