"""Tool contracts and shared result/error types (MVP-0701)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ToolError(RuntimeError):
    """A denied or failed tool call with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str
    exit_code: int | None = None
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
