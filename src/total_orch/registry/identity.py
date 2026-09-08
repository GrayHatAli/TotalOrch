"""Deterministic canonical identity resolution.

The registry groups provider-specific model names under one canonical
Family -> Variant identity. Identity is resolved from explicit alias rules so
it never depends on which catalog source happened to be ingested first:

- A model id such as ``openai/gpt-4o`` names the publisher explicitly.
- A connection provider prefix (``provider-a/luna-5.6-pro``) is stripped and
  the remaining name is resolved.
- ``IdentityRules`` carry optional overrides that map a model name (or a
  specific provider/model pair) onto one canonical variant id such as
  ``openai/luna:luna-5.6-pro``. Unknown names fall back to a deterministic
  heuristic and never guess a publisher.

Canonical ids:

- Family id: ``{publisher}/{family_name}`` or just ``{family_name}`` when the
  publisher is not known, e.g. ``openai/luna``.
- Variant id: ``{family_id}:{variant_name}``, e.g. ``openai/luna:luna-5.6-pro``.
- Endpoint id: ``{provider}/{provider_model_name}``, where
  ``provider_model_name`` is the model name the connection's catalog uses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# Publishers that appear as the leading segment of prefixed model ids. A
# prefix on this list names the model publisher, not a serving provider.
KNOWN_PUBLISHERS = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "google-gemini",
        "meta",
        "meta-llama",
        "mistral",
        "mistralai",
        "deepseek",
        "x-ai",
        "cohere",
        "amazon",
        "ai21",
        "qwen",
        "alibaba",
        "nvidia",
        "microsoft",
        "ibm",
        "databricks",
        "together",
        "perplexity",
    }
)

_DATE_SUFFIX = re.compile(r"[-_](\d{4}-\d{2}-\d{2}|\d{6,8})$")


def strip_qualifier(provider: str | None, model_id: str) -> str:
    """Return the model name without a serving-provider qualifier prefix.

    Publisher prefixes (``openai/gpt-4o``) are intentionally preserved: they
    describe the model, not the serving connection.
    """

    head, sep, tail = model_id.partition("/")
    if sep and provider is not None and head == provider:
        return tail
    return model_id


def strip_trailing_version(model_name: str) -> str:
    """Remove a trailing serving version such as ``-2024-08-06`` or ``-20240806``."""

    stripped = _DATE_SUFFIX.sub("", model_name)
    return stripped if stripped != model_name else model_name


def split_variant_id(variant_id: str) -> tuple[str, str]:
    """Split ``family:variant`` into its two components."""

    family, sep, variant = variant_id.partition(":")
    if not sep or not family or not variant:
        raise ValueError(f"invalid canonical variant id: {variant_id!r}")
    return family, variant


def variant_id(family_id: str, variant_name: str) -> str:
    return f"{family_id}:{variant_name}"


def endpoint_id(provider: str, provider_model_name: str) -> str:
    return f"{provider}/{provider_model_name}"


def split_endpoint_id(value: str) -> tuple[str, str]:
    provider, sep, model_name = value.partition("/")
    if not sep or not provider or not model_name:
        raise ValueError(f"invalid endpoint id: {value!r}")
    return provider, model_name


def publisher_of(family_id: str) -> str | None:
    head, sep, _ = family_id.partition("/")
    return head if sep else None


def family_name_of(family_id: str) -> str:
    _, sep, tail = family_id.partition("/")
    return tail if sep else family_id


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    publisher: str | None
    family_name: str
    variant_name: str

    @classmethod
    def from_variant_id(cls, value: str) -> CanonicalIdentity:
        family, variant = split_variant_id(value)
        return cls(publisher=publisher_of(family), family_name=family_name_of(family), variant_name=variant)

    @property
    def family_id(self) -> str:
        if self.publisher:
            return f"{self.publisher}/{self.family_name}"
        return self.family_name

    @property
    def variant_id(self) -> str:
        return variant_id(self.family_id, self.variant_name)

    def __str__(self) -> str:
        return self.variant_id


@dataclass(frozen=True, slots=True)
class IdentityRules:
    """Alias rules used to resolve provider-specific names onto canonical ids.

    ``name_overrides`` maps a model name to a canonical variant id
    (``"luna-5.6-pro": "openai/luna:luna-5.6-pro"``). It applies regardless of
    the serving provider, so two Providers serving the same model group
    together. ``provider_name_overrides`` maps a specific ``(provider, name)``
    pair and wins over the name-level table.
    """

    publishers: frozenset[str] = field(default_factory=lambda: KNOWN_PUBLISHERS)
    name_overrides: Mapping[str, str] = field(default_factory=dict)
    provider_name_overrides: Mapping[tuple[str, str], str] = field(default_factory=dict)

    def resolve(self, provider: str | None, model_name: str) -> CanonicalIdentity:
        name = strip_qualifier(provider, model_name.strip())
        base = strip_trailing_version(name)

        override = self._lookup(provider, name)
        if override is None and base != name:
            override = self._lookup(provider, base)
        if override is not None:
            return CanonicalIdentity.from_variant_id(override)

        prefix, sep, remainder = base.partition("/")
        if sep and prefix in self.publishers:
            # ``openai/gpt-4o`` names its publisher explicitly. The family is
            # ``openai/gpt-4o`` and the variant inside it is ``gpt-4o``.
            publisher, family_name, variant_name = prefix, remainder, remainder
        elif not sep and provider is not None and provider in self.publishers:
            # A connection named after its own publisher (OpenAI serving
            # ``gpt-4o``) identifies the publisher by connection identity, not
            # by guessing from the model name.
            publisher, family_name, variant_name = provider, base, base
        else:
            # Without an override the family and the variant share the base
            # name (e.g. ``luna-5.6-pro``); deployments add name overrides when
            # a series needs to group several variants under one family
            # (e.g. ``luna``).
            publisher, family_name, variant_name = None, base, base
        return CanonicalIdentity(publisher=publisher, family_name=family_name, variant_name=variant_name)

    def _lookup(self, provider: str | None, name: str) -> str | None:
        if provider is not None:
            direct = self.provider_name_overrides.get((provider, name))
            if direct is not None:
                return direct
        return self.name_overrides.get(name)
