# ADR-0002: Use LangGraph as the Workflow Runtime

- **Status:** Accepted
- **Date:** 2026-08-17

## Decision

LangGraph is the execution runtime for deterministic agent workflows. The
Orchestrator owns the workflow definitions, routing policy, events, and agent
semantics; LangGraph provides graph traversal, state boundaries, checkpoints,
pause/resume, and controlled branching.

## Consequences

- Planner, Developer/Coder, Reviewer, and Tester can be explicit graph nodes.
- Every node can emit a normalized execution event.
- Long-running workflows can resume from checkpoints.
- LangGraph is an execution primitive, not the Model Intelligence Registry or
  the semantic Router.
