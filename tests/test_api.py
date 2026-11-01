import json

from total_orch.api import TotalOrchApplication
from total_orch.config import Settings
from total_orch.domain import ProviderConnectionRef
from total_orch.gateway import FakeGatewayAdapter, GatewayCompletion
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
