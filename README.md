# TotalOrch

The platform is the control plane above a managed execution gateway. It owns
model intelligence, default agent profiles, semantic routing, workflow
coordination, and explainability. The gateway owns provider adapters,
credentials, protocol translation, and operational endpoint fallback.

TotalOrch is **not tied to one gateway product**. OmniRoute is the default
managed gateway, and any OpenAI-compatible LLM router — LiteLLM, 9Router,
One API / New API, FreeLLMAPI, and similar proxies — works through the same
`GatewayAdapter` boundary.

The current implementation covers the foundation, domain contracts, the
Gateway Adapter boundary, the Model Intelligence Registry, default Agent
Profiles with automatic Model Pool generation, and the explainable Router
from MVP phases 0–5.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python -m pytest
```

Secrets are intentionally kept outside the Model Registry and event metadata.

With a local `tests/settings.json` copied from a Claude Code setup, run the
manual live smoke test. It is intentionally excluded from the normal pytest
suite because it calls real Providers:

```bash
uv run --with 'pydantic>=2.7,<3' --with 'httpx>=0.27,<1' \
  python tests/live_omniroute_smoke.py
```

## Gateway compatibility

The gateway is selected with `GATEWAY_TYPE`; every adapter implements the
same `GatewayAdapter` contract, so the registry, profiles, and router do not
change when the gateway changes.

| `GATEWAY_TYPE` | Gateway | Notes |
| :--- | :--- | :--- |
| `omniroute` (default) | OmniRoute | Full management API: connections, per-provider discovery, validation, health |
| `litellm` | LiteLLM proxy | OpenAI-compatible; default port 4000 |
| `9router` | 9Router | OpenAI-compatible; default port 3613 |
| `oneapi` / `newapi` | One API / New API | OpenAI-compatible; default port 3000 |
| `freellmapi` | FreeLLMAPI | OpenAI-compatible |
| any other value | any OpenAI-compatible router | Treated generically |

Generic routers are configured with:

```bash
GATEWAY_TYPE=litellm            # or 9router, oneapi, newapi, freellmapi, ...
GATEWAY_BASE_URL=http://localhost:4000
GATEWAY_API_KEY=sk-...          # optional
GATEWAY_TIMEOUT_SECONDS=120
GATEWAY_MAX_RETRIES=2
```

Precedence for the base URL: explicit constructor argument >
`GATEWAY_BASE_URL` > the known router's default port > the generic fallback.

Behavior notes for generic routers:

- Discovery reads `GET /v1/models`; one Provider connection is derived per
  publisher prefix found in the catalog (bare ids group under `default`).
- Execution sends the catalog model id verbatim — set
  `prefix_model_with_provider=True` (or use the OmniRoute adapter) for
  gateways that route by `provider/model` identifiers.
- Health checks fall back to catalog presence when the router has no health
  API; a router error makes the check report unhealthy rather than guess.
- Registry provenance is labeled per gateway (`omniroute`, `litellm`,
  `9router`, ...), so fields observed through different gateways stay
  distinguishable when several gateways serve the same model.

The default managed deployment remains documented in
[`deployment/README.md`](deployment/README.md): the platform owns the
OmniRoute lifecycle and users do not configure a gateway plugin or
reimplement provider adapters.

## Package boundaries

- `total_orch.domain`: versioned domain contracts.
- `total_orch.gateway`: the gateway-neutral adapter interface, the shared
  OpenAI-compatible base, the fake adapter, the OmniRoute implementation,
  the generic OpenAI-compatible adapter, and the `GATEWAY_TYPE` factory.
- `total_orch.registry`: the local Model Intelligence Registry —
  persistence, catalog ingestion, canonical identity, field-authority
  merging, staleness, and query APIs.
- `total_orch.profiles`: default Agent Profiles and the automatic,
  explainable Model Pool builder with user overrides.
- `total_orch.router`: the explainable Router — hard constraints before
  weighted scoring, policy presets, deterministic tie-breaking, bounded
  exploration, fallback ordering, route preview, and `RouteDecision`
  persistence.
- `total_orch.config`: environment configuration with redacted logging.

LangGraph workflow nodes are intentionally left for the next MVP phase.
