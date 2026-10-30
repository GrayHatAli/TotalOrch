# Gateway_Comparison.md

## Executive Summary

The TotalOrch needs an execution gateway, not a second orchestration brain. The gateway should normalize provider protocols, manage credentials and connections, execute streaming requests, enforce operational policies, and provide low-level retry, health, quota, and fallback behavior.

Based on the project's current requirements and recent hands-on evaluation, the selected default gateway is **OmniRoute**. OmniRoute runs as a managed internal service or sidecar. End users install and configure the Orchestrator Platform, not OmniRoute separately.

The Orchestrator remains responsible for the product-level intelligence:

- Model Family → Model Variant → Provider Endpoint normalization
- Capability resolution and conflict handling
- Default Planner, Developer/Coder, Reviewer, and Researcher profiles
- Automatic model pools and role-based combos
- Task-aware model selection
- Explainable routing and decision history
- Adaptive policies based on task outcomes

## 1. Decision Summary

| Decision | Selected approach |
| :--- | :--- |
| Default execution gateway | OmniRoute, managed internally |
| Provider adapter ownership | OmniRoute |
| Credential and OAuth ownership | OmniRoute, accessed through our onboarding adapter |
| Model intelligence ownership | Orchestrator |
| Agent pool and role-combo ownership | Orchestrator |
| Final semantic routing ownership | Orchestrator |
| Endpoint retry, health, quota, and operational fallback | OmniRoute |
| User-facing gateway configuration | Our UI/API; OmniRoute management is hidden by default |
| External gateway support | Optional adapter for advanced deployments |

## 2. Candidate Overview

### OmniRoute

OmniRoute is a Node.js/TypeScript gateway focused on multi-provider execution, OpenAI-compatible APIs, provider connections, model discovery, combos, health, quota, resilience, and streaming. Its API exposes provider/model catalogs and dedicated provider routes. See the [OmniRoute API Reference](https://github.com/diegosouzapw/OmniRoute/blob/release/v3.8.50/docs/reference/API_REFERENCE.md), [Provider Reference](https://github.com/diegosouzapw/OmniRoute/blob/release/v3.8.50/docs/reference/PROVIDER_REFERENCE.md), and [Auto-Combo reference](https://github.com/diegosouzapw/OmniRoute/blob/release/v3.8.50/docs/routing/AUTO-COMBO.md).

### LiteLLM

LiteLLM remains a capable generic proxy and a valid alternative gateway adapter. It was previously recommended in this document, but it is no longer the default decision for this project. The project favors OmniRoute for its current provider coverage, coding-tool integrations, operational features, and built-in connection/combination workflows.

### 9Router

9Router is useful as a local-first coding proxy and as a source of ideas for compression and fallback, but it does not provide the desired product-level model registry, default agent profiles, and explainable role-based routing surface.

### One API / New API

One API and its variants are useful for aggregation, quota distribution, and multi-tenant proxy deployments. They are less suitable as the execution substrate for our custom orchestration and model-intelligence layer.

### FreeLLMAPI

FreeLLMAPI is useful for experimentation with free-tier aggregation, but free-tier availability, upstream terms, and reliability are not sufficient as the primary gateway foundation.

## 3. Capability Comparison

| Capability | OmniRoute | LiteLLM | 9Router | One API / New API | FreeLLMAPI |
| :--- | :--- | :--- | :--- | :--- | :--- |
| OpenAI-compatible API | Strong | Strong | Strong | Strong | Strong |
| Multi-provider execution | Strong | Strong | Strong for coding use cases | Strong | Focused on free tiers |
| Provider connections and OAuth | Strong | Strong, deployment-dependent | Focused/local | Strong | Basic |
| Model catalog endpoints | Strong | Strong | Limited | Strong | Basic |
| Streaming and protocol translation | Strong | Strong | Strong | Strong | Supported |
| Tool/function calling | Broad translation support | Broad translation support | Partial/provider-dependent | Supported | Provider-dependent |
| Operational health and resilience | Strong | Strong | Good for local fallback | Good | Basic |
| Built-in model combos | Strong | Strong routing primitives | Routing/fallback focused | Channel-based | Fallback focused |
| Semantic Agent Pools | Not the product's core abstraction | Not the product's core abstraction | Not the product's core abstraction | Not the product's core abstraction | Not the product's core abstraction |
| Explainable role-based routing | Partial internal signals | Requires custom layer | Limited | Requires custom layer | Limited |
| Default agent profiles | No | No | No | No | No |

The last three rows are intentionally Orchestrator responsibilities. No gateway should become the product's Brain.

## 4. OmniRoute Fit and Boundaries

### What OmniRoute provides

- Provider adapter registry and provider-specific execution
- API-key and OAuth connection management
- Provider/model discovery
- OpenAI-compatible `/v1/models`
- Provider-scoped model discovery
- Dedicated provider execution routes
- Streaming and protocol normalization
- Connection tests and runtime health
- Quota, cooldown, circuit-breaker, retry, and fallback behavior
- Persisted and virtual routing combos
- Runtime usage and latency telemetry

### What the Orchestrator must add

- Canonical model identity independent of serving Provider
- Model Family and Model Variant grouping across Providers
- Effective endpoint capabilities and reasoning-effort metadata
- Default agent profiles built automatically after synchronization
- Pools such as `tool_calling`, `openai/luna-family`, `large_context_reasoning`, and `planner-quality`
- Role-aware policies for Planner, Developer/Coder, Reviewer, and Researcher
- A transparent routing decision with score factors and exclusion reasons
- Task-outcome learning and project-specific suitability scores

OmniRoute's `auto/*` and combo features can be used for endpoint-level resilience, but they are not a replacement for the Orchestrator's semantic Pool and Role model. In particular, the Orchestrator must not depend on a user manually understanding OmniRoute's complete strategy/configuration surface.

## 5. Integration Contract

### 5.1 Provider onboarding

```text
Our UI/API
    ↓
Gateway Adapter
    ↓
OmniRoute provider management or OAuth flow
    ↓
OmniRoute Provider Connection
    ↓
Provider validation and model discovery
    ↓
Orchestrator Model Registry synchronization
```

The Orchestrator does not duplicate OmniRoute's provider adapters or store raw provider secrets in its model registry. The public product surface hides OmniRoute's management API.

### 5.2 Model discovery

The Gateway Adapter may read:

- `GET /v1/models`
- `GET /v1/providers/{provider}/models`
- `GET /api/providers`
- `GET /api/providers/{id}/models`
- `GET /api/models/catalog`
- Runtime health and latency endpoints where available

The adapter merges those results with models.dev, OpenRouter, local-provider discovery, and Orchestrator telemetry.

### 5.3 Request execution

The Orchestrator resolves a role and Pool, selects a concrete target or allowed target set, and sends the request through OmniRoute:

```text
Orchestrator Router
  → Developer/Coder
  → tool_calling pool
  → provider-a / luna-5.6-pro

Gateway Adapter
  → POST OmniRoute /v1/chat/completions
  → model: provider-a/luna-5.6-pro

OmniRoute
  → credential resolution
  → provider request
  → streaming response / operational fallback
```

For a precise Provider/model target, use a provider-prefixed model identifier or OmniRoute's dedicated provider route. Exact Account/Connection pinning is a separate operational concern and should use an explicit managed combo or a controlled internal gateway capability.

### 5.4 Routing ownership and double-routing prevention

The Orchestrator owns the semantic decision: role, Pool, policy, hard constraints, and permitted targets. OmniRoute owns the operational decision: credential selection, connection health, retry, and endpoint fallback within the permitted scope.

The adapter must record both decisions so that a request is explainable:

```text
Semantic decision:
  Coder → tool_calling pool → Luna family

Operational decision:
  provider-a/account-1 selected
  provider-b used as fallback after a 429
```

Unconstrained double-routing, where both systems independently select from the entire catalog, must be avoided.

## 6. Deployment Model

### Default installation

```text
Our Platform
  ├── API Gateway and UI
  ├── Orchestrator Brain
  ├── Model Registry and Pool Builder
  ├── LangGraph Runtime
  └── OmniRoute managed service
```

Docker Compose, Helm, or another supported installer provisions OmniRoute automatically. Users configure Providers through our UI and do not need to learn or expose OmniRoute's dashboard.

### Advanced installation

An enterprise or developer deployment may point the Gateway Adapter at an existing OmniRoute instance. This is optional and must not change the Orchestrator's Model Registry and routing contract.

## 7. Build vs. Reuse Decision

| Component | Decision | Reason |
| :--- | :--- | :--- |
| Provider adapters | Reuse OmniRoute | Avoid duplicating hundreds of integrations |
| Credential/OAuth flows | Reuse OmniRoute through adapter | Keep secrets and provider-specific auth in one boundary |
| Streaming/protocol translation | Reuse OmniRoute | Mature gateway responsibility |
| Provider/model discovery | Read from OmniRoute and enrich | Reflect actual configured availability |
| Canonical Model Registry | Build in Orchestrator | Requires cross-provider semantic identity |
| Default Agent Profiles | Build in Orchestrator | Product-specific behavior |
| Model Pools and Role Combos | Build in Orchestrator | Planner/Coder/Reviewer semantics belong to us |
| Explainable Router | Build in Orchestrator | Core product intelligence |
| Adaptive suitability learning | Build in Orchestrator | Uses task outcomes and project telemetry |

## 8. Final Recommendation

Use **OmniRoute as the managed internal execution gateway** and build a stable `GatewayAdapter` around its APIs. Do not make users install OmniRoute separately. Do not embed the Orchestrator as a fragile plugin inside OmniRoute. Do not duplicate OmniRoute's Provider Registrar.

The architectural relationship is:

```text
Orchestrator:
  decides what model family, variant, pool, role, and policy are appropriate

OmniRoute:
  decides how to authenticate, reach, execute, retry, and fail over operationally
```

This preserves the project's core intellectual property in the Orchestrator while reusing OmniRoute for the provider execution complexity where it provides the most value.
