"""Telemetry tests (MVP-0801..0810, MVP-0908)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from total_orch.domain import ExecutionEvent, WorkflowStatus
from total_orch.telemetry import TelemetryStore, WorkflowRun, redact


def _event(
    event_type: str,
    *,
    endpoint: str = "provider-a/gpt-test",
    metadata: dict[str, object] | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        workflow_id="wf-test",
        task_id="wf-test",
        event_type=event_type,
        status=WorkflowStatus.completed,
        provider_endpoint_id=endpoint,
        metadata={
            "trace_id": "trace-1",
            "provider": "provider-a",
            "model": "gpt-test",
            "latency_ms": 42.0,
            **(metadata or {}),
        },
    )


def test_events_are_persisted_redacted_and_aggregated_into_health() -> None:
    store = TelemetryStore(":memory:")

    store.record_event(_event("gateway.completion", metadata={"api_key": "sk-secret"}))
    events = store.events_for("wf-test")
    health = store.health("provider-a/gpt-test")

    assert events[0]["event_type"] == "gateway.completion"
    assert events[0]["metadata"]["api_key"] == "[redacted]"
    assert health["samples"] == 1
    assert health["success_rate"] == 1.0
    assert health["avg_latency_ms"] == 42.0
    assert health["cooldown_until"] is None
    store.close()


def test_three_consecutive_failures_open_a_cooldown() -> None:
    store = TelemetryStore(":memory:")

    for _ in range(3):
        store.record_event(
            _event("gateway.error", metadata={"error_code": "provider_unavailable"})
        )
    health = store.health("provider-a/gpt-test")

    assert health["consecutive_failures"] == 3
    assert health["cooldown_until"] is not None
    assert store.is_available("provider-a/gpt-test") is False
    store.close()


def test_run_lifecycle_report_and_metrics() -> None:
    store = TelemetryStore(":memory:")
    started = datetime.now(UTC)
    run = WorkflowRun(
        trace_id="trace-1",
        workflow_id="wf-test",
        request="Add a feature",
        task_type="feature",
        status="running",
        started_at=started,
    )
    store.record_run(run)
    store.finish_run(
        "wf-test",
        status="completed",
        finished_at=started + timedelta(seconds=2),
        error_code=None,
        result={"review": {"approved": True}},
    )

    stored = store.get_run("wf-test")
    report = store.report()
    metrics = store.metrics()

    assert stored["status"] == "completed"
    assert stored["result"]["review"]["approved"] is True
    assert report["runs_by_status"] == {"completed": 1}
    assert report["adaptive_policy_enabled"] is False
    assert metrics["workflow_runs"] == 1
    assert metrics["completed"] == 1
    store.close()


def test_redact_recurses_through_lists_and_nested_objects() -> None:
    assert redact(
        {"token": "x", "nested": [{"password": "y", "safe": "z"}], "count": 2}
    ) == {
        "token": "[redacted]",
        "nested": [{"password": "[redacted]", "safe": "z"}],
        "count": 2,
    }
