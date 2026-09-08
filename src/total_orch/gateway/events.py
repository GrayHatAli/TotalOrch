"""Translate normalized gateway outcomes into platform execution events."""

from __future__ import annotations

from total_orch.domain import ExecutionEvent, WorkflowStatus

from .errors import GatewayError
from .models import GatewayCompletion


def completion_event(
    completion: GatewayCompletion,
    *,
    workflow_id: str,
    task_id: str,
    provider_endpoint_id: str | None = None,
) -> ExecutionEvent:
    """Create a secret-free event from a normalized gateway completion."""

    return ExecutionEvent(
        workflow_id=workflow_id,
        task_id=task_id,
        event_type="gateway.completion",
        status=WorkflowStatus.completed,
        provider_endpoint_id=provider_endpoint_id,
        metadata={
            "request_id": completion.request_id,
            "provider": completion.provider,
            "model": completion.model,
            "finish_reason": completion.finish_reason,
            "usage": dict(completion.usage),
            "gateway_metadata": dict(completion.metadata),
        },
    )


def error_event(
    error: GatewayError,
    *,
    workflow_id: str,
    task_id: str,
    provider_endpoint_id: str | None = None,
) -> ExecutionEvent:
    """Create a normalized failure event without including raw gateway bodies."""

    return ExecutionEvent(
        workflow_id=workflow_id,
        task_id=task_id,
        event_type="gateway.error",
        status=WorkflowStatus.failed,
        provider_endpoint_id=provider_endpoint_id,
        metadata={
            "error_code": error.code,
            "retryable": error.retryable,
            "status_code": error.status_code,
            "provider": error.provider,
            "request_id": error.request_id,
        },
    )
