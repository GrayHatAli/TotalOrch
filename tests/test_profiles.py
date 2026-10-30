"""Default agent profile and automatic pool builder tests (MVP-0401..0415)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from total_orch.domain import (
    ModelCapabilities,
    ModelFamily,
    ModelPool,
    ModelVariant,
    ProviderConnectionRef,
    ProviderEndpoint,
)
from total_orch.profiles import (
    BUILTIN_AGENT_DEFINITIONS,
    ROLE_POOL,
    PoolBuild,
    PoolBuilder,
    PoolOverrides,
    default_agent_profiles,
)
from total_orch.registry import IdentityRules, ModelRegistryService, ModelRegistryStore

LUNA = "openai/luna:luna-5.6-pro"
NOVA = "openai/nova:nova-2"
FERRET = "openai/ferret:ferret-8b"

A = "provider-a/luna-5.6-pro"
B = "provider-b/luna-5.6-pro"
C = "provider-b/nova-2"
D = "provider-a/nova-2"
F = "provider-a/ferret-8b"

ALL_ENDPOINTS = (A, B, C, D, F)


class _FakeRegistry:
    """Minimal structural stand-in for the registry query surface."""

    def __init__(
        self,
        *,
        families: list[ModelFamily],
        variants: list[ModelVariant],
        endpoints: list[ProviderEndpoint],
    ) -> None:
        self._families = families
        self._variants = variants
        self._endpoints = endpoints

    def list_families(self) -> list[ModelFamily]:
        return list(self._families)

    def list_variants(self, family_id: str | None = None) -> list[ModelVariant]:
        if family_id is None:
            return list(self._variants)
        return [variant for variant in self._variants if variant.family_id == family_id]

    def list_endpoints(
        self,
        *,
        model_variant_id: str | None = None,
        provider: str | None = None,
    ) -> list[ProviderEndpoint]:
        endpoints = list(self._endpoints)
        if model_variant_id is not None:
            endpoints = [e for e in endpoints if e.model_variant_id == model_variant_id]
        if provider is not None:
            endpoints = [e for e in endpoints if e.provider == provider]
        return endpoints


def _connection(provider: str) -> ProviderConnectionRef:
    return ProviderConnectionRef(id=f"{provider}-account-1", provider=provider)


def _variant(
    variant_id: str,
    family_id: str,
    name: str,
    *,
    context_window: int,
    display_name: str | None = None,
    **capabilities: bool,
) -> ModelVariant:
    return ModelVariant(
        id=variant_id,
        family_id=family_id,
        name=name,
        display_name=display_name,
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
        connection=_connection(provider),
        capabilities=ModelCapabilities(**cast(Any, capabilities)),
        health=health,
        latency_p95_ms=latency_p95_ms,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
    )


def _world() -> tuple[_FakeRegistry, dict[str, str]]:
    """A deterministic three-family deployment.

    - Luna 5.6 Pro (128k, all capabilities) is served by ``A`` (no
      endpoint-level claims) and ``B`` (endpoint claims, structured output
      left to the variant).
    - Nova 2 (8k, tool calling and streaming) is served by ``C`` (endpoint
      claims reasoning False) and ``D`` (nothing claimed at endpoint level).
    - Ferret 8B (40k, reasoning + structured output, no tool calling) is
      served by ``F``.
    """
    families = [
        ModelFamily(id="openai/luna", publisher="openai", display_name="Luna"),
        ModelFamily(id="openai/nova", publisher="openai", display_name="Nova"),
        ModelFamily(id="openai/ferret", publisher="openai", display_name="Ferret"),
    ]
    variants = [
        _variant(
            LUNA,
            "openai/luna",
            "luna-5.6-pro",
            context_window=128_000,
            display_name="Luna 5.6 Pro",
            tool_calling=True,
            reasoning=True,
            streaming=True,
            structured_output=True,
        ),
        _variant(
            NOVA,
            "openai/nova",
            "nova-2",
            context_window=8_000,
            display_name="Nova 2",
            tool_calling=True,
            streaming=True,
        ),
        _variant(
            FERRET,
            "openai/ferret",
            "ferret-8b",
            context_window=40_000,
            display_name="Ferret 8B",
            reasoning=True,
            structured_output=True,
            tool_calling=False,
        ),
    ]
    endpoints = [
        _endpoint(A, LUNA, health=0.99, latency_p95_ms=120, input_cost_per_million=3.0, output_cost_per_million=15.0),
        _endpoint(
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
        _endpoint(
            C,
            NOVA,
            health=0.9,
            latency_p95_ms=90,
            input_cost_per_million=0.25,
            output_cost_per_million=1.0,
            reasoning=False,
        ),
        _endpoint(D, NOVA, health=0.8, latency_p95_ms=150, input_cost_per_million=0.4, output_cost_per_million=2.0),
        _endpoint(F, FERRET, health=0.7, latency_p95_ms=200, input_cost_per_million=0.1, output_cost_per_million=0.4),
    ]
    return _FakeRegistry(families=families, variants=variants, endpoints=endpoints), {
        "A": A,
        "B": B,
        "C": C,
        "D": D,
        "F": F,
    }


def _builds_by_id(builds: tuple[PoolBuild, ...]) -> dict[str, PoolBuild]:
    return {build.pool.id: build for build in builds}


def _members(pool: ModelPool) -> list[str]:
    return list(pool.member_endpoint_ids)


def _default_builder() -> PoolBuilder:
    registry, _ids = _world()
    return PoolBuilder(registry)


# -- capability pools (MVP-0407..0409) -------------------------------------


def test_tool_calling_pool_honors_effective_claims() -> None:
    builds = _builds_by_id(_default_builder().build())
    pool = builds["tool_calling"].pool
    # A..D claim tool calling somewhere (variant or endpoint); F is claimed False.
    assert _members(pool) == [A, B, C, D]

    memberships = {m.endpoint_id: m for m in builds["tool_calling"].memberships}
    assert memberships[F].member is False
    assert memberships[F].reason == "tool calling claimed False"
    assert memberships[A].reason == "tool calling claimed True (variant metadata)"
    assert memberships[B].reason == "tool calling claimed True (endpoint metadata)"


def test_reasoning_pool_keeps_unknown_unknown() -> None:
    builds = _builds_by_id(_default_builder().build())
    # C's endpoint explicitly denies reasoning; D leaves it unknown (variant
    # metadata does not claim it either) — neither may satisfy the pool.
    assert _members(builds["reasoning"].pool) == [A, B, F]

    memberships = {m.endpoint_id: m for m in builds["reasoning"].memberships}
    assert memberships[C].reason == "reasoning claimed False"
    assert memberships[D].reason == "reasoning unknown - not claimed by endpoint or variant metadata"


def test_large_context_pool_uses_variant_context_window() -> None:
    builds = _builds_by_id(_default_builder().build())
    pool = builds["large_context"].pool
    assert _members(pool) == [A, B]

    memberships = {m.endpoint_id: m for m in builds["large_context"].memberships}
    assert memberships[F].member is False
    assert memberships[F].reason == "context window 40000 < 64000"
    assert "context window 128000 >= 64000" in memberships[A].reason


def test_capability_pool_members_are_health_ordered() -> None:
    builds = _builds_by_id(_default_builder().build())
    # Reliability ordering: health descending (0.99, 0.95, 0.9, 0.8).
    assert _members(builds["tool_calling"].pool) == [A, B, C, D]


# -- family pools (MVP-0410) ------------------------------------------------


def test_family_pools_cover_every_family_with_endpoints() -> None:
    builds = _builds_by_id(_default_builder().build())
    assert _members(builds["openai/luna-family"].pool) == [A, B]
    assert _members(builds["openai/nova-family"].pool) == [C, D]
    assert _members(builds["openai/ferret-family"].pool) == [F]

    memberships = {m.endpoint_id: m for m in builds["openai/luna-family"].memberships}
    assert memberships[A].reason == "serves a variant of the openai/luna family"
    assert memberships[C].reason == "serves a variant of openai/nova, not openai/luna"


def test_family_pool_ids_include_the_canonical_family_id() -> None:
    openai_variant = _variant(
        "openai/shared:one",
        "openai/shared",
        "one",
        context_window=64_000,
    )
    meta_variant = _variant(
        "meta/shared:two",
        "meta/shared",
        "two",
        context_window=64_000,
    )
    openai_endpoint = _endpoint(
        "provider-a/one",
        openai_variant.id,
        health=0.9,
        latency_p95_ms=100,
        input_cost_per_million=1.0,
        output_cost_per_million=1.0,
    )
    meta_endpoint = _endpoint(
        "provider-b/two",
        meta_variant.id,
        health=0.8,
        latency_p95_ms=100,
        input_cost_per_million=1.0,
        output_cost_per_million=1.0,
    )
    builder = PoolBuilder(
        _FakeRegistry(
            families=[ModelFamily(id="openai/shared"), ModelFamily(id="meta/shared")],
            variants=[openai_variant, meta_variant],
            endpoints=[openai_endpoint, meta_endpoint],
        )
    )

    builds = _builds_by_id(
        builder.build(
            {
                "openai/shared-family": PoolOverrides(pinned=(openai_endpoint.id,)),
                "meta/shared-family": PoolOverrides(pinned=(meta_endpoint.id,)),
            }
        )
    )

    assert _members(builds["openai/shared-family"].pool) == [openai_endpoint.id]
    assert _members(builds["meta/shared-family"].pool) == [meta_endpoint.id]


# -- role pools (MVP-0405, 0411) --------------------------------------------


def test_role_pools_enforce_hard_requirements() -> None:
    builds = _builds_by_id(_default_builder().build())
    assert _members(builds["planner-quality"].pool) == [A, B, F]
    assert _members(builds["reviewer-quality"].pool) == [A, B, F]
    # Developer needs tool calling + streaming: F fails tool calling, C/D
    # fail the 32k context floor.
    assert _members(builds["coder-balanced"].pool) == [A, B]
    # Researcher needs tool calling and 64k context.
    assert _members(builds["researcher-reliable"].pool) == [A, B]
    # Tester needs tool calling + structured output.
    assert _members(builds["tester-fast"].pool) == [A, B]

    planner = {m.endpoint_id: m for m in builds["planner-quality"].memberships}
    assert planner[C].reason == "reasoning claimed False"
    assert planner[D].reason == "reasoning unknown - not claimed by endpoint or variant metadata"
    # B leaves structured output to the variant claim and still qualifies.
    assert "structured output claimed True (variant metadata)" in planner[B].reason
    assert planner[A].reason.startswith("meets Planner constraints:")

    tester = {m.endpoint_id: m for m in builds["tester-fast"].memberships}
    assert tester[C].reason == "structured output unknown - not claimed by endpoint or variant metadata"


def test_role_pool_ordering_follows_dominant_preference_axis() -> None:
    builds = _builds_by_id(_default_builder().build())
    # Planner is quality-first: health descending (0.99, 0.95, 0.7). A pure
    # latency ordering would have placed F before B (120, 200, 300).
    assert _members(builds["planner-quality"].pool) == [A, B, F]


def test_default_agent_profiles_wire_every_role_to_its_pool() -> None:
    builds = _default_builder().build()
    pools = {build.pool.id: build.pool for build in builds}
    profiles = default_agent_profiles(BUILTIN_AGENT_DEFINITIONS, pools)

    assert [profile.agent_definition_id for profile in profiles] == [
        "planner",
        "developer",
        "reviewer",
        "researcher",
        "tester",
    ]
    for profile in profiles:
        assert profile.id == f"{profile.agent_definition_id}-default"
        assert profile.pool_id == ROLE_POOL[profile.agent_definition_id]
        # A fresh installation must produce usable pools (MVP DoD).
        assert pools[profile.pool_id].member_endpoint_ids
        assert profile.preferences


def test_empty_registry_still_produces_role_pools() -> None:
    store = ModelRegistryStore(":memory:")
    store.initialize()
    service = ModelRegistryService(store, IdentityRules())
    builds = PoolBuilder(service).build()
    assert {build.pool.id for build in builds} == set(ROLE_POOL.values())
    assert all(not build.pool.member_endpoint_ids for build in builds)


# -- explanations (MVP-0415) -------------------------------------------------


def test_every_considered_endpoint_has_exactly_one_membership() -> None:
    for build in _default_builder().build():
        memberships = build.memberships
        assert len(memberships) == len(ALL_ENDPOINTS)
        assert {m.endpoint_id for m in memberships} == set(ALL_ENDPOINTS)
        # Members first, in pool member order.
        assert [m.endpoint_id for m in memberships if m.member] == _members(build.pool)
        assert all(m.member is False for m in memberships[len(build.pool.member_endpoint_ids) :])


# -- user overrides (MVP-0412..0414) -----------------------------------------


def test_exclude_override_wins_over_natural_membership() -> None:
    builds = _default_builder().build(
        {"planner-quality": PoolOverrides(exclude=(A,))}
    )
    planner = _builds_by_id(builds)["planner-quality"]
    assert _members(planner.pool) == [B, F]
    memberships = {m.endpoint_id: m for m in planner.memberships}
    assert memberships[A].member is False
    assert memberships[A].reason == "excluded by user override"


def test_include_override_adds_endpoint_the_gates_reject() -> None:
    builds = _default_builder().build(
        {"coder-balanced": PoolOverrides(include=(F,))}
    )
    coder = _builds_by_id(builds)["coder-balanced"]
    # Ferret has no tool calling, so it can only enter via an explicit
    # include; it is listed first, ahead of the natural members.
    assert _members(coder.pool) == [F, A, B]
    memberships = {m.endpoint_id: m for m in coder.memberships}
    assert memberships[F].member is True
    assert memberships[F].reason == "included by user override (does not meet this pool's constraints)"


def test_pinning_restricts_pool_to_pinned_endpoints() -> None:
    builds = _default_builder().build(
        {"planner-quality": PoolOverrides(pinned=(A,))}
    )
    planner = _builds_by_id(builds)["planner-quality"]
    assert _members(planner.pool) == [A]
    memberships = {m.endpoint_id: m for m in planner.memberships}
    assert memberships[A].reason == "pinned by user override (pool planner-quality restricted to the pinned selection)"
    # B and F are naturally eligible but outside the pinned selection.
    assert memberships[B].member is False
    assert memberships[F].reason == "outside the pinned selection for planner-quality"


def test_pinning_a_variant_pins_all_its_endpoints() -> None:
    builds = _default_builder().build(
        {"openai/luna-family": PoolOverrides(pinned=(LUNA,))}
    )
    family = _builds_by_id(builds)["openai/luna-family"]
    assert _members(family.pool) == [A, B]
    assert all(m.member for m in family.memberships if m.endpoint_id in (A, B))


def test_reset_to_defaults_restores_natural_membership() -> None:
    builder = _default_builder()
    customized = builder.build({"planner-quality": PoolOverrides(pinned=(A,))})
    reset = builder.build({})
    default = builder.build()

    # The customized build narrowed the pool; the reset restores the defaults.
    assert customized != default
    assert reset == default
    planner_reset = _builds_by_id(reset)["planner-quality"]
    planner_default = _builds_by_id(default)["planner-quality"]
    assert planner_reset.pool.member_endpoint_ids == planner_default.pool.member_endpoint_ids == (A, B, F)
    assert any(member.reason.startswith("meets Planner constraints") for member in planner_default.memberships)


def test_unknown_override_references_raise() -> None:
    builder = _default_builder()
    with pytest.raises(ValueError, match="unknown endpoint"):
        builder.build({"planner-quality": PoolOverrides(exclude=("acme/ghost-1",))})
    with pytest.raises(ValueError, match="unknown variant"):
        builder.build({"planner-quality": PoolOverrides(exclude=("acme/ghost:ghost-1",))})
    with pytest.raises(ValueError, match="unknown pool"):
        builder.build({"ghost-pool": PoolOverrides(exclude=(A,))})


def test_deterministic_rebuilds() -> None:
    builder = _default_builder()
    first = builder.build()
    second = builder.build()
    assert first == second
