"""SQLite persistence for routing decisions (MVP-0513).

``RouteDecisionStore`` persists the full ``RouteDecision`` payload so a
historical decision can be replayed after policy changes: the record carries
the per-factor scores, the weighted totals, and a snapshot of the factor
weights that produced the selection. Like the registry store, SQLite is the
MVP choice: embedded, dependency-free, and fast enough for the audit queries
the explanation UI performs.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from total_orch.domain import RouteDecision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_decisions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    selected_endpoint_id TEXT,
    policy_version TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_decisions_request ON route_decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_route_decisions_selected ON route_decisions(selected_endpoint_id);
"""


def _utc_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


class RouteDecisionStore:
    """Thread-confined SQLite store. Open one instance per owner."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = RLock()
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- writes --------------------------------------------------------------

    def save_decision(self, decision: RouteDecision) -> bool:
        """Insert a decision; returns False when its id already exists.

        Decisions are immutable audit records: an existing id is never
        rewritten, so replaying a stored decision keeps the original facts.
        """

        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO route_decisions "
                "(id, request_id, selected_endpoint_id, policy_version, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    str(decision.id),
                    str(decision.request_id),
                    decision.selected_endpoint_id,
                    decision.policy_version,
                    decision.model_dump_json(),
                    _utc_iso(decision.created_at),
                ),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    # -- queries -------------------------------------------------------------

    def get_decision(self, decision_id: UUID | str) -> RouteDecision:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM route_decisions WHERE id = ?", (str(decision_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown route decision: {decision_id}")
        return RouteDecision.model_validate_json(str(row["payload"]))

    def decisions_for_request(self, request_id: UUID | str) -> list[RouteDecision]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM route_decisions WHERE request_id = ? ORDER BY created_at, id",
                (str(request_id),),
            ).fetchall()
        return [RouteDecision.model_validate_json(str(row["payload"])) for row in rows]

    def list_decisions(
        self,
        *,
        policy_version: str | None = None,
        selected_endpoint_id: str | None = None,
        explored: bool | None = None,
        limit: int | None = None,
    ) -> list[RouteDecision]:
        """Decisions ordered oldest first, matching the audit use case."""

        clauses: list[str] = []
        params: list[object] = []
        if policy_version is not None:
            clauses.append("policy_version = ?")
            params.append(policy_version)
        if selected_endpoint_id is not None:
            clauses.append("selected_endpoint_id = ?")
            params.append(selected_endpoint_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT payload FROM route_decisions {where} ORDER BY created_at, id"
        if limit is not None and limit < 0:
            raise ValueError("limit must not be negative")
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        decisions = [RouteDecision.model_validate_json(str(row["payload"])) for row in rows]
        if explored is not None:
            decisions = [decision for decision in decisions if decision.explored is explored]
        return decisions if limit is None else decisions[:limit]

    def count_decisions(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM route_decisions").fetchone()
        return int(row["n"])
