"""RouteDecision persistence tests (MVP-0513)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from total_orch.domain import RouteDecision, RouteRequest
from total_orch.router import RouteDecisionStore


def _decision(**overrides: object) -> RouteDecision:
    request = RouteRequest(agent_profile_id="coder-default", task_type="bug_fix")
    fields: dict[str, object] = {
        # Fresh ids per call so distinct decisions never collide by request.
        "request_id": request.id,
        "selected_endpoint_id": "provider-a/luna-5.6-pro",
        "candidate_endpoint_ids": ("provider-a/luna-5.6-pro", "provider-b/luna-5.6-pro"),
        "excluded_reasons": {"provider-a/nova-2": "reasoning claimed False"},
        "score_factors": {
            "provider-a/luna-5.6-pro": {"role_fit": 1.0, "cost": 0.0},
            "provider-b/luna-5.6-pro": {"role_fit": 1.0, "cost": 1.0},
        },
        "factor_weights": {"role_fit": 0.5, "cost": 0.5},
        "total_scores": {"provider-a/luna-5.6-pro": 0.5, "provider-b/luna-5.6-pro": 1.0},
        "fallback_endpoint_ids": ("provider-b/luna-5.6-pro",),
        "policy_version": "balanced-v1",
    }
    fields.update(overrides)
    return RouteDecision(**fields)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path) -> RouteDecisionStore:
    decision_store = RouteDecisionStore(str(tmp_path / "routing.db"))
    decision_store.initialize()
    try:
        yield decision_store
    finally:
        decision_store.close()


def test_save_and_reload_round_trip_preserves_every_field(store: RouteDecisionStore) -> None:
    decision = _decision()

    assert store.save_decision(decision) is True
    reloaded = store.get_decision(decision.id)

    assert reloaded == decision
    assert reloaded.score_factors == decision.score_factors
    assert reloaded.factor_weights == decision.factor_weights
    assert reloaded.total_scores == decision.total_scores
    assert reloaded.fallback_endpoint_ids == decision.fallback_endpoint_ids
    assert reloaded.excluded_reasons == decision.excluded_reasons
    assert reloaded.explored is False


def test_save_is_idempotent_on_decision_id(store: RouteDecisionStore) -> None:
    decision = _decision()
    store.save_decision(decision)
    duplicate = _decision(id=decision.id)

    assert store.save_decision(duplicate) is False
    assert store.get_decision(decision.id) == decision


def test_decisions_for_request_returns_all_decisions_of_one_request(store: RouteDecisionStore) -> None:
    first = _decision()
    second = _decision(request_id=first.request_id)
    other = _decision()
    for decision in (first, second, other):
        store.save_decision(decision)

    for_request = store.decisions_for_request(first.request_id)

    assert {decision.id for decision in for_request} == {first.id, second.id}
    assert other.request_id not in {decision.request_id for decision in for_request}


def test_list_decisions_filters_by_policy_and_selection(store: RouteDecisionStore) -> None:
    balanced = _decision()
    cost_first = _decision(policy_version="cost-first-v1")
    explored = _decision(explored=True, selected_endpoint_id="provider-b/luna-5.6-pro")
    for decision in (balanced, cost_first, explored):
        store.save_decision(decision)

    assert [d.id for d in store.list_decisions(policy_version="cost-first-v1")] == [cost_first.id]
    assert [d.id for d in store.list_decisions(selected_endpoint_id="provider-a/luna-5.6-pro")] == [
        balanced.id,
        cost_first.id,
    ]
    assert [d.id for d in store.list_decisions(explored=True)] == [explored.id]
    assert [d.id for d in store.list_decisions(limit=2)] == [balanced.id, cost_first.id]


def test_list_decisions_orders_oldest_first(store: RouteDecisionStore) -> None:
    early = _decision()
    late = _decision()
    store.save_decision(late)
    store.save_decision(early)

    decisions = store.list_decisions()

    assert [d.id for d in decisions] == [early.id, late.id]


def test_get_unknown_decision_raises_key_error(store: RouteDecisionStore) -> None:
    with pytest.raises(KeyError, match="unknown route decision"):
        store.get_decision(uuid4())


def test_negative_limit_is_rejected(store: RouteDecisionStore) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.list_decisions(limit=-1)


def test_count_and_reinitialize(store: RouteDecisionStore, tmp_path) -> None:
    store.save_decision(_decision())
    assert store.count_decisions() == 1

    store.close()
    reopened = RouteDecisionStore(str(tmp_path / "routing.db"))
    reopened.initialize()
    try:
        assert reopened.count_decisions() == 1
    finally:
        reopened.close()
