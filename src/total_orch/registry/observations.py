"""Typed observations produced by catalog ingestors.

An observation is a single source's claim about one registry entity. Values
live in ``FieldValue`` records so every field keeps its own provenance and
freshness (MVP-0311). A field a source cannot vouch for is simply absent: an
absent capability never becomes an optimistic ``True`` or a vetoing ``False``
during merging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from total_orch.domain import ModelCapabilities, ProviderConnectionRef, SourceProvenance

from .identity import endpoint_id

ObservedValue = bool | int | float | str | tuple[str, ...]

CapabilityField = Literal[
    "cap.tool_calling",
    "cap.reasoning",
    "cap.structured_output",
    "cap.vision",
    "cap.multimodal",
    "cap.streaming",
    "cap.available_efforts",
]

VariantField = Literal[
    "description",
    "display_name",
    "context_window",
    "max_output_tokens",
    "model_kind",
    CapabilityField,
]

EndpointField = Literal[
    "cap.tool_calling",
    "cap.reasoning",
    "cap.structured_output",
    "cap.vision",
    "cap.multimodal",
    "cap.streaming",
    "cap.available_efforts",
    "health",
    "latency_p95_ms",
    "input_cost_per_million",
    "output_cost_per_million",
    "quota_remaining",
    "last_checked_at",
]


@dataclass(frozen=True, slots=True)
class FieldValue:
    value: ObservedValue
    provenance: SourceProvenance


def claimed(field_value: ObservedValue, source: str, *, confidence: float | None = None) -> FieldValue:
    """Wrap a raw value with provenance for the claiming source."""

    provenance = SourceProvenance(source=source, confidence=confidence)
    return FieldValue(value=field_value, provenance=provenance)


@dataclass(frozen=True, slots=True)
class Observation:
    """Common base: one source's claims about one entity."""

    source: str
    fields: dict[str, FieldValue] = field(default_factory=dict)

    def merged_fields(self) -> dict[str, FieldValue]:
        return dict(self.fields)


@dataclass(frozen=True, slots=True)
class FamilyObservation(Observation):
    family_id: str = ""


@dataclass(frozen=True, slots=True)
class VariantObservation(Observation):
    variant_id: str = ""


@dataclass(frozen=True, slots=True)
class EndpointRef:
    provider: str
    provider_model_name: str
    model_variant_id: str
    connection: ProviderConnectionRef | None = None
    source_record_id: str | None = None

    @property
    def id(self) -> str:
        return endpoint_id(self.provider, self.provider_model_name)


@dataclass(frozen=True, slots=True)
class EndpointObservation(Observation):
    endpoint: EndpointRef | None = None


@dataclass(frozen=True, slots=True)
class CatalogBatch:
    """Everything one sync pass produced before merging."""

    families: list[FamilyObservation] = field(default_factory=list)
    variants: list[VariantObservation] = field(default_factory=list)
    endpoints: list[EndpointObservation] = field(default_factory=list)

    def extend(self, other: CatalogBatch) -> None:
        self.families.extend(other.families)
        self.variants.extend(other.variants)
        self.endpoints.extend(other.endpoints)


def capability_fields(capabilities: ModelCapabilities, source: str) -> dict[str, FieldValue]:
    """Project a pydantic capability record into per-field claims.

    Only explicitly present (non-None) values are claimed; unknown remains
    unknown.
    """

    fields: dict[str, FieldValue] = {}
    for field_name in (
        "tool_calling",
        "reasoning",
        "structured_output",
        "vision",
        "multimodal",
        "streaming",
        "available_efforts",
    ):
        value: Any = getattr(capabilities, field_name)
        if value is None:
            continue
        fields[f"cap.{field_name}"] = claimed(value, source)
    return fields
