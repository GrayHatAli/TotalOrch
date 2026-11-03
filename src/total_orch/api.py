"""Small dependency-free HTTP API for registry, credentials, and execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from total_orch.config import Settings
from total_orch.credentials import CredentialError, EnvironmentSecretResolver
from total_orch.domain import ModelCapabilities, ProviderConnection, RouteRequest
from total_orch.gateway import (
    GatewayAdapter,
    GatewayError,
    GatewayRequest,
    OpenAICompatAdapter,
    build_gateway,
)
from total_orch.gateway.models import ChatMessage
from total_orch.observability import configure_logging
from total_orch.profiles import (
    BUILTIN_AGENT_DEFINITIONS,
    PoolBuilder,
    default_agent_profiles,
)
from total_orch.registry import (
    EntityKind,
    IdentityRules,
    ModelRegistryService,
    ModelRegistryStore,
    ModelsEndpointCatalogSource,
)
from total_orch.router import (
    ExplainableRouter,
    RouteDecisionStore,
    policy_from_preferences,
)
from total_orch.telemetry import TelemetryStore
from total_orch.tools import ToolManager, ToolWorkflowAdapter
from total_orch.workflow import TaskType, WorkflowRequest, WorkflowRuntime
from total_orch.workflow.service import WorkflowService

logger = logging.getLogger("total_orch.api")


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status_code: int
    body: dict[str, Any]


class TotalOrchApplication:
    """HTTP-independent request dispatcher, easy to test without a socket."""

    def __init__(
        self,
        settings: Settings,
        registry: ModelRegistryService,
        gateway: GatewayAdapter,
        *,
        telemetry: TelemetryStore | None = None,
        workflow_service: WorkflowService | None = None,
        route_store: RouteDecisionStore | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.gateway = gateway
        self.telemetry = telemetry
        self._secret_resolver = EnvironmentSecretResolver()
        self._store_lock = RLock()
        self._rate_windows: dict[str, deque[float]] = {}
        self._route_store = route_store or self._default_route_store()
        self._tools = self._build_tools()
        self._workflow_service = workflow_service or self._default_workflow_service()
        self._startup_checks = self._run_startup_checks()

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        client: str | None = None,
    ) -> ApiResponse:
        with self._store_lock:
            return self._handle(method, target, headers=headers, body=body, client=client)

    def _handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        client: str | None = None,
    ) -> ApiResponse:
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        request_headers = {key.lower(): value for key, value in (headers or {}).items()}
        if len(body) > self.settings.request_max_body_bytes:
            return ApiResponse(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": {"message": "request body exceeds the configured limit"}},
            )
        if method == "GET" and path == "/health":
            checks = [{"name": name, "ok": ok, "detail": detail} for name, ok, detail in self._startup_checks]
            return ApiResponse(
                HTTPStatus.OK,
                {
                    "status": "ok" if all(check["ok"] for check in checks) else "degraded",
                    "registry": self._counts(),
                    "auth_required": self.settings.total_orch_api_key is not None,
                    "checks": checks,
                    "metrics": self.telemetry.metrics() if self.telemetry is not None else {},
                },
            )
        if not self._authorized(request_headers):
            return ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": {"message": "authentication required"}})
        if path.startswith("/v1/") and self._rate_limited(client):
            return ApiResponse(HTTPStatus.TOO_MANY_REQUESTS, {"error": {"message": "rate limit exceeded"}})

        try:
            if method == "GET" and path in {"/v1/registry/counts", "/v1/registry/models", "/v1/models"}:
                return self._registry_response(path, parse_qs(parsed.query))
            if method == "GET" and path == "/v1/models/families":
                return ApiResponse(
                    HTTPStatus.OK,
                    {"data": [family.model_dump(mode="json") for family in self.registry.list_families()]},
                )
            if method == "GET" and path == "/v1/models/pools":
                return self._pools_response()
            if method == "GET" and path == "/v1/agents/profiles":
                return self._profiles_response()
            if method == "GET" and path == "/v1/registry/provenance":
                return self._provenance_response(parse_qs(parsed.query))
            if method == "GET" and path == "/v1/registry/stale":
                return self._stale_response(parse_qs(parsed.query))
            if method == "GET" and path == "/v1/registry/overrides":
                return self._overrides_response(parse_qs(parsed.query))
            if method == "POST" and path == "/v1/registry/overrides":
                return self._set_override(body)
            if method == "DELETE" and path.startswith("/v1/registry/overrides/"):
                parts = path.removeprefix("/v1/registry/overrides/").split("/")
                if len(parts) != 3:
                    raise ValueError("override path must be /v1/registry/overrides/{kind}/{id}/{field}")
                kind, entity_id, field = (unquote(part) for part in parts)
                if not self.registry.delete_override(cast(EntityKind, kind), entity_id, field):
                    return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "override not found"}})
                return ApiResponse(HTTPStatus.NO_CONTENT, {})
            if method == "POST" and path == "/v1/registry/sync":
                return self._sync_registry()
            if method == "GET" and path == "/v1/registry/sync/status":
                return self._sync_status()
            if method == "GET" and path == "/v1/auth/keys":
                return ApiResponse(HTTPStatus.OK, {"data": self.registry.store.list_api_keys()})
            if method == "POST" and path == "/v1/auth/keys":
                return self._create_api_key(body)
            if method == "DELETE" and path.startswith("/v1/auth/keys/"):
                key_id = unquote(path.rsplit("/", 1)[-1])
                if not self.registry.store.revoke_api_key(key_id):
                    return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "API key not found"}})
                return ApiResponse(HTTPStatus.NO_CONTENT, {})
            if method == "GET" and path in {"/v1/providers", "/v1/providers/connections"}:
                return self._connections_response(parse_qs(parsed.query))
            if method == "GET" and path == "/v1/providers/export":
                return self._export_connections()
            if method == "POST" and path == "/v1/providers/import":
                return self._import_connections(body)
            if method == "POST" and path == "/v1/providers/connections":
                return self._create_connection(body)
            if method == "POST" and path.startswith("/v1/providers/connections/") and path.endswith("/health"):
                connection_id = unquote(path.removesuffix("/health").rsplit("/", 1)[-1])
                return self._check_connection(connection_id)
            if method == "POST" and path.startswith("/v1/providers/") and path.endswith("/test"):
                connection_id = unquote(path.removesuffix("/test").rsplit("/", 1)[-1])
                return self._check_connection(connection_id)
            if method == "DELETE" and path.startswith("/v1/providers/connections/"):
                return self._delete_connection(unquote(path.rsplit("/", 1)[-1]))
            if method == "POST" and path == "/v1/routing/preview":
                return self._route_preview(body)
            if method == "GET" and path.startswith("/v1/routing/decisions/"):
                return self._decision_response(unquote(path.rsplit("/", 1)[-1]))
            if method == "GET" and path == "/v1/routing/decisions":
                return self._decisions_response(parse_qs(parsed.query))
            if method == "GET" and path == "/v1/telemetry/report":
                return self._telemetry_response("report")
            if method == "GET" and path == "/v1/telemetry/metrics":
                return self._telemetry_response("metrics")
            if method == "GET" and path == "/v1/tasks":
                return self._list_tasks()
            if method == "POST" and path == "/v1/tasks":
                return self._submit_task(body)
            if path.startswith("/v1/tasks/"):
                return self._task_route(method, path, parsed)
            if method == "POST" and path == "/v1/chat/completions":
                return self._completion(body)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
        return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    # -- construction helpers ----------------------------------------------

    @staticmethod
    def _default_route_store() -> RouteDecisionStore:
        store = RouteDecisionStore(":memory:")
        store.initialize()
        return store

    def _build_tools(self) -> ToolWorkflowAdapter | None:
        root = self.settings.workflow_workspace_root
        if root is None:
            return None
        try:
            return ToolWorkflowAdapter(ToolManager(root))
        except (OSError, ValueError):
            return None

    def _default_workflow_service(self) -> WorkflowService:
        runtime = WorkflowRuntime(
            self.registry,
            self.gateway,
            settings=self.settings,
            route_store=self._route_store,
            event_sink=self.telemetry.record_event if self.telemetry is not None else None,
            tools=self._tools,
        )
        return WorkflowService(runtime, self.telemetry)

    def _run_startup_checks(self) -> list[tuple[str, bool, str]]:
        counts = self._counts()
        checks: list[tuple[str, bool, str]] = [
            ("registry", True, f"{counts['families']} families, {counts['variants']} variants"),
            ("gateway", True, self.settings.gateway_type),
        ]
        root = self.settings.workflow_workspace_root
        if root is None:
            checks.append(("workspace", True, "not configured; repository tools are disabled"))
        else:
            checks.append(("workspace", Path(root).is_dir(), root))
        missing_secrets = [
            connection.id
            for connection in self.registry.store.list_connections()
            if connection.secret_ref is not None
            and not self._secret_resolver.configured(connection.secret_ref)
        ]
        checks.append(
            (
                "secret_references",
                not missing_secrets,
                "all configured" if not missing_secrets else f"unresolved: {', '.join(missing_secrets)}",
            )
        )
        if self.telemetry is not None:
            checks.append(("telemetry", True, "sqlite ledger ready"))
        return checks

    def _rate_limited(self, client: str | None) -> bool:
        limit = self.settings.rate_limit_per_minute
        if limit <= 0 or client is None:
            return False
        now = time.monotonic()
        with self._store_lock:
            window = self._rate_windows.setdefault(client, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= limit:
                return True
            window.append(now)
            return False

    # -- registry and provider queries -------------------------------------

    def _pools_response(self) -> ApiResponse:
        builds = PoolBuilder(self.registry).build()
        return ApiResponse(
            HTTPStatus.OK,
            {
                "data": [
                    {
                        "pool": build.pool.model_dump(mode="json"),
                        "memberships": [
                            {
                                "endpoint_id": membership.endpoint_id,
                                "member": membership.member,
                                "reason": membership.reason,
                            }
                            for membership in build.memberships
                        ],
                    }
                    for build in builds
                ]
            },
        )

    def _profiles_response(self) -> ApiResponse:
        builds = PoolBuilder(self.registry).build()
        pools = {build.pool.id: build.pool for build in builds}
        profiles = default_agent_profiles(BUILTIN_AGENT_DEFINITIONS, pools)
        return ApiResponse(
            HTTPStatus.OK,
            {
                "definitions": [
                    definition.model_dump(mode="json") for definition in BUILTIN_AGENT_DEFINITIONS
                ],
                "profiles": [profile.model_dump(mode="json") for profile in profiles],
                "pools": [pool.model_dump(mode="json") for pool in pools.values()],
            },
        )

    def _provenance_response(self, query: Mapping[str, list[str]]) -> ApiResponse:
        kind = query.get("kind", [None])[0]
        entity_id = query.get("id", [None])[0]
        if kind not in {"family", "variant", "endpoint"} or not entity_id:
            raise ValueError("kind (family|variant|endpoint) and id are required")
        provenance = self.registry.provenance(cast(EntityKind, kind), entity_id)
        return ApiResponse(
            HTTPStatus.OK,
            {
                "kind": kind,
                "id": entity_id,
                "data": {field: source.model_dump(mode="json") for field, source in provenance.items()},
            },
        )

    def _stale_response(self, query: Mapping[str, list[str]]) -> ApiResponse:
        raw = query.get("max_age_seconds", [str(self.settings.registry_stale_after_seconds)])[0]
        max_age = float(raw)
        if max_age <= 0:
            raise ValueError("max_age_seconds must be positive")
        report = self.registry.stale(timedelta(seconds=max_age))
        return ApiResponse(
            HTTPStatus.OK,
            {
                "families": list(report.families),
                "variants": list(report.variants),
                "endpoints": list(report.endpoints),
                "empty": report.empty,
            },
        )

    def _overrides_response(self, query: Mapping[str, list[str]]) -> ApiResponse:
        kind = query.get("kind", [None])[0]
        if kind is not None and kind not in {"family", "variant", "endpoint"}:
            raise ValueError("kind must be family, variant, or endpoint")
        overrides = self.registry.list_overrides(
            kind=cast(EntityKind, kind) if kind is not None else None,
            entity_id=query.get("id", [None])[0],
        )
        return ApiResponse(HTTPStatus.OK, {"data": overrides})

    def _set_override(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        kind = str(payload.get("kind", ""))
        entity_id = str(payload.get("id", ""))
        field = str(payload.get("field", ""))
        if not kind or not entity_id or not field or "value" not in payload:
            raise ValueError("kind, id, field, and value are required")
        self.registry.set_override(cast(EntityKind, kind), entity_id, field, payload["value"])
        return ApiResponse(
            HTTPStatus.CREATED,
            {"data": {"kind": kind, "id": entity_id, "field": field, "value": payload["value"]}},
        )

    def _export_connections(self) -> ApiResponse:
        connections = self.registry.store.list_connections()
        return ApiResponse(
            HTTPStatus.OK,
            {
                "data": [self._public_connection(connection) for connection in connections],
                "credentials_included": False,
                "note": "Only secret references are exported; secret values never enter TotalOrch.",
            },
        )

    def _import_connections(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("data must be a non-empty list of provider connections")
        forbidden = {"secret", "api_key", "token", "password", "credential"}
        if any(forbidden.intersection(item) for item in data if isinstance(item, dict)):
            raise ValueError("raw credentials are not accepted; import secret references only")
        has_secret_refs = any(
            isinstance(item, dict) and item.get("secret_ref") for item in data
        )
        if has_secret_refs and payload.get("confirm") is not True:
            raise ValueError("credential-bearing import requires confirm=true")
        imported: list[dict[str, Any]] = []
        for item in data:
            connection = ProviderConnection.model_validate(item)
            self.registry.store.upsert_connection(connection)
            imported.append(self._public_connection(connection))
        return ApiResponse(HTTPStatus.CREATED, {"data": imported, "imported": len(imported)})

    # -- routing ------------------------------------------------------------

    def _route_preview(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        profile_id = str(payload.get("agent_profile_id", "developer-default"))
        builds = PoolBuilder(self.registry).build()
        pools = {build.pool.id: build.pool for build in builds}
        profiles = default_agent_profiles(BUILTIN_AGENT_DEFINITIONS, pools)
        profile = next((item for item in profiles if item.id == profile_id), None)
        if profile is None:
            raise KeyError(f"unknown agent profile: {profile_id}")
        definition = next(
            item for item in BUILTIN_AGENT_DEFINITIONS if item.id == profile.agent_definition_id
        )
        pool = pools.get(profile.pool_id)
        if pool is None:
            raise KeyError(f"pool for profile {profile_id} was not generated")
        endpoints = [
            endpoint
            for endpoint in self.registry.list_endpoints()
            if endpoint.id in set(pool.member_endpoint_ids)
        ]
        variants = {variant.id: variant for variant in self.registry.list_variants()}
        required = payload.get("required_capabilities")
        request = RouteRequest(
            agent_profile_id=profile.id,
            task_type=str(payload.get("task_type", "feature")),
            required_capabilities=(
                ModelCapabilities.model_validate(required)
                if isinstance(required, dict)
                else definition.required_capabilities
            ),
            minimum_context_window=payload.get("minimum_context_window", definition.minimum_context_window),
            candidate_pool_id=pool.id,
            preferred_family_id=payload.get("preferred_family_id"),
        )
        preview = ExplainableRouter().preview(
            request,
            endpoints=endpoints,
            variants_by_id=variants,
            policy=policy_from_preferences(profile.preferences),
        )
        return ApiResponse(
            HTTPStatus.OK,
            {
                "profile_id": profile.id,
                "pool_id": pool.id,
                "selected_endpoint_id": preview.selected_endpoint_id,
                "fallback_endpoint_ids": list(preview.fallback_endpoint_ids),
                "candidate_endpoint_ids": list(preview.candidate_endpoint_ids),
                "excluded_reasons": dict(preview.excluded_reasons),
                "score_factors": {key: dict(value) for key, value in preview.score_factors.items()},
                "total_scores": dict(preview.total_scores),
                "factor_weights": dict(preview.weights),
                "policy_version": preview.policy_version,
                "explanation": preview.explanation,
            },
        )

    def _decision_response(self, decision_id: str) -> ApiResponse:
        try:
            decision = self._route_store.get_decision(decision_id)
        except KeyError:
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "route decision not found"}})
        return ApiResponse(HTTPStatus.OK, {"data": decision.model_dump(mode="json")})

    def _decisions_response(self, query: Mapping[str, list[str]]) -> ApiResponse:
        explored_raw = query.get("explored", [None])[0]
        explored = None if explored_raw is None else explored_raw.lower() in {"1", "true", "yes"}
        limit_raw = query.get("limit", [None])[0]
        decisions = self._route_store.list_decisions(
            policy_version=query.get("policy_version", [None])[0],
            selected_endpoint_id=query.get("selected_endpoint_id", [None])[0],
            explored=explored,
            limit=int(limit_raw) if limit_raw is not None else None,
        )
        return ApiResponse(
            HTTPStatus.OK,
            {"data": [decision.model_dump(mode="json") for decision in decisions]},
        )

    def _telemetry_response(self, kind: str) -> ApiResponse:
        if self.telemetry is None:
            return ApiResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": {"message": "telemetry is not configured"}})
        if kind == "report":
            return ApiResponse(HTTPStatus.OK, self.telemetry.report())
        return ApiResponse(HTTPStatus.OK, self.telemetry.metrics())

    # -- tasks --------------------------------------------------------------

    def _submit_task(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        root = self.settings.workflow_workspace_root
        workspace = payload.get("workspace")
        if workspace is not None:
            if not isinstance(workspace, str) or not workspace:
                raise ValueError("workspace must be a non-empty path")
            if root is None:
                raise ValueError("workspace is not configurable; set ORCHESTRATOR_WORKSPACE_ROOT first")
            resolved = Path(workspace).resolve()
            if not resolved.is_relative_to(Path(root).resolve()):
                raise ValueError("workspace must be inside the configured workspace root")
            workspace = str(resolved)
        task_type_raw = payload.get("task_type")
        task_type = TaskType(str(task_type_raw)) if task_type_raw is not None else None
        request = WorkflowRequest(
            request=str(payload.get("request", "")),
            workspace=workspace,
            task_type=task_type,
            run_tests=bool(payload.get("run_tests", True)),
            max_corrections=payload.get("max_corrections"),
            metadata={str(key): str(value) for key, value in payload.get("metadata", {}).items()}
            if isinstance(payload.get("metadata"), dict)
            else {},
        )
        run = self._workflow_service.submit(request)
        logger.info(json.dumps({"event": "task.submitted", "workflow_id": run.workflow_id}))
        return ApiResponse(HTTPStatus.ACCEPTED, {"data": run.model_dump(mode="json")})

    def _list_tasks(self) -> ApiResponse:
        if self.telemetry is None:
            return ApiResponse(HTTPStatus.OK, {"data": []})
        return ApiResponse(HTTPStatus.OK, {"data": self.telemetry.list_runs()})

    def _task_route(self, method: str, path: str, parsed: Any) -> ApiResponse:
        parts = path.removeprefix("/v1/tasks/").split("/")
        workflow_id = unquote(parts[0])
        action = parts[1] if len(parts) > 1 else None
        if method == "GET" and action is None:
            return self._task_status(workflow_id)
        if method == "GET" and action == "events":
            return ApiResponse(HTTPStatus.OK, {"data": self._workflow_service.events(workflow_id)})
        if method == "POST" and action == "pause":
            return self._task_pause(workflow_id)
        if method == "POST" and action == "cancel":
            return self._task_cancel(workflow_id)
        if method == "POST" and action == "resume":
            return self._task_resume(workflow_id, replay=False)
        if method == "POST" and action == "replay":
            return self._task_resume(workflow_id, replay=True)
        return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def _task_status(self, workflow_id: str) -> ApiResponse:
        run = self._workflow_service.get(workflow_id)
        if run is None:
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "workflow not found"}})
        return ApiResponse(HTTPStatus.OK, {"data": run})

    def _task_pause(self, workflow_id: str) -> ApiResponse:
        run = self._workflow_service.get(workflow_id)
        if run is None:
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "workflow not found"}})
        if str(run.get("status")) in {"completed", "failed", "cancelled"}:
            return ApiResponse(HTTPStatus.CONFLICT, {"error": {"message": "workflow is already terminal"}})
        if not self._workflow_service.pause(workflow_id):
            return ApiResponse(HTTPStatus.CONFLICT, {"error": {"message": "workflow could not be paused"}})
        return ApiResponse(HTTPStatus.ACCEPTED, {"data": {"workflow_id": workflow_id, "status": "pause_requested"}})

    def _task_cancel(self, workflow_id: str) -> ApiResponse:
        run = self._workflow_service.get(workflow_id)
        if run is None:
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "workflow not found"}})
        if str(run.get("status")) in {"completed", "failed", "cancelled"}:
            return ApiResponse(HTTPStatus.CONFLICT, {"error": {"message": "workflow is already terminal"}})
        if not self._workflow_service.cancel(workflow_id):
            return ApiResponse(HTTPStatus.CONFLICT, {"error": {"message": "workflow could not be cancelled"}})
        return ApiResponse(HTTPStatus.OK, {"data": {"workflow_id": workflow_id, "status": "cancelled"}})

    def _task_resume(self, workflow_id: str, *, replay: bool) -> ApiResponse:
        try:
            outcome = (
                self._workflow_service.replay(workflow_id)
                if replay
                else self._workflow_service.resume(workflow_id)
            )
        except KeyError:
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "workflow not found"}})
        return ApiResponse(HTTPStatus.OK, {"data": outcome.result.model_dump(mode="json")})

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        expected = self.settings.total_orch_api_key
        if expected is None:
            return self.settings.environment.lower() in {"development", "test"}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        if hmac.compare_digest(token, expected):
            return True
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return self.registry.store.has_active_api_key(token_hash)

    def _registry_response(self, path: str, query: Mapping[str, list[str]]) -> ApiResponse:
        if path.endswith("/counts"):
            return ApiResponse(HTTPStatus.OK, self._counts())
        provider = query.get("provider", [None])[0]
        families = {family.id: family for family in self.registry.list_families()}
        variants = self.registry.list_variants()
        endpoints = self.registry.list_endpoints(provider=provider)
        return ApiResponse(
            HTTPStatus.OK,
            {
                "object": "list",
                "data": [
                    {
                        "id": variant.id,
                        "object": "model",
                        "display_name": variant.display_name,
                        "owned_by": self._family_publisher(families, variant.family_id),
                        "context_window": variant.context_window,
                        "model_kind": variant.model_kind.value if variant.model_kind is not None else None,
                        "lifecycle_state": variant.lifecycle_state,
                    }
                    for variant in variants
                ],
                "endpoints": [self._public_endpoint(endpoint) for endpoint in endpoints],
            },
        )

    def _sync_registry(self) -> ApiResponse:
        summary = asyncio.run(self.registry.sync())
        return ApiResponse(
            HTTPStatus.OK,
            {
                "succeeded": summary.succeeded,
                "sources": [
                    {
                        "source": result.source,
                        "status": result.status,
                        "families": result.families,
                        "variants": result.variants,
                        "endpoints": result.endpoints,
                        "error": result.error,
                        "error_kind": result.error_kind,
                    }
                    for result in summary.sources
                ],
                "counts": self._counts(),
            },
        )

    def _sync_status(self) -> ApiResponse:
        summary = self.registry.last_sync
        if summary is None:
            return ApiResponse(HTTPStatus.OK, {"data": None, "message": "no sync has run in this process"})
        return ApiResponse(
            HTTPStatus.OK,
            {
                "data": {
                    "succeeded": summary.succeeded,
                    "started_at": summary.started_at.isoformat(),
                    "finished_at": summary.finished_at.isoformat(),
                    "sources": [
                        {
                            "source": result.source,
                            "status": result.status,
                            "error": result.error,
                            "error_kind": result.error_kind,
                        }
                        for result in summary.sources
                    ],
                    "entity_errors": list(summary.entity_errors),
                }
            },
        )

    def _connections_response(self, query: Mapping[str, list[str]]) -> ApiResponse:
        provider = query.get("provider", [None])[0]
        return ApiResponse(
            HTTPStatus.OK,
            {"data": [self._public_connection(item) for item in self.registry.store.list_connections(provider=provider)]},
        )

    def _create_connection(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        forbidden = {"secret", "api_key", "token", "password", "credential"}
        if forbidden.intersection(payload):
            return ApiResponse(
                HTTPStatus.BAD_REQUEST,
                {"error": {"message": "raw credentials are not accepted; use secret_ref=env://NAME"}},
            )
        payload.setdefault("id", f"conn-{uuid4().hex[:12]}")
        connection = ProviderConnection.model_validate(payload)
        self.registry.store.upsert_connection(connection)
        return ApiResponse(HTTPStatus.CREATED, {"data": self._public_connection(connection)})

    def _delete_connection(self, connection_id: str) -> ApiResponse:
        if not self.registry.store.delete_connection(connection_id):
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "connection not found"}})
        return ApiResponse(HTTPStatus.NO_CONTENT, {})

    def _check_connection(self, connection_id: str) -> ApiResponse:
        try:
            connection = self.registry.store.get_connection(connection_id)
        except KeyError:
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "connection not found"}})
        if not connection.enabled:
            return ApiResponse(HTTPStatus.CONFLICT, {"error": {"message": "connection is disabled"}})
        try:
            healthy = asyncio.run(self._gateway_for_connection(connection).health_check(connection.provider))
        except (CredentialError, GatewayError) as exc:
            if isinstance(exc, CredentialError):
                return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
            return ApiResponse(exc.status_code or HTTPStatus.BAD_GATEWAY, {"error": exc.as_dict()})
        capabilities = connection.capability_observations
        try:
            models = asyncio.run(self.gateway.list_models())
        except GatewayError:
            pass
        else:
            observed = {
                key
                for model in models
                if model.provider == connection.provider
                or (model.provider is None and model.id.partition("/")[0] == connection.provider)
                for key, value in model.capabilities.items()
                if value
            }
            capabilities = tuple(sorted(observed))
        now = datetime.now(UTC)
        updated = connection.model_copy(
            update={
                "healthy": healthy,
                "last_health_check_at": now,
                "last_success_at": now if healthy else connection.last_success_at,
                "capability_observations": capabilities,
            }
        )
        self.registry.store.upsert_connection(updated)
        return ApiResponse(HTTPStatus.OK, {"data": self._public_connection(updated)})

    def _create_api_key(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body) if body else {}
        label = payload.get("label", "Harness")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("label must be a non-empty string")
        token = f"to_{secrets.token_urlsafe(32)}"
        key_id = f"key-{uuid4().hex[:12]}"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.registry.store.create_api_key(key_id, label.strip(), token_hash)
        return ApiResponse(
            HTTPStatus.CREATED,
            {
                "data": {
                    "id": key_id,
                    "label": label.strip(),
                    "token": token,
                    "warning": "Store this token now; it will not be returned again.",
                }
            },
        )

    def _completion(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        if payload.get("stream") is True:
            return ApiResponse(HTTPStatus.NOT_IMPLEMENTED, {"error": {"message": "streaming is not supported by this HTTP facade yet"}})
        _payload, request = self._completion_request(body)
        try:
            completion = asyncio.run(self._gateway_for_request(request).complete(request))
        except (CredentialError, GatewayError) as exc:
            if isinstance(exc, CredentialError):
                return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
            status = exc.status_code if exc.status_code is not None else HTTPStatus.BAD_GATEWAY
            return ApiResponse(status, {"error": exc.as_dict()})
        return ApiResponse(
            HTTPStatus.OK,
            {
                "id": completion.request_id or f"chatcmpl-{uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": completion.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": completion.output_text}, "finish_reason": completion.finish_reason}],
                "usage": dict(completion.usage),
            },
        )

    def _completion_request(self, body: bytes) -> tuple[dict[str, Any], GatewayRequest]:
        payload = self._json_object(body)
        model = payload.get("model")
        messages = payload.get("messages")
        if not isinstance(model, str) or not model:
            raise ValueError("model is required")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        parsed_messages = tuple(
            ChatMessage(role=str(item["role"]), content=str(item["content"]))
            for item in messages
            if isinstance(item, dict) and "role" in item and "content" in item
        )
        if len(parsed_messages) != len(messages):
            raise ValueError("each message must contain role and content")
        return payload, GatewayRequest(
            provider=str(payload.get("provider", "auto")),
            model=model,
            messages=parsed_messages,
            temperature=payload.get("temperature"),
            reasoning_effort=payload.get("reasoning_effort"),
        )

    async def _stream_events(self, request: GatewayRequest) -> AsyncIterator[bytes]:
        stream_id = f"chatcmpl-{uuid4().hex}"
        first = True
        try:
            async for chunk in self._gateway_for_request(request).stream(request):
                delta: dict[str, Any] = {"content": chunk.delta}
                if first:
                    delta["role"] = "assistant"
                    first = False
                event: dict[str, Any] = {
                    "id": chunk.request_id or stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": chunk.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": chunk.finish_reason,
                        }
                    ],
                }
                if chunk.usage:
                    event["usage"] = dict(chunk.usage)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        except (CredentialError, GatewayError) as exc:
            if isinstance(exc, CredentialError):
                error: dict[str, Any] = {"error": {"message": str(exc)}}
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n".encode()
                return
            error = {"error": exc.as_dict()}
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n".encode()
        finally:
            yield b"data: [DONE]\n\n"

    def _gateway_for_request(self, request: GatewayRequest) -> GatewayAdapter:
        connections = self.registry.store.list_connections(provider=request.provider)
        connection = next((item for item in connections if item.enabled and item.base_url), None)
        if connection is None:
            return self.gateway
        return self._gateway_for_connection(connection)

    def _gateway_for_connection(self, connection: ProviderConnection) -> GatewayAdapter:
        if connection.base_url is None:
            return self.gateway
        if connection.auth_method == "custom":
            raise CredentialError("custom provider authentication is not supported by the direct adapter")
        secret = self._secret_resolver.resolve(connection.secret_ref) if connection.secret_ref else None
        return OpenAICompatAdapter(
            base_url=connection.base_url,
            api_key=secret,
            timeout_seconds=self.settings.gateway_timeout_seconds,
            max_retries=self.settings.gateway_max_retries,
            router_name=connection.provider,
        )

    @staticmethod
    def _json_object(body: bytes) -> dict[str, Any]:
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("request body must be a JSON object")
        return value

    def _counts(self) -> dict[str, int]:
        store = self.registry.store
        return {
            "families": store.count_families(),
            "variants": store.count_variants(),
            "endpoints": store.count_endpoints(),
            "connections": len(store.list_connections()),
        }

    @staticmethod
    def _family_publisher(families: Mapping[str, Any], family_id: str) -> str:
        family = families.get(family_id)
        return family.publisher if family is not None and family.publisher is not None else "unknown"

    @staticmethod
    def _public_connection(connection: ProviderConnection) -> dict[str, Any]:
        data = connection.model_dump(mode="json")
        data.pop("secret_ref", None)
        data["secret_configured"] = connection.secret_ref is not None
        return data

    @staticmethod
    def _public_endpoint(endpoint: Any) -> dict[str, Any]:
        return {
            "id": endpoint.id,
            "provider": endpoint.provider,
            "provider_model_name": endpoint.provider_model_name,
            "model_variant_id": endpoint.model_variant_id,
            "health": endpoint.health,
        }

    def serve(self, host: str, port: int) -> None:
        handler = _RequestHandler
        handler.application = self
        with ThreadingHTTPServer((host, port), handler) as server:
            server.serve_forever()


class _RequestHandler(BaseHTTPRequestHandler):
    application: TotalOrchApplication

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > self.application.settings.request_max_body_bytes:
            self._write_response(
                ApiResponse(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": {"message": "request body exceeds the configured limit"}},
                )
            )
            return
        body = self.rfile.read(length)
        parsed = urlsplit(self.path)
        if (
            method == "GET"
            and parsed.path.startswith("/v1/tasks/")
            and parsed.path.endswith("/events")
            and parse_qs(parsed.query).get("stream", [""])[0].lower() in {"1", "true", "yes"}
        ):
            self._dispatch_task_events(parsed.path)
            return
        if method == "POST" and urlsplit(self.path).path == "/v1/chat/completions":
            try:
                stream_requested = TotalOrchApplication._json_object(body).get("stream") is True
            except (TypeError, ValueError, json.JSONDecodeError):
                stream_requested = False
            if stream_requested:
                self._dispatch_stream(body)
                return
        response = self.application.handle(
            method,
            self.path,
            headers={str(key): str(value) for key, value in self.headers.items()},
            body=body,
            client=self.client_address[0] if self.client_address else None,
        )
        self._write_response(response)

    def _dispatch_task_events(self, path: str) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        if not self.application._authorized(headers):
            self._write_response(ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": {"message": "authentication required"}}))
            return
        workflow_id = unquote(path.removeprefix("/v1/tasks/").removesuffix("/events").strip("/"))
        if self.application._workflow_service.get(workflow_id) is None:
            self._write_response(ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "workflow not found"}}))
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sent = 0
        for _ in range(120):
            events = self.application._workflow_service.events(workflow_id)
            for event in events[sent:]:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            sent = len(events)
            run = self.application._workflow_service.get(workflow_id)
            if run is None or str(run.get("status")) in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.25)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _dispatch_stream(self, body: bytes) -> None:
        headers = {str(key): str(value) for key, value in self.headers.items()}
        with self.application._store_lock:
            if not self.application._authorized({key.lower(): value for key, value in headers.items()}):
                self._write_response(ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": {"message": "authentication required"}}))
                return
            try:
                _payload, request = self.application._completion_request(body)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._write_response(ApiResponse(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}}))
                return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        async def write_events() -> None:
            async for event in self.application._stream_events(request):
                self.wfile.write(event)
                self.wfile.flush()

        asyncio.run(write_events())

    def _write_response(self, response: ApiResponse) -> None:
        payload = b"" if response.status_code == HTTPStatus.NO_CONTENT else json.dumps(
            response.body, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(response.status_code)
        self.send_header("X-Request-ID", f"req-{uuid4().hex}")
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_application(settings: Settings | None = None) -> TotalOrchApplication:
    settings = settings or Settings.from_env()
    if settings.environment.lower() not in {"development", "test"} and settings.total_orch_api_key is None:
        raise ValueError("TOTAL_ORCH_API_KEY is required outside development and test environments")
    if settings.workflow_workspace_root is not None and not Path(settings.workflow_workspace_root).is_dir():
        raise ValueError(f"ORCHESTRATOR_WORKSPACE_ROOT is not a directory: {settings.workflow_workspace_root}")
    store = ModelRegistryStore(settings.registry_database_path)
    store.initialize()
    rules = IdentityRules()
    gateway = build_gateway(settings)
    registry = ModelRegistryService(store, rules, sources=(ModelsEndpointCatalogSource(gateway, rules),))
    telemetry = TelemetryStore(settings.registry_database_path)
    route_store = RouteDecisionStore(settings.registry_database_path)
    route_store.initialize()
    tools = (
        ToolWorkflowAdapter(ToolManager(settings.workflow_workspace_root))
        if settings.workflow_workspace_root is not None
        else None
    )
    checkpointer = SqliteSaver(sqlite3.connect(settings.registry_database_path, check_same_thread=False))
    runtime = WorkflowRuntime(
        registry,
        gateway,
        settings=settings,
        checkpointer=checkpointer,
        route_store=route_store,
        event_sink=telemetry.record_event,
        tools=tools,
    )
    service = WorkflowService(runtime, telemetry)
    return TotalOrchApplication(
        settings,
        registry,
        gateway,
        telemetry=telemetry,
        workflow_service=service,
        route_store=route_store,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TotalOrch HTTP API")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    logger.info("starting total-orch", extra={"event": json.dumps(settings.redacted())})
    application = create_application(settings)
    application.serve(args.host or settings.total_orch_host, args.port or settings.total_orch_port)
