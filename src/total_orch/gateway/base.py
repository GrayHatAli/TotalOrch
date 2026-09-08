"""Stable interface between the Orchestrator and an execution gateway."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from total_orch.domain import ProviderConnectionRef

from .models import GatewayCompletion, GatewayModel, GatewayRequest, GatewayStreamChunk


class GatewayAdapter(ABC):
    """The only execution boundary used by the Orchestrator.

    Provider credentials, provider-specific translation, and operational
    fallback remain behind this interface in OmniRoute.
    """

    @abstractmethod
    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        raise NotImplementedError

    @abstractmethod
    async def list_models(self) -> Sequence[GatewayModel]:
        raise NotImplementedError

    @abstractmethod
    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        raise NotImplementedError

    @abstractmethod
    async def validate_connection(self, connection_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def health_check(self, provider: str, model: str | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        raise NotImplementedError

    @abstractmethod
    def stream(self, request: GatewayRequest) -> AsyncIterator[GatewayStreamChunk]:
        raise NotImplementedError
