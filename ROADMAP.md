# TotalOrch Registry and Provider Independence Roadmap

This roadmap moves TotalOrch toward owning its canonical model and provider
registry while keeping execution backends replaceable. OmniRoute, LiteLLM,
9Router, and direct Provider adapters remain integrations behind stable
boundaries; none is the source of truth for the TotalOrch domain model.

- [x] Phase 0 — Establish the verified baseline
  - [x] Run the Python test suite and record the passing baseline.
  - [x] Run lint, type checking, and package build validation.
  - [x] Verify live model discovery through the local OmniRoute inference API.
  - [x] Verify live completion and streaming through `OmniRouteAdapter`.
  - [x] Record the separate credential requirement for OmniRoute management APIs.

- [ ] Phase 1 — Make the catalog registry independent of gateway management APIs
  - [x] Ingest standard `/v1/models` catalogs without requiring provider-management endpoints.
  - [x] Derive a stable serving-provider reference from explicit provider metadata or model IDs.
  - [x] Keep canonical family/variant identity separate from provider model names and aliases.
  - [x] Classify virtual, automatic, alias, and concrete model entries.
  - [ ] Preserve per-field provenance, freshness, and source conflict behavior.
  - [ ] Add live-sync diagnostics that distinguish catalog, authentication, and provider failures.

- [ ] Phase 2 — Make Provider connections first-class TotalOrch records
  - [x] Add provider and connection domain contracts without storing raw secrets.
  - [x] Store secret references, authentication method, base URL, enabled state, and ownership metadata.
  - [x] Add connection health and last-success timestamps.
  - [x] Add provider capability observations.
  - [x] Persist external secret references while keeping provider secret values outside TotalOrch.
  - [ ] Add import/export with secret redaction and explicit confirmation for credential-bearing data.

- [ ] Phase 3 — Build the canonical model registry service
  - [x] Add a registry sync coordinator for provider adapters and public metadata sources.
  - [ ] Normalize model IDs, aliases, context limits, capabilities, pricing, and lifecycle state.
  - [ ] Make the registry the routing source of truth instead of a live router catalog.
  - [ ] Add manual overrides and safe conflict resolution.
  - [ ] Add API queries for providers, models, endpoints, health, provenance, and sync status.

- [ ] Phase 4 — Separate discovery from execution
  - [ ] Keep the existing gateway adapters as compatibility execution backends.
  - [ ] Define a provider-neutral execution contract for direct Provider adapters.
  - [x] Implement direct OpenAI-compatible execution first.
  - [ ] Implement direct Anthropic execution with streaming and tool-call coverage.
  - [ ] Add further Provider adapters only when the contract and tests justify them.
  - [ ] Compare direct execution with gateway execution before removing any gateway dependency.

- [ ] Phase 5 — Add the TotalOrch public service
  - [x] Add a health endpoint and a documented application entrypoint.
  - [x] Add an OpenAI-compatible request facade for Harness clients, including streaming.
  - [x] Authenticate Harness requests with a configured TotalOrch API key.
  - [x] Issue and revoke TotalOrch API keys without storing raw tokens.
  - [ ] Route requests through the canonical registry and record explainable decisions.
  - [ ] Keep Provider credentials and backend-specific management APIs private.

- [ ] Phase 6 — Deployment and operational hardening
  - [x] Add a reproducible TotalOrch container build.
  - [x] Define persistence for the registry and credential references.
  - [ ] Add startup checks for registry, execution backend, and secret configuration.
  - [ ] Add metrics, request IDs, structured logs, and redaction checks.
  - [x] Deploy TotalOrch and an execution backend as independently replaceable services.
  - [ ] Verify restart, redeploy, backup, restore, and provider-failure behavior.

- [ ] Phase 7 — Completion criteria
  - [ ] Switching between OmniRoute, LiteLLM, 9Router, and a direct adapter does not change registry contracts.
  - [ ] Routing decisions remain explainable and reproducible from persisted registry data.
  - [ ] No raw Provider credential is returned by catalog or public execution APIs.
  - [ ] Live tests cover discovery, health, completion, streaming, fallback, and failure isolation.
  - [ ] Deployment documentation describes the actual supported topology and persistence guarantees.
