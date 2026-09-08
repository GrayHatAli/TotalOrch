"""OpenRouter Models API ingestion (MVP-0304).

OpenRouter contributes live pricing and context/output limits plus explicit
modality and tooling signals. Only signals OpenRouter states explicitly are
claimed; everything else stays unknown. Pricing lands on an endpoint
observation named after the ``openrouter`` provider slug so it attaches to an
OpenRouter aggregator connection when one is configured.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .http import CatalogResponse, CatalogTransport, default_transport
from .identity import IdentityRules
from .observations import (
    CatalogBatch,
    EndpointObservation,
    EndpointRef,
    FieldValue,
    VariantObservation,
    claimed,
)

ALLOWED_EFFORTS = frozenset({"low", "medium", "high", "ultra"})


class OpenRouterCatalogSource:
    """Reads the public OpenRouter model catalog."""

    source = "openrouter"

    def __init__(
        self,
        base_url: str,
        rules: IdentityRules,
        *,
        transport: CatalogTransport | None = None,
        path: str = "/v1/models",
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._rules = rules
        self._transport = transport or default_transport()
        self._path = path
        self._timeout = timeout
        self._api_key = api_key

    async def fetch(self) -> CatalogBatch:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = await self._transport.get(
            f"{self._base_url}{self._path}",
            headers=headers,
            timeout=self._timeout,
        )
        _raise_for_status(response)
        return self._batch_from_payload(response.data)

    def _batch_from_payload(self, payload: Any) -> CatalogBatch:
        batch = CatalogBatch()
        records = payload.get("data") if isinstance(payload, Mapping) else payload
        if not isinstance(records, list):
            return batch
        for record in records:
            if not isinstance(record, Mapping):
                continue
            item = self._observation(record)
            if item is not None:
                batch.extend(item)
        return batch

    def _observation(self, record: Mapping[str, Any]) -> CatalogBatch | None:
        model_id = str(record.get("id") or "").strip()
        if not model_id:
            return None
        identity = self._rules.resolve(None, model_id)

        batch = CatalogBatch()
        fields: dict[str, FieldValue] = {}
        name = _optional_string(record.get("name"))
        if name is not None:
            fields["display_name"] = claimed(name, self.source)

        context = record.get("context_length")
        if isinstance(context, (int, float)) and context > 0:
            fields["context_window"] = claimed(int(context), self.source)
        max_output = _max_output(record)
        if max_output is not None:
            fields["max_output_tokens"] = claimed(max_output, self.source)

        raw_architecture = record.get("architecture")
        architecture: Mapping[str, Any] = raw_architecture if isinstance(raw_architecture, Mapping) else {}
        input_modalities = _string_set(architecture.get("input_modalities"))
        output_modalities = _string_set(architecture.get("output_modalities"))
        parameters = _string_set(record.get("supported_parameters"))
        if "image" in input_modalities:
            fields["cap.vision"] = claimed(True, self.source)
        other = (input_modalities | output_modalities) - {"text"}
        if other:
            fields["cap.multimodal"] = claimed(True, self.source)
        if "tools" in parameters:
            fields["cap.tool_calling"] = claimed(True, self.source)
        if "reasoning" in parameters:
            fields["cap.reasoning"] = claimed(True, self.source)
        for token in ("json_schema", "json_mode", "structured outputs", "structured_output"):
            if token in parameters:
                fields["cap.structured_output"] = claimed(True, self.source)
                break
        efforts = _efforts(record.get("reasoning"))
        if efforts is not None:
            fields["cap.available_efforts"] = claimed(efforts, self.source)

        batch.variants.append(
            VariantObservation(source=self.source, fields=fields, variant_id=identity.variant_id)
        )

        pricing = record.get("pricing")
        if isinstance(pricing, Mapping):
            input_cost = _money(pricing.get("prompt"))
            output_cost = _money(pricing.get("completion"))
            price_fields: dict[str, FieldValue] = {}
            if input_cost is not None:
                price_fields["input_cost_per_million"] = claimed(input_cost, self.source)
            if output_cost is not None:
                price_fields["output_cost_per_million"] = claimed(output_cost, self.source)
            if price_fields:
                batch.endpoints.append(
                    EndpointObservation(
                        source=self.source,
                        fields=price_fields,
                        endpoint=EndpointRef(
                            provider="openrouter",
                            provider_model_name=model_id,
                            model_variant_id=identity.variant_id,
                        ),
                    )
                )
        return batch


def _max_output(record: Mapping[str, Any]) -> int | None:
    value = record.get("max_completion_tokens")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    top = record.get("top_provider")
    if isinstance(top, Mapping):
        value = top.get("max_completion_tokens")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _efforts(value: Any) -> tuple[str, ...] | None:
    raw: Any = value.get("efforts") if isinstance(value, Mapping) else value
    if not isinstance(raw, (list, tuple)):
        return None
    allowed = tuple(
        dict.fromkeys(str(item) for item in raw if isinstance(item, str) and item in ALLOWED_EFFORTS)
    )
    return allowed or None


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple)):
        return set()
    return {str(item) for item in value if isinstance(item, str)}


def _money(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _raise_for_status(response: CatalogResponse) -> None:
    if response.status_code >= 400:
        raise RuntimeError(f"openrouter returned HTTP {response.status_code}")
