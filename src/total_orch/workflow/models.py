"""Typed inputs, model outputs, and results for the workflow runtime (MVP Phase 6)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from total_orch.domain import ExecutionEvent, WorkflowResult
from total_orch.domain.contracts import Contract


class TaskType(StrEnum):
    feature = "feature"
    bug_fix = "bug_fix"


class TaskClassification(Contract):
    task_type: TaskType
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class _ModelOutput(BaseModel):
    """Structured output from an agent; unknown keys are ignored for forwards compatibility."""

    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class PlanStep(_ModelOutput):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Plan(_ModelOutput):
    summary: str = Field(min_length=1)
    steps: tuple[PlanStep, ...] = ()
    risks: tuple[str, ...] = ()


class Change(_ModelOutput):
    path: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Implementation(_ModelOutput):
    summary: str = Field(min_length=1)
    changes: tuple[Change, ...] = ()
    diff: str = ""


class Review(_ModelOutput):
    approved: bool
    summary: str = Field(min_length=1)
    issues: tuple[str, ...] = ()


class TestResult(_ModelOutput):
    ran: bool = False
    passed: bool | None = None
    command: str | None = None
    output: str = ""
    exit_code: int | None = None


class WorkflowRequest(Contract):
    """One software-development task submitted to the runtime."""

    request: str = Field(min_length=1)
    workspace: str | None = None
    task_type: TaskType | None = None
    run_tests: bool = True
    max_corrections: int | None = Field(default=None, ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    """The persisted result plus the in-memory state and emitted events."""

    workflow_id: str
    result: WorkflowResult
    state: dict[str, Any]
    events: tuple[ExecutionEvent, ...]
