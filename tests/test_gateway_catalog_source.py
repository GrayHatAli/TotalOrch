"""GatewayCatalogSource ingestion through non-OmniRoute gateways.

The registry must ingest identically from any adapter behind the
``GatewayAdapter`` boundary, with per-gateway provenance labels so fields
observed through different gateways stay distinguishable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_openai_compat_gateway import StubTransport, responses
from total_orch.domain import ModelKind
from total_orch.gateway import GatewayAdapter, GatewayModel, OpenAICompatAdapter
from total_orch.gateway.omniroute import HttpResponse
from total_orch.registry import GatewayCatalogSource, IdentityRules, ModelsEndpointCatalogSource


@pytest.mark.asyncio
async def test_generic_source_uses_router_name_as_provenance() -> None:
    catalog = {
        "data": [
            {
                "id": "openai/gpt-4o",
                "context_length": 128000,
                "capabilities": {"tool_calling": True, "reasoning": True},
            },
            {"id": "openai/gpt-4o-mini", "context_length": 128000},
        ]
    }
    transport = StubTransport(responses(catalog, 2))
    adapter = OpenAICompatAdapter(
        base_url="http://router.test",
        router_name="LiteLLM",
        transport=transport,
    )
    source = GatewayCatalogSource(adapter, IdentityRules(), source="litellm")

    batch = await source.fetch()

    assert len(batch.variants) == 2
    assert len(batch.endpoints) == 2
    gpt4o = batch.variants[0]
    assert gpt4o.source == "litellm"
    assert gpt4o.fields["context_window"].value == 128000
    assert gpt4o.fields["context_window"].provenance.source == "litellm"
    assert batch.endpoints[0].fields["cap.tool_calling"].provenance.source == "litellm"
    # The serving-provider qualifier is stripped; the publisher prefix is
    # recovered through identity resolution (provider == publisher here).
    endpoint = batch.endpoints[0].endpoint
    assert endpoint is not None
    assert endpoint.provider_model_name == "gpt-4o"
    assert endpoint.model_variant_id == "openai/gpt-4o:gpt-4o"
    assert endpoint.id == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_generic_source_defaults_to_gateway_label() -> None:
    catalog = {"data": [{"id": "openai/gpt-4o", "context_length": 128000}]}
    transport = StubTransport(responses(catalog, 2))
    adapter = OpenAICompatAdapter(base_url="http://router.test", transport=transport)
    source = GatewayCatalogSource(adapter, IdentityRules())

    batch = await source.fetch()

    assert batch.variants[0].source == "gateway"


@pytest.mark.asyncio
async def test_two_gateways_serving_one_model_keep_distinct_provenance() -> None:
    """One variant, two endpoints, two provenance labels."""

    rules = IdentityRules()
    payloads = (
        HttpResponse(200, {"data": [{"id": "openai/gpt-4o", "context_length": 128000}]}, {}),
        HttpResponse(200, {"data": [{"id": "openai/gpt-4o", "context_length": 64000}]}, {}),
    )
    batch_parts = []
    for index, payload in enumerate(payloads):
        adapter = OpenAICompatAdapter(
            base_url="http://router.test",
            transport=StubTransport([payload, payload]),
        )
        source = GatewayCatalogSource(adapter, rules, source="litellm" if index == 0 else "9router")
        batch_parts.append(await source.fetch())

    combined = batch_parts[0]
    combined.extend(batch_parts[1])

    assert {observation.variant_id for observation in combined.variants} == {"openai/gpt-4o:gpt-4o"}
    assert {observation.source for observation in combined.variants} == {"litellm", "9router"}
    assert {
        observation.endpoint.provider
        for observation in combined.endpoints
        if observation.endpoint is not None
    } == {"openai"}


@pytest.mark.asyncio
async def test_models_endpoint_source_does_not_require_management_api() -> None:
    adapter = Mock(spec=GatewayAdapter)
    adapter.list_models = AsyncMock(
        return_value=(
            GatewayModel(id="auto/best-coding", provider="auto", context_window=2_000_000),
            GatewayModel(id="openai/gpt-4o", provider=None, context_window=128_000),
            GatewayModel(id="bare-model", provider=None),
        )
    )
    adapter.list_connections = AsyncMock(side_effect=AssertionError("management API must not be called"))
    adapter.list_provider_models = AsyncMock(side_effect=AssertionError("management API must not be called"))

    source = ModelsEndpointCatalogSource(adapter, IdentityRules())

    batch = await source.fetch()

    assert len(batch.variants) == 3
    assert len(batch.endpoints) == 3
    assert {observation.endpoint.provider for observation in batch.endpoints if observation.endpoint} == {
        "auto",
        "openai",
        "default",
    }
    assert {observation.endpoint.id for observation in batch.endpoints if observation.endpoint} == {
        "auto/best-coding",
        "openai/gpt-4o",
        "default/bare-model",
    }
    adapter.list_models.assert_awaited_once_with()
    adapter.list_connections.assert_not_awaited()
    adapter.list_provider_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_models_endpoint_classifies_virtual_automatic_alias_and_concrete_entries() -> None:
    adapter = Mock(spec=GatewayAdapter)
    adapter.list_models = AsyncMock(
        return_value=(
            GatewayModel(id="auto/best-coding", provider="auto"),
            GatewayModel(id="virtual/fast", provider="router", metadata={"virtual": True}),
            GatewayModel(id="alias/coding", provider="router", metadata={"alias_of": "openai/gpt-4o"}),
            GatewayModel(id="openai/gpt-4o", provider="openai"),
        )
    )

    batch = await ModelsEndpointCatalogSource(adapter, IdentityRules()).fetch()

    kinds = {
        observation.variant_id: observation.fields["model_kind"].value
        for observation in batch.variants
        if observation.fields.get("model_kind") is not None
    }
    assert kinds == {
        "best-coding:best-coding": ModelKind.automatic,
        "virtual/fast:virtual/fast": ModelKind.virtual,
        "alias/coding:alias/coding": ModelKind.alias,
        "openai/gpt-4o:gpt-4o": ModelKind.concrete,
    }


@pytest.mark.asyncio
async def test_gateway_lifecycle_metadata_is_normalized() -> None:
    catalog = {
        "data": [
            {"id": "openai/gpt-4o", "context_length": 128000, "deprecated": True},
            {"id": "openai/gpt-5", "context_length": 128000, "status": "preview"},
        ]
    }
    adapter = OpenAICompatAdapter(
        base_url="http://router.test",
        transport=StubTransport(responses(catalog, 2)),
    )
    source = GatewayCatalogSource(adapter, IdentityRules())

    batch = await source.fetch()

    assert batch.variants[0].fields["lifecycle_state"].value == "deprecated"
    assert batch.variants[1].fields["lifecycle_state"].value == "preview"
