# TotalOrch Audit Remediation Checklist

This document turns the latest code audit into small, verifiable work items.
Each section is independent. Check the box only after the code, tests, and
acceptance criteria for that section are complete.

Audit baseline: the repository had 134 passing tests, five Ruff errors, and 35
mypy errors. The expected runtime is multiple workers sharing one deployment,
with gateway calls and SQLite persistence on the request path.

## [x] 1. Protect reserved gateway request fields

**Location:** `src/total_orch/gateway/_openai.py:387-399`

### Problem

`GatewayRequest.extra` is merged after the canonical `model`, `messages`, and
`stream` fields. A caller can therefore replace them:

```python
extra={"model": "another-model", "stream": True}
```

This sends the wrong model or makes a non-streaming call look like a streaming
call.

### Fix

Build the provider-specific fields first, then write the canonical fields last:

1. Start with `dict(request.extra)`.
2. Set `model`, `messages`, and `stream` from the request arguments.
3. Set `tools`, `temperature`, and `reasoning_effort` from their typed fields.
4. Either reject reserved keys with a clear `ValueError`, or document that the
   typed fields always win. Prefer typed fields winning for the smallest change.

### Tests and acceptance

- Add a test with `extra={"model": "wrong", "stream": True}`.
- Assert that the sent body contains the requested model and requested stream
  value.
- Keep provider-specific keys such as `user` and `response_format` intact.

## [x] 2. Preserve tool calls in gateway responses

**Locations:** `src/total_orch/gateway/_openai.py:187-217` and
`src/total_orch/gateway/models.py:34-52`

### Problem

The request contract can send tools, but the response contract only keeps text.
An OpenAI-compatible response with `finish_reason="tool_calls"` is returned as
an empty `output_text`, and the tool call payload is lost.

### Fix

Add a small normalized representation for tool calls:

1. Add immutable `GatewayToolCall` data to the gateway models.
2. Add `tool_calls` to `GatewayCompletion` and `GatewayStreamChunk`.
3. Parse `message.tool_calls` in `completion_from_response`.
4. Parse `delta.tool_calls` in `stream_chunk_from_response`.
5. Preserve the call id, function name, and JSON argument string without
   executing or interpreting the arguments in the adapter.
6. Update `FakeGatewayAdapter` so tests can return tool calls.

### Tests and acceptance

- A non-streaming `tool_calls` response retains every call.
- A streaming response retains incremental tool-call chunks.
- Text-only responses remain unchanged.
- `completion_event` does not silently drop the normalized calls if events are
  the next consumer.

## [x] 3. Reject malformed successful gateway responses

**Locations:** `src/total_orch/gateway/omniroute.py:116-136` and
`src/total_orch/gateway/_openai.py:155-165`

### Problem

OmniRoute returns `True` for any successful response without an explicit health
value. The generic adapter also treats a successful payload such as
`{"error": "router unavailable"}` as a model named `unknown`.

### Fix

Validate the response shape at the adapter boundary:

1. For connection validation, require an explicit boolean result when the
   endpoint returns a status object; do not turn an empty body into `True`.
2. For health checks, accept only a recognized boolean or status value. Treat
   an empty or unrecognized response as unhealthy or raise a normalized
   `GatewayError`.
3. In catalog parsing, require a non-empty model id for every model record.
4. If a mapping has an `error` field and no valid model list, raise an invalid
   gateway response error instead of creating `unknown`.

### Tests and acceptance

- Empty 200 health and validation responses are not successful.
- `{"error": "down"}` never creates a model or connection.
- Valid `healthy`, `ok`, and recognized status strings still work.
- A malformed catalog response is reported to the registry sync as an error.

## [x] 4. Make redacted settings safe for URLs containing secrets

**Location:** `src/total_orch/config.py:86-105`

### Problem

`Settings.redacted()` returns base URLs unchanged. A URL such as
`https://user:secret@example.test/?token=query-secret` leaks credentials when
the returned dictionary is logged.

### Fix

Add one small URL-redaction helper in `config.py`:

1. Parse each configured URL with `urllib.parse.urlsplit`.
2. Keep only scheme, hostname, port, and path when needed.
3. Remove username, password, query, and fragment.
4. Use the helper for gateway, OmniRoute, models.dev, and OpenRouter URLs in
   `redacted()`.
5. Do not change the original runtime URL used for requests.

### Tests and acceptance

- A URL with userinfo and query credentials produces no secret in `redacted()`.
- A normal URL keeps its scheme, host, port, and path.
- API keys in the dedicated key fields remain represented only by their
  configured booleans.

## [x] 5. Report partial registry syncs as failures

**Locations:** `src/total_orch/registry/service.py:59-71` and
`src/total_orch/registry/service.py:122-142`

### Problem

`SyncSummary.succeeded` checks only source fetch status. Entity assembly errors
are stored in `entity_errors` but do not change the success result. A sync can
therefore report success while silently omitting malformed families, variants,
or endpoints.

### Fix

Make success reflect the whole pass:

1. Return `False` when `entity_errors` is non-empty.
2. Return `False` when no source was configured or no source succeeded.
3. Keep the detailed errors in `entity_errors`; do not hide them.
4. Keep successful entities from healthy sources available, because partial
   progress is useful even when the summary is not successful.

### Tests and acceptance

- A source with one invalid entity produces `succeeded is False` and a useful
  entity error.
- A failed source produces `succeeded is False`.
- An empty source list is not reported as a successful sync.
- Valid entities from the same pass remain queryable.

## [x] 6. Make decision persistence idempotent across workers

**Location:** `src/total_orch/router/store.py:55-81`

### Problem

`save_decision()` performs a check-then-insert. When multiple workers save the
same decision id at once, one succeeds and the others raise SQLite
`IntegrityError` instead of returning `False` as the method promises.

### Fix

Replace the separate `SELECT` and `INSERT` with one atomic SQLite statement:

1. Use `INSERT ... ON CONFLICT(id) DO NOTHING`.
2. Commit the statement.
3. Return `True` only when one row was inserted; otherwise return `False`.
4. Keep the stored first payload immutable.

Do not add an application-wide lock. The database constraint is the correct
cross-process boundary.

### Tests and acceptance

- Multiple processes saving the same id produce one stored row.
- Exactly one call returns `True`; the others return `False`.
- No caller receives `IntegrityError` for a duplicate id.
- Saving a different id still works while duplicates are being retried.

## [ ] 7. Make family pool ids globally unique

**Location:** `src/total_orch/profiles/builder.py:488-491`

### Problem

Family pool ids use only the final family name. `openai/shared` and
`meta/shared` both become `shared-family`, so a dictionary keyed by pool id
loses one pool and overrides cannot identify the intended family.

### Fix

Build the id from the complete canonical family id using a stable safe format,
for example:

```text
openai/shared-family
meta/shared-family
```

Update the generated pool references, override validation, documentation, and
tests. Existing ids are MVP data, so make the naming rule explicit before
persisting or exposing these ids to users.

### Tests and acceptance

- Two families with the same leaf but different publishers produce two unique
  pool ids.
- Both pools contain only their own endpoints.
- Overrides can target either pool independently.
- Existing single-publisher family tests still pass.

## [ ] 8. Apply `explored` filtering before `LIMIT`

**Location:** `src/total_orch/router/store.py:100-129`

### Problem

The store applies SQL `LIMIT` before filtering `explored` in Python. With two
normal decisions followed by one explored decision,
`list_decisions(explored=True, limit=1)` returns an empty list.

### Fix

Choose one of these small fixes:

1. Add an `explored` column and filter it in SQL, keeping the payload as the
   source of truth; or
2. Fetch the filtered payloads first, then apply `limit` in Python.

The second option is the smallest change for the current MVP. Validate the
negative limit before querying as the code already does.

### Tests and acceptance

- Matching records are returned even when earlier records do not match.
- `limit=0` returns an empty list.
- `limit=None` returns every matching record.
- Existing policy and endpoint filters still combine correctly.

## [ ] 9. Enforce Ruff and mypy in CI

**Locations:** the deployment workflow around lines 56-58 and
`pyproject.toml:16-38`

### Problem

The project declares Ruff and strict mypy, but the CI build runs only pytest and
the package build. The current checkout already reports five Ruff errors and
35 mypy errors, so deployment can pass while the declared quality checks fail.

### Fix

1. Add `uv run ruff check .` to the validation commands.
2. Add `uv run mypy src tests` to the validation commands.
3. Fix the current errors rather than weakening strict mode or adding broad
   ignores.
4. Keep pytest and build after the static checks.

### Tests and acceptance

- A clean checkout passes pytest, Ruff, mypy, and build.
- Introducing a real type error makes CI fail.
- Introducing an unused import or the current exception-style violation makes
  CI fail.

## [ ] 10. Add bounded backoff to retryable completion failures

**Location:** `src/total_orch/gateway/_openai.py:402-413`

### Problem

Retryable 429 and 5xx responses are retried immediately. With several workers,
the retry wave can amplify rate limiting and increase latency and cost.

### Fix

1. Keep the existing retry count as the hard upper bound.
2. Read `Retry-After` when the transport exposes it.
3. Otherwise wait with capped exponential backoff and small random jitter.
4. Do not retry non-retryable errors or malformed successful responses.
5. Keep streaming retry behavior explicit; do not replay a stream after partial
   output unless the contract guarantees it is safe.

### Tests and acceptance

- A retryable failure waits before the next attempt.
- `Retry-After` is respected up to a safe cap.
- Non-retryable failures make one request only.
- The configured maximum number of attempts is never exceeded.

## [ ] Final validation

Run the full local validation after all checked sections are complete:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m mypy src tests
.venv/bin/python -m build
```

Review the diff, confirm that no credentials or generated files are included,
then commit the completed remediation work with a focused commit message.
