"""Live-sync diagnostics distinguish catalog, auth, and provider failures."""

from __future__ import annotations

import asyncio

from total_orch.gateway import GatewayError
from total_orch.registry import IdentityRules, ModelRegistryService, ModelRegistryStore


class _FailingSource:
    source = "failing"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fetch(self):  # type: ignore[no-untyped-def]
        raise self.error


def _summary(error: Exception) -> object:
    store = ModelRegistryStore(":memory:")
    store.initialize()
    service = ModelRegistryService(store, IdentityRules(), sources=(_FailingSource(error),))
    return asyncio.run(service.sync())


def test_authentication_catalog_and_provider_failures_are_classified() -> None:
    auth = _summary(GatewayError("unauthorized", "bad key", retryable=False, status_code=401))
    catalog = _summary(GatewayError("invalid_gateway_response", "bad body", retryable=False, status_code=200))
    provider = _summary(GatewayError("provider_unavailable", "down", retryable=True, status_code=503))

    assert auth.succeeded is False  # type: ignore[attr-defined]
    assert auth.sources[0].error_kind == "authentication"  # type: ignore[attr-defined]
    assert catalog.sources[0].error_kind == "catalog"  # type: ignore[attr-defined]
    assert provider.sources[0].error_kind == "provider"  # type: ignore[attr-defined]
