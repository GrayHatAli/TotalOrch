# ADR-0001: Use OmniRoute as the Managed Internal Execution Gateway

- **Status:** Accepted
- **Date:** 2026-08-17

## Decision

The default TotalOrch distribution provisions OmniRoute as an internal
service or sidecar. The Orchestrator communicates with it through a small,
versioned `GatewayAdapter` contract. An external OmniRoute deployment may be
configured for advanced installations, but it is not a prerequisite for
users.

## Ownership boundary

OmniRoute owns Provider adapters, credentials and OAuth, provider-specific
translation, streaming transport, connection selection, operational retries,
and endpoint-level fallback. The Orchestrator owns canonical model identity,
capability intelligence, Agent Profiles, Model Pools, semantic routing, and
workflow coordination.

## Consequences

- Provider integrations are not duplicated in the Orchestrator.
- OmniRoute API changes are isolated in one adapter module.
- The product can expose a simpler Provider onboarding surface.
- The Orchestrator must not treat OmniRoute's operational fallback as its own
  semantic routing decision.
- The adapter must never return raw provider secrets to the registry.
