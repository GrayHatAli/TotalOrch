# MVP Definition of Done — Phases 0–2

The first implementation slice is complete when:

- The package installs from `pyproject.toml` and the documented test command
  runs successfully.
- Domain contracts reject invalid capability, identity, routing, and workflow
  records.
- A single model variant can be represented by multiple Provider Endpoints.
- Unknown capabilities remain unknown (`null`) until supported metadata exists.
- The Gateway Adapter exposes discovery, validation, health, completion, and
  streaming without leaking provider-specific details into the domain.
- The Fake Gateway can simulate success, streaming, and a retryable Provider
  failure deterministically.
- The OmniRoute adapter can read connections and models, execute an explicit
  `provider/model` target, normalize SSE responses, retry transient failures,
  and classify non-transient failures.
- Configuration is environment-based and raw credentials do not enter model
  or event records.
- The managed deployment contract and external OmniRoute mode are documented.

This document describes the original phases 0–2 slice. Later phases added
Model Registry persistence, automatic Agent Pool building, semantic Router
scoring, the LangGraph workflow runtime, repository tools, telemetry, and the
public task API. Live end-to-end connectivity to a real OmniRoute instance
still requires a configured live environment and real credentials.
