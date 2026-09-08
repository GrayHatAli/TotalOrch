"""OmniRoute implementation of the GatewayAdapter contract.

OmniRoute speaks the shared OpenAI-compatible surface for execution; only
this module additionally knows its management API for connection
registration, per-provider discovery, validation, and health checks. The
rest of the platform sees gateway-neutral records and normalized errors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from total_orch.config import Settings
from total_orch.domain import ProviderConnectionRef

from ._openai import (
    AsyncTransport,
    HttpResponse,
    OpenAICompatGatewayAdapter,
    is_retryable_status,
    items_from_payload,
    optional_string,
)
from .errors import GatewayError
from .models import GatewayModel

__all__ = ["AsyncTransport", "HttpResponse", "OmniRouteAdapter"]


class OmniRouteAdapter(OpenAICompatGatewayAdapter):
    """Adapter for OmniRoute management endpoints and OpenAI-compatible execution."""

    gateway_name = "OmniRoute"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: AsyncTransport | None = None,
        connections_path: str = "/api/providers",
        provider_models_path_template: str = "/api/providers/{provider}/models",
    ) -> None:
        self.settings = settings or Settings.from_env()
        super().__init__(
            base_url=self.settings.omniroute_base_url,
            api_key=self.settings.omniroute_api_key,
            timeout_seconds=self.settings.omniroute_timeout_seconds,
            max_retries=self.settings.omniroute_max_retries,
            prefix_model_with_provider=True,
            transport=transport,
        )
        self._connections_path = connections_path
        self._provider_models_path_template = provider_models_path_template

    def _headers(self, *, management: bool = False) -> dict[str, str]:
        api_key = self.settings.omniroute_management_key if management else self.settings.omniroute_api_key
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _management_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            response = await self._transport.request(
                method,
                self._url(path),
                headers=self._headers(management=True),
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
                retryable=is_retryable_status(response.status_code),
                status_code=response.status_code,
            )
        return response

    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        response = await self._management_request("GET", self._connections_path)
        records = items_from_payload(response.data, "connections", "providers", "data")
        result: list[ProviderConnectionRef] = []
        for record in records:
            provider = str(record.get("provider") or record.get("name") or record.get("id") or "unknown")
            connection_id = str(record.get("id") or record.get("connection_id") or provider)
            result.append(
                ProviderConnectionRef(
                    id=connection_id,
                    provider=provider,
                    label=optional_string(record.get("label") or record.get("display_name")),
                    account_ref=optional_string(record.get("account_ref") or record.get("account")),
                )
            )
        return tuple(result)

    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        path = self._provider_models_path_template.format(provider=quote(provider, safe=""))
        response = await self._management_request("GET", path)
        records = items_from_payload(response.data, "data", "models", "providers")
        return tuple(self._model_from_record(record, default_provider=provider) for record in records)

    async def validate_connection(self, connection_id: str) -> bool:
        path = f"/api/providers/{quote(connection_id, safe='')}/test"
        response = await self._management_request("POST", path, body={})
        if isinstance(response.data, Mapping):
            value = response.data.get("valid", response.data.get("healthy", response.data.get("ok")))
            if isinstance(value, bool):
                return value
        return True

    async def health_check(self, provider: str, model: str | None = None) -> bool:
        path = f"/api/providers/{quote(provider, safe='')}/health"
        if model:
            path += f"?model={quote(model, safe='')}"
        response = await self._management_request("GET", path)
        if isinstance(response.data, Mapping):
            value = response.data.get("healthy", response.data.get("ok", response.data.get("status")))
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in {"ok", "healthy", "ready", "up"}
        return True
