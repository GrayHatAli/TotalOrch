"""Typed, versioned contracts for the first three MVP phases.

The contracts deliberately represent unknown metadata with ``None``. The
registry must not turn missing provider facts into optimistic capability claims.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceProvenance(Contract):
    source: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=utc_now)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_record_id: str | None = None


class ProviderConnectionRef(Contract):
    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    label: str | None = None
    account_ref: str | None = None


ProviderAuthMethod = Literal["none", "api_key", "bearer", "basic", "custom"]


class ProviderConnection(Contract):
    """TotalOrch-owned provider metadata; secret values never belong here."""

    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    label: str | None = None
    base_url: str | None = None
    auth_method: ProviderAuthMethod = "none"
    secret_ref: str | None = Field(default=None, pattern=r"^env://[A-Za-z_][A-Za-z0-9_]*$")
    owner_ref: str | None = None
    enabled: bool = True
    healthy: bool | None = None
    last_health_check_at: datetime | None = None
    last_success_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    schema_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_auth_reference(self) -> ProviderConnection:
        if self.auth_method == "none" and self.secret_ref is not None:
            raise ValueError("secret_ref cannot be set when auth_method is none")
        if self.auth_method != "none" and self.secret_ref is None:
            raise ValueError("secret_ref is required for authenticated connections")
        return self


ReasoningEffort = Literal["low", "medium", "high", "ultra"]


class ModelKind(StrEnum):
    concrete = "concrete"
    alias = "alias"
    automatic = "automatic"
    virtual = "virtual"


class ModelCapabilities(Contract):
    tool_calling: bool | None = None
    reasoning: bool | None = None
    structured_output: bool | None = None
    vision: bool | None = None
    multimodal: bool | None = None
    streaming: bool | None = None
    available_efforts: tuple[ReasoningEffort, ...] | None = None

    @field_validator("available_efforts")
    @classmethod
    def unique_efforts(cls, value: tuple[ReasoningEffort, ...] | None) -> tuple[ReasoningEffort, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("available_efforts must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_effort_dependency(self) -> ModelCapabilities:
        if self.available_efforts and self.reasoning is False:
            raise ValueError("available_efforts cannot be set when reasoning is false")
        return self


class ModelFamily(Contract):
    id: str = Field(min_length=1, description="Canonical family id, e.g. openai/luna")
    publisher: str | None = None
    display_name: str | None = None
    provenance: tuple[SourceProvenance, ...] = ()
    schema_version: int = Field(default=1, ge=1)


class ModelVariant(Contract):
    id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    display_name: str | None = None
    description: str | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    model_kind: ModelKind | None = None
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    provenance: tuple[SourceProvenance, ...] = ()
    schema_version: int = Field(default=1, ge=1)


class ProviderEndpoint(Contract):
    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_model_name: str = Field(min_length=1)
    model_variant_id: str = Field(min_length=1)
    connection: ProviderConnectionRef
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    health: float | None = Field(default=None, ge=0, le=1)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    quota_remaining: float | None = Field(default=None, ge=0)
    last_checked_at: datetime | None = None
    provenance: tuple[SourceProvenance, ...] = ()
    schema_version: int = Field(default=1, ge=1)


class AgentDefinition(Contract):
    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    required_capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    minimum_context_window: int | None = Field(default=None, gt=0)
    schema_version: int = Field(default=1, ge=1)


class AgentProfile(Contract):
    id: str = Field(min_length=1)
    agent_definition_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    preferences: dict[str, float] = Field(default_factory=dict)
    enabled: bool = True
    schema_version: int = Field(default=1, ge=1)


class ModelPool(Contract):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    member_endpoint_ids: tuple[str, ...] = ()
    generated: bool = True
    policy_version: str = Field(default="default-v1", min_length=1)
    provenance: tuple[SourceProvenance, ...] = ()
    schema_version: int = Field(default=1, ge=1)

    @field_validator("member_endpoint_ids")
    @classmethod
    def unique_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("member_endpoint_ids must not contain duplicates")
        return value


class RouteRequest(Contract):
    id: UUID = Field(default_factory=uuid4)
    agent_profile_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    required_capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    minimum_context_window: int | None = Field(default=None, gt=0)
    candidate_pool_id: str | None = None
    preferred_family_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RouteDecision(Contract):
    """The persisted output of the explainable router (MVP Phase 5).

    ``score_factors`` maps an endpoint id to that endpoint's per-factor
    scores in ``[0, 1]``, where 1 is best and an unknown factor is neutral
    (0.5). ``factor_weights`` snapshots the policy weights that produced the
    decision so a stored decision can be reconstructed after policy changes,
    and ``total_scores`` is the weighted total per endpoint. ``explored`` is
    True only when bounded exploration, rather than the strict best score,
    produced the selection.
    """

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    selected_endpoint_id: str | None = None
    candidate_endpoint_ids: tuple[str, ...] = ()
    excluded_reasons: dict[str, str] = Field(default_factory=dict)
    score_factors: dict[str, dict[str, float]] = Field(default_factory=dict)
    factor_weights: dict[str, float] = Field(default_factory=dict)
    total_scores: dict[str, float] = Field(default_factory=dict)
    fallback_endpoint_ids: tuple[str, ...] = ()
    policy_version: str = Field(default="default-v1", min_length=1)
    explored: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def selected_must_be_candidate(self) -> RouteDecision:
        if self.selected_endpoint_id and self.selected_endpoint_id not in self.candidate_endpoint_ids:
            raise ValueError("selected_endpoint_id must be included in candidate_endpoint_ids")
        return self

    @field_validator("score_factors")
    @classmethod
    def factor_scores_in_unit_range(cls, value: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for endpoint_id, factors in value.items():
            for factor, score in factors.items():
                if score < 0 or score > 1:
                    raise ValueError(f"score for {factor!r} on {endpoint_id!r} must lie in [0, 1]")
        return value

    @field_validator("total_scores")
    @classmethod
    def total_scores_in_unit_range(cls, value: dict[str, float]) -> dict[str, float]:
        for endpoint_id, total in value.items():
            if total < 0 or total > 1:
                raise ValueError(f"total score for {endpoint_id!r} must lie in [0, 1]")
        return value

    @field_validator("factor_weights")
    @classmethod
    def weights_are_non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        if any(weight < 0 for weight in value.values()):
            raise ValueError("factor weights must be non-negative")
        return value


class WorkflowStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class WorkflowTask(Contract):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


class WorkflowDefinition(Contract):
    id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    tasks: tuple[WorkflowTask, ...] = Field(min_length=1)
    schema_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_task_dependencies(self) -> WorkflowDefinition:
        task_ids = {task.id for task in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise ValueError("workflow task ids must be unique")
        missing = {dependency for task in self.tasks for dependency in task.depends_on} - task_ids
        if missing:
            raise ValueError(f"workflow references missing tasks: {sorted(missing)}")
        return self


class WorkflowResult(Contract):
    workflow_id: str = Field(min_length=1)
    status: WorkflowStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    completed_at: datetime | None = None


class ExecutionEvent(Contract):
    id: UUID = Field(default_factory=uuid4)
    workflow_id: str = Field(min_length=1)
    task_id: str | None = None
    event_type: str = Field(min_length=1)
    status: WorkflowStatus
    timestamp: datetime = Field(default_factory=utc_now)
    provider_endpoint_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: tuple[SourceProvenance, ...] = ()


class RoutingEvent(Contract):
    id: UUID = Field(default_factory=uuid4)
    workflow_id: str = Field(min_length=1)
    request_id: UUID
    decision_id: UUID
    timestamp: datetime = Field(default_factory=utc_now)
    selected_endpoint_id: str | None = None
    excluded_reasons: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: tuple[SourceProvenance, ...] = ()
