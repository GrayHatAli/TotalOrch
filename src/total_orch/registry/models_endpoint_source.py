"""Gateway-independent catalog ingestion from the standard models endpoint."""

from __future__ import annotations

from collections.abc import Sequence

from total_orch.domain import ProviderConnectionRef
from total_orch.gateway import GatewayAdapter, GatewayModel

from .gateway_source import GatewayCatalogSource
from .identity import IdentityRules
from .observations import CatalogBatch


class ModelsEndpointCatalogSource(GatewayCatalogSource):
    """Build registry observations from ``GatewayAdapter.list_models()`` only.

    This source deliberately avoids connection-management APIs. It works with
    OmniRoute and generic OpenAI-compatible routers when only ``GET /v1/models``
    is available, so catalog sync does not depend on a gateway management key.
    """

    source = "models_endpoint"

    def __init__(
        self,
        adapter: GatewayAdapter,
        rules: IdentityRules,
        *,
        source: str | None = None,
    ) -> None:
        super().__init__(adapter, rules, source=source)

    async def fetch(self) -> CatalogBatch:
        batch = CatalogBatch()
        models: Sequence[GatewayModel] = await self._adapter.list_models()
        for model in models:
            provider = model.provider or _provider_from_model_id(model.id)
            connection = ProviderConnectionRef(id=provider, provider=provider, label=provider)
            observation = self._observation(connection, model)
            if observation is not None:
                variant, endpoint = observation
                batch.variants.append(variant)
                batch.endpoints.append(endpoint)
        return batch


def _provider_from_model_id(model_id: str) -> str:
    provider, separator, _model_name = model_id.partition("/")
    return provider if separator and provider else "default"
