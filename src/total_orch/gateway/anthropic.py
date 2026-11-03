"""Direct Anthropic Messages API adapter (ROADMAP Phase 4).

The rest of the platform sees the same ``GatewayAdapter`` contract; only this
module knows Anthropic's wire format. Tool calls are preserved without being
executed, and streaming is exposed as normalized chunks.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from total_orch.domain import ProviderConnectionRef

from ._openai import (
    AsyncTransport,
    HttpResponse,
    _HttpxTransport,
    _retry_after_seconds,
    is_retryable_status,
)
from .base import GatewayAdapter
from .errors import GatewayError
from .models import (
    GatewayCompletion,
    GatewayModel,
    GatewayRequest,
    GatewayStreamChunk,
    GatewayToolCall,
)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicAdapter(GatewayAdapter):
    """Provider-neutral execution over Anthropic's ``/v1/messages`` API."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        transport: AsyncTransport | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._transport = transport or _HttpxTransport()

    # -- discovery ----------------------------------------------------------

    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        if self._api_key is None:
            return ()
        return (ProviderConnectionRef(id="anthropic", provider="anthropic", label="Anthropic"),)

    async def list_models(self) -> Sequence[GatewayModel]:
        response = await self._request("GET", "/v1/models?limit=1000")
        data = response.data if isinstance(response.data, dict) else {}
        models = data.get("data", [])
        if not isinstance(models, list):
            raise GatewayError("invalid_gateway_response", "Anthropic returned a malformed model catalog", retryable=False)
        return [
            GatewayModel(
                id=str(item["id"]),
                provider="anthropic",
                display_name=str(item.get("display_name", item["id"])),
                metadata={"created_at": item.get("created_at")},
            )
            for item in models
            if isinstance(item, dict) and item.get("id")
        ]

    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        return await self.list_models() if provider == "anthropic" else ()

    async def validate_connection(self, connection_id: str) -> bool:
        if connection_id != "anthropic":
            return False
        return await self.health_check("anthropic")

    async def health_check(self, provider: str, model: str | None = None) -> bool:
        try:
            await self.list_models()
        except GatewayError:
            return False
        return provider == "anthropic"

    # -- completion ---------------------------------------------------------

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        response = await self._request("POST", "/v1/messages", self._body(request))
        data = response.data if isinstance(response.data, dict) else {}
        blocks = data.get("content", [])
        if not isinstance(blocks, list):
            raise GatewayError("invalid_gateway_response", "Anthropic returned malformed content", retryable=False)
        texts: list[str] = []
        tool_calls: list[GatewayToolCall] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    GatewayToolCall(
                        id=str(block.get("id", "")),
                        function_name=str(block.get("name", "")),
                        arguments=json.dumps(block.get("input", {}), ensure_ascii=False),
                        index=index,
                    )
                )
        usage = data.get("usage", {})
        return GatewayCompletion(
            request_id=str(data["id"]) if data.get("id") else None,
            provider="anthropic",
            model=str(data.get("model", self._model(request))),
            output_text="".join(texts),
            finish_reason=str(data["stop_reason"]) if data.get("stop_reason") else None,
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
            metadata={"type": "message"},
            tool_calls=tuple(tool_calls),
        )

    async def stream(self, request: GatewayRequest) -> AsyncIterator[GatewayStreamChunk]:
        body = self._body(request)
        body["stream"] = True
        request_id: str | None = None
        model = self._model(request)
        usage: dict[str, Any] = {}
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        async for line in self._transport.stream(
            "POST",
            f"{self._base_url}/v1/messages",
            headers=self._headers(),
            json_body=body,
            timeout=self._timeout,
        ):
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "message_start":
                message = event.get("message", {})
                request_id = str(message.get("id")) if message.get("id") else None
                model = str(message.get("model", model))
                usage.update(_usage(message.get("usage", {})))
            elif event_type == "content_block_start":
                block = event.get("content_block", {})
                index = int(event.get("index", 0))
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls[index] = {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "arguments": "",
                    }
            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                index = int(event.get("index", 0))
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = str(delta.get("text", ""))
                    if text:
                        yield GatewayStreamChunk(
                            request_id=request_id,
                            provider="anthropic",
                            model=model,
                            delta=text,
                        )
                elif isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                    if index in tool_calls:
                        tool_calls[index]["arguments"] += str(delta.get("partial_json", ""))
            elif event_type == "content_block_stop":
                index = int(event.get("index", 0))
                pending = tool_calls.pop(index, None)
                if pending is not None:
                    yield GatewayStreamChunk(
                        request_id=request_id,
                        provider="anthropic",
                        model=model,
                        tool_calls=(
                            GatewayToolCall(
                                id=pending["id"],
                                function_name=pending["name"],
                                arguments=pending["arguments"],
                                index=index,
                            ),
                        ),
                    )
            elif event_type == "message_delta":
                delta = event.get("delta", {})
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    finish_reason = str(delta["stop_reason"])
                usage.update(_usage(event.get("usage", {})))
                yield GatewayStreamChunk(
                    request_id=request_id,
                    provider="anthropic",
                    model=model,
                    finish_reason=finish_reason,
                    usage=usage,
                )
        if finish_reason is None:
            yield GatewayStreamChunk(
                request_id=request_id,
                provider="anthropic",
                model=model,
                finish_reason="end_turn",
                usage=usage,
            )

    # -- internals ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self._api_key is not None:
            headers["x-api-key"] = self._api_key
        return headers

    def _model(self, request: GatewayRequest) -> str:
        return request.model.removeprefix("anthropic/")

    def _body(self, request: GatewayRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, str]] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                messages.append({"role": message.role, "content": message.content})
        body: dict[str, Any] = {
            "model": self._model(request),
            "max_tokens": int(request.extra.get("max_tokens", 1024)),
            "messages": messages,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [_tool_schema(tool) for tool in request.tools]
        for key, value in request.extra.items():
            if key not in {"max_tokens", "model", "messages", "stream", "tools"}:
                body[key] = value
        return body

    async def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        if self._api_key is None and method != "GET":
            raise GatewayError(
                "authentication_failed",
                "ANTHROPIC_API_KEY is not configured",
                retryable=False,
                status_code=401,
                provider="anthropic",
            )
        url = f"{self._base_url}{path}"
        last_error: GatewayError | None = None
        for attempt in range(self._max_retries + 1):
            response = await self._transport.request(
                method,
                url,
                headers=self._headers(),
                json_body=body,
                timeout=self._timeout,
            )
            if response.status_code < 400:
                return response
            error = GatewayError(
                "gateway_http_error",
                f"Anthropic returned HTTP {response.status_code}",
                retryable=is_retryable_status(response.status_code),
                status_code=response.status_code,
                provider="anthropic",
            )
            if "error" in (response.data or {}):
                payload = response.data.get("error", {})
                if isinstance(payload, dict) and payload.get("message"):
                    error = GatewayError(
                        str(payload.get("type", "gateway_http_error")),
                        str(payload["message"]),
                        retryable=is_retryable_status(response.status_code),
                        status_code=response.status_code,
                        provider="anthropic",
                    )
            last_error = error
            if not error.retryable or attempt >= self._max_retries:
                raise error
            delay = _retry_after_seconds(response.headers) or min(0.25 * (2**attempt), 4.0)
            await asyncio.sleep(delay)
        raise last_error or GatewayError("gateway_http_error", "Anthropic request failed", retryable=False)


def _usage(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    return {
        "prompt_tokens": int(payload.get("input_tokens", 0) or 0),
        "completion_tokens": int(payload.get("output_tokens", 0) or 0),
    }


def _tool_schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    function = tool.get("function")
    if not isinstance(function, Mapping):
        return dict(tool)
    return {
        "name": str(function.get("name", "")),
        "description": str(function.get("description", "")),
        "input_schema": function.get("parameters", {"type": "object"}),
    }
