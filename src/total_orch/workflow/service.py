"""Background workflow submissions and run lookup (MVP-0901, 0906, 0907)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import Lock, Thread
from uuid import uuid4

from total_orch.telemetry import TelemetryStore, WorkflowRun

from .models import WorkflowOutcome, WorkflowRequest
from .runtime import WorkflowRuntime


class WorkflowService:
    """Run workflows off the request thread and persist their lifecycle."""

    def __init__(self, runtime: WorkflowRuntime, telemetry: TelemetryStore | None = None) -> None:
        self.runtime = runtime
        self.telemetry = telemetry
        self._threads: dict[str, Thread] = {}
        self._lock = Lock()

    def submit(self, request: WorkflowRequest) -> WorkflowRun:
        workflow_id = f"wf-{uuid4().hex}"
        run = WorkflowRun(
            trace_id=workflow_id,
            workflow_id=workflow_id,
            request=request.request,
            task_type=request.task_type.value if request.task_type is not None else None,
            workspace=request.workspace,
            status="running",
        )
        if self.telemetry is not None:
            self.telemetry.record_run(run)
        thread = Thread(
            target=self._execute,
            args=(workflow_id, request),
            name=f"workflow-{workflow_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[workflow_id] = thread
        thread.start()
        return run

    def wait(self, workflow_id: str, *, timeout: float | None = None) -> dict[str, object] | None:
        with self._lock:
            thread = self._threads.get(workflow_id)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.get(workflow_id)

    def get(self, workflow_id: str) -> dict[str, object] | None:
        if self.telemetry is not None:
            try:
                return self.telemetry.get_run(workflow_id)
            except KeyError:
                return None
        state = asyncio.run(self.runtime.get_state(workflow_id))
        if not state:
            return None
        return {
            "workflow_id": workflow_id,
            "trace_id": workflow_id,
            "status": state.get("status"),
            "error_code": state.get("error_code"),
            "result": state.get("result"),
            "state": state,
        }

    def events(self, workflow_id: str) -> list[dict[str, object]]:
        if self.telemetry is not None:
            return self.telemetry.events_for(workflow_id)
        return [event.model_dump(mode="json") for event in self.runtime.events(workflow_id)]

    def cancel(self, workflow_id: str) -> bool:
        return asyncio.run(self.runtime.cancel(workflow_id))

    def resume(self, workflow_id: str) -> WorkflowOutcome:
        return asyncio.run(self.runtime.resume(workflow_id))

    def replay(self, workflow_id: str) -> WorkflowOutcome:
        return asyncio.run(self.runtime.replay(workflow_id))

    def _execute(self, workflow_id: str, request: WorkflowRequest) -> None:
        outcome: WorkflowOutcome | None = None
        error: Exception | None = None
        try:
            outcome = asyncio.run(self.runtime.start(request, workflow_id=workflow_id))
        except Exception as exc:  # noqa: BLE001 - a failed submission must be recorded, not lost
            error = exc
        if self.telemetry is None:
            return
        if outcome is not None:
            self.telemetry.finish_run(
                workflow_id,
                status=outcome.result.status.value,
                finished_at=outcome.result.completed_at or datetime.now(UTC),
                error_code=outcome.result.error_code,
                result=outcome.result.model_dump(mode="json"),
            )
        else:
            self.telemetry.finish_run(
                workflow_id,
                status="failed",
                finished_at=datetime.now(UTC),
                error_code="workflow_error",
                result={"error": f"{type(error).__name__}: {error}"},
            )


__all__ = ["WorkflowService"]
