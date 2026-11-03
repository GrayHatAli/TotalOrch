"""Workflow adapter over the workspace tool manager (MVP-0706..0708)."""

from __future__ import annotations

from total_orch.workflow.models import TestResult
from total_orch.workflow.runtime import WorkflowToolbox

from .manager import ToolManager
from .models import ToolResult


class ToolWorkflowAdapter(WorkflowToolbox):
    """Expose the safe tool subset a workflow node may use."""

    def __init__(self, manager: ToolManager) -> None:
        self.manager = manager

    def context(self, workspace: str, query: str) -> str:
        return self.manager.context("developer", query)

    def run_tests(self, workspace: str) -> TestResult:
        result = self.manager.run_tests("tester")
        return TestResult(
            ran=True,
            passed=result.exit_code == 0,
            command=str(result.metadata.get("command", "")),
            output=result.output,
            exit_code=result.exit_code,
        )

    def apply_diff(self, workspace: str, diff: str) -> ToolResult:
        return self.manager.apply_patch("developer", diff)
