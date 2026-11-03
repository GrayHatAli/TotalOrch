# Direct Execution vs Gateway Execution

Both paths implement `GatewayAdapter`; the rest of TotalOrch sees the same
requests, completions, stream chunks, tool calls, and normalized errors.

| Concern | Gateway (OmniRoute / OpenAI-compatible router) | Direct Provider adapter |
| :--- | :--- | :--- |
| Credentials | Gateway-owned; TotalOrch holds a gateway key | TotalOrch resolves an `env://` reference per connection |
| Provider adapters | Gateway owns provider-specific translation | One adapter owns one provider's translation |
| Discovery | Router catalog through `GET /v1/models` | Provider catalog endpoint |
| Operational fallback | Gateway retries and fails over | Adapter retries; workflow falls back across routed endpoints |
| Tool calls | Normalized from OpenAI-style deltas | Normalized from provider-specific blocks |
| Streaming | SSE normalized by the shared base | SSE normalized by the direct adapter |
| Credential exposure | TotalOrch never sees provider secrets | Secret value enters the process only for the outbound call |

## Current position

- Direct OpenAI-compatible execution is available for connections with a
  `base_url`.
- Anthropic direct execution covers streaming and tool calls.
- Gateway execution remains the default and has not been removed. Removal
  requires the comparison plus live discovery, health, completion, streaming,
  fallback, and failure-isolation tests.
