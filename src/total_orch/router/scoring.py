"""Per-factor scoring for the explainable router (MVP-0502..0508).

Every surviving candidate receives a score in ``[0, 1]`` for every factor,
where 1 is best. Semantics are deterministic and documented per factor:

- ``role_fit``: 1 for the request's preferred family (or for every candidate
  when no family is preferred), 0 otherwise.
- ``capability_fit``: the fraction of the request's hard capability
  requirements the candidate satisfies (all survivors score 1 because the
  hard filter already ran; the score is still recorded per endpoint).
- ``quality``: task-fit proxy from the variant's context window — the largest
  window in the pool scores 1 and smaller models score proportionally, since
  benchmark data lands in a later phase.
- ``health``: the endpoint health in ``[0, 1]``; when a ``HealthObservation``
  with an error rate is available the two are combined as
  ``health * (1 - error_rate)`` (MVP-0505).
- ``latency``: relative — the fastest observed latency in the pool scores 1,
  the slowest 0 (MVP-0506).
- ``cost``: relative inverse of the blended per-million price (input + output)
  — the cheapest candidate scores 1 (MVP-0507).
- ``quota``: absolute — ``quota_remaining`` clipped to ``[0, 1]`` (MVP-0507).
- ``history``: the supplied task-type success rate; absent data means unknown
  (MVP-0508).

An unknown observation never invents a value: the factor scores a neutral 0.5
and its note says so. Relative factors (latency, cost) ignore unknown
candidates when computing their range. The weighted total is computed by the
engine from these scores and the policy weights.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from total_orch.domain import ModelCapabilities
from total_orch.registry.capabilities import BOOL_CAPABILITY_FIELDS, capability_claim

from .candidates import RouteCandidate
from .policies import FACTOR_LABELS

NEUTRAL_SCORE = 0.5


def _neutral_note(factor: str) -> str:
    return f"{FACTOR_LABELS[factor]} unknown - no observation available; treated as neutral"


def _clip_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """Runtime error-rate observation for one endpoint (MVP-0505).

    ``error_rate`` is the share of recent requests that failed, in ``[0, 1]``.
    Phase 8 telemetry will produce these; callers may supply them now.
    """

    error_rate: float

    def __post_init__(self) -> None:
        if not isinstance(self.error_rate, (int, float)):
            raise TypeError("error_rate must be a number in [0, 1]")
        if self.error_rate < 0 or self.error_rate > 1:
            raise ValueError("error_rate must lie in [0, 1]")


def _required_bool_fields(capabilities: ModelCapabilities) -> tuple[str, ...]:
    return tuple(field for field in BOOL_CAPABILITY_FIELDS if getattr(capabilities, field) is not None)


def _score_health(candidate: RouteCandidate, observation: HealthObservation | None) -> tuple[float, str]:
    health = candidate.endpoint.health
    error_rate = None if observation is None else observation.error_rate
    if health is not None and error_rate is not None:
        combined = _clip_unit(health * (1.0 - error_rate))
        return combined, f"health {health:.2f} adjusted by error rate {error_rate:.2f}"
    if health is not None:
        return _clip_unit(health), f"health {health:.2f}"
    if error_rate is not None:
        return _clip_unit(1.0 - error_rate), f"health unknown; adjusted by error rate {error_rate:.2f}"
    return NEUTRAL_SCORE, _neutral_note("health")


def _quality_note(context_window: int | None, largest: int | None) -> tuple[float, str]:
    if context_window is None or largest is None or largest <= 0:
        return NEUTRAL_SCORE, _neutral_note("quality")
    share = context_window / largest
    if share >= 1.0:
        return 1.0, f"largest context window in the pool ({context_window:,} tokens)"
    return share, f"context window {context_window:,} of the pool maximum {largest:,} tokens"


def _latency(candidate: RouteCandidate, fastest: float | None, slowest: float | None) -> tuple[float, str]:
    value = candidate.endpoint.latency_p95_ms
    if value is None or fastest is None or slowest is None:
        return NEUTRAL_SCORE, _neutral_note("latency")
    span = slowest - fastest
    if span <= 0:
        return 1.0, f"latency {value:.0f}ms (single observed value in the pool)"
    score = (slowest - value) / span
    if score >= 1.0:
        note = f"latency {value:.0f}ms, fastest in the candidate pool (range {fastest:.0f}-{slowest:.0f}ms)"
    elif score <= 0.0:
        note = f"latency {value:.0f}ms, slowest in the candidate pool (range {fastest:.0f}-{slowest:.0f}ms)"
    else:
        note = f"latency {value:.0f}ms (range {fastest:.0f}-{slowest:.0f}ms)"
    return score, note


def _cost(candidate: RouteCandidate, cheapest: float | None, most_expensive: float | None) -> tuple[float, str]:
    index = _cost_index(candidate)
    if index is None or cheapest is None or most_expensive is None:
        return NEUTRAL_SCORE, _neutral_note("cost")
    span = most_expensive - cheapest
    if span <= 0:
        return 1.0, f"price ${index:.2f}/M (single observed price in the pool)"
    score = (most_expensive - index) / span
    if score >= 1.0:
        note = f"price ${index:.2f}/M, cheapest in the candidate pool"
    elif score <= 0.0:
        note = f"price ${index:.2f}/M, most expensive in the candidate pool"
    else:
        note = f"price ${index:.2f}/M"
    return score, note


def _cost_index(candidate: RouteCandidate) -> float | None:
    """Blended per-million-token price; unknown only when neither half is known."""

    input_cost = candidate.endpoint.input_cost_per_million
    output_cost = candidate.endpoint.output_cost_per_million
    if input_cost is None and output_cost is None:
        return None
    return float(input_cost or 0.0) + float(output_cost or 0.0)


def score_candidates(
    candidates: Sequence[RouteCandidate],
    *,
    preferred_family_id: str | None = None,
    required_capabilities: ModelCapabilities | None = None,
    health: Mapping[str, HealthObservation] | None = None,
    history: Mapping[str, float] | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, str]]]:
    """Score every candidate on every factor.

    Returns ``(scores, notes)``, both keyed by endpoint id then factor.
    ``scores`` feeds the weighted total and ``RouteDecision.score_factors``;
    ``notes`` explains each value in human-readable form.
    """

    candidates = tuple(sorted(candidates, key=lambda candidate: candidate.endpoint_id))
    largest_context = max(
        (candidate.context_window for candidate in candidates if candidate.context_window is not None),
        default=None,
    )
    latencies = [
        float(candidate.endpoint.latency_p95_ms)
        for candidate in candidates
        if candidate.endpoint.latency_p95_ms is not None
    ]
    costs = [value for value in (_cost_index(candidate) for candidate in candidates) if value is not None]
    fastest = min(latencies) if latencies else None
    slowest = max(latencies) if latencies else None
    cheapest = min(costs) if costs else None
    most_expensive = max(costs) if costs else None

    health = health or {}
    history = history or {}
    required = required_capabilities or ModelCapabilities()
    required_fields = _required_bool_fields(required)

    scores: dict[str, dict[str, float]] = {}
    notes: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        endpoint_id = candidate.endpoint_id
        factor_scores: dict[str, float] = {}
        factor_notes: dict[str, str] = {}

        # Role fit (MVP-0502).
        preferred = preferred_family_id
        if preferred is None:
            factor_scores["role_fit"] = 1.0
            factor_notes["role_fit"] = "no preferred family on the request; every pool member fits the role"
        elif candidate.family_id == preferred:
            factor_scores["role_fit"] = 1.0
            factor_notes["role_fit"] = f"serves the preferred family {preferred}"
        else:
            factor_scores["role_fit"] = 0.0
            factor_notes["role_fit"] = (
                f"serves family {candidate.family_id or '(unknown)'}, not the preferred family {preferred}"
            )

        # Capability fit (MVP-0503).
        if not required_fields:
            factor_scores["capability_fit"] = 1.0
            factor_notes["capability_fit"] = "the request adds no hard capability requirements"
        else:
            satisfied = sum(
                1
                for field in required_fields
                if capability_claim(candidate.capabilities, candidate.capability_origin, field)[0]
            )
            factor_scores["capability_fit"] = satisfied / len(required_fields)
            factor_notes["capability_fit"] = (
                f"satisfies {satisfied} of {len(required_fields)} required capabilities"
            )

        # Quality / task fit (MVP-0504).
        factor_scores["quality"], factor_notes["quality"] = _quality_note(
            candidate.context_window, largest_context
        )

        # Health and error rate (MVP-0505).
        observation = health.get(endpoint_id)
        factor_scores["health"], factor_notes["health"] = _score_health(candidate, observation)

        # Latency (MVP-0506).
        factor_scores["latency"], factor_notes["latency"] = _latency(candidate, fastest, slowest)

        # Cost and quota (MVP-0507).
        factor_scores["cost"], factor_notes["cost"] = _cost(candidate, cheapest, most_expensive)
        quota = candidate.endpoint.quota_remaining
        if quota is None:
            factor_scores["quota"] = NEUTRAL_SCORE
            factor_notes["quota"] = _neutral_note("quota")
        else:
            factor_scores["quota"] = _clip_unit(float(quota))
            factor_notes["quota"] = f"quota remaining {quota:.2f}"

        # Historical task outcome (MVP-0508).
        success_rate = history.get(endpoint_id)
        if success_rate is None:
            factor_scores["history"] = NEUTRAL_SCORE
            factor_notes["history"] = _neutral_note("history")
        else:
            factor_scores["history"] = _clip_unit(float(success_rate))
            factor_notes["history"] = f"historical success rate {success_rate:.2f}"

        scores[endpoint_id] = factor_scores
        notes[endpoint_id] = factor_notes

    return scores, notes
