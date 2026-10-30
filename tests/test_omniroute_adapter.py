from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from total_orch.config import Settings
from total_orch.gateway import GatewayRequest, OmniRouteAdapter
from total_orch.gateway.errors import GatewayError
from total_orch.gateway.models import ChatMessage
from total_orch.gateway.omniroute import HttpResponse


class StubTransport:
    def __init__(self, responses: list[HttpResponse], stream_lines: list[str] | None = None) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.stream_lines = stream_lines or []
        self.last_headers: dict[str, str] = {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append((method, url, json_body))
        self.last_headers = dict(headers)
        response = self.responses.pop(0)
        return response

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        timeout: float,
    ) -> AsyncIterator[str]:
        async def lines() -> AsyncIterator[str]:
            for line in self.stream_lines:
                yield line

        return lines()


def adapter(transport: StubTransport) -> OmniRouteAdapter:
    return OmniRouteAdapter(
        Settings(
            omniroute_base_url="http://omni.test",
            omniroute_api_key="runtime-secret",
            omniroute_management_key="management-secret",
            omniroute_max_retries=1,
        ),
        transport=transport,
    )


def request() -> GatewayRequest:
    return GatewayRequest(
        provider="provider-a",
        model="luna-5.6-pro",
        messages=(ChatMessage(role="user", content="hello"),),
    )


@pytest.mark.asyncio
async def test_omniroute_discovers_connections_and_models() -> None:
    transport = StubTransport(
        [
            HttpResponse(200, {"providers": [{"id": "a", "provider": "provider-a", "api_key": "must-not-leak"}]}, {}),
            HttpResponse(200, {"data": [{"id": "provider-a/luna-5.6-pro", "context_length": 128000}]}, {}),
        ]
    )
    live = adapter(transport)

    connections = await live.list_connections()
    models = await live.list_models()

    assert connections[0].provider == "provider-a"
    assert not hasattr(connections[0], "api_key")
    assert models[0].provider == "provider-a"
    assert models[0].context_window == 128000


@pytest.mark.asyncio
async def test_omniroute_executes_provider_prefixed_model() -> None:
    transport = StubTransport(
        [
            HttpResponse(
                200,
                {
                    "id": "req-1",
                    "model": "provider-a/luna-5.6-pro",
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 3},
                },
                {},
            )
        ]
    )
    result = await adapter(transport).complete(request())

    assert result.output_text == "done"
    body = transport.calls[0][2]
    assert body is not None
    assert body["model"] == "provider-a/luna-5.6-pro"


@pytest.mark.asyncio
async def test_omniroute_retries_transient_http_failure() -> None:
    transport = StubTransport(
        [
            HttpResponse(503, {"error": "temporary"}, {}),
            HttpResponse(200, {"id": "req-2", "choices": [{"message": {"content": "ok"}}]}, {}),
        ]
    )
    result = await adapter(transport).complete(request())

    assert result.output_text == "ok"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_omniroute_normalizes_streaming_sse() -> None:
    transport = StubTransport(
        [],
        [
            'data: {"id":"req-3","model":"provider-a/luna-5.6-pro","choices":[{"delta":{"content":"hi"}}]}',
            'data: {"id":"req-3","choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ],
    )
    chunks = [chunk async for chunk in adapter(transport).stream(request())]

    assert chunks[0].delta == "hi"
    assert chunks[1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_omniroute_does_not_retry_non_transient_failure() -> None:
    transport = StubTransport([HttpResponse(401, {"error": "unauthorized"}, {})])

    with pytest.raises(GatewayError) as raised:
        await adapter(transport).complete(request())

    assert raised.value.retryable is False
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_omniroute_empty_or_unknown_health_status_is_unhealthy() -> None:
    empty = StubTransport([HttpResponse(200, None, {})])
    assert await adapter(empty).health_check("provider-a") is False

    unknown = StubTransport([HttpResponse(200, {"status": "maybe"}, {})])
    assert await adapter(unknown).health_check("provider-a") is False


@pytest.mark.asyncio
async def test_omniroute_empty_validation_status_is_not_valid() -> None:
    transport = StubTransport([HttpResponse(200, {}, {})])

    assert await adapter(transport).validate_connection("connection-a") is False
