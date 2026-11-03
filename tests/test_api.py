import json

import pytest

from total_orch.api import TotalOrchApplication
from total_orch.config import Settings
from total_orch.domain import ProviderConnectionRef
from total_orch.gateway import (
    FakeGatewayAdapter,
    GatewayCompletion,
    GatewayModel,
    OpenAICompatAdapter,
)
from total_orch.registry import (
    IdentityRules,
    ModelRegistryService,
    ModelRegistryStore,
    ModelsEndpointCatalogSource,
)


def application() -> TotalOrchApplication:
    store = ModelRegistryStore(":memory:")
    store.initialize()
    gateway = FakeGatewayAdapter(
        connections=(ProviderConnectionRef(id="openai-main", provider="openai"),),
        models=(GatewayModel(id="gpt-test", provider="openai", capabilities={"tool_calling": True}),),
    )
    gateway.register_response(
        "openai",
        "gpt-test",
        GatewayCompletion(
            request_id="req-1",
            provider="openai",
            model="openai/gpt-test",
            output_text="done",
            finish_reason="stop",
        ),
    )
    rules = IdentityRules()
    return TotalOrchApplication(
        Settings(environment="test", total_orch_api_key="totalorch-test"),
        ModelRegistryService(store, rules, sources=(ModelsEndpointCatalogSource(gateway, rules),)),
        gateway,
    )


def test_health_is_public_but_management_routes_require_total_orch_key() -> None:
    app = application()

    health = app.handle("GET", "/health")
    denied = app.handle("GET", "/v1/providers/connections")

    assert health.status_code == 200
    assert health.body["status"] == "ok"
    assert denied.status_code == 401


def test_total_orch_can_issue_and_revoke_a_harness_api_key() -> None:
    app = application()
    bootstrap = {"Authorization": "Bearer totalorch-test"}
    created = app.handle(
        "POST",
        "/v1/auth/keys",
        headers=bootstrap,
        body=json.dumps({"label": "Harness local"}).encode(),
    )
    token = created.body["data"]["token"]
    issued_headers = {"Authorization": f"Bearer {token}"}

    authorized = app.handle("GET", "/v1/models", headers=issued_headers)
    listed = app.handle("GET", "/v1/auth/keys", headers=bootstrap)
    revoked = app.handle(
        "DELETE",
        f"/v1/auth/keys/{created.body['data']['id']}",
        headers=bootstrap,
    )
    denied_after_revoke = app.handle("GET", "/v1/models", headers=issued_headers)

    assert created.status_code == 201
    assert token.startswith("to_")
    assert authorized.status_code == 200
    assert listed.body["data"][0]["id"] == created.body["data"]["id"]
    assert "token" not in json.dumps(listed.body)
    assert revoked.status_code == 204
    assert denied_after_revoke.status_code == 401


def test_registry_sync_is_exposed_without_using_gateway_management_apis() -> None:
    app = application()
    response = app.handle(
        "POST",
        "/v1/registry/sync",
        headers={"Authorization": "Bearer totalorch-test"},
    )

    assert response.status_code == 200
    assert response.body["succeeded"] is True
    assert response.body["sources"][0]["source"] == "models_endpoint"


def test_connection_api_stores_only_secret_reference_and_redacts_it_from_response() -> None:
    app = application()
    headers = {"Authorization": "Bearer totalorch-test"}
    payload = json.dumps(
        {
            "provider": "openai",
            "label": "Primary",
            "auth_method": "api_key",
            "secret_ref": "env://OPENAI_API_KEY",
        }
    ).encode()

    created = app.handle("POST", "/v1/providers/connections", headers=headers, body=payload)
    listed = app.handle("GET", "/v1/providers/connections", headers=headers)

    assert created.status_code == 201
    assert created.body["data"]["secret_configured"] is True
    assert "secret_ref" not in created.body["data"]
    assert listed.body["data"][0]["secret_configured"] is True

    deleted = app.handle(
        "DELETE",
        f"/v1/providers/connections/{created.body['data']['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204

    raw = json.dumps({"provider": "openai", "api_key": "sk-should-not-enter"}).encode()
    rejected = app.handle("POST", "/v1/providers/connections", headers=headers, body=raw)
    assert rejected.status_code == 400
    assert "sk-should-not-enter" not in json.dumps(rejected.body)


def test_connection_health_updates_last_check_and_success_timestamps() -> None:
    app = application()
    headers = {"Authorization": "Bearer totalorch-test"}
    created = app.handle(
        "POST",
        "/v1/providers/connections",
        headers=headers,
        body=json.dumps({"id": "openai-main", "provider": "openai"}).encode(),
    )

    checked = app.handle("POST", "/v1/providers/connections/openai-main/health", headers=headers)

    assert created.status_code == 201
    assert checked.status_code == 200
    assert checked.body["data"]["healthy"] is True
    assert checked.body["data"]["last_health_check_at"] is not None
    assert checked.body["data"]["last_success_at"] is not None
    assert checked.body["data"]["capability_observations"] == ["tool_calling"]


def test_openai_compatible_completion_facade_uses_gateway_boundary() -> None:
    app = application()
    response = app.handle(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer totalorch-test"},
        body=json.dumps(
            {
                "model": "gpt-test",
                "provider": "openai",
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode(),
    )

    assert response.status_code == 200
    assert response.body["choices"][0]["message"]["content"] == "done"
    assert response.body["object"] == "chat.completion"


@pytest.mark.asyncio
async def test_openai_compatible_stream_emits_sse_chunks_and_done_marker() -> None:
    app = application()
    _payload, request = app._completion_request(
        json.dumps(
            {
                "model": "gpt-test",
                "provider": "openai",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        ).encode()
    )

    events = [event async for event in app._stream_events(request)]

    assert any(b"chat.completion.chunk" in event for event in events)
    assert events[-1] == b"data: [DONE]\n\n"


def test_connection_secret_reference_builds_direct_openai_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    app = application()
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-secret-for-test")
    app.handle(
        "POST",
        "/v1/providers/connections",
        headers={"Authorization": "Bearer totalorch-test"},
        body=json.dumps(
            {
                "id": "direct-openai",
                "provider": "direct-openai",
                "base_url": "http://provider.test",
                "auth_method": "api_key",
                "secret_ref": "env://PROVIDER_API_KEY",
            }
        ).encode(),
    )

    _payload, request = app._completion_request(
        json.dumps(
            {
                "model": "gpt-test",
                "provider": "direct-openai",
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode()
    )
    adapter = app._gateway_for_request(request)

    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter._base_url == "http://provider.test"
    assert adapter._api_key == "provider-secret-for-test"
