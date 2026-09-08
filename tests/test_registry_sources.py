"""Catalog source ingestion tests (MVP-0302..0304, 0307..0310).

Each ingestor is exercised against canned gateway state or canned HTTP
payloads so expectations never depend on a live catalog.
"""

from __future__ import annotations

import pytest

from total_orch.domain import ProviderConnectionRef
from total_orch.gateway import FakeGatewayAdapter, GatewayModel
from total_orch.registry import (
    IdentityRules,
    ModelsDevCatalogSource,
    OmniRouteCatalogSource,
    OpenRouterCatalogSource,
    normalize_capability_flags,
)

LUNA_RULES = IdentityRules(name_overrides={"luna-5.6-pro": "openai/luna:luna-5.6-pro"})


# -- OmniRoute catalog ingestion (MVP-0302) ---------------------------------


def luna_gateway() -> FakeGatewayAdapter:
    return FakeGatewayAdapter(
        connections=(
            ProviderConnectionRef(id="conn-a", provider="provider-a", label="Alpha"),
            ProviderConnectionRef(id="conn-b", provider="provider-b", label="Beta"),
        ),
        models=(
            GatewayModel(
                id="luna-5.6-pro",
                provider="provider-a",
                context_window=200_000,
                capabilities={"tool_call": True},
            ),
            GatewayModel(
                id="provider-b/luna-5.6-pro",
                provider="provider-b",
                capabilities={"tools": ["web"], "reasoning": True, "available_efforts": ["high", "low"]},
            ),
        ),
    )


@pytest.mark.asyncio
async def test_omniroute_ingestion_groups_serving_providers_onto_one_variant() -> None:
    source = OmniRouteCatalogSource(luna_gateway(), LUNA_RULES)

    batch = await source.fetch()

    assert len(batch.variants) == 2
    assert len(batch.endpoints) == 2
    assert {observation.variant_id for observation in batch.variants} == {"openai/luna:luna-5.6-pro"}
    assert {observation.endpoint.id for observation in batch.endpoints} == {
        "provider-a/luna-5.6-pro",
        "provider-b/luna-5.6-pro",
    }

    contexts = {
        observation.variant_id: observation.fields["context_window"].value
        for observation in batch.variants
        if "context_window" in observation.fields
    }
    assert contexts == {"openai/luna:luna-5.6-pro": 200_000}

    assert any(observation.fields["cap.tool_calling"].value is True for observation in batch.endpoints)
    reasoning = next(
        observation
        for observation in batch.endpoints
        if observation.fields.get("cap.reasoning") is not None
        and observation.fields["cap.reasoning"].value is True
    )
    assert reasoning.fields["cap.available_efforts"].value == ("high", "low")
    assert all(observation.endpoint.connection is not None for observation in batch.endpoints)


@pytest.mark.asyncio
async def test_omniroute_preserves_publisher_prefixes_from_aggregators() -> None:
    adapter = FakeGatewayAdapter(
        connections=(ProviderConnectionRef(id="or", provider="openrouter"),),
        models=(GatewayModel(id="openai/gpt-4o", provider="openrouter", context_window=128_000),),
    )
    source = OmniRouteCatalogSource(adapter, IdentityRules())

    batch = await source.fetch()

    endpoint = batch.endpoints[0]
    assert endpoint.endpoint.provider == "openrouter"
    assert endpoint.endpoint.provider_model_name == "openai/gpt-4o"
    assert endpoint.endpoint.model_variant_id == "openai/gpt-4o:gpt-4o"
    assert endpoint.endpoint.source_record_id == "openai/gpt-4o"


def test_capability_flag_normalization_is_tri_state() -> None:
    empty = normalize_capability_flags({})
    assert empty.model_dump(exclude_none=True) == {}

    caps = normalize_capability_flags(
        {
            "tool_call": True,
            "tools": [],
            "attachment": ["image/png"],
            "streaming": True,
            "reasoning_efforts": ["high", "high", "low"],
        }
    )
    assert caps.tool_calling is True
    assert caps.vision is True
    assert caps.streaming is True
    assert caps.available_efforts == ("high", "low")
    assert caps.reasoning is None
    assert caps.multimodal is None


# -- models.dev ingestion (MVP-0303) -----------------------------------------


MODELS_DEV_PAYLOAD = [
    {
        "id": "openai/gpt-4o",
        "name": "GPT-4o",
        "description": "flagship",
        "context": {"default": 128000, "openai": 128000},
        "tool_call": True,
        "structured_output": {"enabled": False},
        "attachment": True,
        "reasoning": {"enabled": True, "efforts": ["low", "high"]},
        "pricing": {"openai": {"prompt": 2.5, "completion": 10.0}},
    },
    {
        "id": "anthropic/claude-3-5-sonnet",
        "name": "Claude 3.5 Sonnet",
        "context": 200000,
    },
]


@pytest.mark.asyncio
async def test_modelsdev_ingestion_claims_stated_facts_only(transport_factory) -> None:
    source = ModelsDevCatalogSource(
        "https://models.dev",
        IdentityRules(),
        transport=transport_factory(data=MODELS_DEV_PAYLOAD),
    )

    batch = await source.fetch()

    assert len(batch.variants) == 2
    assert len(batch.endpoints) == 1

    gpt4o = next(observation for observation in batch.variants if "display_name" in observation.fields)
    assert gpt4o.variant_id == "openai/gpt-4o:gpt-4o"
    assert gpt4o.fields["display_name"].value == "GPT-4o"
    assert gpt4o.fields["description"].value == "flagship"
    assert gpt4o.fields["context_window"].value == 128000
    assert gpt4o.fields["cap.tool_calling"].value is True
    assert gpt4o.fields["cap.vision"].value is True
    assert gpt4o.fields["cap.structured_output"].value is False
    assert gpt4o.fields["cap.reasoning"].value is True
    assert gpt4o.fields["cap.available_efforts"].value == ("low", "high")

    pricing = batch.endpoints[0]
    assert pricing.endpoint.id == "openai/gpt-4o"
    assert pricing.fields["input_cost_per_million"].value == 2.5
    assert pricing.fields["output_cost_per_million"].value == 10.0

    claude = next(
        observation
        for observation in batch.variants
        if observation.variant_id == "anthropic/claude-3-5-sonnet:claude-3-5-sonnet"
    )
    # A model without explicit flags keeps unknown capabilities unknown.
    assert "cap.vision" not in claude.fields
    assert claude.fields["context_window"].value == 200000


@pytest.mark.asyncio
async def test_modelsdev_http_failure_raises(transport_factory) -> None:
    source = ModelsDevCatalogSource(
        "https://models.dev",
        IdentityRules(),
        transport=transport_factory(data=None, status_code=503),
    )

    with pytest.raises(RuntimeError, match="503"):
        await source.fetch()


# -- OpenRouter ingestion (MVP-0304) -----------------------------------------


OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context_length": 128000,
            "max_completion_tokens": 16384,
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
            },
            "supported_parameters": ["tools", "reasoning", "json_schema"],
            "reasoning": {"efforts": ["low", "high"]},
            "pricing": {"prompt": 2.5, "completion": 10.0},
        },
    ]
}


@pytest.mark.asyncio
async def test_openrouter_ingestion_claims_explicit_modalities_and_limits(transport_factory) -> None:
    transport = transport_factory(data=OPENROUTER_PAYLOAD)
    source = OpenRouterCatalogSource(
        "https://openrouter.ai",
        IdentityRules(),
        transport=transport,
        api_key="secret-token",
    )

    batch = await source.fetch()

    assert transport.calls[0][1]["Authorization"] == "Bearer secret-token"
    assert len(batch.variants) == 1
    variant = batch.variants[0]
    assert variant.variant_id == "openai/gpt-4o:gpt-4o"
    assert variant.fields["display_name"].value == "GPT-4o"
    assert variant.fields["context_window"].value == 128000
    assert variant.fields["max_output_tokens"].value == 16384
    assert variant.fields["cap.vision"].value is True
    assert variant.fields["cap.multimodal"].value is True
    assert variant.fields["cap.tool_calling"].value is True
    assert variant.fields["cap.reasoning"].value is True
    assert variant.fields["cap.structured_output"].value is True
    assert variant.fields["cap.available_efforts"].value == ("low", "high")

    assert len(batch.endpoints) == 1
    pricing = batch.endpoints[0]
    assert pricing.endpoint.id == "openrouter/openai/gpt-4o"
    assert pricing.fields["input_cost_per_million"].value == 2.5
