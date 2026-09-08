"""Shared fixtures for the total_orch test suite."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from total_orch.registry.http import CatalogResponse, CatalogTransport


class StubCatalogTransport:
    """In-memory catalog transport that records its requests."""

    def __init__(
        self,
        data: Any = None,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> CatalogResponse:
        self.calls.append((url, dict(headers)))
        return CatalogResponse(self.status_code, self.data, self.headers)


@pytest.fixture
def transport_factory() -> type[StubCatalogTransport]:
    return StubCatalogTransport
