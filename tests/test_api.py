import json

import pytest

from total_orch.api import TotalOrchApplication
from total_orch.config import Settings
from total_orch.domain import (
    ModelCapabilities,
    ModelFamily,
    ModelVariant,
    ProviderConnectionRef,
    ProviderEndpoint,
)
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


class _RoleGateway(FakeGatewayAdapter):
    """Answer each workflow role with valid structured JSON."""

    def __init__(self) -> None:
        super().__init__(connections=(ProviderConnectionRef(id="openai-main", provider="openai"),))
        self.calls: list[object] = []

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        system = request.messages[0].content
        if "the planner agent" in system:
            text = '{"summary": "plan", "steps": [], "risks": []}'
        elif "the reviewer agent" in system:
            text = '{"approved": true, "summary": "ok", "issues": []}'
        else:
            text = '{"summary": "done", "changes": [], "diff": ""}'
        return GatewayCompletion(
            request_id="req",
            provider=request.provider,
            model=request.model,
            output_text=text,
            finish_reason="stop",
        )


def task_application(rate_limit_per_minute: int = 1_000) -> TotalOrchApplication:
    store = ModelRegistryStore(":memory:")
    store.initialize()
    store.upsert_family(ModelFamily(id="openai/gpt", publisher="openai"), {}, "test")
    store.upsert_variant(
        ModelVariant(
            id="openai/gpt:gpt-test",
            family_id="openai/gpt",
            name="gpt-test",
            context_window=128_000,
            capabilities=ModelCapabilities(
                tool_calling=True,
                reasoning=True,
                streaming=True,
                structured_output=True,
            ),
        ),
        {},
        "test",
    )
    store.upsert_endpoint(
        ProviderEndpoint(
            id="openai/gpt-test",
            provider="openai",
            provider_model_name="gpt-test",
            model_variant_id="openai/gpt:gpt-test",
            connection=ProviderConnectionRef(id="openai-main", provider="openai"),
            health=0.9,
            latency_p95_ms=100.0,
            capabilities=ModelCapabilities(
                tool_calling=True,
                reasoning=True,
                streaming=True,
                structured_output=True,
            ),
        ),
        {},
        "test",
    )
    settings = Settings(
        environment="test",
        total_orch_api_key="totalorch-test",
        workflow_max_attempts=1,
        rate_limit_per_minute=rate_limit_per_minute,
    )
    return TotalOrchApplication(
        settings,
        ModelRegistryService(store, IdentityRules()),
        _RoleGateway(),
    )


def test_provider_export_is_redacted_and_import_requires_confirmation() -> None:
    app = application()
    headers = {"Authorization": "Bearer totalorch-test"}
    app.handle(
        "POST",
        "/v1/providers/connections",
        headers=headers,
        body=json.dumps(
            {
                "id": "openai-main",
                "provider": "openai",
                "auth_method": "api_key",
                "secret_ref": "env://OPENAI_API_KEY",
            }
        ).encode(),
    )

    exported = app.handle("GET", "/v1/providers/export", headers=headers)
    rejected = app.handle(
        "POST",
        "/v1/providers/import",
        headers=headers,
        body=json.dumps({"data": [{"id": "imported", "provider": "openai", "auth_method": "api_key", "secret_ref": "env://X"}]}).encode(),
    )
    accepted = app.handle(
        "POST",
        "/v1/providers/import",
        headers=headers,
        body=json.dumps(
            {
                "confirm": True,
                "data": [{"id": "imported", "provider": "openai", "auth_method": "api_key", "secret_ref": "env://X"}],
            }
        ).encode(),
    )

    assert exported.status_code == 200
    assert exported.body["credentials_included"] is False
    assert "secret_ref" not in json.dumps(exported.body)
    assert rejected.status_code == 400
    assert accepted.status_code == 201
    assert accepted.body["imported"] == 1


def test_pools_profiles_and_route_preview_use_the_registry() -> None:
    app = task_application()
    headers = {"Authorization": "Bearer totalorch-test"}

    pools = app.handle("GET", "/v1/models/pools", headers=headers)
    profiles = app.handle("GET", "/v1/agents/profiles", headers=headers)
    preview = app.handle(
        "POST",
        "/v1/routing/preview",
        headers=headers,
        body=json.dumps({"agent_profile_id": "developer-default", "task_type": "feature"}).encode(),
    )

    assert pools.status_code == 200
    assert pools.body["data"]
    assert any(profile["id"] == "developer-default" for profile in profiles.body["profiles"])
    assert preview.status_code == 200
    assert preview.body["selected_endpoint_id"] == "openai/gpt-test"
    assert preview.body["explanation"]


def test_task_submission_runs_in_the_background_and_exposes_status_and_events() -> None:
    app = task_application()
    headers = {"Authorization": "Bearer totalorch-test"}

    submitted = app.handle(
        "POST",
        "/v1/tasks",
        headers=headers,
        body=json.dumps({"request": "Add a health endpoint", "run_tests": False}).encode(),
    )
    workflow_id = submitted.body["data"]["workflow_id"]
    finished = app._workflow_service.wait(workflow_id, timeout=5)
    status = app.handle("GET", f"/v1/tasks/{workflow_id}", headers=headers)
    events = app.handle("GET", f"/v1/tasks/{workflow_id}/events", headers=headers)

    assert submitted.status_code == 202
    assert finished is not None
    assert status.status_code == 200
    assert status.body["data"]["status"] == "completed"
    assert any(event["event_type"] == "workflow.completed" for event in events.body["data"])


def test_health_reports_startup_checks_and_rate_limit_rejects_bursts() -> None:
    app = task_application(rate_limit_per_minute=1)
    headers = {"Authorization": "Bearer totalorch-test"}

    health = app.handle("GET", "/health")
    first = app.handle("GET", "/v1/models", headers=headers, client="10.0.0.1")
    second = app.handle("GET", "/v1/models", headers=headers, client="10.0.0.1")

    assert health.status_code == 200
    assert any(check["name"] == "gateway" for check in health.body["checks"])
    assert first.status_code == 200
    assert second.status_code == 429


def test_manual_registry_override_wins_over_catalog_metadata() -> None:
    app = task_application()
    headers = {"Authorization": "Bearer totalorch-test"}

    created = app.handle(
        "POST",
        "/v1/registry/overrides",
        headers=headers,
        body=json.dumps(
            {
                "kind": "variant",
                "id": "openai/gpt:gpt-test",
                "field": "display_name",
                "value": "Manual Name",
            }
        ).encode(),
    )
    models = app.handle("GET", "/v1/models", headers=headers)
    listed = app.handle("GET", "/v1/registry/overrides", headers=headers)
    deleted = app.handle(
        "DELETE",
        "/v1/registry/overrides/variant/openai%2Fgpt%3Agpt-test/display_name",
        headers=headers,
    )

    assert created.status_code == 201
    assert models.body["data"][0]["display_name"] == "Manual Name"
    assert listed.body["data"][0]["field"] == "display_name"
    assert deleted.status_code == 204
