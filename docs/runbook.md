# TotalOrch Runbook

Operational notes for the current MVP slice. Commands assume the virtualenv at
`.venv` and a running API on `http://127.0.0.1:8080`.

## First run

```bash
cp .env.example .env
# set TOTAL_ORCH_API_KEY, GATEWAY_* and, optionally, ORCHESTRATOR_WORKSPACE_ROOT
.venv/bin/total-orch --host 127.0.0.1 --port 8080
```

`GET /health` is public and returns startup checks (`registry`, `gateway`,
`workspace`, `secret_references`, `telemetry`) plus counters. Every `/v1/*`
route requires `Authorization: Bearer $TOTAL_ORCH_API_KEY`.

```bash
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/v1/registry/sync \
  -H "Authorization: Bearer $TOTAL_ORCH_API_KEY"
curl -s http://127.0.0.1:8080/v1/registry/sync/status \
  -H "Authorization: Bearer $TOTAL_ORCH_API_KEY"
```

A failed sync that reports `succeeded=false` also reports `error_kind`
(`authentication`, `catalog`, `provider`, `gateway`, `unknown`) per source.

## Provider connections

```bash
curl -s -X POST http://127.0.0.1:8080/v1/providers/connections \
  -H "Authorization: Bearer $TOTAL_ORCH_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"id":"openai-main","provider":"openai","base_url":"https://api.openai.com/v1","auth_method":"api_key","secret_ref":"env://OPENAI_API_KEY"}'
```

`GET /v1/providers/export` never includes secret values. `POST
/v1/providers/import` requires `"confirm": true` when the payload contains a
`secret_ref`. Credential values must exist in the process environment; a
missing reference is reported by `/health`.

## Tasks and workflow operations

```bash
curl -s -X POST http://127.0.0.1:8080/v1/tasks \
  -H "Authorization: Bearer $TOTAL_ORCH_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"request":"Add a health endpoint and tests","run_tests":true}'
```

The response is `202` with a `workflow_id`. Useful follow-ups:

```bash
curl -s  .../v1/tasks/$ID
curl -s  .../v1/tasks/$ID/events
curl -sN .../v1/tasks/$ID/events?stream=true   # SSE until terminal
curl -s -X POST .../v1/tasks/$ID/pause
curl -s -X POST .../v1/tasks/$ID/resume
curl -s -X POST .../v1/tasks/$ID/cancel
curl -s -X POST .../v1/tasks/$ID/replay
```

A task submitted with `"workspace"` must resolve inside
`ORCHESTRATOR_WORKSPACE_ROOT`. Without a configured root, repository tools are
disabled and the workflow still plans, implements, and reviews from the task
text alone.

`ORCHESTRATOR_WORKFLOW_APPLY_CHANGES=true` enables the opt-in `apply` node:
after the reviewer approves a non-empty diff, the developer tool permission
applies it with `git apply --check` first. The default is `false`.

## Repository tools and limits

- Read/list/context are rooted at `ORCHESTRATOR_WORKSPACE_ROOT`; symlink and
  `..` escapes, `.git`, `.venv`, and build/cache directories are rejected.
- The Tester tool runs one configured argument vector (default
  `python -m pytest -q`), never a model-supplied shell string.
- `ORCHESTRATOR_WORKFLOW_NODE_TIMEOUT_SECONDS` bounds each model call;
  `ORCHESTRATOR_WORKFLOW_MAX_ATTEMPTS` bounds node retries.
- `ORCHESTRATOR_REQUEST_MAX_BODY_BYTES` and
  `ORCHESTRATOR_RATE_LIMIT_PER_MINUTE` bound the public API.
- The MCP spike (`total_orch.tools.mcp`) is an allowlisted read-only client;
  it is not yet wired into the workflow.

## Telemetry, health, and reporting

```bash
curl -s .../v1/telemetry/report
curl -s .../v1/telemetry/metrics
```

The ledger records one run per task, every execution event, and endpoint
observations (success, latency, error code, fallback). Three consecutive
failures place an endpoint in a 60-second cooldown; `report` exposes
`consecutive_failures` and `cooldown_until`. Adaptive policy mutation remains
disabled.

## Backup and restore

The registry, route decisions, workflow checkpoints, API keys, and telemetry
live in one SQLite file (`ORCHESTRATOR_REGISTRY_DATABASE_PATH`, default
`registry.db`). Stop the process before a cold backup, or use SQLite's online
backup:

```bash
sqlite3 registry.db ".backup 'registry-backup.db'"
```

Restore by stopping the process, replacing `registry.db`, and starting again.
`/health` and `GET /v1/registry/sync/status` verify the restored state; run a
sync to refresh catalogs.

## Restart, redeploy, and provider failure

- Checkpoints are persisted in SQLite, so a restart keeps task state. A
  submitted task that was running is not automatically resumed; use
  `POST /v1/tasks/{id}/resume`, or `replay` for a new audited run.
- A provider outage is normalized as `GatewayError`; the workflow retries
  retryable errors and then walks the routed fallback list. Non-retryable
  errors move to the next endpoint without sleeping.
- A failed sync or provider failure never crashes the process; it is visible
  in `/v1/registry/sync/status`, `/v1/telemetry/report`, and task events.

## Known limitations

- SQLite is a single-node store; multi-worker deployment needs a shared
  database before horizontal scaling.
- The repository tool sandbox is path- and command-scoped, not a container or
  kernel sandbox. Run untrusted repositories in a disposable container.
- MCP is a read-only spike and is not part of the default workflow.
- Direct Anthropic execution is available but not yet the default; gateway
  compatibility remains.
- No multi-tenant authorization scopes yet: one TotalOrch API key gates all
  management and execution routes.
