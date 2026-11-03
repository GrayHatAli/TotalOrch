"""Telemetry contracts (MVP-0801..0806)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from total_orch.domain.contracts import Contract


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorkflowRun(Contract):
    """One request-level execution record; ``trace_id`` is the correlation id."""

    trace_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    request: str
    task_type: str | None = None
    workspace: str | None = None
    status: str = "running"
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class EndpointObservation(Contract):
    endpoint_id: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    success: bool
    latency_ms: float | None = Field(default=None, ge=0)
    ttft_ms: float | None = Field(default=None, ge=0)
    error_code: str | None = None
    route_decision_id: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
