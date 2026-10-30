"""Explainable router tests (MVP-0501..0515)."""

from __future__ import annotations

from collections.abc import Callable
from itertools import chain, repeat
from typing import Any, cast
from uuid import UUID

import pytest

from total_orch.domain import (
    ModelCapabilities,
    ModelVariant,
    ProviderConnectionRef,
    ProviderEndpoint,
    RouteRequest,
)
from total_orch.router import (
    FACTOR_KEYS,
    POLICY_PRESETS,
    ExplainableRouter,
    HealthObservation,
    RouteOutcome,
    RoutePreview,
    RoutingPolicy,
    assemble_candidates,
    policy_from_preferences,
    preset_policy,
    score_candidates,
)

LUNA = "openai/luna:luna-5.6-pro"
NOVA = "openai/nova:nova-2"
FERRET = "openai/ferret:ferret-8b"

A = "provider-a/luna-5.6-pro"
B = "provider-b/luna-5.6-pro"
C = "provider-b/nova-2"
D = "provider-a/nova-2"
F = "provider-a/ferret-8b"


def _variant(
    variant_id: str,
    family_id: str,
    name: str,
    *,
    context_window: int | None,
    **capabilities: bool,
) -> ModelVariant:
    return ModelVariant(
        id=variant_id,
        family_id=family_id,
        name=name,
        context_window=context_window,
        capabilities=ModelCapabilities(**cast(Any, capabilities)),
    )


def _endpoint(
    endpoint_id: str,
    variant_id: str,
    *,
    health: float,
    latency_p95_ms: float,
    input_cost_per_million: float,
    output_cost_per_million: float,
    **capabilities: bool,
) -> ProviderEndpoint:
    provider, provider_model_name = endpoint_id.split("/", 1)
    return ProviderEndpoint(
        id=endpoint_id,
        provider=provider,
        provider_model_name=provider_model_name,
        model_variant_id=variant_id,
        connection=ProviderConnectionRef(id=f"{provider}-account-1", provider=provider),
        capabilities=ModelCapabilities(**cast(Any, capabilities)),
        health=health,
        latency_p95_ms=latency_p95_ms,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
    )


def _world() -> tuple[dict[str, ModelVariant], dict[str, ProviderEndpoint]]:
    """The three-family deployment from the profile tests.

    - Luna 5.6 Pro (128k, every capability True): ``A`` (fast, expensive) and
      ``B`` (slow, cheap, endpoint-level claims).
    - Nova 2 (8k, tool calling + streaming): ``C`` (denies reasoning) and
      ``D`` (claims nothing).
    - Ferret 8B (40k, no tool calling): ``F``.
    """

    variants = {
        LUNA: _variant(
            LUNA,
            "openai/luna",
            "luna-5.6-pro",
            context_window=128_000,
            tool_calling=True,
            reasoning=True,
            streaming=True,
            structured_output=True,
        ),
        NOVA: _variant(
            NOVA,
            "openai/nova",
            "nova-2",
            context_window=8_000,
            tool_calling=True,
            streaming=True,
        ),
        FERRET: _variant(
            FERRET,
            "openai/ferret",
            "ferret-8b",
            context_window=40_000,
            reasoning=True,
            structured_output=True,
            tool_calling=False,
        ),
    }
    endpoints = {
        A: _endpoint(A, LUNA, health=0.99, latency_p95_ms=120, input_cost_per_million=3.0, output_cost_per_million=15.0),
        B: _endpoint(
            B,
            LUNA,
            health=0.95,
            latency_p95_ms=300,
            input_cost_per_million=1.5,
            output_cost_per_million=7.5,
            tool_calling=True,
            reasoning=True,
            streaming=True,
        ),
        C: _endpoint(
            C,
            NOVA,
            health=0.9,
            latency_p95_ms=90,
            input_cost_per_million=0.25,
            output_cost_per_million=1.0,
            reasoning=False,
        ),
        D: _endpoint(D, NOVA, health=0.8, latency_p95_ms=150, input_cost_per_million=0.4, output_cost_per_million=2.0),
        F: _endpoint(F, FERRET, health=0.7, latency_p95_ms=200, input_cost_per_million=0.1, output_cost_per_million=0.4),
    }
    return variants, endpoints


def _request(**overrides: object) -> RouteRequest:
    fields: dict[str, object] = {
        "agent_profile_id": "coder-default",
        "task_type": "bug_fix",
    }
    fields.update(overrides)
    return RouteRequest(**fields)  # type: ignore[arg-type]


def _preview(
    request: RouteRequest,
    endpoint_ids: tuple[str, ...],
    policy: RoutingPolicy | None = None,
    **kwargs: object,
) -> RoutePreview:
    variants, endpoints = _world()
    return ExplainableRouter().preview(
        request,
        endpoints=[endpoints[endpoint_id] for endpoint_id in endpoint_ids],
        variants_by_id=variants,
        policy=policy or preset_policy("balanced"),
        **kwargs,  # type: ignore[arg-type]
    )


def _route(
    request: RouteRequest,
    endpoint_ids: tuple[str, ...],
    policy: RoutingPolicy | None = None,
    **kwargs: object,
) -> RouteOutcome:
    variants, endpoints = _world()
    return ExplainableRouter().route(
        request,
        endpoints=[endpoints[endpoint_id] for endpoint_id in endpoint_ids],
        variants_by_id=variants,
        policy=policy or preset_policy("balanced"),
        **kwargs,  # type: ignore[arg-type]
    )


def _rng(values: list[float]) -> Callable[[], float]:
    iterator = iter(chain(values, repeat(0.0)))
    return lambda: next(iterator)


# -- hard constraints (MVP-0501) ----------------------------------------------


def test_denied_capability_excludes_endpoint_before_scoring() -> None:
    request = _request(required_capabilities=ModelCapabilities(reasoning=True))
    preview = _preview(request, (C, D))

    # Candidates come back sorted by endpoint id: D (provider-a) before C.
    assert preview.candidate_endpoint_ids == (D, C)
    assert preview.excluded_reasons == {
        C: "reasoning claimed False",
        D: "reasoning unknown - not claimed by endpoint or variant metadata",
    }
    assert preview.selected_endpoint_id is None
    assert preview.score_factors == {}
    assert preview.fallback_endpoint_ids == ()
    assert "Excluded by hard constraints before scoring" in preview.explanation
    assert f"{C}: reasoning claimed False" in preview.explanation
    assert "No candidate satisfied the hard constraints" in preview.explanation


def test_unknown_context_window_fails_the_hard_floor() -> None:
    variants, _ = _world()
    blind = _variant("openai/blind:blind-1", "openai/blind", "blind-1", context_window=None, tool_calling=True)
    endpoint = _endpoint(
        "provider-a/blind-1",
        "openai/blind:blind-1",
        health=0.99,
        latency_p95_ms=100,
        input_cost_per_million=1.0,
        output_cost_per_million=1.0,
    )
    request = _request(minimum_context_window=1_000)

    preview = ExplainableRouter().preview(
        request,
        endpoints=[endpoint],
        variants_by_id={**variants, blind.id: blind},
        policy=preset_policy("balanced"),
    )
    assert preview.excluded_reasons == {
        "provider-a/blind-1": "context window unknown - no variant record claims one"
    }


def test_context_window_below_the_floor_is_excluded() -> None:
    request = _request(minimum_context_window=64_000)
    preview = _preview(request, (A, F))

    assert preview.excluded_reasons == {F: "context window 40000 < 64000"}
    assert preview.selected_endpoint_id == A


def test_router_never_selects_an_endpoint_that_fails_a_hard_constraint() -> None:
    request = _request(required_capabilities=ModelCapabilities(tool_calling=True))
    outcome = _route(request, (A, B, F))

    assert outcome.decision.selected_endpoint_id == A
    assert outcome.decision.selected_endpoint_id not in outcome.decision.excluded_reasons
    assert F in outcome.decision.excluded_reasons


# -- per-factor scoring (MVP-0502..0508) ---------------------------------------


def test_role_fit_follows_the_preferred_family() -> None:
    request = _request(preferred_family_id="openai/luna")
    preview = _preview(request, (A, B, F))

    assert preview.score_factors[A]["role_fit"] == 1.0
    assert preview.score_factors[B]["role_fit"] == 1.0
    assert preview.score_factors[F]["role_fit"] == 0.0
    assert "not the preferred family openai/luna" in preview.notes[F]["role_fit"]


def test_role_fit_without_preference_scores_every_candidate_one() -> None:
    preview = _preview(_request(), (A, F))

    assert preview.score_factors[A]["role_fit"] == 1.0
    assert preview.score_factors[F]["role_fit"] == 1.0
    assert "no preferred family" in preview.notes[A]["role_fit"]


def test_capability_fit_counts_satisfied_requirements() -> None:
    _, endpoints = _world()
    request = _request(required_capabilities=ModelCapabilities(tool_calling=True, reasoning=True))
    scores, notes = score_candidates(
        assemble_candidates([endpoints[F]], _world()[0]),
        required_capabilities=request.required_capabilities,
    )
    # Ferret claims reasoning True but tool calling False: one of two.
    assert scores[F]["capability_fit"] == 0.5
    assert notes[F]["capability_fit"] == "satisfies 1 of 2 required capabilities"


def test_capability_fit_is_one_for_survivors_of_the_hard_filter() -> None:
    request = _request(required_capabilities=ModelCapabilities(tool_calling=True))
    preview = _preview(request, (A, B, F))

    assert preview.score_factors[A]["capability_fit"] == 1.0
    assert preview.score_factors[B]["capability_fit"] == 1.0
    assert preview.notes[A]["capability_fit"] == "satisfies 1 of 1 required capabilities"


def test_quality_scores_context_window_relative_to_the_pool() -> None:
    preview = _preview(_request(), (A, F))

    assert preview.score_factors[A]["quality"] == 1.0
    assert preview.score_factors[F]["quality"] == pytest.approx(40_000 / 128_000)
    assert "largest context window" in preview.notes[A]["quality"]


def test_health_combines_endpoint_health_with_error_rate() -> None:
    _, endpoints = _world()
    scores, notes = score_candidates(
        assemble_candidates([endpoints[A]], _world()[0]),
        health={A: HealthObservation(error_rate=0.5)},
    )
    assert scores[A]["health"] == pytest.approx(0.99 * 0.5)
    assert "adjusted by error rate 0.50" in notes[A]["health"]


def test_health_falls_back_to_error_rate_alone() -> None:
    _, endpoints = _world()
    bare = endpoints[A].model_copy(update={"health": None})
    scores, notes = score_candidates(
        assemble_candidates([bare], _world()[0]),
        health={A: HealthObservation(error_rate=0.2)},
    )
    assert scores[A]["health"] == pytest.approx(0.8)
    assert "health unknown" in notes[A]["health"]


def test_latency_scores_relative_to_the_pool_range() -> None:
    preview = _preview(_request(), (A, B, F))

    assert preview.score_factors[A]["latency"] == 1.0
    assert preview.score_factors[B]["latency"] == 0.0
    assert preview.score_factors[F]["latency"] == pytest.approx((300 - 200) / (300 - 120))
    assert "fastest in the candidate pool" in preview.notes[A]["latency"]


def test_cost_scores_relative_to_the_pool_price_range() -> None:
    preview = _preview(_request(), (A, B, F))

    assert preview.score_factors[F]["cost"] == 1.0
    assert preview.score_factors[A]["cost"] == 0.0
    assert preview.score_factors[B]["cost"] == pytest.approx((18.0 - 9.0) / (18.0 - 0.5))
    assert "cheapest in the candidate pool" in preview.notes[F]["cost"]


def test_quota_is_absolute_and_clipped() -> None:
    _, endpoints = _world()
    crowded = endpoints[A].model_copy(update={"quota_remaining": 1.5})
    scores, notes = score_candidates(assemble_candidates([crowded], _world()[0]))
    assert scores[A]["quota"] == 1.0
    assert notes[A]["quota"] == "quota remaining 1.50"


def test_unknown_observations_score_neutral_half() -> None:
    variants, endpoints = _world()
    bare = endpoints[A].model_copy(
        update={
            "health": None,
            "latency_p95_ms": None,
            "input_cost_per_million": None,
            "output_cost_per_million": None,
        }
    )
    preview = ExplainableRouter().preview(
        _request(),
        endpoints=[bare],
        variants_by_id=variants,
        policy=preset_policy("balanced"),
    )

    for factor in ("health", "latency", "cost", "quota", "history"):
        assert preview.score_factors[bare.id][factor] == 0.5, factor
        assert "unknown" in preview.notes[bare.id][factor], factor


def test_history_uses_the_supplied_success_rate() -> None:
    _, endpoints = _world()
    scores, notes = score_candidates(
        assemble_candidates([endpoints[B]], _world()[0]),
        history={B: 0.75},
    )
    assert scores[B]["history"] == pytest.approx(0.75)
    assert notes[B]["history"] == "historical success rate 0.75"


def test_every_candidate_is_scored_on_every_factor() -> None:
    preview = _preview(_request(), (A, B, F))
    for endpoint_id, factors in preview.score_factors.items():
        assert set(factors) == set(FACTOR_KEYS), endpoint_id
        assert all(0 <= score <= 1 for score in factors.values()), endpoint_id


# -- policies and presets (MVP-0509) -------------------------------------------


def test_five_policy_presets_exist_with_normalized_weights() -> None:
    assert set(POLICY_PRESETS) == {
        "quality-first",
        "balanced",
        "cost-first",
        "latency-first",
        "reliable-first",
    }
    for name, policy in POLICY_PRESETS.items():
        weights = policy.normalized_weights()
        assert sum(weights.values()) == pytest.approx(1.0), name
        assert set(weights) == set(FACTOR_KEYS), name


def test_preset_policy_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown policy preset"):
        preset_policy("cheapest-possible")


def test_policy_rejects_unknown_factors_and_negative_weights() -> None:
    with pytest.raises(ValueError, match="unknown scoring factors"):
        RoutingPolicy(id="broken", weights={"vibes": 1.0})
    with pytest.raises(ValueError, match="non-negative"):
        RoutingPolicy(id="broken", weights={"cost": -0.5})
    with pytest.raises(ValueError, match="positive total weight"):
        RoutingPolicy(id="broken", weights={"cost": 0.0})


def test_policy_version_is_recorded() -> None:
    assert preset_policy("balanced").policy_version == "balanced-v1"


def test_policy_from_preferences_spreads_axes_over_factors() -> None:
    policy = policy_from_preferences({"quality": 0.5, "reliability": 0.3, "cost": 0.1, "latency": 0.1})

    assert policy.id == "role-derived"
    assert policy.policy_version == "role-derived-v1"
    weights = policy.normalized_weights()
    assert weights["role_fit"] == pytest.approx(0.5 / 3)
    assert weights["capability_fit"] == pytest.approx(0.5 / 3)
    assert weights["quality"] == pytest.approx(0.5 / 3)
    assert weights["health"] == pytest.approx(0.1)
    assert weights["quota"] == pytest.approx(0.1)
    assert weights["history"] == pytest.approx(0.1)
    assert weights["latency"] == pytest.approx(0.1)
    assert weights["cost"] == pytest.approx(0.1)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_policy_from_preferences_requires_a_supported_axis() -> None:
    with pytest.raises(ValueError, match="cannot derive a policy"):
        policy_from_preferences({"sparkle": 1.0})


# -- weighted totals, tie-breaking, determinism (MVP-0509, 0510) ----------------


def test_weighted_total_combines_factor_scores_with_policy_weights() -> None:
    preview = _preview(_request(), (A,))

    expected = sum(
        preview.weights[factor] * preview.score_factors[A][factor] for factor in FACTOR_KEYS
    )
    assert preview.total_scores[A] == pytest.approx(expected)


def test_balanced_policy_ranks_the_fast_expensive_luna_first() -> None:
    preview = _preview(_request(), (A, B, F))

    assert preview.ranking == (A, B, F)
    assert preview.total_scores[A] > preview.total_scores[B] > preview.total_scores[F]


def test_cost_first_policy_flips_the_ranking() -> None:
    preview = _preview(_request(), (A, B, F))
    cost_preview = _preview(_request(), (A, B, F), policy=preset_policy("cost-first"))

    assert preview.ranking[0] == A
    assert cost_preview.ranking[0] == F
    assert cost_preview.total_scores[F] > cost_preview.total_scores[B] > cost_preview.total_scores[A]


def test_equal_totals_tie_break_on_endpoint_id() -> None:
    variants, _ = _world()
    first = _endpoint(
        "provider-a/twin",
        LUNA,
        health=0.9,
        latency_p95_ms=100,
        input_cost_per_million=1.0,
        output_cost_per_million=1.0,
    )
    second = _endpoint(
        "provider-b/twin",
        LUNA,
        health=0.9,
        latency_p95_ms=100,
        input_cost_per_million=1.0,
        output_cost_per_million=1.0,
    )
    preview = ExplainableRouter().preview(
        _request(),
        endpoints=[second, first],
        variants_by_id=variants,
        policy=preset_policy("balanced"),
    )
    assert preview.ranking == ("provider-a/twin", "provider-b/twin")
    assert preview.selected_endpoint_id == "provider-a/twin"


def test_same_input_and_policy_produce_the_same_decision() -> None:
    request = _request()
    first = _route(request, (A, B, F)).decision
    second = _route(request, (A, B, F)).decision

    assert first.request_id == second.request_id
    assert first.selected_endpoint_id == second.selected_endpoint_id
    assert first.candidate_endpoint_ids == second.candidate_endpoint_ids
    assert first.total_scores == second.total_scores
    assert first.fallback_endpoint_ids == second.fallback_endpoint_ids
    assert first.score_factors == second.score_factors


# -- bounded exploration (MVP-0511) ---------------------------------------------


def test_exploration_promotes_a_runner_up_and_flags_the_decision() -> None:
    preview = _preview(_request(), (A, B, F), explore_rate=1.0, rng=_rng([0.0, 0.99]))

    assert preview.explored is True
    assert preview.selected_endpoint_id == preview.ranking[1]
    assert preview.fallback_endpoint_ids == tuple(
        endpoint_id for endpoint_id in preview.ranking if endpoint_id != preview.selected_endpoint_id
    )


def test_exploration_pool_size_bounds_the_promotion_window() -> None:
    preview = _preview(_request(), (A, B, F), explore_rate=1.0, explore_pool_size=3, rng=_rng([0.0, 0.95]))

    assert preview.explored is True
    assert preview.selected_endpoint_id == preview.ranking[2]


def test_low_explore_roll_keeps_the_strict_best() -> None:
    preview = _preview(_request(), (A, B, F), explore_rate=1.0, rng=_rng([0.0, 0.0]))

    assert preview.explored is False
    assert preview.selected_endpoint_id == preview.ranking[0]


def test_zero_explore_rate_never_rolls_the_dice() -> None:
    def exploding_rng() -> float:
        raise AssertionError("rng must not be called when exploration is disabled")

    preview = _preview(_request(), (A, B, F), explore_rate=0.0, rng=exploding_rng)

    assert preview.explored is False
    assert preview.selected_endpoint_id == A


def test_exploration_parameters_are_validated() -> None:
    request = _request()
    with pytest.raises(ValueError, match="explore_rate"):
        _preview(request, (A,), explore_rate=1.5)
    with pytest.raises(ValueError, match="explore_pool_size"):
        _preview(request, (A,), explore_pool_size=0)


def test_exploration_never_selects_an_excluded_endpoint() -> None:
    request = _request(required_capabilities=ModelCapabilities(tool_calling=True))
    outcome = _route(request, (A, F), explore_rate=1.0, rng=_rng([0.0, 0.99]))

    # F fails the hard filter, so only A survives: nothing to explore.
    assert outcome.decision.explored is False
    assert outcome.decision.selected_endpoint_id == A


# -- fallback ordering (MVP-0512) -----------------------------------------------


def test_fallback_list_is_the_ranked_order_without_the_selection() -> None:
    outcome = _route(_request(), (A, B, F))

    assert outcome.decision.selected_endpoint_id == A
    assert outcome.decision.fallback_endpoint_ids == (B, F)


def test_fallback_list_is_empty_for_a_single_candidate() -> None:
    outcome = _route(_request(), (A,))

    assert outcome.decision.selected_endpoint_id == A
    assert outcome.decision.fallback_endpoint_ids == ()


# -- decisions, snapshots, and explanations (MVP-0513, 0515) ---------------------


def test_decision_snapshots_policy_weights_and_version() -> None:
    outcome = _route(_request(), (A, B))

    assert outcome.decision.policy_version == "balanced-v1"
    assert outcome.decision.factor_weights == pytest.approx(preset_policy("balanced").normalized_weights())
    assert set(outcome.decision.total_scores) == {A, B}


def test_decision_links_to_its_request() -> None:
    request = _request()
    outcome = _route(request, (A,))

    assert outcome.decision.request_id == request.id
    assert isinstance(outcome.decision.id, UUID)
    assert outcome.decision.selected_endpoint_id in outcome.decision.candidate_endpoint_ids


def test_route_and_preview_agree() -> None:
    request = _request()
    outcome = _route(request, (A, B, F))
    preview = _preview(request, (A, B, F))

    assert preview.selected_endpoint_id == outcome.decision.selected_endpoint_id
    assert preview.total_scores == outcome.decision.total_scores
    assert preview.ranking == (outcome.decision.selected_endpoint_id, *outcome.decision.fallback_endpoint_ids)


def test_explanation_reports_scores_weights_and_contributions() -> None:
    outcome = _route(_request(), (A, B))

    text = outcome.explanation
    assert "policy balanced-v1" in text
    assert f"Selected {A} with total score" in text
    assert "Per-factor contributions" in text
    for factor in FACTOR_KEYS:
        assert factor in outcome.notes[A], factor
    assert "Fallback order:" in text
    assert B in text


def test_explanation_covers_an_empty_pool() -> None:
    outcome = _route(_request(), ())

    assert outcome.decision.selected_endpoint_id is None
    assert outcome.decision.candidate_endpoint_ids == ()
    assert "candidate pool is empty" in outcome.explanation


def test_notes_explain_every_factor_of_the_selection() -> None:
    outcome = _route(_request(), (A, B))

    for endpoint_id in (A, B):
        assert set(outcome.notes[endpoint_id]) == set(FACTOR_KEYS)
        assert all(outcome.notes[endpoint_id][factor] for factor in FACTOR_KEYS)


def test_exploration_is_visible_in_the_explanation() -> None:
    outcome = _route(_request(), (A, B), explore_rate=1.0, rng=_rng([0.0, 0.99]))

    assert outcome.decision.explored is True
    assert "bounded exploration" in outcome.explanation
