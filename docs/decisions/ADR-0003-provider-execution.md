# ADR-0003: Direct Provider Adapters and Gateway Compatibility

- **Status:** Accepted
- **Date:** 2026-11-03

## Decision

`GatewayAdapter` remains the only execution boundary. OmniRoute and the
OpenAI-compatible routers stay available as compatibility execution backends,
and direct Provider adapters implement the same normalized contract.

The direct execution set grows only when the provider justifies a dedicated
translation layer and the adapter can prove streaming and tool-call coverage
with contract tests. Today that is:

- any OpenAI-compatible endpoint through `OpenAICompatAdapter`;
- Anthropic through `AnthropicAdapter`.

No additional adapter is added on speculation. A new adapter must first show
that the shared contract cannot express the provider's required behavior.

## Comparison before removal

A gateway is never removed because a direct adapter exists. The comparison in
`docs/execution-comparison.md` lists the operational differences, and the
gateway path is retained until the comparison and live tests show that direct
execution covers discovery, health, retry, streaming, and tool calls without
regression.
