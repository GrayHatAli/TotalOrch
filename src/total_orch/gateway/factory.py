"""Gateway selection: build the adapter named by configuration.

``GATEWAY_TYPE`` picks the implementation behind the ``GatewayAdapter``
boundary. The default remains the managed OmniRoute instance; any
OpenAI-compatible router (LiteLLM, 9Router, One API / New API, FreeLLMAPI,
...) is selected by setting the variable to that router's name and pointing
``GATEWAY_BASE_URL`` at it.
"""

from __future__ import annotations

from typing import Any

from total_orch.config import Settings

from .anthropic import AnthropicAdapter
from .base import GatewayAdapter
from .omniroute import OmniRouteAdapter
from .openai_compat import OpenAICompatAdapter

__all__ = ["build_gateway", "supported_gateway_types"]


def build_gateway(
    settings: Settings | None = None,
    *,
    transport: Any = None,
) -> GatewayAdapter:
    """Build the gateway adapter selected by ``settings.gateway_type``.

    ``"omniroute"`` (default) builds :class:`OmniRouteAdapter`. Any other
    value builds :class:`OpenAICompatAdapter`; known router names
    (``litellm``, ``9router``, ``oneapi``, ``newapi``, ``freellmapi``) pick
    up their default ports and addressing conventions.
    """

    settings = settings or Settings.from_env()
    gateway_type = settings.gateway_type.strip().lower()
    if gateway_type in {"", "omniroute"}:
        return OmniRouteAdapter(settings, transport=transport)
    if gateway_type == "anthropic":
        return AnthropicAdapter(
            api_key=settings.gateway_api_key,
            base_url=settings.gateway_base_url or "https://api.anthropic.com",
            timeout_seconds=settings.gateway_timeout_seconds,
            max_retries=settings.gateway_max_retries,
            transport=transport,
        )
    return OpenAICompatAdapter.for_router(
        gateway_type,
        settings=settings,
        transport=transport,
    )


def supported_gateway_types() -> tuple[str, ...]:
    """Gateway discriminators accepted by :func:`build_gateway`."""

    return ("omniroute", "litellm", "9router", "oneapi", "newapi", "freellmapi", "anthropic")
