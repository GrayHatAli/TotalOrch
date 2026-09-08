"""Generic OpenAI-compatible adapter for third-party LLM routers.

TotalOrch is not tied to OmniRoute. Any router that exposes the de-facto
OpenAI surface — ``GET /v1/models`` plus ``POST /v1/chat/completions`` with
SSE streaming — works through :class:`OpenAICompatAdapter`. This covers
LiteLLM, 9Router, One API / New API, FreeLLMAPI, and similar proxies;
OmniRoute remains the default managed gateway through its own subclass with
its management API.

Model addressing: the catalog id is sent on the wire verbatim (e.g.
``openai/gpt-4o`` or a router's channel alias). Set
``prefix_model_with_provider=True`` for gateways that route by
``provider/model`` identifiers instead.
"""

from __future__ import annotations

from total_orch.config import Settings

from ._openai import AsyncTransport, OpenAICompatGatewayAdapter

__all__ = ["OpenAICompatAdapter"]


class OpenAICompatAdapter(OpenAICompatGatewayAdapter):
    """Gateway adapter for any OpenAI-compatible LLM router.

    Parameters map directly onto constructor arguments; ``Settings`` supplies
    defaults for the gateway selected in ``GATEWAY_TYPE``.
    """

    gateway_name = "OpenAI-compatible router"

    @classmethod
    def for_router(
        cls,
        router: str,
        *,
        settings: Settings | None = None,
        transport: AsyncTransport | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> OpenAICompatAdapter:
        """Build an adapter for a named third-party router.

        ``router`` is a convenience discriminator (``"litellm"``,
        ``"9router"``, ``"oneapi"``, ``"newapi"``, ``"freellmapi"``, ...)
        recorded in metadata; behavior is identical across the OpenAI
        surface. Unrecognized names are accepted as-is.
        """

        settings = settings or Settings.from_env()
        defaults = _ROUTER_DEFAULTS.get(router.lower())
        # Precedence: explicit argument > GATEWAY_BASE_URL > router default >
        # the generic fallback port. An unset GATEWAY_BASE_URL must not mask a
        # known router's default port, but an explicit URL always wins.
        resolved_base_url = (
            base_url
            or settings.gateway_base_url
            or (defaults.base_url if defaults else _FALLBACK_BASE_URL)
        )
        resolved_api_key = api_key if api_key is not None else settings.gateway_api_key
        return cls(
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            timeout_seconds=settings.gateway_timeout_seconds,
            max_retries=settings.gateway_max_retries,
            prefix_model_with_provider=defaults.prefix_model_with_provider if defaults else False,
            router_name=defaults.display_name if defaults else router,
            transport=transport,
        )

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        extra_headers: dict[str, str] | None = None,
        prefix_model_with_provider: bool = False,
        router_name: str | None = None,
        transport: AsyncTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            extra_headers=extra_headers,
            prefix_model_with_provider=prefix_model_with_provider,
            transport=transport,
        )
        self._router_name = router_name

    @property
    def router_name(self) -> str | None:
        """The concrete router this adapter was built for, if named."""

        return self._router_name


class _RouterDefaults:
    """Per-router addressing conventions for known third-party routers."""

    __slots__ = ("base_url", "display_name", "prefix_model_with_provider")

    def __init__(self, *, base_url: str, display_name: str, prefix_model_with_provider: bool = False) -> None:
        self.base_url = base_url
        self.display_name = display_name
        self.prefix_model_with_provider = prefix_model_with_provider


_FALLBACK_BASE_URL = "http://localhost:4000"

_ROUTER_DEFAULTS: dict[str, _RouterDefaults] = {
    "litellm": _RouterDefaults(base_url="http://localhost:4000", display_name="LiteLLM"),
    "9router": _RouterDefaults(base_url="http://localhost:3613", display_name="9Router"),
    "oneapi": _RouterDefaults(base_url="http://localhost:3000", display_name="One API"),
    "newapi": _RouterDefaults(base_url="http://localhost:3000", display_name="New API"),
    "freellmapi": _RouterDefaults(base_url="http://localhost:8080", display_name="FreeLLMAPI"),
}
