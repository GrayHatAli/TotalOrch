"""The explainable router (MVP Phase 5, MVP-0501..0515).

``ExplainableRouter`` turns a ``RouteRequest`` plus the candidate endpoints of
one model pool into a persisted ``RouteDecision`` and a human-readable
explanation:

1. *Hard constraints* (MVP-0501) run before any scoring: an endpoint whose
   effective capability claim contradicts a required capability, or whose
   context window is unknown or below the request's floor, is excluded with
   the same reason phrasing the pool builder uses. Unknown claims never pass —
   the router must not invent provider facts.
2. Survivors are scored per factor (MVP-0502..0508; see ``scoring``) and the
   weighted total is computed from the policy's normalized weights
   (MVP-0509).
3. Ranking is deterministic: total score descending, endpoint id ascending
   (MVP-0510). With exploration disabled, the same input and policy always
   produce the same decision.
4. *Bounded exploration* (MVP-0511) may, with probability ``explore_rate``,
   promote one of the top ``explore_pool_size`` candidates over the strict
   best; such decisions are flagged ``explored``.
5. The ranked runners-up form the ordered fallback list (MVP-0512).

``route`` produces the ``RouteDecision``; ``preview`` (MVP-0514) returns the
same evaluation without minting a decision id, for debugging and UI use.
Every factor score carries a human-readable note and the assembled
explanation quotes them, so every route has an explanation (MVP DoD). The
decision snapshots the policy weights, so a stored decision can be replayed
after policy changes (MVP-0513).

Pool resolution happens upstream (MVP-0607): the caller passes the member
endpoints of the agent's pool and the registry's variant records.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from total_orch.domain import ModelVariant, ProviderEndpoint, RouteDecision, RouteRequest
from total_orch.registry.capabilities import (
    BOOL_CAPABILITY_FIELDS,
    capability_claim,
    context_claim,
)

from .candidates import RouteCandidate, assemble_candidates
from .policies import FACTOR_KEYS, FACTOR_LABELS, RoutingPolicy
from .scoring import HealthObservation, score_candidates


def _clip_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _hard_constraint_reason(request: RouteRequest, candidate: RouteCandidate) -> str | None:
    """The first hard constraint the candidate fails, or ``None`` (MVP-0501)."""

    for field in BOOL_CAPABILITY_FIELDS:
        required = getattr(request.required_capabilities, field)
        if required is None:
            continue
        satisfied, reason = capability_claim(candidate.capabilities, candidate.capability_origin, field)
        if not satisfied:
            return reason
    minimum = request.minimum_context_window
    if minimum is not None:
        satisfied, reason = context_claim(candidate.context_window, minimum)
        if not satisfied:
            return reason
    return None


def _explain(
    request: RouteRequest,
    *,
    policy: RoutingPolicy,
    weights: Mapping[str, float],
    excluded: Mapping[str, str],
    totals: Mapping[str, float],
    ranking: Sequence[str],
    selected: str | None,
    explored: bool,
    scores: Mapping[str, Mapping[str, float]],
    notes: Mapping[str, Mapping[str, str]],
) -> str:
    """Assemble the human-readable explanation for one evaluation."""

    considered = len(ranking) + len(excluded)
    lines = [
        (
            f"Routing task {request.task_type!r} for profile {request.agent_profile_id!r} "
            f"with policy {policy.policy_version} over {considered} candidate(s)."
        )
    ]
    if excluded:
        lines.append("Excluded by hard constraints before scoring:")
        for endpoint_id in sorted(excluded):
            lines.append(f"  - {endpoint_id}: {excluded[endpoint_id]}")
    if selected is None:
        if excluded:
            lines.append("No candidate satisfied the hard constraints; nothing was selected.")
        else:
            lines.append("The candidate pool is empty; nothing was selected.")
        return "\n".join(lines)

    selection_note = (" via bounded exploration" if explored else "")
    lines.append(f"Selected {selected} with total score {totals[selected]:.3f}{selection_note}.")
    lines.append("Per-factor contributions for the selected endpoint:")
    for factor in FACTOR_KEYS:
        score = scores[selected][factor]
        contribution = weights[factor] * score
        lines.append(
            f"  - {FACTOR_LABELS[factor]}: {score:.2f} x {weights[factor]:.2f}"
            f" = {contribution:.3f} - {notes[selected][factor]}"
        )
    runners_up = [endpoint_id for endpoint_id in ranking if endpoint_id != selected]
    if runners_up:
        lines.append(
            "Fallback order: "
            + ", ".join(f"{endpoint_id} ({totals[endpoint_id]:.3f})" for endpoint_id in runners_up)
            + "."
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RouteOutcome:
    """The persisted decision plus the artifacts behind it."""

    decision: RouteDecision
    explanation: str
    notes: Mapping[str, Mapping[str, str]]
    policy: RoutingPolicy


@dataclass(frozen=True, slots=True)
class RoutePreview:
    """A full route evaluation without minting a decision id (MVP-0514)."""

    request_id: UUID
    policy_version: str
    weights: Mapping[str, float]
    candidate_endpoint_ids: tuple[str, ...]
    excluded_reasons: Mapping[str, str]
    score_factors: Mapping[str, Mapping[str, float]]
    total_scores: Mapping[str, float]
    ranking: tuple[str, ...]
    selected_endpoint_id: str | None
    fallback_endpoint_ids: tuple[str, ...]
    explored: bool
    notes: Mapping[str, Mapping[str, str]]
    explanation: str


@dataclass(frozen=True, slots=True)
class _Evaluation:
    weights: dict[str, float]
    candidate_endpoint_ids: tuple[str, ...]
    excluded_reasons: dict[str, str]
    scores: dict[str, dict[str, float]]
    notes: dict[str, dict[str, str]]
    totals: dict[str, float]
    ranking: tuple[str, ...]
    selected: str | None
    fallbacks: tuple[str, ...]
    explored: bool
    explanation: str


class ExplainableRouter:
    """Deterministic, explainable endpoint selection for one model pool."""

    def route(
        self,
        request: RouteRequest,
        *,
        endpoints: Sequence[ProviderEndpoint],
        variants_by_id: Mapping[str, ModelVariant],
        policy: RoutingPolicy,
        health: Mapping[str, HealthObservation] | None = None,
        history: Mapping[str, float] | None = None,
        explore_rate: float = 0.0,
        explore_pool_size: int = 2,
        rng: Callable[[], float] | None = None,
    ) -> RouteOutcome:
        """Route the request and return the decision with its explanation."""

        evaluation = self._evaluate(
            request,
            endpoints=endpoints,
            variants_by_id=variants_by_id,
            policy=policy,
            health=health,
            history=history,
            explore_rate=explore_rate,
            explore_pool_size=explore_pool_size,
            rng=rng,
        )
        decision = RouteDecision(
            request_id=request.id,
            selected_endpoint_id=evaluation.selected,
            candidate_endpoint_ids=evaluation.candidate_endpoint_ids,
            excluded_reasons=evaluation.excluded_reasons,
            score_factors=evaluation.scores,
            factor_weights=evaluation.weights,
            total_scores=evaluation.totals,
            fallback_endpoint_ids=evaluation.fallbacks,
            policy_version=policy.policy_version,
            explored=evaluation.explored,
        )
        return RouteOutcome(
            decision=decision,
            explanation=evaluation.explanation,
            notes=evaluation.notes,
            policy=policy,
        )

    def preview(
        self,
        request: RouteRequest,
        *,
        endpoints: Sequence[ProviderEndpoint],
        variants_by_id: Mapping[str, ModelVariant],
        policy: RoutingPolicy,
        health: Mapping[str, HealthObservation] | None = None,
        history: Mapping[str, float] | None = None,
        explore_rate: float = 0.0,
        explore_pool_size: int = 2,
        rng: Callable[[], float] | None = None,
    ) -> RoutePreview:
        """Evaluate a route without minting a decision id (MVP-0514).

        Applies the same constraints, scoring, and exploration parameters as
        ``route`` so a preview shows exactly what a route would decide.
        """

        evaluation = self._evaluate(
            request,
            endpoints=endpoints,
            variants_by_id=variants_by_id,
            policy=policy,
            health=health,
            history=history,
            explore_rate=explore_rate,
            explore_pool_size=explore_pool_size,
            rng=rng,
        )
        return RoutePreview(
            request_id=request.id,
            policy_version=policy.policy_version,
            weights=evaluation.weights,
            candidate_endpoint_ids=evaluation.candidate_endpoint_ids,
            excluded_reasons=evaluation.excluded_reasons,
            score_factors=evaluation.scores,
            total_scores=evaluation.totals,
            ranking=evaluation.ranking,
            selected_endpoint_id=evaluation.selected,
            fallback_endpoint_ids=evaluation.fallbacks,
            explored=evaluation.explored,
            notes=evaluation.notes,
            explanation=evaluation.explanation,
        )

    def _evaluate(
        self,
        request: RouteRequest,
        *,
        endpoints: Sequence[ProviderEndpoint],
        variants_by_id: Mapping[str, ModelVariant],
        policy: RoutingPolicy,
        health: Mapping[str, HealthObservation] | None,
        history: Mapping[str, float] | None,
        explore_rate: float,
        explore_pool_size: int,
        rng: Callable[[], float] | None,
    ) -> _Evaluation:
        if explore_rate < 0 or explore_rate > 1:
            raise ValueError("explore_rate must lie in [0, 1]")
        if explore_pool_size < 1:
            raise ValueError("explore_pool_size must be at least 1")

        candidates = assemble_candidates(endpoints, variants_by_id)
        weights = policy.normalized_weights()

        excluded: dict[str, str] = {}
        survivors: list[RouteCandidate] = []
        for candidate in candidates:
            reason = _hard_constraint_reason(request, candidate)
            if reason is None:
                survivors.append(candidate)
            else:
                excluded[candidate.endpoint_id] = reason

        scores, notes = score_candidates(
            survivors,
            preferred_family_id=request.preferred_family_id,
            required_capabilities=request.required_capabilities,
            health=health,
            history=history,
        )
        totals = {
            endpoint_id: _clip_unit(
                sum(weights[factor] * factor_scores[factor] for factor in FACTOR_KEYS)
            )
            for endpoint_id, factor_scores in scores.items()
        }

        ranking = tuple(sorted(totals, key=lambda endpoint_id: (-totals[endpoint_id], endpoint_id)))
        selected = ranking[0] if ranking else None
        explored = False
        if selected is not None and explore_rate > 0 and len(ranking) >= 2:
            roller = rng if rng is not None else random.random
            if roller() < explore_rate:
                bound = min(explore_pool_size, len(ranking))
                index = min(int(roller() * bound), bound - 1)
                if index > 0:
                    selected = ranking[index]
                    explored = True
        fallbacks = tuple(endpoint_id for endpoint_id in ranking if endpoint_id != selected)

        explanation = _explain(
            request,
            policy=policy,
            weights=weights,
            excluded=excluded,
            totals=totals,
            ranking=ranking,
            selected=selected,
            explored=explored,
            scores=scores,
            notes=notes,
        )
        return _Evaluation(
            weights=weights,
            candidate_endpoint_ids=tuple(candidate.endpoint_id for candidate in candidates),
            excluded_reasons=excluded,
            scores=scores,
            notes=notes,
            totals=totals,
            ranking=ranking,
            selected=selected,
            fallbacks=fallbacks,
            explored=explored,
            explanation=explanation,
        )
