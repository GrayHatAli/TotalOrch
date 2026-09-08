"""Minimal HTTP transport for public catalog sources.

Kept registry-local so ``total_orch.registry`` does not depend on gateway
internals. httpx is imported lazily so domain-only imports never require IO
dependencies; tests inject a stub transport instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CatalogResponse:
    status_code: int
    data: Any
    headers: Mapping[str, str]


class CatalogTransport(Protocol):
    async def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> CatalogResponse: ...


class _HttpxCatalogTransport:
    async def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> CatalogResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - exercised in packaging
            raise RuntimeError("httpx is required for live catalog ingestion") from exc
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=dict(headers), timeout=timeout)
            try:
                data = response.json()
            except ValueError:
                data = None
            return CatalogResponse(response.status_code, data, response.headers)
        except Exception as exc:  # pragma: no cover - transport faults surface in live mode
            raise RuntimeError(f"catalog request failed: {exc}") from exc


def default_transport() -> CatalogTransport:
    return _HttpxCatalogTransport()
