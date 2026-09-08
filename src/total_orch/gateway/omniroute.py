"""OmniRoute implementation of the GatewayAdapter contract.

Only this module knows OmniRoute endpoint shapes. The rest of the platform sees
gateway-neutral records and normalized errors.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from total_orch.config import Settings
from total_orch.domain import ProviderConnectionRef

from .base import GatewayAdapter
from .errors import GatewayError
from .models import GatewayCompletion, GatewayModel, GatewayRequest, GatewayStreamChunk


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
                "httpx is required for the live OmniRoute adapter",
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
                raise GatewayError("gateway_timeout", "OmniRoute request timed out", retryable=True) from exc
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
                "httpx is required for the live OmniRoute adapter",
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
                        f"OmniRoute returned HTTP {response.status_code}",
                        retryable=_is_retryable_status(response.status_code),
                        status_code=response.status_code,
                    )
                async for line in response.aiter_lines():
                    yield line
        except httpx.TimeoutException as exc:
            raise GatewayError("gateway_timeout", "OmniRoute stream timed out", retryable=True) from exc

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


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _items(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
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


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part.get("text", "") for part in value if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return ""


class OmniRouteAdapter(GatewayAdapter):
    """Adapter for OmniRoute management endpoints and OpenAI-compatible execution."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: AsyncTransport | None = None,
        connections_path: str = "/api/providers",
        provider_models_path_template: str = "/api/providers/{provider}/models",
    ) -> None:
        self.settings = settings or Settings.from_env()
        self._transport = transport or _HttpxTransport()
        self._connections_path = connections_path
        self._provider_models_path_template = provider_models_path_template

    def _headers(self, *, management: bool = False) -> dict[str, str]:
        api_key = self.settings.omniroute_management_key if management else self.settings.omniroute_api_key
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.settings.omniroute_base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        management: bool = False,
        body: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            response = await self._transport.request(
                method,
                self._url(path),
                headers=self._headers(management=management),
                json_body=body,
                timeout=self.settings.omniroute_timeout_seconds,
            )
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError("gateway_transport_error", "OmniRoute transport failed", retryable=True) from exc

        if response.status_code >= 400:
            raise GatewayError(
                "gateway_http_error",
                f"OmniRoute returned HTTP {response.status_code}",
                retryable=_is_retryable_status(response.status_code),
                status_code=response.status_code,
            )
        return response

    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        response = await self._request("GET", self._connections_path, management=True)
        records = _items(response.data, "connections", "providers", "data")
        result: list[ProviderConnectionRef] = []
        for record in records:
            provider = str(record.get("provider") or record.get("name") or record.get("id") or "unknown")
            connection_id = str(record.get("id") or record.get("connection_id") or provider)
            result.append(
                ProviderConnectionRef(
                    id=connection_id,
                    provider=provider,
                    label=_optional_string(record.get("label") or record.get("display_name")),
                    account_ref=_optional_string(record.get("account_ref") or record.get("account")),
                )
            )
        return tuple(result)

    async def list_models(self) -> Sequence[GatewayModel]:
        response = await self._request("GET", "/v1/models")
        records = _items(response.data, "data", "models")
        return tuple(self._model_from_record(record) for record in records)

    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        path = self._provider_models_path_template.format(provider=quote(provider, safe=""))
        response = await self._request("GET", path, management=True)
        records = _items(response.data, "data", "models", "providers")
        return tuple(self._model_from_record(record, default_provider=provider) for record in records)

    async def validate_connection(self, connection_id: str) -> bool:
        path = f"/api/providers/{quote(connection_id, safe='')}/test"
        response = await self._request("POST", path, management=True, body={})
        if isinstance(response.data, Mapping):
            value = response.data.get("valid", response.data.get("healthy", response.data.get("ok")))
            if isinstance(value, bool):
                return value
        return True

    async def health_check(self, provider: str, model: str | None = None) -> bool:
        path = f"/api/providers/{quote(provider, safe='')}/health"
        if model:
            path += f"?model={quote(model, safe='')}"
        response = await self._request("GET", path, management=True)
        if isinstance(response.data, Mapping):
            value = response.data.get("healthy", response.data.get("ok", response.data.get("status")))
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in {"ok", "healthy", "ready", "up"}
        return True

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        body: dict[str, Any] = {
            "model": request.target_model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
            "stream": False,
            **dict(request.extra),
        }
        if request.tools:
            body["tools"] = list(request.tools)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            body["reasoning_effort"] = request.reasoning_effort

        attempts = self.settings.omniroute_max_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._request("POST", "/v1/chat/completions", body=body)
                return self._completion_from_response(response.data, request)
            except GatewayError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
        raise AssertionError("unreachable")

    async def stream(self, request: GatewayRequest) -> AsyncIterator[GatewayStreamChunk]:
        body: dict[str, Any] = {
            "model": request.target_model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
            "stream": True,
            **dict(request.extra),
        }
        if request.tools:
            body["tools"] = list(request.tools)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            body["reasoning_effort"] = request.reasoning_effort

        try:
            lines = self._transport.stream(
                "POST",
                self._url("/v1/chat/completions"),
                headers={**self._headers(), "Accept": "text/event-stream"},
                json_body=body,
                timeout=self.settings.omniroute_timeout_seconds,
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
                    raise GatewayError("invalid_stream_payload", "OmniRoute returned invalid SSE JSON", retryable=False) from exc
                yield self._stream_chunk_from_response(data, request)
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError("gateway_stream_error", "OmniRoute streaming failed", retryable=True) from exc

    def _model_from_record(self, record: Mapping[str, Any], default_provider: str | None = None) -> GatewayModel:
        model_id = str(record.get("id") or record.get("name") or "unknown")
        provider = _optional_string(record.get("provider") or record.get("provider_id")) or default_provider
        if provider is None and "/" in model_id:
            provider = model_id.split("/", 1)[0]
        context_window = record.get("context_window") or record.get("context_length")
        return GatewayModel(
            id=model_id,
            provider=provider,
            display_name=_optional_string(record.get("display_name") or record.get("name")),
            context_window=int(context_window) if isinstance(context_window, (int, float)) else None,
            capabilities=record.get("capabilities", {}) if isinstance(record.get("capabilities", {}), Mapping) else {},
            metadata={key: value for key, value in record.items() if key not in {"id", "name", "capabilities"}},
        )

    def _completion_from_response(self, data: Any, request: GatewayRequest) -> GatewayCompletion:
        if not isinstance(data, Mapping):
            raise GatewayError("invalid_gateway_response", "OmniRoute returned a non-object response", retryable=False)
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        message = first.get("message", {}) if isinstance(first, Mapping) else {}
        return GatewayCompletion(
            request_id=_optional_string(data.get("id")),
            provider=request.provider,
            model=str(data.get("model") or request.target_model),
            output_text=_text_content(message.get("content") if isinstance(message, Mapping) else None),
            finish_reason=_optional_string(first.get("finish_reason") if isinstance(first, Mapping) else None),
            usage=data.get("usage", {}) if isinstance(data.get("usage", {}), Mapping) else {},
            metadata=_safe_metadata(data),
        )

    def _stream_chunk_from_response(self, data: Any, request: GatewayRequest) -> GatewayStreamChunk:
        if not isinstance(data, Mapping):
            raise GatewayError("invalid_stream_payload", "OmniRoute returned a non-object stream chunk", retryable=False)
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        delta = first.get("delta", {}) if isinstance(first, Mapping) else {}
        return GatewayStreamChunk(
            request_id=_optional_string(data.get("id")),
            provider=request.provider,
            model=str(data.get("model") or request.target_model),
            delta=_text_content(delta.get("content") if isinstance(delta, Mapping) else None),
            finish_reason=_optional_string(first.get("finish_reason") if isinstance(first, Mapping) else None),
            usage=data.get("usage", {}) if isinstance(data.get("usage", {}), Mapping) else {},
            metadata=_safe_metadata(data),
        )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _safe_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"id", "model", "system_fingerprint", "service_tier"}
    return {key: data[key] for key in allowed if key in data}
