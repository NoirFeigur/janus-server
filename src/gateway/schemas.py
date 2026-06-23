from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OpenAIRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    stream: bool = False
    messages: list[Any]


class AnthropicRequest(BaseModel):
    """Anthropic /v1/messages request — kept in native format for passthrough."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    max_tokens: int = 4096
    stream: bool = False
    system: Any | None = None
    messages: list[Any]


class GeminiRequest(BaseModel):
    """Gemini generateContent request — kept in native format for passthrough."""

    model_config = ConfigDict(extra="allow")

    contents: list[Any]
    system_instruction: Any | None = None
    generationConfig: dict[str, Any] | None = None
