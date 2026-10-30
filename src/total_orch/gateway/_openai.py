"""Shared OpenAI-compatible execution surface for gateway adapters.

Most LLM routers — OmniRoute, LiteLLM, 9Router, One API / New API,
FreeLLMAPI — expose an OpenAI-compatible ``GET /v1/models`` and
``POST /v1/chat/completions`` surface with server-sent-event streaming. This
module holds the transport, request encoding, response parsing, retry, and
streaming logic once; each adapter subclass only describes how it differs
(connection management, model addressing, extra headers).

Only this module and the adapters built on it know gateway wire shapes. The
rest of the platform sees gateway-neutral records and normalized errors.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from total_orch.domain import ProviderConnectionRef

from .base import GatewayAdapter
from .errors import GatewayError
from .models import (
    GatewayCompletion,
    GatewayModel,
    GatewayRequest,
    GatewayStreamChunk,
    GatewayToolCall,
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    data: Any
    headers: Mapping[str, str]


class AsyncTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> HttpResponse: ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> AsyncIterator[str]: ...


class _HttpxTransport:
    """Lazy httpx transport so importing domain contracts does not require IO deps."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> HttpResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - exercised in packaging, not unit tests
            raise GatewayError(
                "http_client_missing",
                "httpx is required for the live gateway adapters",
                retryable=False,
            ) from exc

        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=dict(headers),
                    json=json_body,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                raise GatewayError("gateway_timeout", "gateway request timed out", retryable=True) from exc
            try:
                data = response.json()
            except ValueError:
                data = None
            return HttpResponse(response.status_code, data, response.headers)

    async def _stream_lines(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout: float,
    ) -> AsyncIterator[str]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise GatewayError(
                "http_client_missing",
                "httpx is required for the live gateway adapters",
                retryable=False,
            ) from exc

        try:
            async with httpx.AsyncClient() as client, client.stream(
                method,
                url,
                headers=dict(headers),
                json=json_body,
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    raise GatewayError(
                        "gateway_http_error",
                        f"gateway returned HTTP {response.status_code}",
                        retryable=is_retryable_status(response.status_code),
                        status_code=response.status_code,
                    )
                async for line in response.aiter_lines():
                    yield line
        except httpx.TimeoutException as exc:
            raise GatewayError("gateway_timeout", "gateway stream timed out", retryable=True) from exc

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> AsyncIterator[str]:
        return self._stream_lines(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )


def is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def items_from_payload(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        if all(isinstance(key, str) for key in payload):
            return [payload]
    return []


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part.get("text", "") for part in value if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return ""


def optional_string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def safe_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"id", "model", "system_fingerprint", "service_tier"}
    return {key: data[key] for key in allowed if key in data}


def tool_calls_from_payload(value: Any) -> tuple[GatewayToolCall, ...]:
    if not isinstance(value, list):
        return ()
    calls: list[GatewayToolCall] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        arguments = function.get("arguments")
        calls.append(
            GatewayToolCall(
                id=optional_string(item.get("id")),
                function_name=optional_string(function.get("name")),
                arguments=arguments if isinstance(arguments, str) else "",
                index=item.get("index") if isinstance(item.get("index"), int) else None,
            )
        )
    return tuple(calls)


def completion_from_response(data: Any, request: GatewayRequest) -> GatewayCompletion:
    if not isinstance(data, Mapping):
        raise GatewayError("invalid_gateway_response", "gateway returned a non-object response", retryable=False)
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message", {}) if isinstance(first, Mapping) else {}
    message_mapping = message if isinstance(message, Mapping) else {}
    return GatewayCompletion(
        request_id=optional_string(data.get("id")),
        provider=request.provider,
        model=str(data.get("model") or request.target_model),
        output_text=text_content(message_mapping.get("content")),
        finish_reason=optional_string(first.get("finish_reason") if isinstance(first, Mapping) else None),
        usage=data.get("usage", {}) if isinstance(data.get("usage", {}), Mapping) else {},
        metadata=safe_metadata(data),
        tool_calls=tool_calls_from_payload(message_mapping.get("tool_calls")),
    )


def stream_chunk_from_response(data: Any, request: GatewayRequest) -> GatewayStreamChunk:
    if not isinstance(data, Mapping):
        raise GatewayError("invalid_stream_payload", "gateway returned a non-object stream chunk", retryable=False)
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    delta = first.get("delta", {}) if isinstance(first, Mapping) else {}
    delta_mapping = delta if isinstance(delta, Mapping) else {}
    return GatewayStreamChunk(
        request_id=optional_string(data.get("id")),
        provider=request.provider,
        model=str(data.get("model") or request.target_model),
        delta=text_content(delta_mapping.get("content")),
        finish_reason=optional_string(first.get("finish_reason") if isinstance(first, Mapping) else None),
        usage=data.get("usage", {}) if isinstance(data.get("usage", {}), Mapping) else {},
        metadata=safe_metadata(data),
        tool_calls=tool_calls_from_payload(delta_mapping.get("tool_calls")),
    )


class OpenAICompatGatewayAdapter(GatewayAdapter):
    """Base class for gateways exposing an OpenAI-compatible API surface.

    Discovery reads ``GET /v1/models``; execution posts to
    ``POST /v1/chat/completions`` with SSE streaming and bounded retry on
    transient failures. Subclasses override the pieces a concrete gateway
    does differently — OmniRoute, for example, keeps its management API for
    connections, per-provider discovery, validation, and health.
    """

    gateway_name = "openai-compatible gateway"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        extra_headers: Mapping[str, str] | None = None,
        models_path: str = "/v1/models",
        completions_path: str = "/v1/chat/completions",
        prefix_model_with_provider: bool = False,
        transport: AsyncTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._extra_headers = dict(extra_headers or {})
        self._models_path = models_path
        self._completions_path = completions_path
        self._prefix_model_with_provider = prefix_model_with_provider
        self._transport = transport or _HttpxTransport()

    # -- HTTP plumbing -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self._extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        try:
            response = await self._transport.request(
                method,
                self._url(path),
                headers=headers if headers is not None else self._headers(),
                json_body=body,
                timeout=self._timeout_seconds,
            )
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(
                "gateway_transport_error",
                f"{self.gateway_name} transport failed",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise GatewayError(
                "gateway_http_error",
                f"{self.gateway_name} returned HTTP {response.status_code}",
                retryable=is_retryable_status(response.status_code),
                status_code=response.status_code,
            )
        return response

    # -- discovery -----------------------------------------------------------

    @staticmethod
    def _model_provider(model: GatewayModel) -> str:
        """The serving provider a catalog record belongs to.

        Explicit provider fields win; otherwise a publisher-prefixed id such
        as ``openai/gpt-4o`` names its prefix; bare ids fall under
        ``default`` (single-channel routers).
        """

        if model.provider:
            return model.provider
        if "/" in model.id:
            return model.id.split("/", 1)[0]
        return "default"

    def _model_from_record(self, record: Mapping[str, Any], default_provider: str | None = None) -> GatewayModel:
        raw_model_id = record.get("id") or record.get("name")
        if not isinstance(raw_model_id, str) or not raw_model_id.strip():
            raise GatewayError(
                "invalid_gateway_response",
                "gateway returned a model without a non-empty id",
                retryable=False,
            )
        model_id = raw_model_id.strip()
        provider = optional_string(record.get("provider") or record.get("provider_id")) or default_provider
        if provider is None and "/" in model_id:
            provider = model_id.split("/", 1)[0]
        context_window = record.get("context_window") or record.get("context_length")
        return GatewayModel(
            id=model_id,
            provider=provider,
            display_name=optional_string(record.get("display_name") or record.get("name")),
            context_window=int(context_window) if isinstance(context_window, (int, float)) else None,
            capabilities=record.get("capabilities", {}) if isinstance(record.get("capabilities", {}), Mapping) else {},
            metadata={key: value for key, value in record.items() if key not in {"id", "name", "capabilities"}},
        )

    async def list_models(self) -> Sequence[GatewayModel]:
        response = await self._request("GET", self._models_path)
        records = items_from_payload(response.data, "data", "models")
        return tuple(self._model_from_record(record) for record in records)

    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        models = await self.list_models()
        return tuple(model for model in models if self._model_provider(model) == provider)

    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        """Derive one connection per provider found in the model catalog.

        OpenAI-compatible routers generally do not expose a connection-
        management API, so the catalog itself is the source of truth for
        which providers are configured.
        """

        models = await self.list_models()
        providers: list[str] = []
        for model in models:
            provider = self._model_provider(model)
            if provider not in providers:
                providers.append(provider)
        return tuple(ProviderConnectionRef(id=provider, provider=provider) for provider in providers)

    async def validate_connection(self, connection_id: str) -> bool:
        return await self.health_check(connection_id)

    async def health_check(self, provider: str, model: str | None = None) -> bool:
        """Catalog-presence health check for routers without a health API."""

        try:
            models = await self.list_models()
        except GatewayError:
            return False
        matching = [candidate for candidate in models if self._model_provider(candidate) == provider]
        if model is None:
            return bool(matching)
        return any(candidate.id in {model, f"{provider}/{model}"} for candidate in matching)

    # -- execution -----------------------------------------------------------

    def _request_model(self, request: GatewayRequest) -> str:
        """The model id sent on the wire.

        Gateways that route by ``provider/model`` (OmniRoute) enable
        prefixing; routers whose catalog ids are already complete
        (One API / New API, 9Router, FreeLLMAPI) receive the id verbatim.
        """

        if self._prefix_model_with_provider:
            return request.target_model
        return request.model

    def _completions_body(self, request: GatewayRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = dict(request.extra)
        body.update(
            {
                "model": self._request_model(request),
                "messages": [{"role": message.role, "content": message.content} for message in request.messages],
                "stream": stream,
            }
        )
        if request.tools:
            body["tools"] = list(request.tools)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            body["reasoning_effort"] = request.reasoning_effort
        return body

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        body = self._completions_body(request, stream=False)

        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._request("POST", self._completions_path, body=body)
                return completion_from_response(response.data, request)
            except GatewayError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
        raise AssertionError("unreachable")

    async def stream(self, request: GatewayRequest) -> AsyncIterator[GatewayStreamChunk]:
        body = self._completions_body(request, stream=True)

        try:
            lines = self._transport.stream(
                "POST",
                self._url(self._completions_path),
                headers={**self._headers(), "Accept": "text/event-stream"},
                json_body=body,
                timeout=self._timeout_seconds,
            )
            async for line in lines:
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise GatewayError(
                        "invalid_stream_payload", "gateway returned invalid SSE JSON", retryable=False
                    ) from exc
                yield stream_chunk_from_response(data, request)
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(
                "gateway_stream_error", f"{self.gateway_name} streaming failed", retryable=True
            ) from exc
