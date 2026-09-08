import pytest
from pydantic import ValidationError

from total_orch.domain import (
    ModelCapabilities,
    ModelFamily,
    ModelPool,
    ModelVariant,
    ProviderConnectionRef,
    ProviderEndpoint,
    RouteDecision,
    RouteRequest,
    WorkflowDefinition,
    WorkflowTask,
)


def endpoint(endpoint_id: str, provider: str) -> ProviderEndpoint:
    connection = ProviderConnectionRef(id=f"{provider}-main", provider=provider)
    return ProviderEndpoint(
        id=endpoint_id,
        provider=provider,
        provider_model_name="luna-5.6-pro",
        model_variant_id="openai/luna:luna-5.6-pro",
        connection=connection,
        capabilities=ModelCapabilities(tool_calling=True, reasoning=True),
        health=0.95,
    )


def test_same_variant_can_have_multiple_provider_endpoints() -> None:
    family = ModelFamily(id="openai/luna", publisher="OpenAI")
    variant = ModelVariant(
        id="openai/luna:luna-5.6-pro",
        family_id=family.id,
        name="luna-5.6-pro",
        context_window=128_000,
    )

    first = endpoint("provider-a/luna-5.6-pro", "provider-a")
    second = endpoint("provider-b/luna-5.6-pro", "provider-b")

    assert variant.family_id == family.id
    assert first.model_variant_id == second.model_variant_id == variant.id
    assert first.connection.provider != second.connection.provider


def test_unknown_capabilities_are_not_optimistically_inferred() -> None:
    capabilities = ModelCapabilities()

    assert capabilities.tool_calling is None
    assert capabilities.reasoning is None


def test_invalid_capability_efforts_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelCapabilities(reasoning=False, available_efforts=("high",))


def test_route_decision_must_select_a_candidate() -> None:
    request = RouteRequest(agent_profile_id="coder", task_type="bug_fix")

    with pytest.raises(ValidationError):
        RouteDecision(
            request_id=request.id,
            selected_endpoint_id="provider-a/luna",
            candidate_endpoint_ids=("provider-b/luna",),
        )


def test_workflow_dependencies_are_validated() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition(
            id="workflow-1",
            task_type="feature",
            tasks=(WorkflowTask(id="coder", kind="coder", description="Implement", depends_on=("planner",)),),
        )


def test_contract_serialization_is_stable_and_versioned() -> None:
    pool = ModelPool(id="pool-coder", name="Coder", purpose="Tool-capable models", member_endpoint_ids=())
    payload = pool.model_dump(mode="json")

    assert payload["schema_version"] == 1
    assert payload["member_endpoint_ids"] == []
    assert "created_at" not in payload
