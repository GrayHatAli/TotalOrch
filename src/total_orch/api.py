"""Small dependency-free HTTP API for registry, credentials, and execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from total_orch.config import Settings
from total_orch.credentials import CredentialError, EnvironmentSecretResolver
from total_orch.domain import ProviderConnection
from total_orch.gateway import (
    GatewayAdapter,
    GatewayError,
    GatewayRequest,
    OpenAICompatAdapter,
    build_gateway,
)
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
        self._secret_resolver = EnvironmentSecretResolver()
        self._store_lock = RLock()

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        with self._store_lock:
            return self._handle(method, target, headers=headers, body=body)

    def _handle(
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
            if method == "GET" and path == "/v1/auth/keys":
                return ApiResponse(HTTPStatus.OK, {"data": self.registry.store.list_api_keys()})
            if method == "POST" and path == "/v1/auth/keys":
                return self._create_api_key(body)
            if method == "DELETE" and path.startswith("/v1/auth/keys/"):
                key_id = unquote(path.rsplit("/", 1)[-1])
                if not self.registry.store.revoke_api_key(key_id):
                    return ApiResponse(HTTPStatus.NOT_FOUND, {"error": {"message": "API key not found"}})
                return ApiResponse(HTTPStatus.NO_CONTENT, {})
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
        body = self.rfile.read(length)
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
        )
        self._write_response(response)

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
