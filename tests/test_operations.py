"""Operational checks: SQLite backup/restore and restart-safe telemetry."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from total_orch.domain import ModelFamily, WorkflowStatus
from total_orch.registry import ModelRegistryStore
from total_orch.telemetry import TelemetryStore, WorkflowRun


def test_registry_file_can_be_backed_up_and_reopened(tmp_path: Path) -> None:
    database = tmp_path / "registry.db"
    store = ModelRegistryStore(str(database))
    store.initialize()
    store.upsert_family(ModelFamily(id="openai/gpt", publisher="openai"), {}, "test")
    store.close()

    backup = tmp_path / "registry-backup.db"
    shutil.copyfile(database, backup)
    restored = ModelRegistryStore(str(backup))
    restored.initialize()

    assert [family.id for family in restored.list_families()] == ["openai/gpt"]
    restored.close()


def test_telemetry_survives_a_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "registry.db"
    first = TelemetryStore(str(database), async_writes=False)
    first.record_run(
        WorkflowRun(
            trace_id="trace-1",
            workflow_id="wf-1",
            request="Add a feature",
            status="running",
            started_at=datetime.now(UTC),
        )
    )
    first.finish_run(
        "wf-1",
        status=WorkflowStatus.completed.value,
        finished_at=datetime.now(UTC),
        error_code=None,
        result={"ok": True},
    )
    first.close()

    second = TelemetryStore(str(database), async_writes=False)
    restored = second.get_run("wf-1")
    second.close()

    assert restored["status"] == "completed"
    assert restored["result"] == {"ok": True}
