# Model_Catalog_Research.md

## Executive Summary

To enable intelligent model selection, context management, cost estimation, and dynamic routing, an open-source **TotalOrch** requires a reliable **Model Intelligence Layer**.

This research evaluates primary metadata sources—**OmniRoute**, **models.dev**, **OpenRouter Models API**, **Artificial Analysis**, **Hugging Face Model Hub**, and the **Ollama Registry**. OmniRoute is the source of Provider Connections, gateway-visible models, and runtime availability for the current deployment; the other sources enrich the Orchestrator's normalized Model Intelligence Layer. We analyze their data schemas, freshness, accessibility, and quality, and present a normalized data model and system architecture for a local **Model Registry, Capability Graph, and Default Agent Pool Builder**.

---

## 1. Deep Dive: Primary Sources

### 0. OmniRoute Gateway Catalog
* **Overview:** The default execution gateway and source of truth for the Provider Connections actually configured in a deployment.
* **Integration Role:** Provides provider registration, OAuth/API-key connections, provider/model discovery, connection tests, gateway-visible `/v1/models`, endpoint-level health, quota, and execution results.
* **Boundary:** OmniRoute owns provider adapters, credentials, protocol translation, streaming, retry, and operational fallback. The Orchestrator owns canonical model identity, cross-provider family grouping, capabilities, default agent pools, and semantic routing.
* **Access:** Use the internal Gateway Adapter and management APIs such as `/api/providers`, `/api/providers/{id}/models`, `/api/models/catalog`, and `/v1/models`. Never expose the management API directly to end users.

### 1. models.dev (by Anomaly / opencode)
* **Overview:** Community-driven open-source database storing model specifications, pricing, limits, and capabilities in version-controlled TOML files (`models/` and `providers/`).
* **Data Schema & Quality:** High structural clarity separating provider-agnostic facts (underlying model capabilities like context limit, reasoning support, tool calling) from provider-specific serving details (pricing, environment variables, base URLs).
* **Endpoints & Access:** Public REST API endpoints without authentication (`/api.json`, `/models.json`, `/catalog.json`).
* **Strengths:** Explicit boolean flags for `reasoning`, `tool_call`, `structured_output`, `attachment`, context limits, and open-weights details. Excellent for static capability validation.
* **Limitations:** Relies on manual pull requests and automated schema checks; real-time pricing updates or dynamic provider outages are not captured instantly.

### 2. OpenRouter Models API
* **Overview:** Dynamic REST endpoint (`GET https://openrouter.ai/api/v1/models`) listing all 400+ models aggregated across dozens of underlying cloud providers.
* **Data Schema & Quality:** Exceptional real-time accuracy for pricing (prompt/completion tokens, prompt caching costs), maximum context lengths, maximum output tokens, and modality flags.
* **Endpoints & Access:** Publicly accessible endpoint, no API key required for GET queries, edge-cached with high availability.
* **Strengths:** Live pricing updates, per-model modality tags (text, image, audio), architecture info, and live routing availability.
* **Limitations:** Specific to cloud API endpoints; lacks local model specs (e.g., GGUF quantization levels for Ollama) and deep benchmark quality metrics.

---

## 2. Deep Dive: Alternative Sources

### 1. Artificial Analysis
* **Overview:** Independent benchmarking platform providing quantitative metrics for LLM performance, latency, throughput, and costs.
* **Capabilities Covered:** Time-to-First-Token (TTFT), Tokens Per Second (TPS), Quality Index scores, SWE-bench / Coding Agent benchmarks, and verified API pricing.
* **Integration Role:** Serves as the primary source for **Performance & Benchmarking metrics** (speed, latency, coding capability scores) used by the Router's cost/speed optimization algorithms.

### 2. Hugging Face Model Hub
* **Overview:** The central repository for open-weights models, datasets, and code spaces.
* **Capabilities Covered:** Model licenses, exact parameter counts, architecture tags, tensor formats (`safetensors`, `GGUF`), and quantization variants.
* **Integration Role:** Serves as the authoritative source for **Open Weights verification**, license compliance (Apache-2.0, MIT, Llama Community), and downloading local weights.

### 3. Ollama Local Registry
* **Overview:** Local daemon endpoint (`GET /api/tags` and `POST /api/show`) returning model specs for locally pulled or run models.
* **Capabilities Covered:** Quantization type (`Q4_K_M`, `Q8_0`, `FP16`), parameters (`8B`, `70B`), model file digest, system prompt, and VRAM/RAM footprint.
* **Integration Role:** Provides live local availability telemetry. Automatically registers locally running models into the Orchestrator's model catalog.

---

## 3. Metadata Availability Comparison Matrix

| Metadata Field | models.dev | OpenRouter API | Artificial Analysis | Hugging Face Hub | Ollama Registry |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Model List Completeness** | High (Major & Open) | Very High (400+ Cloud) | Medium (Major/Popular) | Extreme (Open Weights) | Local/Pulled Models |
| **Context Window Size** | High | High | High | In Readme/Config | High |
| **Real-time Pricing** | Medium | High (Live per-token) | High | N/A (Self-hosted) | Free (Local) |
| **Latency / TTFT / TPS** | N/A | N/A | High (Measured) | N/A | Hardware-dependent |
| **Coding & Quality Index** | Basic links | N/A | High (SWE / Coding) | Leaderboards (Open) | N/A |
| **Tool / Function Calling** | Explicit boolean | Supported array | Evaluated | Tag-based | Native flag |
| **Reasoning Support** | Explicit boolean | Architecture tags | Benchmark scores | Model card tags | Model params |
| **Open Weights & License** | Explicit metadata | License string | License information | High (Spdx/Tag) | Model details |
| **API Access / Auth** | Public (No Key) | Public (No Key) | API / Scrape | Public REST / Token | Local localhost |
| **Update Frequency** | Git PR / Daily | Real-time Edge | Daily / Weekly | Continuous | Local event-driven |

---

## 4. Normalization Strategy & Schema Design

The initial research used a Three-Tier Identifier to prevent provider name collisions and schema discrepancies (e.g., `gpt-4o` on OpenAI vs Azure vs OpenRouter vs Bedrock):

1. **Base Model Canonical ID:** `canonical_id` (e.g., `openai/gpt-4o`, `anthropic/claude-3-5-sonnet`, `deepseek/deepseek-v3`).
2. **Serving Endpoint Provider:** `provider_id` (e.g., `openai`, `azure`, `openrouter`, `ollama`).
3. **Provider Endpoint ID:** `endpoint_model_id` (e.g., `gpt-4o-2024-08-06`).

```
                              +-------------------------+
                              |    Base Model Entity    |
                              | canonical_id: openai/v4 |
                              +-------------------------+
                                           |
                    +----------------------+----------------------+
                    |                                             |
                    v                                             v
     +------------------------------+              +------------------------------+
     |   Provider Serving Spec 1    |              |   Provider Serving Spec 2    |
     |  provider: openai            |              |  provider: openrouter        |
     |  endpoint_id: gpt-4o         |              |  endpoint_id: openai/gpt-4o   |
     +------------------------------+              +------------------------------+
```

### Legacy Endpoint-Centric Record

The following shape is retained as a migration reference. The user-facing and routing-oriented representation is now the model-centric Family → Variant → Provider Endpoint view below.

```json
{
  "canonical_id": "anthropic/claude-3-5-sonnet",
  "name": "Claude 3.5 Sonnet",
  "family": "claude-3",
  "open_weights": false,
  "license": "Proprietary",
  "capabilities": {
    "context_window": 200000,
    "max_output_tokens": 8192,
    "modalities": ["text", "image"],
    "tool_calling": true,
    "structured_output": true,
    "reasoning": false
  },
  "benchmarks": {
    "quality_index": 89.2,
    "coding_index": 92.4,
    "avg_ttft_ms": 320,
    "avg_tps": 75.4
  },
  "providers": [
    {
      "provider_id": "anthropic",
      "endpoint_model_id": "claude-3-5-sonnet-20241022",
      "cost_per_1m_input": 3.00,
      "cost_per_1m_output": 15.00,
      "cost_per_1m_cache_read": 0.30,
      "is_available": true
    },
    {
      "provider_id": "openrouter",
      "endpoint_model_id": "anthropic/claude-3.5-sonnet",
      "cost_per_1m_input": 3.00,
      "cost_per_1m_output": 15.00,
      "cost_per_1m_cache_read": 0.30,
      "is_available": true
    }
  ]
}
```

### Current Model-Centric View

The canonical product view groups models by family and variant, then lists every concrete Provider Endpoint that can serve each variant. Internally, these remain separate entities so endpoint-specific health, pricing, quota, and capability overrides are not lost.

```json
{
  "canonical_family": "luna",
  "publisher": "openai",
  "source_ids": {
    "openrouter": "openai/luna-5.6-pro",
    "models_dev": "..."
  },
  "models": [
    {
      "id": "luna-5.6-pro",
      "display_name": "Luna 5.6 Pro",
      "description": "High-performance reasoning model",
      "capabilities": {
        "tool_calling": true,
        "structured_output": true,
        "reasoning": {
          "supported": true,
          "parameter": "reasoning_effort",
          "available_efforts": ["low", "medium", "high"],
          "default_effort": "medium"
        },
        "modalities": ["text"]
      },
      "context_window": 8192,
      "max_output_tokens": 4096,
      "providers": [
        {
          "provider_id": "provider-a",
          "connection_id": "provider-a-account-1",
          "endpoint_model_id": "luna-5.6-pro",
          "effective_capabilities": {
            "tool_calling": true,
            "reasoning_efforts": ["low", "medium", "high"]
          },
          "health": 0.97,
          "latency_p95_ms": 840,
          "error_rate": 0.012,
          "quota_remaining": 0.64,
          "availability": "available",
          "cost_per_1m_input": 1.2,
          "cost_per_1m_output": 4.8,
          "last_checked_at": "2026-08-15T10:30:00Z"
        }
      ]
    }
  ],
  "provenance": {
    "capabilities": {"source": "models.dev", "updated_at": "..."},
    "pricing": {"source": "openrouter", "updated_at": "..."},
    "health": {"source": "orchestrator_telemetry", "updated_at": "..."}
  }
}
```

`available_efforts` is optional. It is present only when the model and serving endpoint expose a controllable reasoning-effort parameter. A model may support reasoning without accepting `low`, `medium`, or `high`; unknown values must remain unknown rather than being inferred.

Context limits, capabilities, health, latency, cost, quota, and availability may differ by Provider Endpoint. Base model metadata and endpoint-effective metadata must therefore remain separate.

---

## 5. Merging & Conflict Resolution Pipeline

When ingesting from multiple sources, conflicts are resolved via **Source Authority Weighting**:

1. **Pricing & Context Windows:** `OpenRouter API` (Weight: 0.9) > `models.dev` (Weight: 0.8) > Provider Docs.
2. **Capability Flags (Tool calling, Reasoning):** `models.dev` (Weight: 0.95) > `OpenRouter` (Weight: 0.85).
3. **Speed & Latency Metrics:** `Artificial Analysis` (Weight: 0.95).
4. **Local Hardware & Quantization:** `Ollama API` (Weight: 1.0).
5. **Configured availability and runtime health:** `OmniRoute` and Orchestrator telemetry override public catalog assumptions for the current deployment.

---

## 6. Architecture & Integration Plan

Legacy Target Pipeline (superseded):
`OmniRoute Catalog Adapter` → `Model Registry (Local DB)` → `Capability Graph` → `Default Agent Pool Builder` → `Explainable Router`

The original pipeline below is retained only as a historical sketch:
`Model Registry (Local DB)` $ightarrow$ `Capability Graph` $ightarrow$ `Intelligent Router`

```
+-----------------------------------------------------------------------------------+
|                            EXTERNAL DATA SOURCES                                  |
| [ OmniRoute ] [ models.dev ] [ OpenRouter API ] [ Artificial Analysis ] [ Ollama ]|
+-----------------------------------------------------------------------------------+
                                          |
                                          v (Scheduled Sync / Webhooks)
+-----------------------------------------------------------------------------------+
|                        INGESTION & NORMALIZATION PIPELINE                         |
|   - Multi-source fetcher & merger                                                |
|   - Source authority conflict resolution                                          |
|   - Family / Variant / Endpoint mapping                                          |
|   - Canonical ID and capability mapping                                          |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                         LOCAL MODEL REGISTRY DATABASE                             |
|                           (SQLite / PostgreSQL)                                   |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                          CAPABILITY GRAPH (In-Memory)                             |
|   - Fast index by family, capability, context size, cost, speed, and coding rank   |
|   - Endpoint health, quota, and availability indexes                              |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                          INTELLIGENT ROUTER ENGINE                                |
|   - Default Agent Profile and Pool resolution                                     |
|   - Dynamic model/provider selection per task                                     |
|   - Explainable scores, exclusions, and fallback path                            |
+-----------------------------------------------------------------------------------+
```

### Component Breakdown

1. **Local Model Registry Database (SQLite / Postgres):**
   * Stores normalized families, variants, provider endpoints, historical cost trends, endpoint health, latency history, provenance, and default pool membership.
2. **Sync Engine Daemon:**
   * Reads configured Provider Connections and models from OmniRoute, then periodically queries `models.dev` and the `OpenRouter Models API` to refresh public metadata, pricing, and context limits.
   * Connects to local `localhost:11434` (Ollama) on startup/event to dynamically detect local models.
3. **In-Memory Capability Graph:**
   * High-speed memory index allowing the Router to execute complex filter queries in < 1ms:
     * *Example Query:* "Find available endpoints with `tool_calling=true`, `context >= 64k`, `cost_per_1m <= $1.00`, ranked in the top coding tier, and healthy in the current deployment."

---

## 7. Strategic Recommendations

1. **Do not rely on a single source:** No single provider captures local models, live cloud pricing, benchmark quality, and explicit capability booleans simultaneously.
2. **Combine models.dev + OpenRouter API + Ollama:**
   * Use **models.dev** for definitive capability flags (`reasoning`, `tool_call`, `structured_output`).
   * Use **OpenRouter API** for live cloud pricing and context limits across hundreds of providers.
   * Use **Ollama API** for local workstation model detection.
3. **Build a Local Model Registry DB:** Maintain a lightweight local database (SQLite embedded into the Orchestrator) to cache and serve metadata instantly without blocking gateway requests on external API latency.
4. **Build Default Agent Profiles:** Generate Planner, Developer/Coder, Reviewer, and Researcher pools automatically from hard capability constraints plus quality, health, latency, cost, quota, and task-outcome signals.
5. **Keep the user-facing view model-centric:** expose Family → Variant → Provider Endpoint, while retaining normalized storage for accurate routing and telemetry.
