from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GatewayRequestBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    _FORBIDDEN_KEYS: ClassVar[set[str]] = {
        "api_base",
        "api_key",
        "headers",
        "litellm_metadata",
        "litellm_params",
        "metadata",
        "custom_llm_provider",
        "mock_response",
        "preset_cache_key",
        "no-log",
        "cache",
        "caching",
    }

    @model_validator(mode="before")
    @classmethod
    def reject_reserved_litellm_params(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        forbidden = cls._FORBIDDEN_KEYS.intersection(data)
        if forbidden:
            raise ValueError("request contains gateway-reserved parameters")
        return data


class OpenAIRequest(GatewayRequestBase):
    model: str | None = None
    stream: bool = False
    messages: list[Any]


class AnthropicRequest(GatewayRequestBase):
    """Anthropic /v1/messages request — kept in native format for passthrough."""

    model: str | None = None
    max_tokens: int = 4096
    stream: bool = False
    system: Any | None = None
    messages: list[Any]


class GeminiRequest(GatewayRequestBase):
    """Gemini generateContent request — kept in native format for passthrough."""

    contents: list[Any]
    system_instruction: Any | None = None
    generationConfig: dict[str, Any] | None = None
    tools: Any | None = None
    tool_config: Any | None = Field(default=None, alias="toolConfig")
    safety_settings: Any | None = Field(default=None, alias="safetySettings")
    cached_content: Any | None = Field(default=None, alias="cachedContent")
