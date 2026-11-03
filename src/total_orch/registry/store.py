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
from threading import RLock
from typing import Literal

from total_orch.domain import (
    ModelFamily,
    ModelVariant,
    ProviderConnection,
    ProviderEndpoint,
    SourceProvenance,
)

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
CREATE TABLE IF NOT EXISTS provider_connections (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_connections_provider ON provider_connections(provider);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
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
CREATE TABLE IF NOT EXISTS field_overrides (
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity_kind, entity_id, field)
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
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = RLock()
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")

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

    # -- provider connection metadata --------------------------------------

    def upsert_connection(self, record: ProviderConnection) -> bool:
        payload = record.model_dump_json()
        row = self._connection.execute(
            "SELECT payload FROM provider_connections WHERE id = ?", (record.id,)
        ).fetchone()
        if row is not None and _stable_payload(str(row["payload"])) == _stable_payload(payload):
            return False
        self._connection.execute(
            "INSERT INTO provider_connections (id, provider, schema_version, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, "
            "schema_version = excluded.schema_version, payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (record.id, record.provider, record.schema_version, payload, _utc_iso()),
        )
        self._connection.commit()
        return True

    def list_connections(self, *, provider: str | None = None) -> list[ProviderConnection]:
        if provider is None:
            rows = self._connection.execute(
                "SELECT payload FROM provider_connections ORDER BY id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM provider_connections WHERE provider = ? ORDER BY id",
                (provider,),
            ).fetchall()
        return [ProviderConnection.model_validate_json(str(row["payload"])) for row in rows]

    def get_connection(self, connection_id: str) -> ProviderConnection:
        row = self._connection.execute(
            "SELECT payload FROM provider_connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown provider connection: {connection_id}")
        return ProviderConnection.model_validate_json(str(row["payload"]))

    def delete_connection(self, connection_id: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM provider_connections WHERE id = ?", (connection_id,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    # -- TotalOrch API keys -------------------------------------------------

    def create_api_key(self, key_id: str, label: str, token_hash: str) -> None:
        self._connection.execute(
            "INSERT INTO api_keys (id, label, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (key_id, label, token_hash, _utc_iso()),
        )
        self._connection.commit()

    def has_active_api_key(self, token_hash: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM api_keys WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        return row is not None

    def list_api_keys(self) -> list[dict[str, str | None]]:
        rows = self._connection.execute(
            "SELECT id, label, created_at, revoked_at FROM api_keys ORDER BY created_at, id"
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "label": str(row["label"]),
                "created_at": str(row["created_at"]),
                "revoked_at": str(row["revoked_at"]) if row["revoked_at"] is not None else None,
            }
            for row in rows
        ]

    def revoke_api_key(self, key_id: str) -> bool:
        cursor = self._connection.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_utc_iso(), key_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

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

    # -- manual overrides ---------------------------------------------------

    def set_override(self, kind: EntityKind, entity_id: str, field: str, value: object) -> None:
        self._connection.execute(
            "INSERT INTO field_overrides (entity_kind, entity_id, field, value, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_kind, entity_id, field) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (kind, entity_id, field, json.dumps(value, ensure_ascii=False), _utc_iso()),
        )
        self._connection.commit()

    def delete_override(self, kind: EntityKind, entity_id: str, field: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM field_overrides WHERE entity_kind = ? AND entity_id = ? AND field = ?",
            (kind, entity_id, field),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def overrides_for(self, kind: EntityKind, entity_id: str) -> dict[str, object]:
        rows = self._connection.execute(
            "SELECT field, value FROM field_overrides WHERE entity_kind = ? AND entity_id = ?",
            (kind, entity_id),
        ).fetchall()
        return {str(row["field"]): json.loads(str(row["value"])) for row in rows}

    def list_overrides(self, *, kind: EntityKind | None = None, entity_id: str | None = None) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[str] = []
        if kind is not None:
            clauses.append("entity_kind = ?")
            params.append(kind)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT entity_kind, entity_id, field, value, updated_at FROM field_overrides {where} "
            "ORDER BY entity_kind, entity_id, field",
            params,
        ).fetchall()
        return [
            {
                "kind": str(row["entity_kind"]),
                "id": str(row["entity_id"]),
                "field": str(row["field"]),
                "value": json.loads(str(row["value"])),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]
