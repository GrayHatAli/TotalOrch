"""Sandboxed tool tests (MVP-0701..0710)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from total_orch.tools import ToolError, ToolManager, ToolWorkflowAdapter


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("def health():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\nA health endpoint demo.\n", encoding="utf-8")
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / ".git" / "secret").write_text("nope", encoding="utf-8")
    return tmp_path


def test_read_and_context_stay_inside_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tools = ToolManager(str(workspace), test_command=(sys.executable, "-c", "print('ok')"))

    read = tools.read("developer", "src/app.py")
    context = tools.context("developer", "health endpoint")
    outside = workspace.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    assert read.ok is True
    assert "def health" in read.output
    assert '<file path="src/app.py"' in context or '<file path="README.md"' in context
    with pytest.raises(ToolError, match="escapes"):
        tools.read("developer", "../outside.txt")
    with pytest.raises(ToolError, match="excluded"):
        tools.read("developer", ".git/secret")


def test_role_permissions_deny_writes_and_tests(tmp_path: Path) -> None:
    tools = ToolManager(str(_workspace(tmp_path)))

    with pytest.raises(ToolError, match="may not use tool"):
        tools.apply_patch("planner", "diff --git a/x b/x")
    with pytest.raises(ToolError, match="may not use tool"):
        tools.run_tests("reviewer")
    assert tools.list_files("reviewer").ok is True


def test_apply_patch_applies_a_validated_diff(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.test")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "initial")
    tools = ToolManager(str(workspace))

    patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def health():\n"
        "-    return 'ok'\n"
        "+    return 'healthy'\n"
    )
    result = tools.apply_patch("developer", patch)

    assert result.ok is True, result.output
    assert "'healthy'" in (workspace / "src" / "app.py").read_text(encoding="utf-8")


def test_apply_patch_rejects_a_path_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tools = ToolManager(str(workspace))

    with pytest.raises(ToolError, match="unsafe path"):
        tools.apply_patch(
            "developer",
            "diff --git a/../evil.py b/../evil.py\n--- a/../evil.py\n+++ b/../evil.py\n",
        )


def test_run_tests_uses_a_fixed_command_and_reports_exit_code(tmp_path: Path) -> None:
    tools = ToolManager(
        str(_workspace(tmp_path)),
        test_command=(sys.executable, "-c", "print('suite-ok')"),
    )

    passed = tools.run_tests("tester")
    failing = ToolManager(
        str(_workspace(tmp_path)),
        test_command=(sys.executable, "-c", "raise SystemExit(3)"),
    ).run_tests("tester")

    assert passed.ok is True
    assert passed.exit_code == 0
    assert "suite-ok" in passed.output
    assert failing.ok is False
    assert failing.exit_code == 3


def test_tool_timeout_is_normalized(tmp_path: Path) -> None:
    tools = ToolManager(
        str(_workspace(tmp_path)),
        test_command=(sys.executable, "-c", "import time; time.sleep(2)"),
        timeout_seconds=0.1,
    )

    with pytest.raises(ToolError, match="timed out"):
        tools.run_tests("tester")


def test_workflow_adapter_converts_tool_results_to_test_results(tmp_path: Path) -> None:
    tools = ToolWorkflowAdapter(
        ToolManager(
            str(_workspace(tmp_path)),
            test_command=(sys.executable, "-c", "print('ok')"),
        )
    )

    result = tools.run_tests(str(tmp_path))

    assert result.ran is True
    assert result.passed is True
    assert result.exit_code == 0
    assert "ok" in result.output


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
