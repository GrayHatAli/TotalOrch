"""Registry identity, merge, persistence, and service tests (MVP-0301..0316)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from total_orch.domain import ModelVariant, ProviderConnectionRef, SourceProvenance
from total_orch.gateway import FakeGatewayAdapter, GatewayModel
from total_orch.registry import (
    IdentityRules,
    ModelRegistryService,
    ModelRegistryStore,
    ModelsDevCatalogSource,
    OmniRouteCatalogSource,
    OpenRouterCatalogSource,
    VariantObservation,
    assemble_endpoint,
    assemble_variant,
    claimed,
    merge_fields,
)
from total_orch.registry.http import CatalogResponse
from total_orch.registry.identity import split_endpoint_id, split_variant_id
from total_orch.registry.observations import EndpointObservation, EndpointRef, FieldValue


def claim(value: object, source: str) -> FieldValue:
    return claimed(value, source)  # type: ignore[arg-type]


def variant_observation(source: str, variant_id: str, **fields: object) -> VariantObservation:
    return VariantObservation(
        source=source,
        variant_id=variant_id,
        fields={name: claim(value, source) for name, value in fields.items()},
    )


def endpoint_observation(
    source: str,
    *,
    provider: str,
    model_name: str,
    variant_id: str,
    connection: ProviderConnectionRef | None = None,
    **fields: object,
) -> EndpointObservation:
    return EndpointObservation(
        source=source,
        fields={name: claim(value, source) for name, value in fields.items()},
        endpoint=EndpointRef(
            provider=provider,
            provider_model_name=model_name,
            model_variant_id=variant_id,
            connection=connection,
        ),
    )


# -- canonical identity (MVP-0305, MVP-0306) ---------------------------------


def test_identity_publisher_prefixed_ids_are_canonical() -> None:
    identity = IdentityRules().resolve(None, "openai/gpt-4o")

    assert identity.publisher == "openai"
    assert identity.family_id == "openai/gpt-4o"
    assert identity.variant_id == "openai/gpt-4o:gpt-4o"


def test_identity_connection_named_after_its_publisher() -> None:
    rules = IdentityRules()

    direct = rules.resolve("openai", "gpt-4o")
    prefixed = rules.resolve("openai", "openai/gpt-4o")

    assert direct.variant_id == "openai/gpt-4o:gpt-4o"
    assert direct == prefixed


def test_identity_name_overrides_group_series_variants() -> None:
    rules = IdentityRules(name_overrides={"luna-5.6-pro": "openai/luna:luna-5.6-pro"})

    assert rules.resolve("provider-a", "provider-a/luna-5.6-pro").variant_id == "openai/luna:luna-5.6-pro"
    assert rules.resolve("provider-b", "luna-5.6-pro").variant_id == "openai/luna:luna-5.6-pro"
    assert rules.resolve("openai", "luna-5.6-pro").variant_id == "openai/luna:luna-5.6-pro"


def test_identity_provider_overrides_beat_name_overrides() -> None:
    rules = IdentityRules(
        name_overrides={"luna-5.6-pro": "openai/luna:luna-5.6-pro"},
        provider_name_overrides={("provider-a", "luna-5.6-pro"): "acme/other:luna-5.6-pro"},
    )

    assert rules.resolve("provider-a", "luna-5.6-pro").variant_id == "acme/other:luna-5.6-pro"
    assert rules.resolve("provider-b", "luna-5.6-pro").variant_id == "openai/luna:luna-5.6-pro"


def test_identity_strips_serving_qualifiers_and_never_guesses_publishers() -> None:
    rules = IdentityRules()

    unknown = rules.resolve("provider-a", "mystery-model")
    assert unknown.publisher is None
    assert unknown.variant_id == "mystery-model:mystery-model"

    dated = rules.resolve(None, "gpt-4o-2024-08-06")
    assert dated.variant_id == "gpt-4o:gpt-4o"


def test_identity_split_helpers_round_trip() -> None:
    family, name = split_variant_id("openai/gpt-4o:gpt-4o")
    provider, model = split_endpoint_id("openai/gpt-4o")

    assert (family, name) == ("openai/gpt-4o", "gpt-4o")
    assert (provider, model) == ("openai", "gpt-4o")


# -- field-authority merging (MVP-0311, MVP-0312, MVP-0307) ------------------


GPT4O = "openai/gpt-4o:gpt-4o"


def test_merge_picks_highest_authority_value_per_field() -> None:
    observations = [
        variant_observation("models.dev", GPT4O, display_name="GPT-4o", context_window=128000),
        variant_observation("openrouter", GPT4O, display_name="GPT-4o (OpenRouter)", context_window=200000),
        variant_observation("omniroute", GPT4O, context_window=90000),
    ]

    record, field_provenance = assemble_variant(observations)

    assert record.context_window == 200000
    assert record.display_name == "GPT-4o"
    assert field_provenance["context_window"].source == "openrouter"
    assert field_provenance["display_name"].source == "models.dev"


def test_merge_never_turns_unknown_capabilities_into_claims() -> None:
    observations = [
        variant_observation("models.dev", GPT4O, **{"cap.tool_calling": True}),
        variant_observation("openrouter", GPT4O, **{"cap.reasoning": True}),
    ]

    record, _provenance = assemble_variant(observations)

    assert record.capabilities.tool_calling is True
    assert record.capabilities.reasoning is True
    assert record.capabilities.vision is None
    assert record.capabilities.streaming is None


def test_merge_efforts_do_not_survive_an_explicit_reasoning_veto() -> None:
    observations = [
        variant_observation("models.dev", GPT4O, **{"cap.reasoning": False}),
        variant_observation("openrouter", GPT4O, **{"cap.available_efforts": ("low", "high")}),
    ]

    record, _provenance = assemble_variant(observations)

    assert record.capabilities.reasoning is False
    assert record.capabilities.available_efforts is None


def test_merge_fields_without_authority_returns_none() -> None:
    observations = [variant_observation("omniroute", GPT4O, context_window=90000)]

    assert merge_fields(observations, "health") is None


def test_endpoint_keeps_runtime_facts_separate_from_base_metadata() -> None:
    connection = ProviderConnectionRef(id="openai", provider="openai")
    observations = [
        endpoint_observation(
            "omniroute",
            provider="openai",
            model_name="gpt-4o",
            variant_id=GPT4O,
            connection=connection,
            **{"cap.tool_calling": True},
            health=0.98,
            latency_p95_ms=120.5,
        ),
        endpoint_observation(
            "models.dev",
            provider="openai",
            model_name="gpt-4o",
            variant_id=GPT4O,
            input_cost_per_million=2.5,
            output_cost_per_million=10.0,
        ),
    ]

    record, field_provenance = assemble_endpoint(observations)
    assert record is not None
    endpoint, _provenance = record

    assert endpoint.connection == connection
    assert endpoint.capabilities.tool_calling is True
    assert endpoint.capabilities.vision is None
    assert endpoint.health == 0.98
    assert endpoint.latency_p95_ms == 120.5
    assert endpoint.input_cost_per_million == 2.5
    assert endpoint.last_checked_at is None
    assert field_provenance["health"].source == "omniroute"
    assert field_provenance["input_cost_per_million"].source == "models.dev"


def test_endpoint_without_connection_is_not_assembled() -> None:
    observations = [
        endpoint_observation(
            "models.dev",
            provider="openai",
            model_name="gpt-4o",
            variant_id=GPT4O,
            input_cost_per_million=2.5,
        )
    ]

    assert assemble_endpoint(observations) is None


# -- persistence (MVP-0301, MVP-0311, MVP-0313) ------------------------------


def new_store() -> ModelRegistryStore:
    store = ModelRegistryStore(":memory:")
    store.initialize()
    return store


class StubTransport:
    """Minimal in-memory catalog transport for canned payloads."""

    def __init__(self, data: object) -> None:
        self.data = data

    async def get(self, url: str, *, headers: object, timeout: float) -> CatalogResponse:
        return CatalogResponse(200, self.data, {})


def sample_variant() -> ModelVariant:
    return ModelVariant(
        id="openai/luna:luna-5.6-pro",
        family_id="openai/luna",
        name="luna-5.6-pro",
        context_window=200000,
    )


def test_store_round_trip_and_idempotent_upsert() -> None:
    store = new_store()
    record = sample_variant()
    provenance = {"context_window": SourceProvenance(source="omniroute")}

    assert store.upsert_variant(record, provenance, "omniroute") is True
    assert store.upsert_variant(record, provenance, "omniroute") is False
    assert store.count_variants() == 1
    assert store.get_variant(record.id) == record

    stored = store.field_provenance("variant", record.id)
    assert stored["context_window"].source == "omniroute"
    assert store.last_observed_at("variant", record.id) is not None


def test_store_change_detection_ignores_volatile_provenance_timestamps() -> None:
    store = new_store()
    record = sample_variant()
    first = {"context_window": SourceProvenance(source="omniroute", observed_at=datetime(2026, 1, 1, tzinfo=UTC))}
    later = {"context_window": SourceProvenance(source="omniroute", observed_at=datetime(2026, 6, 1, tzinfo=UTC))}

    assert store.upsert_variant(record, first, "omniroute") is True
    assert store.upsert_variant(record, later, "omniroute") is False


def test_store_detects_fact_changes() -> None:
    store = new_store()
    original = sample_variant()
    updated = sample_variant()
    changed = ModelVariant(**{**updated.model_dump(), "context_window": 210000})

    assert store.upsert_variant(original, {}, "omniroute") is True
    assert store.upsert_variant(changed, {}, "omniroute") is True
    assert store.get_variant(original.id).context_window == 210000


# -- service sync, staleness, and queries (MVP-0313..0316) -------------------


def gateway_adapter() -> FakeGatewayAdapter:
    return FakeGatewayAdapter(
        connections=(
            ProviderConnectionRef(id="conn-a", provider="provider-a", label="Alpha"),
            ProviderConnectionRef(id="conn-b", provider="provider-b", label="Beta"),
            ProviderConnectionRef(id="openai", provider="openai"),
        ),
        models=(
            GatewayModel(
                id="luna-5.6-pro",
                provider="provider-a",
                context_window=200_000,
                capabilities={"tool_call": True},
            ),
            GatewayModel(
                id="luna-5.6-pro",
                provider="provider-b",
                context_window=150_000,
                capabilities={"tools": ["web"], "reasoning": True, "available_efforts": ["low", "high"]},
            ),
            GatewayModel(
                id="gpt-4o",
                provider="openai",
                context_window=128_000,
                capabilities={"tool_call": True},
            ),
        ),
    )


def models_dev_payload() -> list[dict[str, object]]:
    return [
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context": {"default": 128000},
            "tool_call": True,
            "structured_output": {"enabled": False},
            "reasoning": {"enabled": True, "efforts": ["low", "high"]},
            "pricing": {"openai": {"prompt": 2.5, "completion": 10.0}},
        }
    ]


def openrouter_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o",
                "context_length": 200000,
                "max_completion_tokens": 16384,
                "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                "supported_parameters": ["tools", "reasoning", "json_schema"],
                "reasoning": {"efforts": ["low", "high"]},
                "pricing": {"prompt": 2.0, "completion": 8.0},
            }
        ]
    }


def build_service() -> ModelRegistryService:
    store = new_store()
    rules = IdentityRules(name_overrides={"luna-5.6-pro": "openai/luna:luna-5.6-pro"})
    return ModelRegistryService(
        store,
        rules,
        sources=[
            OmniRouteCatalogSource(gateway_adapter(), rules),
            ModelsDevCatalogSource("https://models.dev", rules, transport=StubTransport(models_dev_payload())),
            OpenRouterCatalogSource("https://openrouter.ai", rules, transport=StubTransport(openrouter_payload())),
        ],
    )


@pytest.fixture
async def synced_service() -> ModelRegistryService:
    service = build_service()
    await service.sync()
    return service


@pytest.mark.asyncio
async def test_service_groups_one_model_across_two_providers(synced_service) -> None:
    service = synced_service

    counts = service.counts()
    assert counts.families == 2
    assert counts.variants == 2
    assert counts.endpoints == 3

    luna_variants = service.list_variants(family_id="openai/luna")
    assert len(luna_variants) == 1
    assert luna_variants[0].context_window == 200_000

    luna_endpoints = service.endpoint_candidates("openai/luna:luna-5.6-pro")
    assert {endpoint.id for endpoint in luna_endpoints} == {
        "provider-a/luna-5.6-pro",
        "provider-b/luna-5.6-pro",
    }
    assert all(endpoint.model_variant_id == "openai/luna:luna-5.6-pro" for endpoint in luna_endpoints)
    assert all(endpoint.capabilities.tool_calling is True for endpoint in luna_endpoints)


@pytest.mark.asyncio
async def test_service_merges_cross_catalog_claims_with_authority(synced_service) -> None:
    service = synced_service

    variant = service.get_variant("openai/gpt-4o:gpt-4o")
    # openrouter's 200k context wins over models.dev's 128k.
    assert variant.context_window == 200_000
    # models.dev's explicit structured-output veto beats openrouter's claim.
    assert variant.capabilities.structured_output is False
    assert variant.capabilities.tool_calling is True
    assert variant.capabilities.vision is True
    # Unknown capabilities are never silently true.
    assert variant.capabilities.streaming is None

    field_provenance = service.provenance("variant", variant.id)
    assert field_provenance["context_window"].source == "openrouter"
    assert {source.source for source in variant.provenance} == {"omniroute", "models.dev", "openrouter"}


@pytest.mark.asyncio
async def test_service_sync_is_idempotent_and_preserves_row_writes() -> None:
    store = new_store()
    rules = IdentityRules(name_overrides={"luna-5.6-pro": "openai/luna:luna-5.6-pro"})
    sources = [
        OmniRouteCatalogSource(gateway_adapter(), rules),
        ModelsDevCatalogSource("https://models.dev", rules, transport=StubTransport(models_dev_payload())),
        OpenRouterCatalogSource("https://openrouter.ai", rules, transport=StubTransport(openrouter_payload())),
    ]
    service = ModelRegistryService(store, rules, sources=sources)

    first = await service.sync()
    assert first.entity_errors == ()
    updated_at = {
        row["id"]: row["updated_at"]
        for row in store._connection.execute("SELECT id, updated_at FROM variants").fetchall()
    }

    second = await service.sync()

    assert second.families_upserted == 0
    assert second.variants_upserted == 0
    assert second.endpoints_upserted == 0
    assert first.variants_upserted == 2
    unchanged = {
        row["id"]: row["updated_at"]
        for row in store._connection.execute("SELECT id, updated_at FROM variants").fetchall()
    }
    assert unchanged == updated_at


@pytest.mark.asyncio
async def test_service_reports_source_failures_without_aborting() -> None:
    store = new_store()
    rules = IdentityRules()

    class BrokenSource:
        source = "broken"

        async def fetch(self):
            raise RuntimeError("catalog unreachable")

    service = ModelRegistryService(
        store,
        rules,
        sources=[
            BrokenSource(),  # type: ignore[arg-type]
            ModelsDevCatalogSource("https://models.dev", rules, transport=StubTransport(models_dev_payload())),
        ],
    )

    summary = await service.sync()

    assert summary.succeeded is False
    statuses = {result.source: result.status for result in summary.sources}
    assert statuses == {"broken": "error", "models.dev": "ok"}
    failed = next(result for result in summary.sources if result.source == "broken")
    assert failed.error is not None
    assert service.counts().variants >= 1


@pytest.mark.asyncio
async def test_service_query_apis(synced_service) -> None:
    service = synced_service

    assert {family.id for family in service.list_families()} == {"openai/gpt-4o", "openai/luna"}
    assert service.get_family("openai/luna").publisher == "openai"
    assert service.list_variants(family_id="openai/luna")[0].id == "openai/luna:luna-5.6-pro"
    assert [endpoint.id for endpoint in service.list_endpoints(provider="provider-a")] == [
        "provider-a/luna-5.6-pro"
    ]

    resolved = service.resolve_variant("provider-b", "luna-5.6-pro")
    assert resolved.id == "openai/luna:luna-5.6-pro"
    assert service.resolve_variant(None, "openai/gpt-4o").id == "openai/gpt-4o:gpt-4o"

    with pytest.raises(KeyError):
        service.get_variant("does/not:exist")
    with pytest.raises(KeyError):
        service.get_family("does/not-exist")
    with pytest.raises(KeyError):
        service.resolve_variant(None, "does-not-exist")


def test_service_stale_detection_flags_unobserved_entities() -> None:
    store = new_store()
    service = ModelRegistryService(store, IdentityRules())
    now = datetime.now(UTC)
    record = sample_variant()

    store.upsert_variant(record, {}, "omniroute", observed_at=now - timedelta(days=2))

    report = service.stale(timedelta(days=1), now=now)
    assert record.id in report.variants

    store.upsert_variant(record, {}, "omniroute", observed_at=now)
    assert service.stale(timedelta(days=1), now=now).empty
