"""Environment-backed runtime configuration.

Secrets are intentionally kept in this module and are never copied into model
registry records or event metadata.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


def _redact_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    netloc = f"{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    log_level: str = "INFO"
    # Generic gateway selection. "omniroute" (the default managed gateway)
    # uses the OmniRoute management API; any other value selects the generic
    # OpenAI-compatible adapter (LiteLLM, 9Router, One API / New API,
    # FreeLLMAPI, ...).
    gateway_type: str = "omniroute"
    gateway_base_url: str | None = None
    gateway_api_key: str | None = None
    gateway_timeout_seconds: float = 120.0
    gateway_max_retries: int = 2
    omniroute_base_url: str = "http://localhost:20128"
    omniroute_api_key: str | None = None
    omniroute_management_key: str | None = None
    omniroute_timeout_seconds: float = 120.0
    omniroute_max_retries: int = 2
    registry_database_path: str = "registry.db"
    registry_stale_after_seconds: float = 604800.0
    total_orch_api_key: str | None = None
    total_orch_host: str = "127.0.0.1"
    total_orch_port: int = 8080
    modelsdev_base_url: str = "https://models.dev"
    openrouter_base_url: str = "https://openrouter.ai"
    openrouter_api_key: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.getenv("ORCHESTRATOR_ENV", "development"),
            log_level=os.getenv("ORCHESTRATOR_LOG_LEVEL", "INFO"),
            gateway_type=os.getenv("GATEWAY_TYPE", "omniroute").strip().lower(),
            gateway_base_url=(os.getenv("GATEWAY_BASE_URL") or None)
            and os.getenv("GATEWAY_BASE_URL", "").rstrip("/"),
            gateway_api_key=os.getenv("GATEWAY_API_KEY") or None,
            gateway_timeout_seconds=_float_env("GATEWAY_TIMEOUT_SECONDS", 120.0),
            gateway_max_retries=_int_env("GATEWAY_MAX_RETRIES", 2),
            omniroute_base_url=os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128").rstrip("/"),
            omniroute_api_key=os.getenv("OMNIROUTE_API_KEY") or None,
            omniroute_management_key=os.getenv("OMNIROUTE_MANAGEMENT_KEY") or None,
            omniroute_timeout_seconds=_float_env("OMNIROUTE_TIMEOUT_SECONDS", 120.0),
            omniroute_max_retries=_int_env("OMNIROUTE_MAX_RETRIES", 2),
            registry_database_path=os.getenv("ORCHESTRATOR_REGISTRY_DATABASE_PATH", "registry.db"),
            registry_stale_after_seconds=_float_env("ORCHESTRATOR_REGISTRY_STALE_AFTER_SECONDS", 604800.0),
            total_orch_api_key=os.getenv("TOTAL_ORCH_API_KEY") or None,
            total_orch_host=os.getenv("TOTAL_ORCH_HOST", "127.0.0.1"),
            total_orch_port=_int_env("TOTAL_ORCH_PORT", 8080),
            modelsdev_base_url=os.getenv("MODELSDEV_BASE_URL", "https://models.dev").rstrip("/"),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai").rstrip("/"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        )

    def redacted(self) -> dict[str, object]:
        """Return safe-to-log settings without exposing credentials."""

        return {
            "environment": self.environment,
            "log_level": self.log_level,
            "gateway_type": self.gateway_type,
            "gateway_base_url": _redact_url(self.gateway_base_url),
            "gateway_api_key_configured": self.gateway_api_key is not None,
            "gateway_timeout_seconds": self.gateway_timeout_seconds,
            "gateway_max_retries": self.gateway_max_retries,
            "omniroute_base_url": _redact_url(self.omniroute_base_url),
            "omniroute_api_key_configured": self.omniroute_api_key is not None,
            "omniroute_management_key_configured": self.omniroute_management_key is not None,
            "omniroute_timeout_seconds": self.omniroute_timeout_seconds,
            "omniroute_max_retries": self.omniroute_max_retries,
            "registry_database_path": self.registry_database_path,
            "registry_stale_after_seconds": self.registry_stale_after_seconds,
            "total_orch_api_key_configured": self.total_orch_api_key is not None,
            "total_orch_host": self.total_orch_host,
            "total_orch_port": self.total_orch_port,
            "modelsdev_base_url": _redact_url(self.modelsdev_base_url),
            "openrouter_base_url": _redact_url(self.openrouter_base_url),
            "openrouter_api_key_configured": self.openrouter_api_key is not None,
        }
