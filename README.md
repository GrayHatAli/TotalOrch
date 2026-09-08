# TotalOrch

The platform is the control plane above a managed OmniRoute gateway. It owns
model intelligence, default agent profiles, semantic routing, workflow
coordination, and explainability. OmniRoute owns provider adapters,
credentials, protocol translation, and operational endpoint fallback.

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

The live adapter reads `OMNIROUTE_BASE_URL`, `OMNIROUTE_API_KEY`, and
`OMNIROUTE_MANAGEMENT_KEY` from the environment. Secrets are intentionally
kept outside the Model Registry and event metadata.

With a local `tests/settings.json` copied from a Claude Code setup, run the
manual live smoke test. It is intentionally excluded from the normal pytest
suite because it calls real Providers:

```bash
uv run --with 'pydantic>=2.7,<3' --with 'httpx>=0.27,<1' \
  python tests/live_omniroute_smoke.py
```

## Managed OmniRoute

The default deployment contract is documented in
[`deployment/README.md`](deployment/README.md). The platform owns the
OmniRoute lifecycle in the managed deployment; users do not configure an
OmniRoute plugin or reimplement provider adapters.

## Package boundaries

- `total_orch.domain`: versioned domain contracts.
- `total_orch.gateway`: the gateway-neutral adapter interface, fake
  adapter, and OmniRoute implementation.
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
