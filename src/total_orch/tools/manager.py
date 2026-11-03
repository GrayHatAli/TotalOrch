"""Sandboxed filesystem, git, and test tools (MVP-0702..0708).

Every path is resolved under one configured workspace root, symlink escapes
are rejected, and the test command is a fixed argument vector rather than a
model-supplied shell string. Permissions are checked per role before the
filesystem or a subprocess is touched.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock

from .models import ToolError, ToolResult

DEFAULT_TEST_COMMAND: tuple[str, ...] = (sys.executable, "-m", "pytest", "-q")

#: Directories that never participate in repository context.
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
    }
)

#: Role -> tool names the role may call. The workflow only needs this coarse
#: split; a deployment can pass a stricter mapping to the manager.
DEFAULT_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "planner": frozenset({"read", "list", "index", "context"}),
    "developer": frozenset(
        {"read", "list", "index", "context", "git_status", "git_diff", "apply_patch", "run_tests"}
    ),
    "reviewer": frozenset({"read", "list", "index", "context", "git_status", "git_diff"}),
    "tester": frozenset({"read", "list", "index", "context", "git_status", "git_diff", "run_tests"}),
}

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_HEADER_PATH = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$", re.MULTILINE)
_DIFF_GIT_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_RENAME_PATH = re.compile(r"^rename (?:from|to) (?:a/|b/)?(.+)$", re.MULTILINE)


class ToolManager:
    """One workspace-scoped tool surface with role-based permissions."""

    def __init__(
        self,
        root: str,
        *,
        permissions: Mapping[str, frozenset[str]] | None = None,
        test_command: Sequence[str] = DEFAULT_TEST_COMMAND,
        timeout_seconds: float = 60.0,
        max_output_chars: int = 32_768,
        max_files: int = 2_000,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        if not test_command:
            raise ValueError("test_command must not be empty")
        self.permissions = {role: frozenset(tools) for role, tools in (permissions or DEFAULT_PERMISSIONS).items()}
        self.test_command = tuple(test_command)
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.max_files = max_files
        self._process_lock = Lock()
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._cancelled = False

    # -- filesystem ---------------------------------------------------------

    def read(self, role: str, path: str, *, max_chars: int = 32_768) -> ToolResult:
        self._require(role, "read")
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError("not_a_file", f"not a regular file: {path}")
        data = target.read_bytes()
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            return ToolResult(
                ok=True,
                output=text[:max_chars] + "\n[truncated]",
                truncated=True,
                metadata={"path": self._relative(target)},
            )
        return ToolResult(ok=True, output=text, metadata={"path": self._relative(target)})

    def list_files(self, role: str, pattern: str = "**/*", *, limit: int = 200) -> ToolResult:
        self._require(role, "list")
        if limit < 1:
            raise ToolError("invalid_limit", "limit must be positive")
        files = [self._relative(path) for path in self._files(pattern)][:limit]
        return ToolResult(ok=True, output="\n".join(files), metadata={"count": len(files)})

    def project_metadata(self, role: str) -> ToolResult:
        self._require(role, "index")
        files = self._files()
        suffixes: dict[str, int] = {}
        for path in files:
            suffixes[path.suffix or "(none)"] = suffixes.get(path.suffix or "(none)", 0) + 1
        return ToolResult(
            ok=True,
            output=json.dumps(
                {
                    "root": self.root.name,
                    "files": len(files),
                    "languages": dict(sorted(suffixes.items(), key=lambda item: (-item[1], item[0]))),
                    "git": (self.root / ".git").exists(),
                },
                ensure_ascii=False,
            ),
        )

    def index(self, role: str) -> ToolResult:
        self._require(role, "index")
        entries = [
            {
                "path": self._relative(path),
                "size": path.stat().st_size,
                "suffix": path.suffix,
            }
            for path in self._files()
        ]
        return ToolResult(
            ok=True,
            output=json.dumps(entries, ensure_ascii=False),
            metadata={"count": len(entries)},
        )

    def context(self, role: str, query: str, *, max_files: int = 8, max_chars: int = 12_000) -> str:
        """Return provenance-tagged excerpts of the most relevant files."""

        self._require(role, "context")
        terms = {match.group(0).lower() for match in _TOKEN.finditer(query)}
        scored: list[tuple[int, Path]] = []
        for path in self._files():
            relative = self._relative(path).lower()
            path_score = sum(1 for term in terms if term in relative) * 5
            try:
                text = path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            content_score = sum(1 for term in terms if term in text)
            if path_score or content_score:
                scored.append((path_score + content_score, path))
        if not scored:
            return ""
        scored.sort(key=lambda item: (-item[0], self._relative(item[1])))
        chunks: list[str] = []
        used = 0
        for _score, path in scored[:max_files]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.count("\n") + 1
            block = f'<file path="{self._relative(path)}" lines="1-{lines}">\n{text}\n</file>'
            if used + len(block) > max_chars:
                block = block[: max(0, max_chars - used)]
            if block:
                chunks.append(block)
                used += len(block)
            if used >= max_chars:
                break
        return "\n".join(chunks)

    # -- git ----------------------------------------------------------------

    def git_status(self, role: str) -> ToolResult:
        self._require(role, "git_status")
        return self._git(("status", "--short"))

    def git_diff(self, role: str, path: str | None = None) -> ToolResult:
        self._require(role, "git_diff")
        if path is None:
            return self._git(("diff", "--"))
        self._resolve(path)
        return self._git(("diff", "--", path))

    def apply_patch(self, role: str, patch: str) -> ToolResult:
        self._require(role, "apply_patch")
        if not patch.strip():
            raise ToolError("empty_patch", "patch must not be empty")
        for path in _patch_paths(patch):
            self._resolve(path)
        check = self._git(("apply", "--check", "--whitespace=nowarn"), stdin=patch)
        if not check.ok:
            return check
        return self._git(("apply", "--whitespace=nowarn"), stdin=patch)

    # -- tests --------------------------------------------------------------

    def run_tests(self, role: str, *, timeout_seconds: float | None = None) -> ToolResult:
        self._require(role, "run_tests")
        return self._run(
            self.test_command,
            timeout=timeout_seconds or self.timeout_seconds,
        )

    def cancel(self) -> None:
        """Cancel the current tool invocation; the caller gets a non-zero result."""
        self._cancelled = True
        with self._process_lock:
            for process in list(self._active_processes):
                process.terminate()

    # -- internals ----------------------------------------------------------

    def _require(self, role: str, tool: str) -> None:
        allowed = self.permissions.get(role, frozenset())
        if tool not in allowed:
            raise ToolError("permission_denied", f"role {role!r} may not use tool {tool!r}")

    def _resolve(self, path: str) -> Path:
        if not path or "\x00" in path:
            raise ToolError("invalid_path", "path must not be empty")
        candidate = Path(path)
        if candidate.is_absolute():
            raise ToolError("path_escape", "absolute paths are not allowed")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError("path_escape", f"path escapes the workspace: {path}")
        relative = resolved.relative_to(self.root)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            raise ToolError("excluded_path", f"path is excluded from tool access: {path}")
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _files(self, pattern: str = "**/*") -> list[Path]:
        files: list[Path] = []
        for path in sorted(self.root.glob(pattern)):
            if len(files) >= self.max_files:
                break
            relative = path.relative_to(self.root)
            if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
                continue
            if path.is_file() and not path.is_symlink():
                files.append(path)
        return files

    def _git(self, args: Sequence[str], *, stdin: str | None = None) -> ToolResult:
        git = shutil.which("git")
        if git is None:
            raise ToolError("git_missing", "git is not installed")
        return self._run((git, *args), stdin=stdin)

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        if self._cancelled:
            raise ToolError("cancelled", "the tool invocation was cancelled")
        try:
            process = subprocess.Popen(
                list(args),
                cwd=self.root,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ToolError("command_missing", f"command not found: {args[0]}") from exc
        with self._process_lock:
            self._active_processes.add(process)
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout or self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise ToolError("timeout", f"command timed out after {timeout or self.timeout_seconds}s") from exc
        finally:
            with self._process_lock:
                self._active_processes.discard(process)
        output = (stdout + stderr).strip()
        truncated = len(output) > self.max_output_chars
        if truncated:
            output = output[: self.max_output_chars] + "\n[truncated]"
        return ToolResult(
            ok=process.returncode == 0,
            output=output,
            exit_code=process.returncode,
            truncated=truncated,
            metadata={"command": " ".join(args)},
        )


def _patch_paths(patch: str) -> tuple[str, ...]:
    paths: set[str] = set()
    for match in _DIFF_GIT_PATH.finditer(patch):
        paths.update((match.group(1), match.group(2)))
    for match in _HEADER_PATH.finditer(patch):
        paths.add(match.group(1))
    for match in _RENAME_PATH.finditer(patch):
        paths.add(match.group(1))
    paths.discard("/dev/null")
    if not paths:
        raise ToolError("invalid_patch", "patch contains no file paths")
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
            raise ToolError("path_escape", f"patch targets an unsafe path: {path}")
    return tuple(sorted(paths))
