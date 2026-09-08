import pytest

from total_orch.domain import ProviderConnectionRef
from total_orch.gateway import (
    FakeGatewayAdapter,
    GatewayCompletion,
    GatewayError,
    GatewayRequest,
)
from total_orch.gateway.models import ChatMessage


def request() -> GatewayRequest:
    return GatewayRequest(
        provider="provider-a",
        model="luna-5.6-pro",
        messages=(ChatMessage(role="user", content="hello"),),
    )


@pytest.mark.asyncio
async def test_fake_gateway_discovers_connections_and_executes() -> None:
    adapter = FakeGatewayAdapter(
        connections=(ProviderConnectionRef(id="a", provider="provider-a"),),
    )
    adapter.register_response(
        "provider-a",
        "luna-5.6-pro",
        GatewayCompletion(
            request_id="req-1",
            provider="provider-a",
            model="provider-a/luna-5.6-pro",
            output_text="done",
        ),
    )

    assert await adapter.validate_connection("a") is True
    result = await adapter.complete(request())

    assert result.output_text == "done"
    assert result.provider == "provider-a"


@pytest.mark.asyncio
async def test_fake_gateway_normalizes_simulated_failure() -> None:
    adapter = FakeGatewayAdapter()
    adapter.register_failure("provider-a", "luna-5.6-pro")

    with pytest.raises(GatewayError) as raised:
        await adapter.complete(request())

    assert raised.value.code == "provider_unavailable"
    assert raised.value.retryable is True
    assert raised.value.as_dict()["message"] == "simulated provider failure"


@pytest.mark.asyncio
async def test_fake_gateway_stream_is_normalized() -> None:
    adapter = FakeGatewayAdapter()
    adapter.register_response(
        "provider-a",
        "luna-5.6-pro",
        GatewayCompletion(
            request_id="req-1",
            provider="provider-a",
            model="provider-a/luna-5.6-pro",
            output_text="hello world",
            finish_reason="stop",
        ),
    )

    chunks = [chunk async for chunk in adapter.stream(request())]

    assert [chunk.delta for chunk in chunks[:-1]] == ["hello", " world"]
    assert chunks[-1].finish_reason == "stop"
