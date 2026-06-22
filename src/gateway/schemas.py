from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OpenAIRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    stream: bool = False
    messages: list[Any]


class AnthropicRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    stream: bool = False
    messages: list[Any]


class GeminiRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    contents: list[Any]
    system_instruction: Any | None = None
