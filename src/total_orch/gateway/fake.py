"""Deterministic fake gateway used by contract and routing tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from total_orch.domain import ProviderConnectionRef

from .base import GatewayAdapter
from .errors import GatewayError
from .models import GatewayCompletion, GatewayModel, GatewayRequest, GatewayStreamChunk


@dataclass(frozen=True, slots=True)
class FailureSpec:
    code: str = "provider_unavailable"
    message: str = "simulated provider failure"
    retryable: bool = True
    status_code: int | None = 503


class FakeGatewayAdapter(GatewayAdapter):
    """In-memory adapter with explicit responses and failure injection."""

    def __init__(
        self,
        *,
        connections: Sequence[ProviderConnectionRef] = (),
        models: Sequence[GatewayModel] = (),
    ) -> None:
        self._connections = tuple(connections)
        self._models = tuple(models)
        self._responses: dict[tuple[str, str], GatewayCompletion] = {}
        self._failures: dict[tuple[str, str], FailureSpec] = {}

    def register_response(self, provider: str, model: str, response: GatewayCompletion) -> None:
        self._responses[(provider, model)] = response

    def register_failure(self, provider: str, model: str, failure: FailureSpec | None = None) -> None:
        self._failures[(provider, model)] = failure or FailureSpec()

    async def list_connections(self) -> Sequence[ProviderConnectionRef]:
        return self._connections

    async def list_models(self) -> Sequence[GatewayModel]:
        return self._models

    async def list_provider_models(self, provider: str) -> Sequence[GatewayModel]:
        return tuple(model for model in self._models if model.provider in (None, provider))

    async def validate_connection(self, connection_id: str) -> bool:
        return any(connection.id == connection_id for connection in self._connections)

    async def health_check(self, provider: str, model: str | None = None) -> bool:
        if model is None:
            return any(connection.provider == provider for connection in self._connections)
        return (provider, model) not in self._failures

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        key = (request.provider, request.model.removeprefix(f"{request.provider}/"))
        if key in self._failures:
            failure = self._failures[key]
            raise GatewayError(
                failure.code,
                failure.message,
                retryable=failure.retryable,
                status_code=failure.status_code,
                provider=request.provider,
            )
        response = self._responses.get(key)
        if response is None:
            raise GatewayError(
                "model_not_configured",
                f"no fake response registered for {request.target_model}",
                retryable=False,
                status_code=404,
                provider=request.provider,
            )
        return response

    async def stream(self, request: GatewayRequest) -> AsyncIterator[GatewayStreamChunk]:
        response = await self.complete(request)
        words = response.output_text.split(" ")
        for index, word in enumerate(words):
            yield GatewayStreamChunk(
                request_id=response.request_id,
                provider=response.provider,
                model=response.model,
                delta=word if index == 0 else f" {word}",
                metadata={"source": "fake"},
            )
        yield GatewayStreamChunk(
            request_id=response.request_id,
            provider=response.provider,
            model=response.model,
            finish_reason=response.finish_reason or "stop",
            usage=response.usage,
            metadata=response.metadata,
        )
