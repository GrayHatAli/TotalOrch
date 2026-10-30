"""Gateway-neutral request and response contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class GatewayRequest:
    provider: str
    model: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    temperature: float | None = None
    reasoning_effort: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def target_model(self) -> str:
        if self.model.startswith(f"{self.provider}/"):
            return self.model
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True, slots=True)
class GatewayToolCall:
    """A gateway-neutral tool call without interpreting its arguments."""

    id: str | None = None
    function_name: str | None = None
    arguments: str = ""
    index: int | None = None


@dataclass(frozen=True, slots=True)
class GatewayCompletion:
    request_id: str | None
    provider: str
    model: str
    output_text: str
    finish_reason: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tool_calls: tuple[GatewayToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class GatewayStreamChunk:
    request_id: str | None
    provider: str
    model: str
    delta: str = ""
    finish_reason: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tool_calls: tuple[GatewayToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class GatewayModel:
    id: str
    provider: str | None = None
    display_name: str | None = None
    context_window: int | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
