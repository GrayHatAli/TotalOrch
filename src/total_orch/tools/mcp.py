"""Minimal read-only MCP stdio client (MVP-0710).

This is a deliberately small spike, not a general MCP runtime: it performs the
JSON-RPC handshake, lists tools, and calls one named read-only tool. The
process command is deployment-configured; a model can never supply it.
"""

from __future__ import annotations

import json
import queue
import subprocess
from collections.abc import Sequence
from threading import Lock, Thread
from typing import Any, Self

from .models import ToolError

PROTOCOL_VERSION = "2024-11-05"


class McpStdioClient:
    """Speak newline-delimited JSON-RPC to one configured MCP server."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        allowed_tools: frozenset[str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not command:
            raise ValueError("MCP command must not be empty")
        self.timeout_seconds = timeout_seconds
        self.allowed_tools = allowed_tools
        self._process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = Lock()
        self._next_id = 0
        self._reader = Thread(target=self._read_loop, name="mcp-reader", daemon=True)
        self._reader.start()

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "total-orch", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools", [])
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.allowed_tools is not None and name not in self.allowed_tools:
            raise ToolError("mcp_tool_not_allowed", f"MCP tool is not allowlisted: {name}")
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            assert self._process.stdin is not None
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()
        while True:
            try:
                response = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise ToolError("mcp_timeout", f"MCP request timed out: {method}") from exc
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise ToolError("mcp_error", json.dumps(response["error"], ensure_ascii=False))
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._process.stdin is not None:
            self._process.stdin.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        with self._lock:
            self._process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
            )
            self._process.stdin.flush()

    def _read_loop(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._messages.put(message)
