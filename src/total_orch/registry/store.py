"""SQLite persistence for the model registry.

The store keeps the merged, normalized domain records plus two bookkeeping
tables that are not part of the public domain contracts:

- ``field_provenance`` records which source supplied each field of each entity
  and when (MVP-0311).
- ``source_seen`` tracks the last time each source observed each entity so
  stale records can be detected without touching external catalogs
  (MVP-0314).

Upserts are idempotent: a sync pass that changed nothing rewrites nothing
(MVP-0313). SQLite is the selected local database for the MVP: embedded,
dependency-free, and fast enough for routing queries that must never block on
a live external catalog request.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from total_orch.domain import ModelFamily, ModelVariant, ProviderEndpoint, SourceProvenance

EntityKind = Literal["family", "variant", "endpoint"]

_TABLE_BY_KIND: Mapping[EntityKind, str] = {
    "family": "families",
    "variant": "variants",
    "endpoint": "endpoints",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variants (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoints (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_model_name TEXT NOT NULL,
    model_variant_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variants_family ON variants(family_id);
CREATE INDEX IF NOT EXISTS idx_endpoints_variant ON endpoints(model_variant_id);
CREATE INDEX IF NOT EXISTS idx_endpoints_provider ON endpoints(provider);
CREATE TABLE IF NOT EXISTS field_provenance (
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (entity_kind, entity_id, field)
);
CREATE TABLE IF NOT EXISTS source_seen (
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (entity_kind, entity_id, source)
);
"""


def _utc_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _stable_payload(payload: str) -> str:
    """Normalize a serialized record for change detection.

    Provenance ``observed_at`` timestamps move on every sync even when the
    underlying facts are unchanged, and provenance order is not guaranteed
    across processes. Both are stripped before comparison so an idempotent
    refresh rewrites nothing (MVP-0313).
    """

    data = json.loads(payload)
    provenance = data.get("provenance")
    if isinstance(provenance, list):
        normalized: list[dict[str, object]] = []
        for item in provenance:
            if isinstance(item, dict):
                entry = dict(item)
                entry.pop("observed_at", None)
                normalized.append(entry)
        normalized.sort(key=lambda entry: json.dumps(entry, sort_keys=True))
        data["provenance"] = normalized
    return json.dumps(data, sort_keys=True)


class ModelRegistryStore:
    """Thread-confined SQLite store. Open one instance per owner."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # -- upserts ------------------------------------------------------------

    def upsert_family(
        self,
        record: ModelFamily,
        field_provenance: Mapping[str, SourceProvenance],
        source: str,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        changed = self._upsert_row("family", record.id, record.model_dump_json(), record.schema_version, {})
        self._replace_field_provenance("family", record.id, field_provenance)
        self._mark_seen("family", record.id, source, observed_at)
        return changed

    def upsert_variant(
        self,
        record: ModelVariant,
        field_provenance: Mapping[str, SourceProvenance],
        source: str,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        changed = self._upsert_row(
            "variant",
            record.id,
            record.model_dump_json(),
            record.schema_version,
            {"family_id": record.family_id, "name": record.name},
        )
        self._replace_field_provenance("variant", record.id, field_provenance)
        self._mark_seen("variant", record.id, source, observed_at)
        return changed

    def upsert_endpoint(
        self,
        record: ProviderEndpoint,
        field_provenance: Mapping[str, SourceProvenance],
        source: str,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        changed = self._upsert_row(
            "endpoint",
            record.id,
            record.model_dump_json(),
            record.schema_version,
            {
                "provider": record.provider,
                "provider_model_name": record.provider_model_name,
                "model_variant_id": record.model_variant_id,
            },
        )
        self._replace_field_provenance("endpoint", record.id, field_provenance)
        self._mark_seen("endpoint", record.id, source, observed_at)
        return changed

    def _upsert_row(
        self,
        kind: EntityKind,
        entity_id: str,
        payload: str,
        schema_version: int,
        extra: Mapping[str, str],
    ) -> bool:
        table = _TABLE_BY_KIND[kind]
        cursor = self._connection.execute(f"SELECT payload FROM {table} WHERE id = ?", (entity_id,))
        existing = cursor.fetchone()
        if existing is not None and _stable_payload(str(existing["payload"])) == _stable_payload(payload):
            return False
        now = _utc_iso()
        columns = ["id", "schema_version", "payload", "updated_at"]
        values: list[object] = [entity_id, schema_version, payload, now]
        for column, value in extra.items():
            columns.append(column)
            values.append(value)
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
        self._connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )
        self._connection.commit()
        return True

    def _replace_field_provenance(
        self,
        kind: EntityKind,
        entity_id: str,
        field_provenance: Mapping[str, SourceProvenance],
    ) -> None:
        self._connection.execute(
            "DELETE FROM field_provenance WHERE entity_kind = ? AND entity_id = ?", (kind, entity_id)
        )
        for field_name, source in field_provenance.items():
            self._connection.execute(
                "INSERT INTO field_provenance (entity_kind, entity_id, field, source, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, entity_id, field_name, source.source, source.model_dump_json()),
            )
        self._connection.commit()

    def _mark_seen(self, kind: EntityKind, entity_id: str, source: str, observed_at: datetime | None) -> None:
        self._connection.execute(
            "INSERT INTO source_seen (entity_kind, entity_id, source, last_seen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(entity_kind, entity_id, source) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at",
            (kind, entity_id, source, _utc_iso(observed_at)),
        )
        self._connection.commit()

    # -- queries ------------------------------------------------------------

    def list_families(self) -> list[ModelFamily]:
        rows = self._connection.execute("SELECT payload FROM families ORDER BY id").fetchall()
        return [ModelFamily.model_validate_json(str(row["payload"])) for row in rows]

    def list_variants(self, family_id: str | None = None) -> list[ModelVariant]:
        if family_id is None:
            rows = self._connection.execute("SELECT payload FROM variants ORDER BY id").fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM variants WHERE family_id = ? ORDER BY id", (family_id,)
            ).fetchall()
        return [ModelVariant.model_validate_json(str(row["payload"])) for row in rows]

    def list_endpoints(
        self,
        *,
        model_variant_id: str | None = None,
        provider: str | None = None,
    ) -> list[ProviderEndpoint]:
        clauses: list[str] = []
        params: list[str] = []
        if model_variant_id is not None:
            clauses.append("model_variant_id = ?")
            params.append(model_variant_id)
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(f"SELECT payload FROM endpoints {where} ORDER BY id", params).fetchall()
        return [ProviderEndpoint.model_validate_json(str(row["payload"])) for row in rows]

    def get_variant(self, variant_id: str) -> ModelVariant:
        row = self._connection.execute("SELECT payload FROM variants WHERE id = ?", (variant_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown model variant: {variant_id}")
        return ModelVariant.model_validate_json(str(row["payload"]))

    def get_endpoint(self, endpoint_id: str) -> ProviderEndpoint:
        row = self._connection.execute("SELECT payload FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown provider endpoint: {endpoint_id}")
        return ProviderEndpoint.model_validate_json(str(row["payload"]))

    def count_families(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) AS n FROM families").fetchone()["n"])

    def count_variants(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) AS n FROM variants").fetchone()["n"])

    def count_endpoints(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) AS n FROM endpoints").fetchone()["n"])

    # -- provenance and freshness -------------------------------------------

    def field_provenance(self, kind: EntityKind, entity_id: str) -> dict[str, SourceProvenance]:
        rows = self._connection.execute(
            "SELECT field, payload FROM field_provenance WHERE entity_kind = ? AND entity_id = ?",
            (kind, entity_id),
        ).fetchall()
        return {str(row["field"]): SourceProvenance.model_validate_json(str(row["payload"])) for row in rows}

    def last_observed_at(self, kind: EntityKind, entity_id: str) -> datetime | None:
        row = self._connection.execute(
            "SELECT MAX(last_seen_at) AS last_seen FROM source_seen "
            "WHERE entity_kind = ? AND entity_id = ?",
            (kind, entity_id),
        ).fetchone()
        if row is None or row["last_seen"] is None:
            return None
        return _parse_iso(str(row["last_seen"]))

    def stale_entities(self, kind: EntityKind, max_age: timedelta, now: datetime | None = None) -> list[str]:
        """Entity ids whose newest observation is older than ``max_age``."""

        cutoff = _utc_iso((now or datetime.now(UTC)) - max_age)
        rows = self._connection.execute(
            "SELECT entity_id FROM ("
            "  SELECT entity_id, MAX(last_seen_at) AS newest "
            "  FROM source_seen WHERE entity_kind = ? GROUP BY entity_id"
            ") WHERE newest < ? ORDER BY entity_id",
            (kind, cutoff),
        ).fetchall()
        return [str(row["entity_id"]) for row in rows]

    def seen_by(self, kind: EntityKind, entity_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT source FROM source_seen WHERE entity_kind = ? AND entity_id = ? ORDER BY source",
            (kind, entity_id),
        ).fetchall()
        return [str(row["source"]) for row in rows]
