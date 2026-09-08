"""Effective capability resolution and hard-constraint checks.

The registry keeps capability claims tri-state: an absent claim stays unknown
and must never become an optimistic ``True`` or a vetoing ``False``. Both the
pool builder (MVP-0406..0409, 0411) and the explainable router (MVP-0501)
filter endpoints with the same rules, so the resolution and reason phrasing
live here in one place:

- ``effective_capabilities`` merges an endpoint's own claim over its canonical
  variant claim (a concrete gateway observation wins over public catalog
  metadata; neither claiming leaves the field unknown).
- ``capability_claim`` and ``context_claim`` turn a merged claim into an
  (ok, reason) verdict with a stable, human-readable explanation.
"""

from __future__ import annotations

from collections.abc import Mapping

from total_orch.domain import ModelCapabilities, ModelVariant, ProviderEndpoint

#: Boolean capability flags shared by the pool builder and the router, in
#: contract order.
BOOL_CAPABILITY_FIELDS: tuple[str, ...] = (
    "tool_calling",
    "reasoning",
    "structured_output",
    "vision",
    "multimodal",
    "streaming",
)


def capability_display(field: str) -> str:
    return field.replace("_", " ")


def effective_capabilities(
    endpoint: ProviderEndpoint,
    variant: ModelVariant | None,
) -> tuple[ModelCapabilities, dict[str, str]]:
    """Merge endpoint claims over canonical variant claims.

    A concrete endpoint observation (health-checked gateway metadata) wins
    over public-catalog metadata; neither claiming a capability leaves it
    unknown. The returned origin map records, per boolean field, whether the
    winning claim came from ``endpoint`` or ``variant`` metadata.
    """

    endpoint_values = endpoint.capabilities.model_dump()
    variant_values = variant.capabilities.model_dump() if variant is not None else {}
    updates: dict[str, object] = {}
    origin: dict[str, str] = {}
    for field, endpoint_value in endpoint_values.items():
        if endpoint_value is not None:
            updates[field] = endpoint_value
            origin[field] = "endpoint"
        elif variant_values.get(field) is not None:
            origin[field] = "variant"
    base = ModelCapabilities(**variant_values) if variant is not None else ModelCapabilities()
    return base.model_copy(update=updates), origin


def capability_claim(
    capabilities: ModelCapabilities,
    origin: Mapping[str, str],
    field: str,
) -> tuple[bool, str]:
    """Verdict for one boolean capability: unknown never passes."""

    display = capability_display(field)
    value = getattr(capabilities, field)
    if value is True:
        source = "endpoint metadata" if origin.get(field) == "endpoint" else "variant metadata"
        return True, f"{display} claimed True ({source})"
    if value is False:
        return False, f"{display} claimed False"
    return False, f"{display} unknown - not claimed by endpoint or variant metadata"


def context_claim(context_window: int | None, minimum: int) -> tuple[bool, str]:
    """Verdict for a context-window floor: unknown never passes."""

    if context_window is None:
        return False, "context window unknown - no variant record claims one"
    if context_window >= minimum:
        return True, f"context window {context_window} >= {minimum}"
    return False, f"context window {context_window} < {minimum}"
