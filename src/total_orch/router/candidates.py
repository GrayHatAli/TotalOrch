"""Candidate assembly for routing (MVP-0501..0508 inputs).

The router reasons about a concrete ``RouteCandidate`` per pool member: the
``ProviderEndpoint`` plus the canonical ``ModelVariant`` it serves, the
*effective* capability claim (endpoint over variant, unknown stays unknown)
and the origin of each winning claim. Assembling the candidate once keeps the
hard-constraint filter, the scoring pass, and the explanation consistent —
every stage reads the same merged facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from total_orch.domain import ModelCapabilities, ModelVariant, ProviderEndpoint
from total_orch.registry.capabilities import effective_capabilities


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    endpoint: ProviderEndpoint
    variant: ModelVariant | None
    family_id: str | None
    context_window: int | None
    capabilities: ModelCapabilities
    capability_origin: Mapping[str, str]

    @property
    def endpoint_id(self) -> str:
        return self.endpoint.id


def assemble_candidates(
    endpoints: Sequence[ProviderEndpoint],
    variants_by_id: Mapping[str, ModelVariant],
) -> tuple[RouteCandidate, ...]:
    """Merge endpoint and variant facts into deterministic candidates.

    Input order is ignored: candidates are returned sorted by endpoint id so
    every consumer (filter, scoring, tie-breaking, explanations) sees the
    same order.
    """

    candidates: list[RouteCandidate] = []
    for endpoint in sorted(endpoints, key=lambda item: item.id):
        variant = variants_by_id.get(endpoint.model_variant_id)
        capabilities, origin = effective_capabilities(endpoint, variant)
        candidates.append(
            RouteCandidate(
                endpoint=endpoint,
                variant=variant,
                family_id=variant.family_id if variant is not None else None,
                context_window=variant.context_window if variant is not None else None,
                capabilities=capabilities,
                capability_origin=origin,
            )
        )
    return tuple(candidates)
