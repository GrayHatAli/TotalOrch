# Initial Provider Test Matrix

The matrix is capability-oriented so the platform is not coupled to one
vendor's naming or authentication flow. Provider names below are configured in
OmniRoute and may be replaced by any compatible connection.

| Track | Provider shape | Required checks | Phase 2 status |
| :--- | :--- | :--- | :--- |
| A | Remote OpenAI-compatible Provider | connection discovery, model discovery, explicit `provider/model` completion, streaming | Adapter contract covered by fake transport |
| B | Aggregator Provider such as OpenRouter | prefixed model discovery, context metadata, completion, normalized usage | Adapter contract covered by fake transport |
| C | Local Provider such as Ollama or another local gateway | local URL, health check, streaming, timeout classification | Adapter contract covered by fake transport |

## Live acceptance run

Before claiming the phase-2 live acceptance criterion, configure at least two
connections in the managed OmniRoute instance and record:

1. the connection identifiers returned by the management catalog;
2. at least one model from each connection;
3. one successful completion per Provider using a provider-prefixed model;
4. one streaming completion;
5. one controlled failure and the normalized error code;
6. confirmation that no credential value appears in logs, registry records, or
   execution events.
