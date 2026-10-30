"""Model registry service: catalog sync orchestration and query APIs.

The service owns the deterministic pipeline behind the registry:

1. Fetch every configured catalog source concurrently. A failing source is
   reported and never aborts the rest of the pass (a public catalog outage
   must not take down routing).
2. Group observations by canonical entity id, merge fields by per-source
   authority (MVP-0312), assemble typed domain records, and upsert them into
   the SQLite store (MVP-0313).
3. Serve registry queries from the local store only — routing never blocks
   on a live external catalog request.

Families are synthesized from variant observations: every canonical variant
id ``{family}:{name}`` implies one observation of its family, so the families
table stays consistent with the variants that were actually observed.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from total_orch.domain import ModelFamily, ModelVariant, ProviderEndpoint, SourceProvenance

from .identity import IdentityRules, split_variant_id
from .merge import assemble_endpoint, assemble_family, assemble_variant
from .observations import (
    CatalogBatch,
    EndpointObservation,
    FamilyObservation,
    VariantObservation,
)
from .store import EntityKind, ModelRegistryStore


class CatalogSource(Protocol):
    """Anything that yields one ``CatalogBatch`` of normalized observations."""

    source: str

    async def fetch(self) -> CatalogBatch: ...


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    source: str
    status: str
    families: int = 0
    variants: int = 0
    endpoints: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SyncSummary:
    started_at: datetime
    finished_at: datetime
    sources: tuple[SourceSyncResult, ...]
    families_upserted: int = 0
    variants_upserted: int = 0
    endpoints_upserted: int = 0
    entity_errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return bool(self.sources) and all(result.status == "ok" for result in self.sources) and not self.entity_errors


@dataclass(frozen=True, slots=True)
class RegistryCounts:
    families: int
    variants: int
    endpoints: int


@dataclass(frozen=True, slots=True)
class StaleReport:
    """Entity ids whose newest observation is older than the configured age."""

    families: tuple[str, ...]
    variants: tuple[str, ...]
    endpoints: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.families or self.variants or self.endpoints)


class ModelRegistryService:
    """Orchestrates sync and answers queries against the local store."""

    def __init__(
        self,
        store: ModelRegistryStore,
        rules: IdentityRules,
        *,
        sources: Sequence[CatalogSource] = (),
    ) -> None:
        self._store = store
        self._rules = rules
        self._sources: list[CatalogSource] = list(sources)

    @property
    def store(self) -> ModelRegistryStore:
        return self._store

    @property
    def sources(self) -> tuple[CatalogSource, ...]:
        return tuple(self._sources)

    def add_source(self, source: CatalogSource) -> None:
        if not any(existing.source == source.source for existing in self._sources):
            self._sources.append(source)

    # -- sync ---------------------------------------------------------------

    async def sync(self) -> SyncSummary:
        started_at = datetime.now(UTC)
        outcomes = await asyncio.gather(*(self._fetch_one(source) for source in self._sources))
        batches = [batch for _result, batch in outcomes if batch is not None]
        results = [result for result, _batch in outcomes]

        groups = self._group(batches)
        entity_errors: list[str] = []
        families_upserted = self._upsert_families(groups.families, started_at, entity_errors)
        variants_upserted = self._upsert_variants(groups.variants, started_at, entity_errors)
        endpoints_upserted = self._upsert_endpoints(groups.endpoints, started_at, entity_errors)

        return SyncSummary(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            sources=tuple(results),
            families_upserted=families_upserted,
            variants_upserted=variants_upserted,
            endpoints_upserted=endpoints_upserted,
            entity_errors=tuple(entity_errors),
        )

    async def _fetch_one(self, source: CatalogSource) -> tuple[SourceSyncResult, CatalogBatch | None]:
        try:
            batch = await source.fetch()
        except Exception as exc:  # noqa: BLE001 - isolation by design; one source never aborts the pass
            message = f"{type(exc).__name__}: {exc}"
            return SourceSyncResult(source=source.source, status="error", error=message), None
        return (
            SourceSyncResult(
                source=source.source,
                status="ok",
                families=len(batch.families),
                variants=len(batch.variants),
                endpoints=len(batch.endpoints),
            ),
            batch,
        )

    @dataclass(slots=True)
    class _Groups:
        families: dict[str, list[FamilyObservation]] = field(default_factory=dict)
        variants: dict[str, list[VariantObservation]] = field(default_factory=dict)
        endpoints: dict[str, list[EndpointObservation]] = field(default_factory=dict)

    def _group(self, batches: Sequence[CatalogBatch]) -> _Groups:
        groups = self._Groups()
        families: dict[str, list[FamilyObservation]] = defaultdict(list)
        variants: dict[str, list[VariantObservation]] = defaultdict(list)
        endpoints: dict[str, list[EndpointObservation]] = defaultdict(list)

        for batch in batches:
            for family_observation in batch.families:
                if family_observation.family_id:
                    families[family_observation.family_id].append(family_observation)
            for variant_observation in batch.variants:
                if not variant_observation.variant_id:
                    continue
                variants[variant_observation.variant_id].append(variant_observation)
                family_id, _name = split_variant_id(variant_observation.variant_id)
                families[family_id].append(
                    FamilyObservation(source=variant_observation.source, family_id=family_id)
                )
            for endpoint_observation in batch.endpoints:
                if endpoint_observation.endpoint is not None:
                    endpoints[endpoint_observation.endpoint.id].append(endpoint_observation)

        groups.families = dict(families)
        groups.variants = dict(variants)
        groups.endpoints = dict(endpoints)
        return groups

    def _upsert_families(
        self,
        groups: dict[str, list[FamilyObservation]],
        observed_at: datetime,
        entity_errors: list[str],
    ) -> int:
        upserted = 0
        for family_id, observations in groups.items():
            try:
                record, provenance = assemble_family(observations)
            except Exception as exc:  # noqa: BLE001 - one bad entity is reported, the rest of the pass continues
                entity_errors.append(f"family {family_id}: {exc}")
                continue
            if self._upsert_for_sources(record, provenance, observations, observed_at):
                upserted += 1
        return upserted

    def _upsert_variants(
        self,
        groups: dict[str, list[VariantObservation]],
        observed_at: datetime,
        entity_errors: list[str],
    ) -> int:
        upserted = 0
        for variant_id, observations in groups.items():
            try:
                record, provenance = assemble_variant(observations)
            except Exception as exc:  # noqa: BLE001 - one bad entity is reported, the rest of the pass continues
                entity_errors.append(f"variant {variant_id}: {exc}")
                continue
            if self._upsert_for_sources(record, provenance, observations, observed_at):
                upserted += 1
        return upserted

    def _upsert_endpoints(
        self,
        groups: dict[str, list[EndpointObservation]],
        observed_at: datetime,
        entity_errors: list[str],
    ) -> int:
        upserted = 0
        for endpoint_id, observations in groups.items():
            try:
                assembled = assemble_endpoint(observations)
            except Exception as exc:  # noqa: BLE001 - one bad entity is reported, the rest of the pass continues
                entity_errors.append(f"endpoint {endpoint_id}: {exc}")
                continue
            if assembled is None:
                # Pricing and capability observations that never matched a
                # routable connection describe nothing we can route to.
                continue
            (record, _), provenance = assembled
            if self._upsert_for_sources(record, provenance, observations, observed_at):
                upserted += 1
        return upserted

    def _upsert_for_sources(
        self,
        record: ModelFamily | ModelVariant | ProviderEndpoint,
        provenance: dict[str, SourceProvenance],
        observations: Sequence[FamilyObservation | VariantObservation | EndpointObservation],
        observed_at: datetime,
    ) -> bool:
        """Upsert the merged record once per contributing source.

        The store's change detection makes the second and later calls for an
        unchanged record a no-op, so only the first source reports a change.
        """
        changed = False
        for source_name in sorted({observation.source for observation in observations}):
            if isinstance(record, ModelFamily):
                row_changed = self._store.upsert_family(record, provenance, source_name, observed_at=observed_at)
            elif isinstance(record, ModelVariant):
                row_changed = self._store.upsert_variant(record, provenance, source_name, observed_at=observed_at)
            else:
                row_changed = self._store.upsert_endpoint(record, provenance, source_name, observed_at=observed_at)
            changed = changed or row_changed
        return changed

    # -- queries (MVP-0315) --------------------------------------------------

    def list_families(self) -> list[ModelFamily]:
        return self._store.list_families()

    def list_variants(self, family_id: str | None = None) -> list[ModelVariant]:
        return self._store.list_variants(family_id=family_id)

    def list_endpoints(
        self,
        *,
        model_variant_id: str | None = None,
        provider: str | None = None,
    ) -> list[ProviderEndpoint]:
        return self._store.list_endpoints(model_variant_id=model_variant_id, provider=provider)

    def get_family(self, family_id: str) -> ModelFamily:
        for family in self._store.list_families():
            if family.id == family_id:
                return family
        raise KeyError(f"unknown model family: {family_id}")

    def get_variant(self, variant_id: str) -> ModelVariant:
        return self._store.get_variant(variant_id)

    def get_endpoint(self, endpoint_id: str) -> ProviderEndpoint:
        return self._store.get_endpoint(endpoint_id)

    def resolve_variant(self, provider: str | None, model_name: str) -> ModelVariant:
        """Resolve a provider model name onto its canonical variant record."""
        identity = self._rules.resolve(provider, model_name)
        return self._store.get_variant(identity.variant_id)

    def endpoint_candidates(self, model_variant_id: str) -> list[ProviderEndpoint]:
        """Endpoints that can actually serve a canonical variant."""
        return self._store.list_endpoints(model_variant_id=model_variant_id)

    def counts(self) -> RegistryCounts:
        return RegistryCounts(
            families=self._store.count_families(),
            variants=self._store.count_variants(),
            endpoints=self._store.count_endpoints(),
        )

    def provenance(self, kind: EntityKind, entity_id: str) -> dict[str, SourceProvenance]:
        return self._store.field_provenance(kind, entity_id)

    def stale(self, max_age: timedelta, *, now: datetime | None = None) -> StaleReport:
        """Entity ids not observed by any source within ``max_age`` (MVP-0314)."""
        return StaleReport(
            families=tuple(self._store.stale_entities("family", max_age, now=now)),
            variants=tuple(self._store.stale_entities("variant", max_age, now=now)),
            endpoints=tuple(self._store.stale_entities("endpoint", max_age, now=now)),
        )
