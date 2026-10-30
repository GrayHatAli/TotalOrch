"""Field-authority merging and domain record assembly.

Each source observes an entity independently. For every field the merger picks
the value claimed by the highest-authority source that actually vouches for
that field, and records that source's provenance on the field. A source that
keeps silent about a field cannot veto the value another source provides, and
absent capability claims stay ``None`` instead of becoming optimistic
booleans.

Field authority follows the catalog research:

- Capability flags: ``models.dev`` > ``openrouter`` > ``omniroute``
- Context / output limits: ``openrouter`` > ``models.dev`` > ``omniroute``
- Pricing: ``openrouter`` > ``models.dev``
- Runtime facts (health, latency, quota): ``omniroute`` only

The ``config`` source ranks above everything for explicitly user-configured
facts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, cast

from total_orch.domain import (
    ModelCapabilities,
    ModelFamily,
    ModelVariant,
    ProviderConnectionRef,
    ProviderEndpoint,
    ReasoningEffort,
    SourceProvenance,
)

from .identity import publisher_of, split_variant_id
from .observations import (
    EndpointObservation,
    FamilyObservation,
    FieldValue,
    ObservedValue,
    VariantObservation,
)

ALLOWED_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "ultra"})

# Highest authority first. Sources not listed for a field never supply it.
FIELD_AUTHORITY: Mapping[str, tuple[str, ...]] = {
    "publisher": ("config", "models.dev", "openrouter", "omniroute"),
    "display_name": ("models.dev", "openrouter", "omniroute"),
    "description": ("models.dev", "openrouter", "omniroute"),
    "context_window": ("openrouter", "models.dev", "omniroute"),
    "max_output_tokens": ("openrouter", "models.dev", "omniroute"),
    "cap.tool_calling": ("models.dev", "openrouter", "omniroute"),
    "cap.reasoning": ("models.dev", "openrouter", "omniroute"),
    "cap.structured_output": ("models.dev", "openrouter", "omniroute"),
    "cap.vision": ("models.dev", "openrouter", "omniroute"),
    "cap.multimodal": ("models.dev", "openrouter", "omniroute"),
    "cap.streaming": ("models.dev", "openrouter", "omniroute"),
    "cap.available_efforts": ("models.dev", "openrouter", "omniroute"),
    "health": ("omniroute",),
    "latency_p95_ms": ("omniroute",),
    "quota_remaining": ("omniroute",),
    "last_checked_at": ("omniroute",),
    "input_cost_per_million": ("openrouter", "models.dev", "omniroute"),
    "output_cost_per_million": ("openrouter", "models.dev", "omniroute"),
}

EntityObservation = FamilyObservation | VariantObservation | EndpointObservation


def source_rank(field: str, source: str) -> int:
    order = FIELD_AUTHORITY[field]
    return order.index(source) if source in order else len(order)


def merge_fields(observations: Iterable[EntityObservation], field_name: str) -> FieldValue | None:
    """Pick the highest-authority claim for one field across observations."""

    best: FieldValue | None = None
    best_rank = len(FIELD_AUTHORITY.get(field_name, ()))
    for observation in observations:
        claim = observation.fields.get(field_name)
        if claim is None:
            continue
        rank = source_rank(field_name, observation.source)
        if rank < best_rank:
            best, best_rank = claim, rank
    return best


def merged_field_map(observations: Iterable[EntityObservation]) -> dict[str, FieldValue]:
    names = {field_name for observation in observations for field_name in observation.fields}
    merged: dict[str, FieldValue] = {}
    for field_name in names:
        claim = merge_fields(observations, field_name)
        if claim is not None:
            merged[field_name] = claim
    return merged


def field_provenance_map(merged: Mapping[str, FieldValue]) -> dict[str, SourceProvenance]:
    return {field_name: value.provenance for field_name, value in merged.items()}


def observing_provenance(observations: Iterable[EntityObservation]) -> tuple[SourceProvenance, ...]:
    """Record-level provenance: every source that vouched for any field.

    Field-level provenance records the winner per field; record-level
    provenance lists every source that observed the entity at all, so a source
    whose claims lost every field still appears (MVP-0311). Sources that made
    no claim at all (e.g. synthesized family observations) are not listed.
    """

    provenances: list[SourceProvenance] = []
    seen: set[str] = set()
    for observation in observations:
        if not observation.fields or observation.source in seen:
            continue
        seen.add(observation.source)
        provenances.append(next(iter(observation.fields.values())).provenance)
    return tuple(provenances)


def _int_value(value: ObservedValue) -> int:
    if isinstance(value, bool):
        raise TypeError(f"cannot coerce boolean to an integer: {value!r}")
    if isinstance(value, tuple):
        raise TypeError(f"cannot coerce tuple to an integer: {value!r}")
    return int(value)


def _float_value(value: ObservedValue) -> float:
    if isinstance(value, bool):
        raise TypeError(f"cannot coerce boolean to a float: {value!r}")
    if isinstance(value, tuple):
        raise TypeError(f"cannot coerce tuple to a float: {value!r}")
    return float(value)


def _efforts(value: ObservedValue) -> tuple[ReasoningEffort, ...] | None:
    raw = value if isinstance(value, tuple) else tuple(value)  # type: ignore[arg-type]
    allowed = tuple(effort for effort in raw if isinstance(effort, str) and effort in ALLOWED_EFFORTS)
    if not allowed:
        return None
    return cast(tuple[ReasoningEffort, ...], tuple(sorted(set(allowed))))


def _optional_bool(value: ObservedValue | None) -> bool | None:
    return value if value is None or isinstance(value, bool) else None


def capabilities_from_fields(merged: Mapping[str, FieldValue]) -> ModelCapabilities:
    kwargs: dict[str, Any] = {}
    for field_name in (
        "tool_calling",
        "reasoning",
        "structured_output",
        "vision",
        "multimodal",
        "streaming",
    ):
        claim = merged.get(f"cap.{field_name}")
        if claim is not None:
            boolean = _optional_bool(claim.value)
            if boolean is not None:
                kwargs[field_name] = boolean
    efforts = merged.get("cap.available_efforts")
    if efforts is not None:
        parsed = _efforts(efforts.value)
        if parsed:
            kwargs["available_efforts"] = parsed
    if "available_efforts" in kwargs and kwargs.get("reasoning") is False:
        # The domain contract forbids efforts alongside an explicit "no
        # reasoning" flag; per-field merging can produce that combination when
        # one source vetoes reasoning while another lists efforts. Drop the
        # efforts rather than fabricate a reasoning claim.
        kwargs.pop("available_efforts")
    return ModelCapabilities(**kwargs)


def assemble_family(
    observations: Iterable[FamilyObservation],
    *,
    fallback_publisher: str | None = None,
) -> tuple[ModelFamily, dict[str, SourceProvenance]]:
    merged = merged_field_map(observations)
    publisher_claim = merged.get("publisher")
    publisher: str | None = (
        str(publisher_claim.value) if publisher_claim is not None else fallback_publisher
    )
    if publisher is None:
        family_ids = {observation.family_id for observation in observations}
        publisher = next((publisher_of(family_id) for family_id in family_ids if publisher_of(family_id)), None)
    family_id = next(iter({observation.family_id for observation in observations}), "")
    if not family_id:
        raise ValueError("cannot assemble a family from observations without a family id")
    if publisher is not None and not publisher_of(family_id):
        family_id = f"{publisher}/{family_id}"
    display = merged.get("display_name")
    record = ModelFamily(
        id=family_id,
        publisher=publisher,
        display_name=str(display.value) if display is not None else None,
        provenance=observing_provenance(observations),
    )
    return record, field_provenance_map(merged)


def assemble_variant(
    observations: Iterable[VariantObservation],
) -> tuple[ModelVariant, dict[str, SourceProvenance]]:
    merged = merged_field_map(observations)
    variant_id = next(iter({observation.variant_id for observation in observations}), "")
    if not variant_id:
        raise ValueError("cannot assemble a variant from observations without a variant id")
    family_id, variant_name = split_variant_id(variant_id)

    context = merged.get("context_window")
    max_output = merged.get("max_output_tokens")
    display = merged.get("display_name")
    description = merged.get("description")
    record = ModelVariant(
        id=variant_id,
        family_id=family_id,
        name=variant_name,
        display_name=str(display.value) if display is not None else None,
        description=str(description.value) if description is not None else None,
        context_window=_int_value(context.value) if context is not None else None,
        max_output_tokens=_int_value(max_output.value) if max_output is not None else None,
        capabilities=capabilities_from_fields(merged),
        provenance=observing_provenance(observations),
    )
    return record, field_provenance_map(merged)


def assemble_endpoint(
    observations: Iterable[EndpointObservation],
    *,
    connection_fallback: ProviderConnectionRef | None = None,
) -> tuple[tuple[ProviderEndpoint, tuple[SourceProvenance, ...]], dict[str, SourceProvenance]] | None:
    merged = merged_field_map(observations)
    refs = [
        observation.endpoint
        for observation in observations
        if observation.endpoint is not None and observation.endpoint.connection is not None
    ]
    connection = next((ref.connection for ref in refs if ref.connection is not None), connection_fallback)
    endpoint = next((observation.endpoint for observation in observations if observation.endpoint is not None), None)
    if endpoint is None or connection is None:
        return None

    health = merged.get("health")
    latency = merged.get("latency_p95_ms")
    input_cost = merged.get("input_cost_per_million")
    output_cost = merged.get("output_cost_per_million")
    quota = merged.get("quota_remaining")
    checked = merged.get("last_checked_at")
    record = ProviderEndpoint(
        id=endpoint.id,
        provider=endpoint.provider,
        provider_model_name=endpoint.provider_model_name,
        model_variant_id=endpoint.model_variant_id,
        connection=connection,
        capabilities=capabilities_from_fields(merged),
        health=_float_value(health.value) if health is not None else None,
        latency_p95_ms=_float_value(latency.value) if latency is not None else None,
        input_cost_per_million=_float_value(input_cost.value) if input_cost is not None else None,
        output_cost_per_million=_float_value(output_cost.value) if output_cost is not None else None,
        quota_remaining=_float_value(quota.value) if quota is not None else None,
        last_checked_at=_as_datetime(checked.value) if checked is not None else None,
        provenance=observing_provenance(observations),
    )
    return (record, record.provenance), field_provenance_map(merged)


def _as_datetime(value: ObservedValue) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
