"""Direct Anthropic Messages API adapter tests (ROADMAP Phase 4)."""

from __future__ import annotations

import json

import pytest

from tests.test_omniroute_adapter import StubTransport
from total_orch.config import Settings
from total_orch.gateway import AnthropicAdapter, GatewayRequest, build_gateway
from total_orch.gateway.errors import GatewayError
from total_orch.gateway.models import ChatMessage
from total_orch.gateway.omniroute import HttpResponse


def _request() -> GatewayRequest:
    return GatewayRequest(
        provider="anthropic",
        model="anthropic/claude-sonnet-4",
        messages=(
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="read the file"),
        ),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            },
        ),
    )


@pytest.mark.asyncio
async def test_completion_preserves_text_tool_calls_usage_and_system_prompt() -> None:
    transport = StubTransport(
        [
            HttpResponse(
                200,
                {
                    "id": "msg_1",
                    "model": "claude-sonnet-4",
                    "content": [
                        {"type": "text", "text": "I will read it."},
                        {"type": "tool_use", "id": "tool_1", "name": "read_file", "input": {"path": "a.py"}},
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                },
                {},
            )
        ]
    )
    adapter = AnthropicAdapter(api_key="secret", transport=transport)

    completion = await adapter.complete(_request())

    body = transport.calls[0][2]
    assert body is not None
    assert body["model"] == "claude-sonnet-4"
    assert body["system"] == "be brief"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert transport.last_headers["x-api-key"] == "secret"
    assert completion.output_text == "I will read it."
    assert completion.tool_calls[0].function_name == "read_file"
    assert json.loads(completion.tool_calls[0].arguments) == {"path": "a.py"}
    assert completion.usage == {"prompt_tokens": 11, "completion_tokens": 7}
    assert completion.finish_reason == "tool_use"


@pytest.mark.asyncio
async def test_stream_normalizes_text_tool_arguments_and_stop_reason() -> None:
    transport = StubTransport(
        [],
        stream_lines=[
            'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-4","usage":{"input_tokens":3}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tool_1","name":"read_file"}}',
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"a.py\\"}"}}',
            'data: {"type":"content_block_stop","index":1}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}',
            "data: [DONE]",
        ],
    )
    adapter = AnthropicAdapter(api_key="secret", transport=transport)

    chunks = [chunk async for chunk in adapter.stream(_request())]

    assert chunks[0].delta == "Hi"
    tool_chunk = next(chunk for chunk in chunks if chunk.tool_calls)
    assert tool_chunk.tool_calls[0].function_name == "read_file"
    assert json.loads(tool_chunk.tool_calls[0].arguments) == {"path": "a.py"}
    assert chunks[-1].finish_reason == "tool_use"
    assert chunks[-1].usage["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried() -> None:
    transport = StubTransport([HttpResponse(401, {"error": {"type": "authentication_error", "message": "bad key"}}, {})])
    adapter = AnthropicAdapter(api_key="wrong", max_retries=2, transport=transport)

    with pytest.raises(GatewayError) as caught:
        await adapter.complete(_request())

    assert caught.value.retryable is False
    assert caught.value.code == "authentication_error"
    assert len(transport.calls) == 1


def test_factory_selects_the_direct_anthropic_adapter() -> None:
    adapter = build_gateway(
        Settings(
            gateway_type="anthropic",
            gateway_base_url="http://anthropic.test",
            gateway_api_key="secret",
        )
    )

    assert isinstance(adapter, AnthropicAdapter)


@pytest.mark.asyncio
async def test_direct_and_gateway_execution_share_the_normalized_contract() -> None:
    from total_orch.gateway import OpenAICompatAdapter

    direct = AnthropicAdapter(
        api_key="secret",
        transport=StubTransport(
            [
                HttpResponse(
                    200,
                    {
                        "id": "msg_1",
                        "model": "claude-sonnet-4",
                        "content": [{"type": "text", "text": "same answer"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 4, "output_tokens": 2},
                    },
                    {},
                )
            ]
        ),
    )
    gateway = OpenAICompatAdapter(
        base_url="http://router.test",
        transport=StubTransport(
            [
                HttpResponse(
                    200,
                    {
                        "id": "chatcmpl_1",
                        "model": "anthropic/claude-sonnet-4",
                        "choices": [
                            {"message": {"content": "same answer"}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                    },
                    {},
                )
            ]
        ),
    )
    request = GatewayRequest(
        provider="anthropic",
        model="anthropic/claude-sonnet-4",
        messages=(ChatMessage(role="user", content="hello"),),
    )

    direct_result = await direct.complete(request)
    gateway_result = await gateway.complete(request)

    assert direct_result.output_text == gateway_result.output_text
    assert direct_result.usage == gateway_result.usage
    assert direct_result.tool_calls == gateway_result.tool_calls == ()
