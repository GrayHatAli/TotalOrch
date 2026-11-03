"""LangGraph workflow runtime tests (MVP-0601..0614)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from total_orch.config import Settings
from total_orch.domain import (
    ExecutionEvent,
    ModelCapabilities,
    ModelFamily,
    ModelVariant,
    ProviderConnectionRef,
    ProviderEndpoint,
)
from total_orch.gateway import (
    GatewayAdapter,
    GatewayCompletion,
    GatewayError,
    GatewayModel,
    GatewayRequest,
    GatewayStreamChunk,
)
from total_orch.registry import IdentityRules, ModelRegistryService, ModelRegistryStore
from total_orch.router import RouteDecisionStore
from total_orch.workflow import (
    TaskType,
    WorkflowRequest,
    WorkflowRuntime,
    WorkflowStatus,
    WorkflowToolbox,
)
from total_orch.workflow import (
    TestResult as WorkflowTestResult,
)

VARIANT = "openai/gpt:gpt-test"
FAMILY = "openai/gpt"

PLAN = json.dumps(
    {
        "summary": "Add a health endpoint",
        "steps": [{"id": "1", "description": "implement the endpoint"}],
        "risks": ["none"],
    }
)
IMPLEMENTATION = json.dumps(
    {
        "summary": "Added /health",
        "changes": [{"path": "src/app.py", "description": "add handler"}],
        "diff": "+def health(): return 'ok'",
    }
)
APPROVED = json.dumps({"approved": True, "summary": "looks correct", "issues": []})
REJECTED = json.dumps(
    {"approved": False, "summary": "missing test", "issues": ["add a test for /health"]}
)


class ScriptedGateway(GatewayAdapter):
    """Return role-specific JSON and record every request."""

    def __init__(self, scripts: dict[str, list[str]]) -> None:
        self.scripts = {role: list(responses) for role, responses in scripts.items()}
        self.calls: list[GatewayRequest] = []
        self.delay = 0.0

    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        return ()

    async def list_models(self) -> Sequence[GatewayModel]:
        return ()

    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        return ()

    async def validate_connection(self, connection_id: str) -> bool:
        return False

    async def health_check(self, provider: str, model: str | None = None) -> bool:
        return True

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        role = _role(request)
        if role == "developer" and "<review-feedback>" in request.messages[-1].content:
            role = "developer-revision"
        responses = self.scripts.get(role) or ['{"summary": "x"}']
        text = responses.pop(0) if len(responses) > 1 else responses[0]
        return GatewayCompletion(
            request_id=f"req-{len(self.calls)}",
            provider=request.provider,
            model=request.model,
            output_text=text,
            finish_reason="stop",
        )

    def stream(self, request: GatewayRequest) -> AsyncIterator[GatewayStreamChunk]:
        raise NotImplementedError


class FailingGateway(ScriptedGateway):
    """Fail the first endpoint, succeed on the fallback."""

    def __init__(self, scripts: dict[str, list[str]], failing_model: str) -> None:
        super().__init__(scripts)
        self.failing_model = failing_model

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        if request.provider == self.failing_model.partition("/")[0]:
            self.calls.append(request)
            raise GatewayError(
                "provider_unavailable",
                "simulated outage",
                retryable=False,
                status_code=503,
                provider=request.provider,
            )
        return await super().complete(request)


def _role(request: GatewayRequest) -> str:
    system = request.messages[0].content
    for role in ("planner", "developer", "reviewer"):
        if f"the {role} agent" in system:
            return role
    return "unknown"


def _endpoint(endpoint_id: str, *, health: float) -> ProviderEndpoint:
    provider, model_name = endpoint_id.split("/", 1)
    return ProviderEndpoint(
        id=endpoint_id,
        provider=provider,
        provider_model_name=model_name,
        model_variant_id=VARIANT,
        connection=ProviderConnectionRef(id=f"{provider}-account", provider=provider),
        health=health,
        latency_p95_ms=100.0,
        capabilities=ModelCapabilities(
            tool_calling=True,
            reasoning=True,
            streaming=True,
            structured_output=True,
        ),
    )


def _registry(*endpoints: ProviderEndpoint) -> ModelRegistryService:
    store = ModelRegistryStore(":memory:")
    store.initialize()
    store.upsert_family(ModelFamily(id=FAMILY, publisher="openai"), {}, "test")
    store.upsert_variant(
        ModelVariant(
            id=VARIANT,
            family_id=FAMILY,
            name="gpt-test",
            context_window=128_000,
            capabilities=ModelCapabilities(
                tool_calling=True,
                reasoning=True,
                streaming=True,
                structured_output=True,
            ),
        ),
        {},
        "test",
    )
    for endpoint in endpoints:
        store.upsert_endpoint(endpoint, {}, "test")
    return ModelRegistryService(store, IdentityRules())


def _scripts(**overrides: list[str]) -> dict[str, list[str]]:
    base = {
        "planner": [PLAN],
        "developer": [IMPLEMENTATION],
        "developer-revision": [IMPLEMENTATION],
        "reviewer": [APPROVED],
    }
    base.update(overrides)
    return base


class RecordingTools(WorkflowToolbox):
    def __init__(self) -> None:
        self.applied: list[str] = []

    def context(self, workspace: str, query: str) -> str:
        return ""

    def run_tests(self, workspace: str) -> WorkflowTestResult:
        return WorkflowTestResult(ran=False, passed=None)

    def apply_diff(self, workspace: str, diff: str) -> Any:
        self.applied.append(diff)

        class _Result:
            ok = True
            output = "applied"

        return _Result()


def _runtime(
    *,
    endpoints: tuple[ProviderEndpoint, ...] | None = None,
    gateway: GatewayAdapter | None = None,
    sink: list[ExecutionEvent] | None = None,
    empty: bool = False,
    settings: Settings | None = None,
    tools: WorkflowToolbox | None = None,
) -> WorkflowRuntime:
    route_store = RouteDecisionStore(":memory:")
    route_store.initialize()
    selected = () if empty else (endpoints or (_endpoint("openai/gpt-test", health=0.9),))
    return WorkflowRuntime(
        _registry(*selected),
        gateway or ScriptedGateway(_scripts()),
        settings=settings or Settings(environment="test", workflow_max_attempts=1),
        route_store=route_store,
        event_sink=sink.append if sink is not None else None,
        tools=tools,
    )


def test_workflow_runs_planner_developer_and_reviewer_and_records_route() -> None:
    sink: list[ExecutionEvent] = []
    runtime = _runtime(sink=sink)

    outcome = asyncio.run(
        WorkflowRuntime.start(
            runtime,
            WorkflowRequest(
                request="Add a health endpoint and tests",
                task_type=TaskType.feature,
                run_tests=False,
            ),
        )
    )

    assert outcome.result.status is WorkflowStatus.completed
    assert outcome.state["plan"]["summary"] == "Add a health endpoint"
    assert outcome.state["implementation"]["summary"] == "Added /health"
    assert outcome.state["review"]["approved"] is True
    assert outcome.state["routes"]["planner"]["selected_endpoint_id"] == "openai/gpt-test"
    event_types = {event.event_type for event in outcome.events}
    assert {
        "workflow.classification",
        "workflow.plan",
        "workflow.implementation",
        "workflow.tests",
        "workflow.review",
        "workflow.completed",
    } <= event_types
    assert any(event.event_type == "gateway.completion" for event in sink)
    assert outcome.result.output["artifacts"][0]["path"] == "src/app.py"


def test_malformed_structured_output_is_repaired_once() -> None:
    gateway = ScriptedGateway(_scripts(planner=["not json", PLAN]))
    runtime = _runtime(gateway=gateway)

    outcome = asyncio.run(
        WorkflowRuntime.start(runtime, WorkflowRequest(request="Add a feature", run_tests=False))
    )

    assert outcome.result.status is WorkflowStatus.completed
    planner_calls = [call for call in gateway.calls if _role(call) == "planner"]
    assert len(planner_calls) == 2
    assert "could not be parsed" in planner_calls[1].messages[-1].content


def test_review_rejection_runs_a_bounded_correction_loop() -> None:
    gateway = ScriptedGateway(_scripts(reviewer=[REJECTED, APPROVED]))
    runtime = _runtime(gateway=gateway)

    outcome = asyncio.run(
        WorkflowRuntime.start(
            runtime,
            WorkflowRequest(request="Fix the broken endpoint", run_tests=False, max_corrections=1),
        )
    )

    assert outcome.result.status is WorkflowStatus.completed
    assert outcome.state["corrections"] == 1
    assert outcome.state["review"]["approved"] is True
    assert len([call for call in gateway.calls if _role(call) == "developer"]) == 2


def test_gateway_failure_falls_back_to_the_next_endpoint() -> None:
    primary = _endpoint("provider-a/gpt-test", health=0.9)
    fallback = _endpoint("provider-b/gpt-test", health=0.5)
    gateway = FailingGateway(_scripts(), failing_model="provider-a/gpt-test")
    runtime = _runtime(endpoints=(primary, fallback), gateway=gateway)

    outcome = asyncio.run(
        WorkflowRuntime.start(runtime, WorkflowRequest(request="Add a feature", run_tests=False))
    )

    assert outcome.result.status is WorkflowStatus.completed
    assert [call.provider for call in gateway.calls[:2]] == ["provider-a", "provider-b"]


def test_cancelled_workflow_stops_at_the_next_node_boundary() -> None:
    gateway = ScriptedGateway(_scripts())
    gateway.delay = 0.05
    runtime = _runtime(gateway=gateway)

    async def run_and_cancel() -> Any:
        task = asyncio.create_task(
            WorkflowRuntime.start(
                runtime, WorkflowRequest(request="Add a feature", run_tests=False), workflow_id="wf-cancel"
            )
        )
        await asyncio.sleep(0.01)
        assert await runtime.cancel("wf-cancel") is True
        return await task

    outcome = asyncio.run(run_and_cancel())

    assert outcome.result.status is WorkflowStatus.cancelled
    assert outcome.result.error_code == "cancelled"


def test_runtime_rejects_an_empty_pool_without_calling_the_gateway() -> None:
    gateway = ScriptedGateway(_scripts())
    runtime = _runtime(gateway=gateway, empty=True)

    outcome = asyncio.run(
        WorkflowRuntime.start(
            runtime,
            WorkflowRequest(request="Add a feature", run_tests=False, workspace=None),
        )
    )

    assert outcome.result.status is WorkflowStatus.failed
    assert outcome.result.error_code == "no_candidate"
    assert gateway.calls == []


def test_apply_changes_is_opt_in_and_uses_an_approved_tool() -> None:
    tools = RecordingTools()
    runtime = _runtime(
        settings=Settings(
            environment="test",
            workflow_max_attempts=1,
            workflow_apply_changes=True,
        ),
        tools=tools,
    )

    outcome = asyncio.run(
        WorkflowRuntime.start(
            runtime,
            WorkflowRequest(
                request="Add a feature",
                workspace="/tmp/workspace",
                run_tests=False,
            ),
        )
    )

    assert outcome.result.status is WorkflowStatus.completed
    assert outcome.state["apply"]["ok"] is True
    assert tools.applied == ["+def health(): return 'ok'"]
    assert any(event.event_type == "workflow.patch_applied" for event in outcome.events)
