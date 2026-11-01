"""Catalog ingestion through gateway adapter contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from total_orch.domain import ModelCapabilities, ProviderConnectionRef
from total_orch.gateway import GatewayAdapter, GatewayModel

from .identity import IdentityRules, strip_qualifier
from .observations import (
    CatalogBatch,
    EndpointObservation,
    EndpointRef,
    FieldValue,
    VariantObservation,
    capability_fields,
    claimed,
)

_CAPABILITY_SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "tool_calling": ("tool_calling", "tool_call", "tools", "function_calling"),
    "reasoning": ("reasoning",),
    "structured_output": ("structured_output", "structured-output", "json_mode", "json_schema"),
    "vision": ("vision", "image_input", "attachment"),
    "multimodal": ("multimodal",),
    "streaming": ("streaming",),
}


def _bool_claim(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value > 0
    return None


def normalize_capability_flags(raw: Mapping[str, Any]) -> ModelCapabilities:
    """Map gateway-style capability keys onto tri-state normalized flags."""

    kwargs: dict[str, Any] = {}
    efforts: list[str] = []
    for field_name, synonyms in _CAPABILITY_SYNONYMS.items():
        for synonym in synonyms:
            value = _bool_claim(raw.get(synonym))
            if value is not None:
                kwargs[field_name] = value
                break
    for key in ("available_efforts", "reasoning_efforts", "efforts"):
        raw_efforts = raw.get(key)
        if isinstance(raw_efforts, (list, tuple)):
            efforts.extend(str(effort) for effort in raw_efforts if isinstance(effort, str))
    if efforts:
        kwargs["available_efforts"] = tuple(dict.fromkeys(efforts))
    return ModelCapabilities(**kwargs)


class GatewayCatalogSource:
    """Read configured connections and models through ``GatewayAdapter``."""

    source = "gateway"

    def __init__(self, adapter: GatewayAdapter, rules: IdentityRules, *, source: str | None = None) -> None:
        self._adapter = adapter
        self._rules = rules
        if source is not None:
            self.source = source

    async def fetch(self) -> CatalogBatch:
        batch = CatalogBatch()
        connections: Sequence[ProviderConnectionRef] = await self._adapter.list_connections()
        for connection in connections:
            models = await self._adapter.list_provider_models(connection.provider)
            for model in models:
                observation = self._observation(connection, model)
                if observation is not None:
                    variant, endpoint = observation
                    batch.variants.append(variant)
                    batch.endpoints.append(endpoint)
        return batch

    def _observation(
        self,
        connection: ProviderConnectionRef,
        model: GatewayModel,
    ) -> tuple[VariantObservation, EndpointObservation] | None:
        model_id = (model.id or "").strip()
        if not model_id:
            return None
        provider_model_name = strip_qualifier(connection.provider, model_id)
        identity = self._rules.resolve(connection.provider, provider_model_name)

        capabilities = normalize_capability_flags(model.capabilities)
        variant_fields: dict[str, FieldValue] = {}
        if model.context_window is not None and model.context_window > 0:
            variant_fields["context_window"] = claimed(model.context_window, self.source)

        capabilities_claims = capability_fields(capabilities, self.source)
        endpoint_ref = EndpointRef(
            provider=connection.provider,
            provider_model_name=provider_model_name,
            model_variant_id=identity.variant_id,
            connection=connection,
            source_record_id=model_id,
        )
        return (
            VariantObservation(source=self.source, fields=variant_fields, variant_id=identity.variant_id),
            EndpointObservation(source=self.source, fields=capabilities_claims, endpoint=endpoint_ref),
        )
