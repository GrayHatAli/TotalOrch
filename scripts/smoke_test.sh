#!/usr/bin/env bash
# Clean-install smoke test for TotalOrch. Requires network access for wheels.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$(mktemp -d)/smoke-venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "$ROOT"
"$VENV/bin/python" - <<'PY'
from total_orch.api import TotalOrchApplication
from total_orch.config import Settings
from total_orch.gateway import FakeGatewayAdapter
from total_orch.profiles import BUILTIN_AGENT_DEFINITIONS
from total_orch.registry import IdentityRules, ModelRegistryService, ModelRegistryStore
from total_orch.workflow import WorkflowRuntime

store = ModelRegistryStore(":memory:")
store.initialize()
registry = ModelRegistryService(store, IdentityRules())
app = TotalOrchApplication(
    Settings(environment="test"),
    registry,
    FakeGatewayAdapter(),
)
health = app.handle("GET", "/health")
assert health.status_code == 200, health
assert BUILTIN_AGENT_DEFINITIONS
assert WorkflowRuntime is not None
print("smoke ok")
PY
rm -rf "$(dirname "$VENV")"
