"""Small dependency-free HTTP API for registry, credentials, and execution."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from total_orch.config import Settings
from total_orch.domain import ProviderConnection
from total_orch.gateway import GatewayAdapter, GatewayError, GatewayRequest, build_gateway
from total_orch.gateway.models import ChatMessage
from total_orch.registry import (
    IdentityRules,
    ModelRegistryService,
    ModelRegistryStore,
    ModelsEndpointCatalogSource,
)


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
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.gateway = gateway

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        request_headers = {key.lower(): value for key, value in (headers or {}).items()}
        if method == "GET" and path == "/health":
            return ApiResponse(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "registry": self._counts(),
                    "auth_required": self.settings.total_orch_api_key is not None,
                },
            )
        if not self._authorized(request_headers):
            return ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": {"message": "authentication required"}})

        try:
            if method == "GET" and path in {"/v1/registry/counts", "/v1/registry/models", "/v1/models"}:
                return self._registry_response(path, parse_qs(parsed.query))
            if method == "POST" and path == "/v1/registry/sync":
                return self._sync_registry()
            if method == "GET" and path == "/v1/providers/connections":
                return self._connections_response(parse_qs(parsed.query))
            if method == "POST" and path == "/v1/providers/connections":
                return self._create_connection(body)
            if method == "POST" and path.startswith("/v1/providers/connections/") and path.endswith("/health"):
                connection_id = unquote(path.removesuffix("/health").rsplit("/", 1)[-1])
                return self._check_connection(connection_id)
            if method == "DELETE" and path.startswith("/v1/providers/connections/"):
                return self._delete_connection(unquote(path.rsplit("/", 1)[-1]))
            if method == "POST" and path == "/v1/chat/completions":
                return self._completion(body)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
        return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        expected = self.settings.total_orch_api_key
        if expected is None:
            return self.settings.environment.lower() in {"development", "test"}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token, expected)

    def _registry_response(self, path: str, query: Mapping[str, list[str]]) -> ApiResponse:
        if path.endswith("/counts"):
            return ApiResponse(HTTPStatus.OK, self._counts())
        provider = query.get("provider", [None])[0]
        families = {family.id: family for family in self.registry.store.list_families()}
        variants = self.registry.store.list_variants()
        endpoints = self.registry.store.list_endpoints(provider=provider)
        return ApiResponse(
            HTTPStatus.OK,
            {
                "object": "list",
                "data": [
                    {
                        "id": variant.id,
                        "object": "model",
                        "owned_by": self._family_publisher(families, variant.family_id),
                        "context_window": variant.context_window,
                        "model_kind": variant.model_kind.value if variant.model_kind is not None else None,
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
                    }
                    for result in summary.sources
                ],
                "counts": self._counts(),
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
            healthy = asyncio.run(self.gateway.health_check(connection.provider))
        except GatewayError as exc:
            return ApiResponse(exc.status_code or HTTPStatus.BAD_GATEWAY, {"error": exc.as_dict()})
        now = datetime.now(UTC)
        updated = connection.model_copy(
            update={
                "healthy": healthy,
                "last_health_check_at": now,
                "last_success_at": now if healthy else connection.last_success_at,
            }
        )
        self.registry.store.upsert_connection(updated)
        return ApiResponse(HTTPStatus.OK, {"data": self._public_connection(updated)})

    def _completion(self, body: bytes) -> ApiResponse:
        payload = self._json_object(body)
        if payload.get("stream") is True:
            return ApiResponse(HTTPStatus.NOT_IMPLEMENTED, {"error": {"message": "streaming is not supported by this HTTP facade yet"}})
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
        request = GatewayRequest(
            provider=str(payload.get("provider", "auto")),
            model=model,
            messages=parsed_messages,
            temperature=payload.get("temperature"),
            reasoning_effort=payload.get("reasoning_effort"),
        )
        try:
            completion = asyncio.run(self.gateway.complete(request))
        except GatewayError as exc:
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
        response = self.application.handle(
            method,
            self.path,
            headers={str(key): str(value) for key, value in self.headers.items()},
            body=self.rfile.read(length),
        )
        payload = b"" if response.status_code == HTTPStatus.NO_CONTENT else json.dumps(
            response.body, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(response.status_code)
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
    store = ModelRegistryStore(settings.registry_database_path)
    store.initialize()
    rules = IdentityRules()
    gateway = build_gateway(settings)
    registry = ModelRegistryService(store, rules, sources=(ModelsEndpointCatalogSource(gateway, rules),))
    return TotalOrchApplication(settings, registry, gateway)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TotalOrch HTTP API")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = Settings.from_env()
    application = create_application(settings)
    application.serve(args.host or settings.total_orch_host, args.port or settings.total_orch_port)
