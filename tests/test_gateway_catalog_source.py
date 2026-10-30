"""GatewayCatalogSource ingestion through non-OmniRoute gateways.

The registry must ingest identically from any adapter behind the
``GatewayAdapter`` boundary, with per-gateway provenance labels so fields
observed through different gateways stay distinguishable.
"""

from __future__ import annotations

import pytest

from tests.test_openai_compat_gateway import StubTransport, responses
from total_orch.gateway import OpenAICompatAdapter
from total_orch.gateway.omniroute import HttpResponse
from total_orch.registry import GatewayCatalogSource, IdentityRules


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
