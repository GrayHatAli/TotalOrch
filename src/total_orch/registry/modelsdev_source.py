"""models.dev catalog ingestion (MVP-0303).

models.dev supplies provider-agnostic base metadata: explicit capability
flags, context limits, and per-provider pricing. The ingestor tolerates both a
flat list of model records and a mapping of provider sections containing a
``models`` list, and only claims a capability when the catalog states it.

Pricing is emitted as endpoint observations keyed by the provider slug
models.dev prices; those observations only attach to a registry endpoint when
a discovered connection matches the same provider and model name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .http import CatalogResponse, CatalogTransport, default_transport
from .identity import IdentityRules, publisher_of, split_variant_id
from .observations import (
    CatalogBatch,
    EndpointObservation,
    EndpointRef,
    FieldValue,
    VariantObservation,
    claimed,
)

ALLOWED_EFFORTS = frozenset({"low", "medium", "high", "ultra"})


class ModelsDevCatalogSource:
    """Reads the public models.dev model catalog."""

    source = "models.dev"

    def __init__(
        self,
        base_url: str,
        rules: IdentityRules,
        *,
        transport: CatalogTransport | None = None,
        path: str = "/models.json",
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._rules = rules
        self._transport = transport or default_transport()
        self._path = path
        self._timeout = timeout

    async def fetch(self) -> CatalogBatch:
        response = await self._transport.get(
            f"{self._base_url}{self._path}",
            headers={"Accept": "application/json"},
            timeout=self._timeout,
        )
        _raise_for_status(response)
        return self._batch_from_payload(response.data)

    def _batch_from_payload(self, payload: Any) -> CatalogBatch:
        batch = CatalogBatch()
        for record in _records(payload):
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
        # models.dev ids name the publisher explicitly (``openai/gpt-4o``); the
        # prefix is kept so the same canonical variant groups with OpenRouter
        # and OmniRoute observations of that model. The ``provider`` field is
        # display metadata only and never used to guess a publisher.
        identity = self._rules.resolve(None, model_id)
        publisher = identity.publisher or publisher_of(identity.family_id)

        batch = CatalogBatch()
        fields: dict[str, FieldValue] = {}
        display_name = _optional_string(record.get("name"))
        description = _optional_string(record.get("description"))
        if display_name is not None:
            fields["display_name"] = claimed(display_name, self.source)
        if description is not None:
            fields["description"] = claimed(description, self.source)

        context = _resolve_number(record.get("context"), publisher)
        max_output = _resolve_number(record.get("max_output"), publisher)
        if context is not None:
            fields["context_window"] = claimed(int(context), self.source)
        if max_output is not None:
            fields["max_output_tokens"] = claimed(int(max_output), self.source)

        for field_name, value in (
            ("cap.tool_calling", _flag(record.get("tool_call"))),
            ("cap.structured_output", _flag(record.get("structured_output"))),
            ("cap.reasoning", _flag(record.get("reasoning"))),
            ("cap.vision", _flag(record.get("attachment"))),
        ):
            if value is not None:
                fields[field_name] = claimed(value, self.source)
        efforts = _efforts(record.get("reasoning"))
        if efforts is not None:
            fields["cap.available_efforts"] = claimed(efforts, self.source)

        batch.variants.append(
            VariantObservation(source=self.source, fields=fields, variant_id=identity.variant_id)
        )
        batch.extend(_pricing_observations(record, identity.variant_id, publisher, self.source))
        return batch


def _pricing_observations(
    record: Mapping[str, Any],
    variant_id: str,
    publisher: str | None,
    source: str,
) -> CatalogBatch:
    batch = CatalogBatch()
    pricing = record.get("pricing")
    if not isinstance(pricing, Mapping):
        return batch
    _family_id, variant_name = split_variant_id(variant_id)

    sections: list[tuple[str, Mapping[str, Any]]] = []
    if _is_price_set(pricing):
        sections.append((publisher or variant_name, pricing))
    else:
        for provider_key, prices in pricing.items():
            if isinstance(prices, Mapping) and _is_price_set(prices):
                sections.append((str(provider_key), prices))

    for provider_key, prices in sections:
        input_cost = _money(prices.get("prompt"))
        output_cost = _money(prices.get("completion"))
        fields: dict[str, FieldValue] = {}
        if input_cost is not None:
            fields["input_cost_per_million"] = claimed(input_cost, source)
        if output_cost is not None:
            fields["output_cost_per_million"] = claimed(output_cost, source)
        if not fields:
            continue
        provider_model_name = variant_name
        batch.endpoints.append(
            EndpointObservation(
                source=source,
                fields=fields,
                endpoint=EndpointRef(
                    provider=provider_key,
                    provider_model_name=provider_model_name,
                    model_variant_id=variant_id,
                ),
            )
        )
    return batch


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    return None


def _efforts(value: Any) -> tuple[str, ...] | None:
    raw: Any = None
    if isinstance(value, Mapping):
        raw = value.get("efforts") or value.get("available_efforts")
    elif isinstance(value, (list, tuple)):
        raw = value
    if not isinstance(raw, (list, tuple)):
        return None
    allowed = tuple(dict.fromkeys(str(item) for item in raw if str(item) in ALLOWED_EFFORTS))
    return allowed or None


def _resolve_number(value: Any, provider_key: str | None) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if isinstance(value, Mapping):
        for key in (provider_key, "default", "prompt", "completion"):
            if key in value:
                return _resolve_number(value[key], provider_key)
        for item in value.values():
            resolved = _resolve_number(item, provider_key)
            if resolved is not None:
                return resolved
    return None


def _money(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _is_price_set(mapping: Mapping[str, Any]) -> bool:
    return any(key in mapping for key in ("prompt", "completion"))


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        models = payload.get("models")
        if isinstance(models, list):
            return models
        flattened: list[Any] = []
        for section in payload.values():
            if isinstance(section, Mapping) and isinstance(section.get("models"), list):
                flattened.extend(section["models"])
        return flattened
    return []


def _raise_for_status(response: CatalogResponse) -> None:
    if response.status_code >= 400:
        raise RuntimeError(f"models.dev returned HTTP {response.status_code}")
