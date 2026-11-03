"""Async SQLite telemetry ledger (MVP-0801..0810).

Writes are queued to a daemon writer thread so a completed workflow never
blocks on disk. Reads flush the queue first, which keeps the API and tests
consistent without a second database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import RLock, Thread
from typing import Any

from total_orch.domain import ExecutionEvent

from .models import EndpointObservation, WorkflowRun
from .redaction import redact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    request TEXT NOT NULL,
    task_type TEXT,
    workspace TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    payload TEXT NOT NULL,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started ON workflow_runs(started_at);
CREATE TABLE IF NOT EXISTS execution_events (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    trace_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_events_workflow ON execution_events(workflow_id);
CREATE TABLE IF NOT EXISTS endpoint_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    success INTEGER NOT NULL,
    latency_ms REAL,
    ttft_ms REAL,
    error_code TEXT,
    route_decision_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_endpoint_observations_endpoint
    ON endpoint_observations(endpoint_id, created_at);
"""

Operation = Callable[[sqlite3.Connection], None]
COOLDOWN_AFTER_FAILURES = 3
COOLDOWN_SECONDS = 60.0


def _utc_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


class TelemetryStore:
    """Queue-backed telemetry writer with read-side helpers for the API/report."""

    def __init__(self, database_path: str, *, async_writes: bool = True) -> None:
        self.database_path = database_path
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._queue: Queue[Operation | None] = Queue()
        self._thread: Thread | None = None
        self._closed = False
        self._schema()
        if async_writes:
            self._thread = Thread(target=self._worker, name="total-orch-telemetry", daemon=True)
            self._thread.start()

    def _schema(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    # -- writes -------------------------------------------------------------

    def record_event(self, event: ExecutionEvent) -> None:
        payload = redact(event.model_dump(mode="json"))
        encoded = json.dumps(payload, ensure_ascii=False)
        created_at = _utc_iso(event.timestamp)

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO execution_events "
                "(id, workflow_id, trace_id, event_type, status, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.id),
                    event.workflow_id,
                    event.metadata.get("trace_id", event.workflow_id),
                    event.event_type,
                    event.status.value,
                    encoded,
                    created_at,
                ),
            )

        self._submit(write)
        self._observe_event(event)

    def record_run(self, run: WorkflowRun) -> None:
        payload = redact(run.model_dump(mode="json"))
        encoded = json.dumps(payload, ensure_ascii=False)
        result = json.dumps(redact(run.result), ensure_ascii=False)

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO workflow_runs "
                "(workflow_id, trace_id, request, task_type, workspace, status, started_at, "
                " finished_at, error_code, payload, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET "
                "trace_id = excluded.trace_id, request = excluded.request, "
                "task_type = excluded.task_type, workspace = excluded.workspace, "
                "status = excluded.status, started_at = excluded.started_at, "
                "finished_at = excluded.finished_at, error_code = excluded.error_code, "
                "payload = excluded.payload, result = excluded.result",
                (
                    run.workflow_id,
                    run.trace_id,
                    run.request,
                    run.task_type,
                    run.workspace,
                    run.status,
                    _utc_iso(run.started_at),
                    _utc_iso(run.finished_at) if run.finished_at is not None else None,
                    run.error_code,
                    encoded,
                    result,
                ),
            )

        self._submit(write)

    def finish_run(
        self,
        workflow_id: str,
        *,
        status: str,
        finished_at: datetime,
        error_code: str | None,
        result: dict[str, Any],
    ) -> None:
        encoded_result = json.dumps(redact(result), ensure_ascii=False)

        finished = _utc_iso(finished_at)

        def write(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT payload FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                return
            payload = json.loads(str(row["payload"]))
            payload.update({"status": status, "finished_at": finished, "error_code": error_code})
            connection.execute(
                "UPDATE workflow_runs SET status = ?, finished_at = ?, error_code = ?, result = ?, "
                "payload = ? WHERE workflow_id = ?",
                (
                    status,
                    finished,
                    error_code,
                    encoded_result,
                    json.dumps(payload, ensure_ascii=False),
                    workflow_id,
                ),
            )

        self._submit(write)

    def record_observation(self, observation: EndpointObservation) -> None:
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO endpoint_observations "
                "(endpoint_id, provider, model, success, latency_ms, ttft_ms, error_code, "
                " route_decision_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation.endpoint_id,
                    observation.provider,
                    observation.model,
                    1 if observation.success else 0,
                    observation.latency_ms,
                    observation.ttft_ms,
                    observation.error_code,
                    observation.route_decision_id,
                    _utc_iso(observation.observed_at),
                ),
            )

        self._submit(write)

    # -- reads --------------------------------------------------------------

    def get_run(self, workflow_id: str) -> dict[str, Any]:
        self.flush()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow: {workflow_id}")
        data = dict(row)
        data["payload"] = json.loads(str(data["payload"]))
        data["result"] = json.loads(str(data["result"])) if data["result"] else None
        return data

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.flush()
        with self._lock:
            rows = self._connection.execute(
                "SELECT workflow_id, trace_id, request, task_type, status, started_at, finished_at, "
                "error_code FROM workflow_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def events_for(self, workflow_id: str) -> list[dict[str, Any]]:
        self.flush()
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM execution_events WHERE workflow_id = ? ORDER BY created_at, id",
                (workflow_id,),
            ).fetchall()
        return [json.loads(str(row["payload"])) for row in rows]

    def health(self, endpoint_id: str, *, window_seconds: float = 3600.0) -> dict[str, Any]:
        self.flush()
        cutoff = _utc_iso(datetime.now(UTC) - timedelta(seconds=window_seconds))
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS samples, COALESCE(SUM(success), 0) AS successes, "
                "AVG(latency_ms) AS avg_latency, MAX(CASE WHEN success = 1 THEN created_at END) AS last_success "
                "FROM endpoint_observations WHERE endpoint_id = ? AND created_at >= ?",
                (endpoint_id, cutoff),
            ).fetchone()
            recent = self._connection.execute(
                "SELECT success, created_at FROM endpoint_observations "
                "WHERE endpoint_id = ? ORDER BY id DESC LIMIT 20",
                (endpoint_id,),
            ).fetchall()
            last_failure = self._connection.execute(
                "SELECT MAX(created_at) AS last_failure FROM endpoint_observations "
                "WHERE endpoint_id = ? AND success = 0",
                (endpoint_id,),
            ).fetchone()
        samples = int(row["samples"]) if row is not None else 0
        successes = int(row["successes"]) if row is not None else 0
        failures = 0
        for item in recent:
            if int(item["success"]) == 1:
                break
            failures += 1
        cooldown_until: str | None = None
        if failures >= COOLDOWN_AFTER_FAILURES and last_failure is not None and last_failure["last_failure"]:
            until = datetime.fromisoformat(str(last_failure["last_failure"])) + timedelta(
                seconds=COOLDOWN_SECONDS
            )
            if until > datetime.now(UTC):
                cooldown_until = until.isoformat()
        return {
            "endpoint_id": endpoint_id,
            "samples": samples,
            "success_rate": (successes / samples) if samples else None,
            "avg_latency_ms": float(row["avg_latency"]) if row is not None and row["avg_latency"] is not None else None,
            "last_success_at": str(row["last_success"]) if row is not None and row["last_success"] else None,
            "consecutive_failures": failures,
            "cooldown_until": cooldown_until,
        }

    def is_available(self, endpoint_id: str, *, now: datetime | None = None) -> bool:
        state = self.health(endpoint_id)
        until = state["cooldown_until"]
        if until is None:
            return True
        return datetime.fromisoformat(str(until)) <= (now or datetime.now(UTC))

    def report(self) -> dict[str, Any]:
        self.flush()
        with self._lock:
            status_rows = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM workflow_runs GROUP BY status ORDER BY status"
            ).fetchall()
            endpoint_rows = self._connection.execute(
                "SELECT DISTINCT endpoint_id FROM endpoint_observations ORDER BY endpoint_id LIMIT 20"
            ).fetchall()
            recent = self._connection.execute(
                "SELECT workflow_id, trace_id, task_type, status, started_at, finished_at, error_code "
                "FROM workflow_runs ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
        return {
            "runs_by_status": {str(row["status"]): int(row["n"]) for row in status_rows},
            "endpoints": [self.health(str(row["endpoint_id"])) for row in endpoint_rows],
            "recent_runs": [dict(row) for row in recent],
            "adaptive_policy_enabled": False,
        }

    def metrics(self) -> dict[str, int]:
        self.flush()
        with self._lock:
            runs = self._connection.execute("SELECT COUNT(*) AS n FROM workflow_runs").fetchone()
            completed = self._connection.execute(
                "SELECT COUNT(*) AS n FROM workflow_runs WHERE status = 'completed'"
            ).fetchone()
            failed = self._connection.execute(
                "SELECT COUNT(*) AS n FROM workflow_runs WHERE status = 'failed'"
            ).fetchone()
            events = self._connection.execute("SELECT COUNT(*) AS n FROM execution_events").fetchone()
            observations = self._connection.execute(
                "SELECT COUNT(*) AS n FROM endpoint_observations"
            ).fetchone()
        return {
            "workflow_runs": int(runs["n"]) if runs is not None else 0,
            "completed": int(completed["n"]) if completed is not None else 0,
            "failed": int(failed["n"]) if failed is not None else 0,
            "events": int(events["n"]) if events is not None else 0,
            "endpoint_observations": int(observations["n"]) if observations is not None else 0,
        }

    # -- lifecycle ----------------------------------------------------------

    def flush(self) -> None:
        if self._thread is not None:
            self._queue.join()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=5)
        with self._lock:
            self._connection.close()

    # -- internals ----------------------------------------------------------

    def _submit(self, operation: Operation) -> None:
        if self._thread is None:
            with self._lock:
                operation(self._connection)
                self._connection.commit()
            return
        self._queue.put(operation)

    def _worker(self) -> None:
        while True:
            operation = self._queue.get()
            try:
                if operation is None:
                    return
                with self._lock:
                    operation(self._connection)
                    self._connection.commit()
            finally:
                self._queue.task_done()

    def _observe_event(self, event: ExecutionEvent) -> None:
        metadata = event.metadata
        endpoint_id = event.provider_endpoint_id
        if endpoint_id is None:
            return
        route_decision_id = metadata.get("route_decision_id")
        latency = metadata.get("latency_ms")
        ttft = metadata.get("ttft_ms")
        model = metadata.get("model")
        provider = metadata.get("provider")
        if event.event_type == "gateway.completion":
            observation = EndpointObservation(
                endpoint_id=endpoint_id,
                provider=str(provider) if provider is not None else None,
                model=str(model) if model is not None else None,
                success=True,
                latency_ms=float(latency) if latency is not None else None,
                ttft_ms=float(ttft) if ttft is not None else None,
                route_decision_id=str(route_decision_id) if route_decision_id is not None else None,
                observed_at=event.timestamp,
            )
        elif event.event_type in {"gateway.error", "gateway.timeout"}:
            observation = EndpointObservation(
                endpoint_id=endpoint_id,
                provider=str(provider) if provider is not None else None,
                model=str(model) if model is not None else None,
                success=False,
                latency_ms=float(latency) if latency is not None else None,
                error_code=str(metadata.get("error_code", "gateway_error")),
                route_decision_id=str(route_decision_id) if route_decision_id is not None else None,
                observed_at=event.timestamp,
            )
        else:
            return
        self.record_observation(observation)
