"""Multi-router gateway compatibility tests.

The generic OpenAI-compatible adapter must cover the surface third-party
routers (LiteLLM, 9Router, One API / New API, FreeLLMAPI) actually expose,
and the factory must select adapters from configuration without the rest of
the platform caring which gateway is behind the boundary.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_omniroute_adapter import StubTransport
from total_orch.config import Settings
from total_orch.gateway import (
    GatewayRequest,
    OmniRouteAdapter,
    OpenAICompatAdapter,
    build_gateway,
    supported_gateway_types,
)
from total_orch.gateway.errors import GatewayError
from total_orch.gateway.models import ChatMessage
from total_orch.gateway.omniroute import HttpResponse


def settings_for(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "gateway_type": "litellm",
        "gateway_base_url": "http://router.test",
        "gateway_api_key": "router-secret",
        "gateway_timeout_seconds": 5.0,
        "gateway_max_retries": 1,
    }
    base.update(overrides)
    return Settings(**base)


def request() -> GatewayRequest:
    return GatewayRequest(
        provider="openai",
        model="openai/gpt-4o",
        messages=(ChatMessage(role="user", content="hello"),),
    )


def bare_request() -> GatewayRequest:
    return GatewayRequest(
        provider="openai",
        model="gpt-4o",
        messages=(ChatMessage(role="user", content="hello"),),
    )


def responses(payload: Any, count: int) -> list[HttpResponse]:
    """Independent copies of one payload; discovery calls re-fetch the catalog."""

    return [HttpResponse(200, payload, {}) for _ in range(count)]


# -- generic adapter execution ------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_sends_catalog_id_verbatim() -> None:
    transport = StubTransport(
        [
            HttpResponse(
                200,
                {"id": "req-1", "model": "openai/gpt-4o", "choices": [{"message": {"content": "hi"}}]},
                {},
            )
        ]
    )
    adapter = OpenAICompatAdapter(
        base_url="http://router.test",
        api_key="secret",
        transport=transport,
    )

    result = await adapter.complete(request())

    assert result.output_text == "hi"
    # No provider prefixing: One API / New API / FreeLLMAPI style channels.
    assert transport.calls[0][2]["model"] == "openai/gpt-4o"
    assert transport.calls[0][1] == "http://router.test/v1/chat/completions"


@pytest.mark.asyncio
async def test_openai_compat_prefixes_model_when_configured() -> None:
    transport = StubTransport(
        [HttpResponse(200, {"id": "req-2", "choices": [{"message": {"content": "ok"}}]}, {})]
    )
    adapter = OpenAICompatAdapter(
        base_url="http://router.test",
        prefix_model_with_provider=True,
        transport=transport,
    )

    await adapter.complete(bare_request())

    # OmniRoute-style addressing: the provider qualifier is added on the wire.
    assert transport.calls[0][2]["model"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_openai_compat_bearer_auth_and_extra_headers() -> None:
    transport = StubTransport([HttpResponse(200, {"choices": [{"message": {"content": "x"}}]}, {})])
    adapter = OpenAICompatAdapter(
        base_url="http://router.test",
        api_key="secret",
        extra_headers={"X-Title": "TotalOrch"},
        transport=transport,
    )

    await adapter.complete(request())

    headers = transport.last_headers
    assert headers["Authorization"] == "Bearer secret"
    assert headers["X-Title"] == "TotalOrch"


@pytest.mark.asyncio
async def test_openai_compat_retries_transient_failure() -> None:
    transport = StubTransport(
        [
            HttpResponse(503, {"error": "temporary"}, {}),
            HttpResponse(200, {"id": "req-3", "choices": [{"message": {"content": "ok"}}]}, {}),
        ]
    )
    adapter = OpenAICompatAdapter(base_url="http://router.test", max_retries=1, transport=transport)

    result = await adapter.complete(request())

    assert result.output_text == "ok"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_openai_compat_does_not_retry_non_transient() -> None:
    transport = StubTransport([HttpResponse(401, {"error": "unauthorized"}, {})])
    adapter = OpenAICompatAdapter(base_url="http://router.test", max_retries=1, transport=transport)

    with pytest.raises(GatewayError) as raised:
        await adapter.complete(request())

    assert raised.value.retryable is False
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_openai_compat_normalizes_streaming_sse() -> None:
    transport = StubTransport(
        [],
        [
            'data: {"id":"req-4","model":"openai/gpt-4o","choices":[{"delta":{"content":"he"}}]}',
            'data: {"id":"req-4","choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ],
    )
    adapter = OpenAICompatAdapter(base_url="http://router.test", transport=transport)

    chunks = [chunk async for chunk in adapter.stream(request())]

    assert chunks[0].delta == "he"
    assert chunks[1].finish_reason == "stop"


# -- discovery over the generic surface ----------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_derives_connections_from_catalog() -> None:
    catalog = {
        "data": [
            {"id": "openai/gpt-4o", "context_length": 128000},
            {"id": "anthropic/claude-3-5-sonnet", "context_length": 200000},
            {"id": "local-model"},
        ]
    }
    transport = StubTransport(responses(catalog, 3))
    adapter = OpenAICompatAdapter(base_url="http://router.test", transport=transport)

    connections = await adapter.list_connections()
    models = await adapter.list_models()
    provider_models = await adapter.list_provider_models("openai")

    assert [connection.provider for connection in connections] == ["openai", "anthropic", "default"]
    assert models[0].context_window == 128000
    assert [model.id for model in provider_models] == ["openai/gpt-4o"]


@pytest.mark.asyncio
async def test_openai_compat_health_check_uses_catalog_presence() -> None:
    catalog = {"data": [{"id": "openai/gpt-4o"}, {"id": "openai/gpt-4o-mini"}]}
    transport = StubTransport(responses(catalog, 4))
    adapter = OpenAICompatAdapter(base_url="http://router.test", transport=transport)

    assert await adapter.health_check("openai") is True
    assert await adapter.health_check("openai", model="gpt-4o") is True
    assert await adapter.health_check("openai", model="gpt-3.5") is False
    assert await adapter.validate_connection("openai") is True


@pytest.mark.asyncio
async def test_openai_compat_health_check_false_on_gateway_error() -> None:
    transport = StubTransport([HttpResponse(503, {"error": "down"}, {})])
    adapter = OpenAICompatAdapter(base_url="http://router.test", transport=transport)

    assert await adapter.health_check("openai") is False


# -- router defaults and factory ------------------------------------------------


def test_for_router_uses_known_defaults_when_env_unset() -> None:
    adapter = OpenAICompatAdapter.for_router("9router", settings=settings_for(gateway_base_url=None))

    assert adapter.router_name == "9Router"
    assert adapter._base_url == "http://localhost:3613"


def test_for_router_env_url_beats_router_default() -> None:
    adapter = OpenAICompatAdapter.for_router(
        "freellmapi", settings=settings_for(gateway_base_url="http://env-override.test")
    )

    assert adapter._base_url == "http://env-override.test"


def test_for_router_explicit_argument_beats_everything() -> None:
    adapter = OpenAICompatAdapter.for_router(
        "litellm",
        settings=settings_for(gateway_base_url="http://env.test"),
        base_url="http://explicit.test",
    )

    assert adapter._base_url == "http://explicit.test"


def test_for_router_accepts_unknown_router_names() -> None:
    adapter = OpenAICompatAdapter.for_router("my-own-router", settings=settings_for(gateway_base_url=None))

    assert adapter.router_name == "my-own-router"
    assert adapter._base_url == "http://localhost:4000"


def test_supported_gateway_types_lists_defaults() -> None:
    assert supported_gateway_types() == ("omniroute", "litellm", "9router", "oneapi", "newapi", "freellmapi")


def test_build_gateway_selects_omniroute_by_default() -> None:
    gateway = build_gateway(Settings(omniroute_base_url="http://omni.test"))

    assert isinstance(gateway, OmniRouteAdapter)


@pytest.mark.parametrize(
    ("gateway_type", "expected_router_name"),
    [
        ("litellm", "LiteLLM"),
        ("9router", "9Router"),
        ("oneapi", "One API"),
        ("newapi", "New API"),
        ("freellmapi", "FreeLLMAPI"),
        ("custom-router", "custom-router"),
    ],
)
def test_build_gateway_selects_generic_adapter(gateway_type: str, expected_router_name: str) -> None:
    gateway = build_gateway(settings_for(gateway_type=gateway_type, gateway_base_url=None))

    assert isinstance(gateway, OpenAICompatAdapter)
    assert gateway.router_name == expected_router_name


def test_build_gateway_is_case_insensitive() -> None:
    gateway = build_gateway(settings_for(gateway_type="LiteLLM"))

    assert isinstance(gateway, OpenAICompatAdapter)
    assert gateway.router_name == "LiteLLM"
