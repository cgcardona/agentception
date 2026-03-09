from __future__ import annotations

"""Async stdio MCP client for the GitHub MCP server binary.

Spawns ``github-mcp-server stdio`` as a subprocess and communicates via
newline-delimited JSON-RPC 2.0 — the same protocol Cursor uses when it
runs the GitHub MCP server locally.

``GITHUB_PERSONAL_ACCESS_TOKEN`` is set from ``settings.github_token``
(env var ``GITHUB_TOKEN``) so no extra configuration is required beyond
what AgentCeption already expects.

Usage (inside the agent loop)::

    client = GitHubMCPClient()
    tools = await client.list_tools()      # cached after first call
    result = await client.call_tool("get_issue", {"owner": "...", "repo": "...", "issue_number": 42})
    await client.close()                   # terminate subprocess at end of run
"""

import asyncio
import json
import logging
import os

from agentception.config import settings
from agentception.services.llm import ToolDefinition, ToolFunction

logger = logging.getLogger(__name__)

_GH_MCP_BINARY = "github-mcp-server"
_MCP_PROTOCOL_VERSION = "2024-11-05"
_READ_TIMEOUT_SECS = 30.0
# GitHub MCP tools/list response can exceed asyncio's default 64 KB readline
# buffer.  Set a generous limit (16 MB) to accommodate it.
_STREAM_LIMIT = 16 * 1024 * 1024

# GitHub MCP tools we never expose to the agent — either dangerous (delete/fork)
# or irrelevant to AgentCeption's workflow.
_SKIP_TOOLS: frozenset[str] = frozenset(
    {
        "delete_file",
        "fork_repository",
        "create_repository",
        "push_files",
        "create_or_update_file",
    }
)


class GitHubMCPClient:
    """Async stdio MCP client for the GitHub MCP server binary.

    Spawns ``github-mcp-server stdio`` once and reuses the process for the
    lifetime of the agent run.  Messages are newline-delimited JSON-RPC 2.0.
    Tool calls are sequential — do NOT call :meth:`call_tool` concurrently.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id: int = 1
        self._tools_cache: list[ToolDefinition] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bump_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id

    async def _ensure_started(self) -> None:
        """Spawn and MCP-initialize the subprocess if not already running."""
        if self._proc is not None and self._proc.returncode is None:
            return

        token = settings.github_token
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is not set — GitHub MCP tools are unavailable. "
                "Set the GITHUB_TOKEN env var and restart the service."
            )

        env = {**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": token}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                _GH_MCP_BINARY,
                "stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                limit=_STREAM_LIMIT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"'{_GH_MCP_BINARY}' binary not found in PATH. "
                "Ensure it is installed in the Docker image."
            ) from exc

        # MCP session handshake.
        await self._request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentception", "version": "1.0"},
            },
        )
        await self._notify("notifications/initialized", {})
        logger.info("✅ GitHub MCP server started — pid=%d", self._proc.pid)

    async def _write(self, obj: dict[str, object]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_response(self, expected_id: int) -> dict[str, object]:
        """Read lines until we find the JSON-RPC response with *expected_id*."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            raw = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=_READ_TIMEOUT_SECS
            )
            if not raw:
                raise RuntimeError("GitHub MCP server closed stdout unexpectedly.")
            try:
                msg: object = json.loads(raw.decode())
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            # Skip notifications (no id) and messages for other request IDs.
            if "method" in msg or msg.get("id") != expected_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"GitHub MCP error: {msg['error']}")
            result: object = msg.get("result", {})
            return result if isinstance(result, dict) else {}

    async def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        req_id = self._bump_id()
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await self._read_response(req_id)

    async def _notify(self, method: str, params: dict[str, object]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[ToolDefinition]:
        """Return tool definitions from the GitHub MCP server (cached after first call)."""
        if self._tools_cache is not None:
            return self._tools_cache

        await self._ensure_started()
        result = await self._request("tools/list", {})
        raw: object = result.get("tools", [])
        if not isinstance(raw, list):
            raw = []

        defs: list[ToolDefinition] = []
        for tool in raw:
            if not isinstance(tool, dict):
                continue
            name: object = tool.get("name")
            if not isinstance(name, str) or name in _SKIP_TOOLS:
                continue
            description: object = tool.get("description", "")
            if not isinstance(description, str):
                description = ""
            schema_raw: object = tool.get("inputSchema", {})
            schema: dict[str, object] = (
                schema_raw if isinstance(schema_raw, dict) else {}
            )
            defs.append(
                ToolDefinition(
                    type="function",
                    function=ToolFunction(
                        name=name,
                        description=description,
                        parameters=schema,
                    ),
                )
            )

        self._tools_cache = defs
        logger.info("✅ GitHub MCP tools loaded — %d tools", len(defs))
        return defs

    async def call_tool(self, name: str, arguments: dict[str, object]) -> str:
        """Call a GitHub MCP tool and return the text content of the result."""
        await self._ensure_started()
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content: object = result.get("content", [])
        if isinstance(content, list):
            parts: list[str] = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(parts)
        return str(result)

    async def close(self) -> None:
        """Terminate the subprocess gracefully."""
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                pass
        self._proc = None
        self._tools_cache = None
