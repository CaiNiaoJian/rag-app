"""本地模型接口抽象（06 章 §4.2，V1 冻结契约、V2 实装）。

引擎内一切「用模型」的地方只面向 ModelProvider，不关心模型来自哪里：
- V1 默认注册 NullProvider —— 所有调用给出明确的 MODEL_NOT_INSTALLED 错误
  （UI 文案：未安装模型模组），保证链路完整可测。
- V2 安装 llm-runtime 模组后注册 OpenAICompatProvider，转发 127.0.0.1:{modelPort}
  的 OpenAI-compatible 服务（socket guard 白名单放行该本地端口，02 章 §7）。

注册表为模块级单例：register_provider() 挂载、get_provider() 取当前激活者；
线程安全（uvicorn 多线程 + 任务 worker 都可能取用）。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator

from docfactory.errors import MODEL_NOT_INSTALLED, DocFactoryError

# 统一的用户可读错误信息（06 章 §4.2 / §4.3）
_NOT_INSTALLED_MESSAGE = "未安装模型模组"


class ModelProvider(ABC):
    """模型能力抽象（签名冻结，06 章 §4.2）。"""

    @abstractmethod
    def capabilities(self) -> dict:
        """能力声明：{"chat": bool, "max_context": int, "model_id": str}。"""

    @abstractmethod
    def chat(self, messages: list[dict], **kw) -> str | Iterator[str]:
        """对话补全：messages 为 OpenAI 格式 [{"role","content"},…]；
        返回完整字符串，或（流式实现）逐段字符串迭代器。"""

    @abstractmethod
    def health(self) -> bool:
        """模型服务是否可用（探活，不抛异常）。"""


class NullProvider(ModelProvider):
    """V1 默认注册：无模型可用，一切调用返回明确错误而非静默失败。

    - capabilities()/health() 返回「无能力/不健康」的明确结构（供 /v1/models 列空表）；
    - chat() 抛 DocFactoryError(code=MODEL_NOT_INSTALLED)，
      /v1/chat/completions 捕获后转 HTTP 501（06 章 §4.3）。
    """

    def capabilities(self) -> dict:
        return {"chat": False, "max_context": 0, "model_id": None}

    def chat(self, messages: list[dict], **kw) -> str | Iterator[str]:
        raise DocFactoryError(MODEL_NOT_INSTALLED, _NOT_INSTALLED_MESSAGE)

    def health(self) -> bool:
        return False


# ---------------------------------------------------------------- 模块级注册表

_lock = threading.Lock()
_providers: dict[str, ModelProvider] = {}
_active_name: str = "null"


def register_provider(name: str, provider: ModelProvider, *, activate: bool = True) -> None:
    """挂载一个 Provider；activate=True 时同时切为当前激活者。

    典型调用方：llm-runtime 模组启动完成后注册 OpenAICompatProvider（V2）。
    """
    global _active_name
    with _lock:
        _providers[name] = provider
        if activate:
            _active_name = name


def get_provider() -> ModelProvider:
    """取当前激活的 Provider；任何情况下都有值（兜底 NullProvider，绝不返回 None）。"""
    with _lock:
        provider = _providers.get(_active_name)
        if provider is None:
            provider = _providers["null"]
        return provider


def list_providers() -> dict[str, str]:
    """诊断用：已注册 Provider 名单与当前激活者。"""
    with _lock:
        return {"active": _active_name, "registered": ", ".join(sorted(_providers))}


# 默认注册（import 即生效）：保证引擎任何阶段调 get_provider() 都拿到确定行为
register_provider("null", NullProvider(), activate=True)
