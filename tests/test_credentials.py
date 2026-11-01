import pytest
from pydantic import ValidationError

from total_orch.credentials import CredentialError, EnvironmentSecretResolver
from total_orch.domain import ProviderConnection


def test_provider_connection_requires_a_reference_but_never_a_secret() -> None:
    connection = ProviderConnection(
        id="openai-main",
        provider="openai",
        auth_method="api_key",
        secret_ref="env://OPENAI_API_KEY",
    )

    assert connection.secret_ref == "env://OPENAI_API_KEY"
    assert "OPENAI_API_KEY" in connection.model_dump_json()
    assert "sk-" not in connection.model_dump_json()


def test_provider_connection_rejects_missing_or_invalid_secret_reference() -> None:
    with pytest.raises(ValidationError):
        ProviderConnection(id="openai-main", provider="openai", auth_method="api_key")
    with pytest.raises(ValidationError):
        ProviderConnection(
            id="openai-main",
            provider="openai",
            auth_method="api_key",
            secret_ref="literal-secret",
        )


def test_environment_secret_resolver_reads_only_configured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    resolver = EnvironmentSecretResolver()

    assert resolver.resolve("env://OPENAI_API_KEY") == "sk-test-only"
    assert resolver.configured("env://OPENAI_API_KEY") is True
    with pytest.raises(CredentialError):
        resolver.resolve("env://MISSING_API_KEY")
    with pytest.raises(CredentialError):
        resolver.resolve("file:///tmp/secret")
