from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GatewayRequestBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    _FORBIDDEN_KEYS: ClassVar[set[str]] = {
        "api_base",
        "base_url",
        "api_key",
        "api_version",
        "azure_ad_token",
        "azure_ad_token_provider",
        "headers",
        "extra_headers",
        "default_headers",
        "litellm_metadata",
        "litellm_params",
        "metadata",
        "model_list",
        "deployment_id",
        "custom_llm_provider",
        "mock_response",
        "preset_cache_key",
        "no-log",
        "no_log",
        "cache",
        "caching",
        "callbacks",
        "success_callback",
        "failure_callback",
        "logger_fn",
        "client",
        "aclient",
        "api_type",
        "aws_access_key_id",
        "aws_region_name",
        "aws_secret_access_key",
        "aws_session_token",
        "region_name",
        "vertex_credentials",
        "vertex_location",
        "vertex_project",
        "vertex_project_id",
        "organization",
        "drop_params",
        "allowed_openai_params",
        "proxy_server_request",
        "specific_deployment",
        "timeout",
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
