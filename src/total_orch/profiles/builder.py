"""Automatic model pool builder (MVP-0406..0415).

Pools are generated from rules over the registry snapshot rather than
maintained by hand. The builder emits three families of pools:

- Capability pools (MVP-0407..0409): ``tool_calling``, ``reasoning``, and
  ``large_context``, each holding every endpoint whose *effective*
  capability claim satisfies the pool's gate. Endpoint-level claims win
  over canonical variant metadata, and an unknown claim never satisfies a
  hard gate (the registry's tri-state design is preserved).
- Family pools (MVP-0410): ``{family}-family`` for every family that has
  routable endpoints, e.g. ``luna-family``.
- Role pools (MVP-0411): one pool per built-in agent definition under the
  role's default pool id (``planner-quality``, ``coder-balanced``, ...).
  Membership enforces the definition's hard capability requirements before
  any ordering, so a high score can never bypass a hard gate (MVP DoD).
  Members are ordered by the role's dominant preference axis as a
  deterministic preview of the weighted Phase 5 router.

User customization (MVP-0412..0414) is expressed as per-pool
``PoolOverrides`` keyed by pool id. ``exclude`` always wins; ``pinned``
restricts a pool to the pinned endpoints (``include`` may still add to
it); ``include`` force-adds an endpoint that the gates reject. Rebuilding
with no overrides restores the defaults. Every emitted pool carries a
membership explanation for every considered endpoint (MVP-0415), so a user
can see both why a model belongs and why it does not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from total_orch.domain import (
    AgentDefinition,
    ModelCapabilities,
    ModelFamily,
    ModelPool,
    ModelVariant,
    ProviderEndpoint,
)
from total_orch.registry.capabilities import (
    BOOL_CAPABILITY_FIELDS,
    capability_claim,
    context_claim,
    effective_capabilities,
)

from .defaults import BUILTIN_AGENT_DEFINITIONS, ROLE_POOL, ROLE_PREFERENCES

#: Deterministic tie-break when two preference axes share the top weight.
_AXIS_PRIORITY: tuple[str, ...] = ("reliability", "quality", "latency", "cost")


class RegistryReader(Protocol):
    """The read-only registry surface the builder needs.

    ``ModelRegistryService`` satisfies this structurally; tests substitute a
    small fake so pool rules are exercised without a database.
    """

    def list_families(self) -> list[ModelFamily]: ...

    def list_variants(self, family_id: str | None = None) -> list[ModelVariant]: ...

    def list_endpoints(
        self,
        *,
        model_variant_id: str | None = None,
        provider: str | None = None,
    ) -> list[ProviderEndpoint]: ...


@dataclass(frozen=True, slots=True)
class PoolOverrides:
    """User customization for one pool (MVP-0412..0414).

    ``include``, ``exclude``, and ``pinned`` reference either Provider
    Endpoint ids (``provider-a/luna-5.6-pro``) or canonical variant ids
    (``openai/luna:luna-5.6-pro``, expanded to every endpoint of that
    variant). Precedence: ``exclude`` wins over everything; a non-empty
    ``pinned`` restricts the pool to the pinned endpoints (plus any
    ``include`` entries); otherwise ``include`` force-adds endpoints the
    gates rejected.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    pinned: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PoolMembership:
    """Why one endpoint is, or is not, a member of one pool (MVP-0415)."""

    endpoint_id: str
    member: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PoolBuild:
    """One generated pool plus its per-endpoint explanation."""

    pool: ModelPool
    memberships: tuple[PoolMembership, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    endpoint: ProviderEndpoint
    variant: ModelVariant | None
    context_window: int | None
    capabilities: ModelCapabilities
    capability_origin: Mapping[str, str]


@dataclass(slots=True)
class _Snapshot:
    candidates: dict[str, _Candidate]
    endpoints_by_variant: dict[str, tuple[str, ...]]
    families_with_endpoints: frozenset[str]
    family_display_names: Mapping[str, str]


def _family_leaf(family_id: str) -> str:
    return family_id.partition("/")[2] or family_id


class PoolBuilder:
    """Deterministic, rule-based builder over a local registry snapshot.

    The builder is pure: identical registry state plus identical overrides
    produce identical pools, which keeps every membership reproducible and
    makes reset-to-default a plain rebuild with empty overrides.
    """

    def __init__(
        self,
        registry: RegistryReader,
        *,
        large_context_minimum: int = 64_000,
        definitions: Sequence[AgentDefinition] = BUILTIN_AGENT_DEFINITIONS,
        role_pool_ids: Mapping[str, str] = ROLE_POOL,
        role_preferences: Mapping[str, Mapping[str, float]] = ROLE_PREFERENCES,
    ) -> None:
        if large_context_minimum <= 0:
            raise ValueError("large_context_minimum must be positive")
        self._registry = registry
        self._large_context_minimum = large_context_minimum
        self._definitions = tuple(definitions)
        for definition in self._definitions:
            if definition.id not in role_pool_ids:
                raise ValueError(f"no default role pool is defined for agent {definition.id!r}")
            if definition.id not in role_preferences:
                raise ValueError(f"no ordering preferences are defined for agent {definition.id!r}")
        self._role_pool_ids = dict(role_pool_ids)
        self._role_preferences = {role: dict(weights) for role, weights in role_preferences.items()}

    # -- public API ---------------------------------------------------------

    def build(self, overrides: Mapping[str, PoolOverrides] | None = None) -> tuple[PoolBuild, ...]:
        """Generate every automatic pool for the current registry snapshot.

        Passing no overrides (or an empty mapping) yields the defaults;
        that rebuild is the reset-to-default path (MVP-0414).
        """
        snapshot = self._snapshot()
        override_map = self._validate_overrides(overrides or {}, snapshot)

        builds: list[PoolBuild] = []
        for pool_id, name, purpose, axis, gate in self._capability_pool_specs():
            build = self._build_pool(
                pool_id=pool_id,
                name=name,
                purpose=purpose,
                policy_version="capability-v1",
                axis=axis,
                gate=gate,
                overrides=override_map.get(pool_id),
                snapshot=snapshot,
                always_emit=False,
            )
            if build is not None:
                builds.append(build)

        for family_id in sorted(snapshot.families_with_endpoints):
            pool_id = self._family_pool_id(family_id)
            leaf = _family_leaf(family_id)
            display_name = snapshot.family_display_names.get(family_id) or f"{leaf.title()} Family"

            def family_gate(
                candidate: _Candidate,
                _family_id: str = family_id,
            ) -> tuple[bool, str]:
                if candidate.variant is None:
                    return False, "no variant record for this endpoint in the registry snapshot"
                if candidate.variant.family_id == _family_id:
                    return True, f"serves a variant of the {_family_id} family"
                return False, f"serves a variant of {candidate.variant.family_id}, not {_family_id}"

            build = self._build_pool(
                pool_id=pool_id,
                name=display_name,
                purpose=f"Routable endpoints serving variants of the {family_id} family.",
                policy_version="family-v1",
                axis="reliability",
                gate=family_gate,
                overrides=override_map.get(pool_id),
                snapshot=snapshot,
                always_emit=False,
            )
            if build is not None:
                builds.append(build)

        for definition in self._definitions:
            pool_id = self._role_pool_ids[definition.id]
            weights = self._role_preferences[definition.id]
            axis = self._dominant_axis(weights)

            def role_gate(
                candidate: _Candidate,
                _definition: AgentDefinition = definition,
            ) -> tuple[bool, str]:
                return self._role_gate(candidate, _definition)

            build = self._build_pool(
                pool_id=pool_id,
                name=f"{definition.display_name} Pool",
                purpose=(
                    f"Role pool for the {definition.display_name} agent: {definition.purpose} "
                    f"Members are ordered {axis}-first as a deterministic preview of weighted routing."
                ),
                policy_version=f"role-{definition.id}-v1",
                axis=axis,
                gate=role_gate,
                overrides=override_map.get(pool_id),
                snapshot=snapshot,
                always_emit=True,
            )
            if build is not None:
                builds.append(build)
        return tuple(builds)

    # -- snapshot -----------------------------------------------------------

    def _snapshot(self) -> _Snapshot:
        variants = self._registry.list_variants()
        variants_by_id = {variant.id: variant for variant in variants}
        families: dict[str, str] = {}
        for family in self._registry.list_families():
            families[family.id] = family.display_name or f"{_family_leaf(family.id).title()} Family"

        candidates: dict[str, _Candidate] = {}
        endpoints_by_variant: dict[str, list[str]] = {}
        families_with_endpoints: set[str] = set()
        for endpoint in sorted(self._registry.list_endpoints(), key=lambda e: e.id):
            variant = variants_by_id.get(endpoint.model_variant_id)
            capabilities, origin = effective_capabilities(endpoint, variant)
            candidates[endpoint.id] = _Candidate(
                endpoint=endpoint,
                variant=variant,
                context_window=variant.context_window if variant is not None else None,
                capabilities=capabilities,
                capability_origin=origin,
            )
            if variant is not None:
                endpoints_by_variant.setdefault(variant.id, []).append(endpoint.id)
                families_with_endpoints.add(variant.family_id)
        return _Snapshot(
            candidates=candidates,
            endpoints_by_variant={
                variant_id: tuple(sorted(ids)) for variant_id, ids in endpoints_by_variant.items()
            },
            families_with_endpoints=frozenset(families_with_endpoints),
            family_display_names=families,
        )

    # -- pool gates ---------------------------------------------------------

    def _capability_pool_specs(
        self,
    ) -> tuple[tuple[str, str, str, str, Callable[[_Candidate], tuple[bool, str]]], ...]:
        minimum = self._large_context_minimum
        return (
            (
                "tool_calling",
                "Tool Calling",
                "Every endpoint whose effective tool-calling claim is True (unknown never qualifies).",
                "reliability",
                lambda candidate: capability_claim(candidate.capabilities, candidate.capability_origin, "tool_calling"),
            ),
            (
                "reasoning",
                "Reasoning",
                "Every endpoint whose effective reasoning claim is True (unknown never qualifies).",
                "reliability",
                lambda candidate: capability_claim(candidate.capabilities, candidate.capability_origin, "reasoning"),
            ),
            (
                "large_context",
                "Large Context",
                f"Every endpoint serving a variant with a context window of at least {minimum:,} tokens.",
                "reliability",
                lambda candidate: context_claim(candidate.context_window, minimum),
            ),
        )

    def _role_gate(self, candidate: _Candidate, definition: AgentDefinition) -> tuple[bool, str]:
        satisfied: list[str] = []
        required = definition.required_capabilities
        for field in BOOL_CAPABILITY_FIELDS:
            if getattr(required, field) is None:
                continue
            ok, reason = capability_claim(candidate.capabilities, candidate.capability_origin, field)
            if ok:
                satisfied.append(reason)
            else:
                return False, reason
        if definition.minimum_context_window is not None:
            ok, reason = context_claim(candidate.context_window, definition.minimum_context_window)
            if ok:
                satisfied.append(reason)
            else:
                return False, reason
        return True, f"meets {definition.display_name} constraints: " + "; ".join(satisfied)

    # -- membership and ordering -------------------------------------------

    def _build_pool(
        self,
        *,
        pool_id: str,
        name: str,
        purpose: str,
        policy_version: str,
        axis: str,
        gate: Callable[[_Candidate], tuple[bool, str]],
        overrides: PoolOverrides | None,
        snapshot: _Snapshot,
        always_emit: bool,
    ) -> PoolBuild | None:
        natural: dict[str, str] = {}
        excluded: dict[str, str] = {}
        for endpoint_id in sorted(snapshot.candidates):
            ok, reason = gate(snapshot.candidates[endpoint_id])
            (natural if ok else excluded)[endpoint_id] = reason

        member_ids, member_reasons, non_member_reasons = self._membership(
            pool_id=pool_id,
            snapshot=snapshot,
            natural=natural,
            excluded=excluded,
            overrides=overrides,
            axis=axis,
        )
        if not always_emit and not member_ids and overrides is None:
            # An automatic pool with nothing in it is noise unless the user
            # explicitly customized it (then it exists to show that state).
            return None

        pool = ModelPool(
            id=pool_id,
            name=name,
            purpose=purpose,
            member_endpoint_ids=tuple(member_ids),
            generated=True,
            policy_version=policy_version,
        )
        memberships = [
            PoolMembership(endpoint_id=endpoint_id, member=True, reason=member_reasons[endpoint_id])
            for endpoint_id in member_ids
        ]
        memberships.extend(
            PoolMembership(
                endpoint_id=endpoint_id,
                member=False,
                reason=non_member_reasons[endpoint_id],
            )
            for endpoint_id in sorted(non_member_reasons)
        )
        return PoolBuild(pool=pool, memberships=tuple(memberships))

    def _membership(
        self,
        *,
        pool_id: str,
        snapshot: _Snapshot,
        natural: Mapping[str, str],
        excluded: Mapping[str, str],
        overrides: PoolOverrides | None,
        axis: str,
    ) -> tuple[list[str], dict[str, str], dict[str, str]]:
        """Resolve member order and per-endpoint reasons after overrides."""
        if overrides is None:
            ordered = sorted(natural, key=lambda eid: self._axis_key(snapshot.candidates[eid], axis))
            return ordered, dict(natural), dict(excluded)

        include = set(self._expand_refs(overrides.include, snapshot, kind="include"))
        exclude = set(self._expand_refs(overrides.exclude, snapshot, kind="exclude"))
        pinned = self._expand_refs(overrides.pinned, snapshot, kind="pinned")

        member_reasons: dict[str, str] = {}
        # Exclude always wins: every excluded endpoint is a non-member, with
        # its own reason, even when the gates would have admitted it.
        non_member_reasons: dict[str, str] = {
            endpoint_id: "excluded by user override" for endpoint_id in exclude
        }
        natural_ordered = [
            endpoint_id
            for endpoint_id in sorted(natural, key=lambda eid: self._axis_key(snapshot.candidates[eid], axis))
            if endpoint_id not in exclude
        ]

        if pinned:
            pinned_reason = f"pinned by user override (pool {pool_id} restricted to the pinned selection)"
            include_reason = "included by user override alongside the pinned selection"
            outside_reason = f"outside the pinned selection for {pool_id}"
            user_ordered = [endpoint_id for endpoint_id in pinned if endpoint_id not in exclude]
            user_ordered.extend(
                endpoint_id for endpoint_id in sorted(include)
                if endpoint_id not in exclude and endpoint_id not in user_ordered
            )
            for endpoint_id in user_ordered:
                member_reasons[endpoint_id] = (
                    pinned_reason if endpoint_id in pinned else include_reason
                )
            for endpoint_id in natural_ordered:
                if endpoint_id not in user_ordered:
                    non_member_reasons[endpoint_id] = outside_reason
        else:
            include_reason = "included by user override (does not meet this pool's constraints)"
            user_ordered = [endpoint_id for endpoint_id in sorted(include) if endpoint_id not in exclude]
            for endpoint_id in user_ordered:
                member_reasons[endpoint_id] = include_reason
            for endpoint_id in natural_ordered:
                if endpoint_id not in user_ordered:
                    member_reasons[endpoint_id] = natural[endpoint_id]

        for endpoint_id, reason in excluded.items():
            if endpoint_id not in member_reasons and endpoint_id not in non_member_reasons:
                non_member_reasons[endpoint_id] = reason

        if pinned:
            ordered = user_ordered
        else:
            ordered = user_ordered + [
                endpoint_id for endpoint_id in natural_ordered if endpoint_id not in user_ordered
            ]
        return ordered, member_reasons, non_member_reasons

    def _expand_refs(
        self,
        refs: Sequence[str],
        snapshot: _Snapshot,
        *,
        kind: str,
    ) -> list[str]:
        """Resolve endpoint/variant references to concrete endpoint ids."""
        expanded: list[str] = []
        for ref in refs:
            if ref in snapshot.endpoints_by_variant:
                expanded.extend(snapshot.endpoints_by_variant[ref])
            elif ref in snapshot.candidates:
                expanded.append(ref)
            elif ":" in ref:
                raise ValueError(f"unknown variant {ref!r} in {kind} override")
            else:
                raise ValueError(f"unknown endpoint {ref!r} in {kind} override")
        return expanded

    def _validate_overrides(
        self,
        overrides: Mapping[str, PoolOverrides],
        snapshot: _Snapshot,
    ) -> dict[str, PoolOverrides]:
        allowed = {"tool_calling", "reasoning", "large_context"}
        allowed.update(self._family_pool_id(family_id) for family_id in snapshot.families_with_endpoints)
        allowed.update(self._role_pool_ids.values())
        for pool_id in overrides:
            if pool_id not in allowed:
                raise ValueError(f"unknown pool {pool_id!r} in overrides (not an automatic pool)")
        return dict(overrides)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _family_pool_id(family_id: str) -> str:
        leaf = _family_leaf(family_id)
        return f"{leaf}-family"

    @staticmethod
    def _axis_key(candidate: _Candidate, axis: str) -> tuple[object, ...]:
        endpoint = candidate.endpoint
        health = endpoint.health if endpoint.health is not None else -1.0
        latency = endpoint.latency_p95_ms if endpoint.latency_p95_ms is not None else float("inf")
        cost_in = endpoint.input_cost_per_million if endpoint.input_cost_per_million is not None else float("inf")
        cost_out = endpoint.output_cost_per_million if endpoint.output_cost_per_million is not None else float("inf")
        if axis == "latency":
            return (latency, -health, cost_in, cost_out, endpoint.id)
        if axis == "cost":
            return (cost_in, cost_out, -health, latency, endpoint.id)
        if axis == "quality":
            # Benchmark quality lands in a later phase; until then, health
            # descending then cost ascending is the documented proxy.
            return (-health, cost_in, cost_out, latency, endpoint.id)
        return (-health, latency, cost_in, cost_out, endpoint.id)  # reliability / default

    @staticmethod
    def _dominant_axis(weights: Mapping[str, float]) -> str:
        return max(
            weights,
            key=lambda axis: (weights.get(axis, 0.0), -_AXIS_PRIORITY.index(axis)),
        )
