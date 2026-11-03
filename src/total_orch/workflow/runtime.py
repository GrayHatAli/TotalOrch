"""LangGraph workflow runtime (MVP-0601..0614).

The graph owns the first software-development workflow:

    classify -> plan -> develop -> test -> review -> (correct -> develop)* -> finalize

Every node is a separate LangGraph node, so the configured checkpointer saves
state after each one. Routing resolves the role's automatic pool through the
registry and the explainable router; model calls go through the gateway
boundary and are retried or failed over at node level. All expected failures
are converted into a terminal state instead of escaping the graph.
"""

from __future__ import annotations

import asyncio
import operator
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from total_orch.config import Settings
from total_orch.domain import (
    ExecutionEvent,
    ProviderEndpoint,
    RouteRequest,
    WorkflowResult,
    WorkflowStatus,
)
from total_orch.gateway import (
    GatewayAdapter,
    GatewayError,
    GatewayRequest,
    completion_event,
    error_event,
)
from total_orch.profiles import (
    BUILTIN_AGENT_DEFINITIONS,
    PoolBuilder,
    default_agent_profiles,
)
from total_orch.registry import ModelRegistryService
from total_orch.router import (
    ExplainableRouter,
    RouteDecisionStore,
    RouteOutcome,
    policy_from_preferences,
)

from .classification import classify_task
from .models import (
    Implementation,
    Plan,
    Review,
    TaskClassification,
    TaskType,
    TestResult,
    WorkflowOutcome,
    WorkflowRequest,
)
from .parsing import OutputParseError, parse_output
from .prompts import developer_messages, planner_messages, repair_messages, reviewer_messages

_TERMINAL = {WorkflowStatus.failed.value, WorkflowStatus.cancelled.value}


class WorkflowState(TypedDict, total=False):
    workflow_id: str
    request: str
    workspace: str | None
    replay_of: str | None
    run_tests: bool
    task_type: str
    classification: dict[str, Any]
    status: str
    error_code: str | None
    plan: dict[str, Any]
    implementation: dict[str, Any]
    review: dict[str, Any]
    tests: dict[str, Any]
    apply: dict[str, Any]
    corrections: int
    max_corrections: int
    routes: dict[str, dict[str, Any]]
    result: dict[str, Any]
    events: Annotated[list[dict[str, Any]], operator.add]
    artifacts: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]


class WorkflowError(RuntimeError):
    """A workflow-safe failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkflowCancelled(WorkflowError):
    def __init__(self, workflow_id: str) -> None:
        super().__init__("cancelled", f"workflow {workflow_id} was cancelled")


class WorkflowToolbox:
    """The tool surface a workflow node may use; Phase 7 supplies the implementation."""

    def context(self, workspace: str, query: str) -> str:
        return ""

    def run_tests(self, workspace: str) -> TestResult:
        return TestResult(ran=False, passed=None, output="no test tool is configured")


@dataclass(frozen=True, slots=True)
class _RoleRoute:
    outcome: RouteOutcome
    endpoints: Mapping[str, ProviderEndpoint]
    fallbacks: tuple[ProviderEndpoint, ...]

    @property
    def selected(self) -> ProviderEndpoint:
        endpoint_id = self.outcome.decision.selected_endpoint_id
        if endpoint_id is None:
            raise WorkflowError("no_route", self.outcome.explanation)
        return self.endpoints[endpoint_id]


EventSink = Callable[[ExecutionEvent], None]


class WorkflowRuntime:
    """Compile, run, resume, and replay the first workflow graph."""

    def __init__(
        self,
        registry: ModelRegistryService,
        gateway: GatewayAdapter,
        *,
        settings: Settings | None = None,
        checkpointer: Any = None,
        router: ExplainableRouter | None = None,
        route_store: RouteDecisionStore | None = None,
        event_sink: EventSink | None = None,
        tools: WorkflowToolbox | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.registry = registry
        self.gateway = gateway
        self.router = router or ExplainableRouter()
        self.route_store = route_store
        self.tools = tools or _NullTools()
        self._checkpointer = checkpointer or MemorySaver()
        self._event_sink = event_sink
        self._events: dict[str, list[ExecutionEvent]] = {}
        self._cancelled: set[str] = set()
        self._graph = self._build_graph()

    # -- public API ---------------------------------------------------------

    async def start(
        self,
        request: WorkflowRequest,
        *,
        workflow_id: str | None = None,
        replay_of: str | None = None,
    ) -> WorkflowOutcome:
        workflow_id = workflow_id or f"wf-{uuid4().hex}"
        workspace = request.workspace if request.workspace is not None else self.settings.workflow_workspace_root
        max_corrections = (
            request.max_corrections
            if request.max_corrections is not None
            else self.settings.workflow_max_corrections
        )
        self._events[workflow_id] = []
        initial: WorkflowState = {
            "workflow_id": workflow_id,
            "request": request.request,
            "workspace": workspace,
            "replay_of": replay_of,
            "run_tests": request.run_tests,
            "task_type": request.task_type.value if request.task_type is not None else "",
            "classification": {},
            "status": WorkflowStatus.running.value,
            "error_code": None,
            "plan": {},
            "implementation": {},
            "review": {},
            "tests": {},
            "apply": {},
            "corrections": 0,
            "max_corrections": max_corrections,
            "routes": {},
            "events": [],
            "artifacts": [],
            "errors": [],
        }
        config = self._config(workflow_id)
        try:
            await self._graph.ainvoke(initial, config)
        except Exception as exc:  # noqa: BLE001 - a runtime bug must become a failed run, not a hung API call
            message = f"{type(exc).__name__}: {exc}"
            await self._graph.aupdate_state(config, {"errors": [message]})
            await self._mark_terminal(workflow_id, WorkflowStatus.failed, "workflow_error")
            self._emit(
                workflow_id,
                "workflow.error",
                WorkflowStatus.failed,
                {"error": message},
            )
        state = await self._read_state(workflow_id)
        return self._outcome(workflow_id, state)

    async def resume(self, workflow_id: str) -> WorkflowOutcome:
        """Continue a paused or failed graph from its last checkpoint."""

        config = self._config(workflow_id)
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            raise KeyError(f"unknown workflow: {workflow_id}")
        if snapshot.values.get("status") in _TERMINAL:
            await self._graph.aupdate_state(
                config,
                {"status": WorkflowStatus.running.value, "error_code": None},
            )
        await self._graph.ainvoke(None, config)
        state = await self._read_state(workflow_id)
        return self._outcome(workflow_id, state)

    async def replay(self, workflow_id: str) -> WorkflowOutcome:
        """Re-run a failed workflow as a new run, keeping an audit link."""

        state = await self._read_state(workflow_id)
        if not state:
            raise KeyError(f"unknown workflow: {workflow_id}")
        request = WorkflowRequest(
            request=str(state["request"]),
            workspace=state.get("workspace"),
            task_type=_task_type(state.get("task_type")),
            run_tests=bool(state.get("tests", {}).get("ran", True)),
            max_corrections=int(state.get("max_corrections", self.settings.workflow_max_corrections)),
        )
        return await self.start(request, replay_of=workflow_id)

    async def cancel(self, workflow_id: str) -> bool:
        state = await self._read_state(workflow_id)
        if not state or state.get("status") in _TERMINAL:
            return False
        self._cancelled.add(workflow_id)
        await self._mark_terminal(workflow_id, WorkflowStatus.cancelled, "cancelled")
        return True

    async def get_state(self, workflow_id: str) -> dict[str, Any]:
        return await self._read_state(workflow_id)

    def events(self, workflow_id: str) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events.get(workflow_id, ()))

    # -- graph --------------------------------------------------------------

    def _build_graph(self) -> Any:
        builder = StateGraph(WorkflowState)
        builder.add_node("classify", self._classify_node)
        builder.add_node("plan", self._plan_node)
        builder.add_node("develop", self._develop_node)
        builder.add_node("test", self._test_node)
        builder.add_node("review", self._review_node)
        builder.add_node("correct", self._correct_node)
        builder.add_node("apply", self._apply_node)
        builder.add_node("finalize", self._finalize_node)

        builder.add_edge(START, "classify")
        builder.add_conditional_edges(
            "classify", self._continue("plan"), {"plan": "plan", "finalize": "finalize"}
        )
        builder.add_conditional_edges(
            "plan", self._continue("develop"), {"develop": "develop", "finalize": "finalize"}
        )
        builder.add_conditional_edges(
            "develop", self._after_develop, {"test": "test", "review": "review", "finalize": "finalize"}
        )
        builder.add_conditional_edges(
            "test", self._continue("review"), {"review": "review", "finalize": "finalize"}
        )
        builder.add_conditional_edges(
            "review",
            self._after_review,
            {"correct": "correct", "apply": "apply", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "correct",
            self._after_develop,
            {"test": "test", "review": "review", "finalize": "finalize"},
        )
        builder.add_edge("apply", "finalize")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=self._checkpointer)

    def _continue(self, next_node: str) -> Callable[[WorkflowState], str]:
        def route(state: WorkflowState) -> str:
            return "finalize" if state.get("status") in _TERMINAL else next_node

        return route

    def _after_develop(self, state: WorkflowState) -> str:
        if state.get("status") in _TERMINAL:
            return "finalize"
        return "test"

    def _after_review(self, state: WorkflowState) -> str:
        if state.get("status") in _TERMINAL:
            return "finalize"
        review = state.get("review", {})
        if review.get("approved") is True:
            return "apply" if self._should_apply(state) else "finalize"
        if int(state.get("corrections", 0)) >= int(state.get("max_corrections", 0)):
            return "finalize"
        return "correct"

    # -- nodes --------------------------------------------------------------

    def _should_apply(self, state: WorkflowState) -> bool:
        if not self.settings.workflow_apply_changes:
            return False
        if not state.get("workspace") or not state.get("implementation", {}).get("diff"):
            return False
        return callable(getattr(self.tools, "apply_diff", None))

    async def _classify_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            explicit = _task_type(state.get("task_type"))
            classification = classify_task(state["request"], explicit)
        except Exception as exc:  # noqa: BLE001 - classification must not crash the run
            return self._fail(state, WorkflowError("classification_failed", str(exc)))
        event = self._emit(
            state["workflow_id"],
            "workflow.classification",
            WorkflowStatus.running,
            {
                "task_type": classification.task_type.value,
                "reason": classification.reason,
                "confidence": classification.confidence,
            },
        )
        return {
            "task_type": classification.task_type.value,
            "classification": classification.model_dump(mode="json"),
            "events": [event],
        }

    async def _plan_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            self._ensure_not_cancelled(state["workflow_id"])
            classification = TaskClassification.model_validate(state["classification"])
            context = self._context(state, "plan")
            messages = planner_messages(state["request"], classification, context)
            plan, route = await self._complete_structured("planner", state, messages, Plan)
        except WorkflowError as exc:
            return self._fail(state, exc)
        event = self._emit(
            state["workflow_id"],
            "workflow.plan",
            WorkflowStatus.running,
            {"summary": plan.summary, "steps": len(plan.steps), "risks": len(plan.risks)},
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "routes": _with_route(state, "planner", route),
            "events": [event],
        }

    async def _develop_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            self._ensure_not_cancelled(state["workflow_id"])
            plan = Plan.model_validate(state["plan"])
            messages = developer_messages(
                state["request"],
                plan,
                context=self._context(state, "develop"),
            )
            implementation, route = await self._complete_structured(
                "developer", state, messages, Implementation
            )
        except WorkflowError as exc:
            return self._fail(state, exc)
        event = self._emit(
            state["workflow_id"],
            "workflow.implementation",
            WorkflowStatus.running,
            {"summary": implementation.summary, "changes": len(implementation.changes)},
        )
        artifacts = [
            {"kind": "change", "path": change.path, "description": change.description}
            for change in implementation.changes
        ]
        return {
            "implementation": implementation.model_dump(mode="json"),
            "routes": _with_route(state, "developer", route),
            "events": [event],
            "artifacts": artifacts,
        }

    async def _test_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            self._ensure_not_cancelled(state["workflow_id"])
            workspace = state.get("workspace")
            if not workspace:
                tests = TestResult(ran=False, passed=None, output="no workspace configured")
            elif not state.get("run_tests", True):
                tests = TestResult(ran=False, passed=None, output="test execution was not requested")
            elif isinstance(self.tools, _NullTools):
                tests = TestResult(ran=False, passed=None, output="no test tool is configured")
            else:
                tests = self.tools.run_tests(workspace)
        except WorkflowError as exc:
            return self._fail(state, exc)
        except Exception as exc:  # noqa: BLE001 - a broken tool is a failed validation, not a crash
            tests = TestResult(ran=True, passed=False, output=f"{type(exc).__name__}: {exc}")
        event = self._emit(
            state["workflow_id"],
            "workflow.tests",
            WorkflowStatus.running,
            {
                "ran": tests.ran,
                "passed": tests.passed,
                "command": tests.command,
                "exit_code": tests.exit_code,
            },
        )
        return {"tests": tests.model_dump(mode="json"), "events": [event]}

    async def _review_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            self._ensure_not_cancelled(state["workflow_id"])
            plan = Plan.model_validate(state["plan"])
            implementation = Implementation.model_validate(state["implementation"])
            tests = TestResult.model_validate(state.get("tests", {"ran": False}))
            messages = reviewer_messages(
                state["request"],
                plan,
                implementation,
                tests,
                diff=implementation.diff,
            )
            review, route = await self._complete_structured("reviewer", state, messages, Review)
        except WorkflowError as exc:
            return self._fail(state, exc)
        event = self._emit(
            state["workflow_id"],
            "workflow.review",
            WorkflowStatus.running,
            {"approved": review.approved, "issues": len(review.issues), "summary": review.summary},
        )
        return {
            "review": review.model_dump(mode="json"),
            "routes": _with_route(state, "reviewer", route),
            "events": [event],
        }

    async def _correct_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            self._ensure_not_cancelled(state["workflow_id"])
            plan = Plan.model_validate(state["plan"])
            review = Review.model_validate(state["review"])
            messages = developer_messages(
                state["request"],
                plan,
                context=self._context(state, "correct"),
                review="\n".join(review.issues) or review.summary,
            )
            implementation, route = await self._complete_structured(
                "developer", state, messages, Implementation
            )
        except WorkflowError as exc:
            return self._fail(state, exc)
        corrections = int(state.get("corrections", 0)) + 1
        event = self._emit(
            state["workflow_id"],
            "workflow.correction",
            WorkflowStatus.running,
            {"corrections": corrections, "summary": implementation.summary},
        )
        artifacts = [
            {"kind": "change", "path": change.path, "description": change.description}
            for change in implementation.changes
        ]
        return {
            "implementation": implementation.model_dump(mode="json"),
            "corrections": corrections,
            "routes": _with_route(state, "developer", route),
            "events": [event],
            "artifacts": artifacts,
        }

    async def _apply_node(self, state: WorkflowState) -> dict[str, Any]:
        try:
            self._ensure_not_cancelled(state["workflow_id"])
            workspace = state.get("workspace")
            if not workspace:
                raise WorkflowError("no_workspace", "patch application needs a workspace")
            implementation = Implementation.model_validate(state["implementation"])
            apply_diff = getattr(self.tools, "apply_diff", None)
            if apply_diff is None:
                raise WorkflowError("no_tool", "the configured tools cannot apply patches")
            result = apply_diff(workspace, implementation.diff)
            if not result.ok:
                raise WorkflowError("patch_failed", str(result.output))
        except WorkflowError as exc:
            return self._fail(state, exc)
        event = self._emit(
            state["workflow_id"],
            "workflow.patch_applied",
            WorkflowStatus.running,
            {"changes": len(implementation.changes)},
        )
        return {"apply": {"ok": True, "output": result.output}, "events": [event]}

    async def _finalize_node(self, state: WorkflowState) -> dict[str, Any]:
        status = _terminal_status(state)
        error_code = state.get("error_code")
        if status is WorkflowStatus.completed:
            error_code = None
        elif error_code is None:
            error_code = "review_rejected"
        result = WorkflowResult(
            workflow_id=state["workflow_id"],
            status=status,
            output={
                "task_type": state.get("task_type"),
                "classification": state.get("classification", {}),
                "plan": state.get("plan", {}),
                "implementation": state.get("implementation", {}),
                "review": state.get("review", {}),
                "tests": state.get("tests", {}),
                "apply": state.get("apply", {}),
                "corrections": state.get("corrections", 0),
                "routes": state.get("routes", {}),
                "artifacts": state.get("artifacts", []),
                "errors": state.get("errors", []),
                "replay_of": state.get("replay_of"),
            },
            error_code=error_code,
            completed_at=datetime.now(UTC),
        )
        event = self._emit(
            state["workflow_id"],
            "workflow.completed" if status is WorkflowStatus.completed else "workflow.failed",
            status,
            {"error_code": error_code},
        )
        return {
            "status": status.value,
            "error_code": error_code,
            "result": result.model_dump(mode="json"),
            "events": [event],
        }

    # -- routing and model calls -------------------------------------------

    def _route_for(self, role: str, state: WorkflowState) -> _RoleRoute:
        definition = next(
            (item for item in BUILTIN_AGENT_DEFINITIONS if item.id == role),
            None,
        )
        if definition is None:
            raise WorkflowError("unknown_role", f"no built-in agent definition for {role!r}")
        builds = PoolBuilder(self.registry).build()
        pools = {build.pool.id: build.pool for build in builds}
        profiles = {
            profile.agent_definition_id: profile
            for profile in default_agent_profiles(BUILTIN_AGENT_DEFINITIONS, pools)
        }
        profile = profiles.get(role)
        if profile is None:
            raise WorkflowError("no_profile", f"no default profile for role {role!r}")
        pool = pools.get(profile.pool_id)
        if pool is None:
            raise WorkflowError("no_pool", f"default pool {profile.pool_id!r} was not generated")

        all_endpoints = {endpoint.id: endpoint for endpoint in self.registry.list_endpoints()}
        endpoints = [all_endpoints[eid] for eid in pool.member_endpoint_ids if eid in all_endpoints]
        if not endpoints:
            raise WorkflowError(
                "no_candidate",
                f"pool {pool.id!r} is empty; connect a provider and refresh the registry first",
            )
        variants = {variant.id: variant for variant in self.registry.list_variants()}
        request = RouteRequest(
            agent_profile_id=profile.id,
            task_type=state.get("task_type", "feature"),
            required_capabilities=definition.required_capabilities,
            minimum_context_window=definition.minimum_context_window,
            candidate_pool_id=pool.id,
        )
        outcome = self.router.route(
            request,
            endpoints=endpoints,
            variants_by_id=variants,
            policy=policy_from_preferences(profile.preferences),
        )
        if self.route_store is not None:
            self.route_store.save_decision(outcome.decision)
        endpoints_by_id = {endpoint.id: endpoint for endpoint in endpoints}
        fallbacks = tuple(
            endpoints_by_id[eid]
            for eid in outcome.decision.fallback_endpoint_ids
            if eid in endpoints_by_id
        )
        return _RoleRoute(outcome=outcome, endpoints=endpoints_by_id, fallbacks=fallbacks)

    async def _complete_structured(
        self,
        role: str,
        state: WorkflowState,
        messages: tuple[Any, ...],
        schema: type[Any],
    ) -> tuple[Any, _RoleRoute]:
        route = self._route_for(role, state)
        completion = await self._complete_with_fallback(state, route, messages)
        try:
            return parse_output(completion.output_text, schema), route
        except OutputParseError as first_error:
            repair = repair_messages(messages, str(first_error), schema.__name__)
            completion = await self._complete_with_fallback(state, route, repair)
            try:
                return parse_output(completion.output_text, schema), route
            except OutputParseError as second_error:
                raise WorkflowError(
                    "invalid_output",
                    f"{role} returned invalid {schema.__name__}: {second_error}",
                ) from second_error

    async def _complete_with_fallback(
        self,
        state: WorkflowState,
        route: _RoleRoute,
        messages: tuple[Any, ...],
    ) -> Any:
        workflow_id = state["workflow_id"]
        timeout = self.settings.workflow_node_timeout_seconds
        attempts = max(1, self.settings.workflow_max_attempts)
        candidates = (route.selected, *route.fallbacks)
        last_error: str | None = None
        for attempt in range(attempts):
            retryable = False
            for endpoint in candidates:
                self._ensure_not_cancelled(workflow_id)
                request = GatewayRequest(
                    provider=endpoint.provider,
                    model=endpoint.provider_model_name,
                    messages=messages,
                    temperature=0.0,
                )
                started = time.perf_counter()
                try:
                    completion = await asyncio.wait_for(self.gateway.complete(request), timeout)
                except TimeoutError:
                    retryable = True
                    last_error = f"timeout after {timeout}s on {endpoint.id}"
                    self._emit(
                        workflow_id,
                        "gateway.timeout",
                        WorkflowStatus.running,
                        {"endpoint_id": endpoint.id, "attempt": attempt + 1},
                        endpoint_id=endpoint.id,
                    )
                    continue
                except GatewayError as exc:
                    retryable = retryable or exc.retryable
                    last_error = f"{exc.code}: {exc}"
                    self._sink(
                        error_event(
                            exc,
                            workflow_id=workflow_id,
                            task_id=workflow_id,
                            provider_endpoint_id=endpoint.id,
                        ),
                        route,
                        endpoint.id,
                        attempt,
                        time.perf_counter() - started,
                    )
                    continue
                self._sink(
                    completion_event(
                        completion,
                        workflow_id=workflow_id,
                        task_id=workflow_id,
                        provider_endpoint_id=endpoint.id,
                    ),
                    route,
                    endpoint.id,
                    attempt,
                    time.perf_counter() - started,
                    completion=completion,
                    endpoint=endpoint,
                )
                self._ensure_not_cancelled(workflow_id)
                return completion
            if not retryable:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(min(0.05 * (2**attempt), 0.5))
        raise WorkflowError(
            "gateway_exhausted",
            f"all candidate endpoints failed for {workflow_id}: {last_error or 'no attempts'}",
        )

    # -- helpers ------------------------------------------------------------

    def _config(self, workflow_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": workflow_id}}

    async def _read_state(self, workflow_id: str) -> dict[str, Any]:
        snapshot = await self._graph.aget_state(self._config(workflow_id))
        return dict(snapshot.values) if snapshot.values else {}

    def _outcome(self, workflow_id: str, state: dict[str, Any]) -> WorkflowOutcome:
        raw = state.get("result")
        if raw:
            result = WorkflowResult.model_validate(raw)
        else:
            status = WorkflowStatus(state.get("status", WorkflowStatus.failed.value))
            result = WorkflowResult(
                workflow_id=workflow_id,
                status=status,
                output={"errors": state.get("errors", [])},
                error_code=state.get("error_code") or "workflow_incomplete",
                completed_at=datetime.now(UTC),
            )
        return WorkflowOutcome(
            workflow_id=workflow_id,
            result=result,
            state=state,
            events=tuple(self._events.get(workflow_id, ())),
        )

    async def _mark_terminal(self, workflow_id: str, status: WorkflowStatus, error_code: str | None) -> None:
        try:
            await self._graph.aupdate_state(
                self._config(workflow_id),
                {"status": status.value, "error_code": error_code},
            )
        except Exception:  # noqa: BLE001 - terminal bookkeeping is best effort
            return

    def _emit(
        self,
        workflow_id: str,
        event_type: str,
        status: WorkflowStatus,
        metadata: Mapping[str, Any],
        *,
        endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        event = ExecutionEvent(
            workflow_id=workflow_id,
            task_id=workflow_id,
            event_type=event_type,
            status=status,
            provider_endpoint_id=endpoint_id,
            metadata={"trace_id": workflow_id, **metadata},
        )
        self._events.setdefault(workflow_id, []).append(event)
        if self._event_sink is not None:
            self._event_sink(event)
        return event.model_dump(mode="json")

    def _sink(
        self,
        event: ExecutionEvent,
        route: _RoleRoute,
        endpoint_id: str,
        attempt: int,
        latency_seconds: float,
        *,
        completion: Any = None,
        endpoint: ProviderEndpoint | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        metadata = dict(event.metadata)
        metadata.update(
            {
                "trace_id": event.workflow_id,
                "route_decision_id": str(route.outcome.decision.id),
                "attempt": attempt + 1,
                "fallback": endpoint_id != route.outcome.decision.selected_endpoint_id,
                "latency_ms": round(latency_seconds * 1000, 3),
            }
        )
        if completion is not None and endpoint is not None:
            cost = _estimated_cost(completion.usage, endpoint)
            if cost is not None:
                metadata["estimated_cost_usd"] = cost
        self._event_sink(event.model_copy(update={"metadata": metadata}))

    def _ensure_not_cancelled(self, workflow_id: str) -> None:
        if workflow_id in self._cancelled:
            raise WorkflowCancelled(workflow_id)

    def _context(self, state: WorkflowState, role: str) -> str:
        workspace = state.get("workspace")
        if not workspace:
            return ""
        try:
            context = self.tools.context(workspace, state["request"])
        except Exception as exc:  # noqa: BLE001 - missing context is better than a failed node
            return f"context unavailable: {type(exc).__name__}: {exc}"
        self._emit(
            state["workflow_id"],
            "workflow.context",
            WorkflowStatus.running,
            {"role": role, "chars": len(context)},
        )
        return context

    def _fail(self, state: WorkflowState, error: WorkflowError) -> dict[str, Any]:
        status = (
            WorkflowStatus.cancelled
            if isinstance(error, WorkflowCancelled)
            else WorkflowStatus.failed
        )
        event = self._emit(
            state["workflow_id"],
            "workflow.error",
            status,
            {"error_code": error.code, "message": str(error)},
        )
        return {
            "status": status.value,
            "error_code": error.code,
            "errors": [str(error)],
            "events": [event],
        }


class _NullTools(WorkflowToolbox):
    """Explicit sentinel used to keep tests and unwired deployments test-free."""


def _task_type(value: object) -> TaskType | None:
    if value in (None, ""):
        return None
    return TaskType(str(value))


def _with_route(
    state: WorkflowState,
    role: str,
    route: _RoleRoute,
) -> dict[str, dict[str, Any]]:
    routes = dict(state.get("routes", {}))
    decision = route.outcome.decision
    routes[role] = {
        "decision_id": str(decision.id),
        "selected_endpoint_id": decision.selected_endpoint_id,
        "fallback_endpoint_ids": list(decision.fallback_endpoint_ids),
        "policy_version": decision.policy_version,
        "explanation": route.outcome.explanation,
    }
    return routes


def _terminal_status(state: WorkflowState) -> WorkflowStatus:
    current = state.get("status", WorkflowStatus.running.value)
    if current == WorkflowStatus.cancelled.value:
        return WorkflowStatus.cancelled
    if current == WorkflowStatus.failed.value:
        return WorkflowStatus.failed
    if state.get("review", {}).get("approved") is True:
        return WorkflowStatus.completed
    return WorkflowStatus.failed


def _estimated_cost(usage: Mapping[str, Any], endpoint: ProviderEndpoint) -> float | None:
    if endpoint.input_cost_per_million is None and endpoint.output_cost_per_million is None:
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output = usage.get("completion_tokens", usage.get("output_tokens", 0))
    if not isinstance(prompt, (int, float)) or not isinstance(output, (int, float)):
        return None
    cost = 0.0
    if endpoint.input_cost_per_million is not None:
        cost += float(prompt) / 1_000_000 * endpoint.input_cost_per_million
    if endpoint.output_cost_per_million is not None:
        cost += float(output) / 1_000_000 * endpoint.output_cost_per_million
    return round(cost, 8)
