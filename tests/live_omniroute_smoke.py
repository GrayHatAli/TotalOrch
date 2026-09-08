"""Manual live smoke test for the locally installed OmniRoute instance.

This file is intentionally not collected by pytest. It reads the local,
ignored ``tests/settings.json`` and never prints credential values.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from total_orch.config import Settings
from total_orch.gateway import GatewayRequest, OmniRouteAdapter
from total_orch.gateway.models import ChatMessage


def load_settings() -> tuple[Settings, dict[str, str]]:
    raw = json.loads(Path(__file__).with_name("settings.json").read_text())["env"]
    return (
        Settings(
            omniroute_base_url=raw["ANTHROPIC_BASE_URL"],
            omniroute_api_key=raw["ANTHROPIC_AUTH_TOKEN"],
            omniroute_timeout_seconds=float(os.getenv("LIVE_OMNIROUTE_TIMEOUT_SECONDS", "180")),
            omniroute_max_retries=0,
        ),
        raw,
    )


async def main() -> int:
    settings, raw = load_settings()
    adapter = OmniRouteAdapter(settings)

    models = await adapter.list_models()
    print(f"models_discovered: {len(models)}")

    target = raw["ANTHROPIC_DEFAULT_SONNET_MODEL"]
    provider, model = target.split("/", 1)
    request = GatewayRequest(
        provider=provider,
        model=model,
        messages=(ChatMessage(role="user", content="Reply with exactly: ORCHESTRATOR_LIVE_OK"),),
        extra={"max_tokens": 8},
    )

    completion = await adapter.complete(request)
    print("completion_ok: true")
    print(f"routed_model: {completion.model}")
    print(f"output: {completion.output_text[:200]}")

    stream_request = GatewayRequest(
        provider=provider,
        model=model,
        messages=(ChatMessage(role="user", content="Reply with exactly: ORCHESTRATOR_STREAM_OK"),),
        extra={"max_tokens": 8},
    )
    chunks = [chunk async for chunk in adapter.stream(stream_request)]
    print("stream_ok: true")
    print(f"stream_chunks: {len(chunks)}")
    print(f"stream_output: {''.join(chunk.delta for chunk in chunks)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
