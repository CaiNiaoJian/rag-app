"""本地模型接口（06 章 §4，V1 桩：OpenAI-compatible 形状，未装模型时明确报错）。

对外契约与 OpenAI REST 对齐，未来 llm-runtime 模组（llama.cpp llama-server）零改造接入：
- GET  /v1/models            已加载模型列表；无模型 → {"object":"list","data":[]}
- POST /v1/chat/completions  标准 chat 格式；NullProvider → HTTP 501
  {"error":{"code":"MODEL_NOT_INSTALLED","message":"未安装模型模组"}}（06 章 §4.3）

OpenAI 字段结构备忘（响应按此拼装，保证第三方客户端可直连）：
    /v1/models 条目：{"id": 模型名, "object": "model", "created": 秒级时间戳, "owned_by": 归属}
    chat 请求：{"model", "messages":[{"role","content"}], "temperature", "max_tokens", "stream"}
    chat 响应：{"id":"chatcmpl-…", "object":"chat.completion", "created", "model",
               "choices":[{"index":0, "message":{"role":"assistant","content":…},
                           "finish_reason":"stop"}],
               "usage":{"prompt_tokens","completion_tokens","total_tokens"}}

V1 不实现流式（stream=true 也回整包 JSON）；V2 随真 Provider 一并补 SSE 流。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from docfactory.errors import MODEL_NOT_INSTALLED, DocFactoryError
from docfactory.providers import NullProvider, get_provider

router = APIRouter()


def _not_installed_response() -> JSONResponse:
    """501 Not Implemented：能力尚未落地（区别于 4xx 请求错误），文案面向普通用户。"""
    return JSONResponse(
        status_code=501,
        content={"error": {"code": MODEL_NOT_INSTALLED, "message": "未安装模型模组"}},
    )


class ChatCompletionsBody(BaseModel):
    """OpenAI chat 请求体；extra=allow 容忍客户端带非核心字段（top_p 等）不报 422。"""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[dict[str, Any]] = []
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


@router.get("/v1/models")
def list_models() -> dict[str, Any]:
    """已加载模型列表；NullProvider 或无 chat 能力 → 空列表（不报错，便于客户端探测）。"""
    provider = get_provider()
    if isinstance(provider, NullProvider):
        return {"object": "list", "data": []}
    try:
        caps = provider.capabilities()
    except DocFactoryError:
        return {"object": "list", "data": []}
    if not caps.get("chat") or not caps.get("model_id"):
        return {"object": "list", "data": []}
    return {
        "object": "list",
        "data": [{
            "id": caps["model_id"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": "docfactory-local",
        }],
    }


@router.post("/v1/chat/completions")
def chat_completions(body: ChatCompletionsBody) -> Any:
    """chat 补全；V1（NullProvider）恒返回 501 MODEL_NOT_INSTALLED。"""
    provider = get_provider()
    if isinstance(provider, NullProvider):
        return _not_installed_response()

    if not body.messages:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": "messages 不能为空"}},
        )

    # 仅透传显式给出的采样参数，让 Provider 自己的默认值生效
    kw: dict[str, Any] = {}
    if body.temperature is not None:
        kw["temperature"] = body.temperature
    if body.max_tokens is not None:
        kw["max_tokens"] = body.max_tokens
    try:
        result = provider.chat(body.messages, **kw)
    except DocFactoryError as exc:
        if exc.code == MODEL_NOT_INSTALLED:
            return _not_installed_response()
        return JSONResponse(
            status_code=500,
            content={"error": {"code": exc.code, "message": exc.detail or "模型调用失败"}},
        )

    # V1 不做流式：Provider 返回迭代器时聚合为整段文本
    content = result if isinstance(result, str) else "".join(result)
    caps = provider.capabilities()
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model or caps.get("model_id") or "local-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        # token 用量 V1 不精确统计（真 Provider 接入后由其回填）
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
