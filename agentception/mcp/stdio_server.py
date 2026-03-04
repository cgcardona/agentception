from __future__ import annotations

"""AgentCeption MCP stdio transport entry point.

Reads JSON-RPC 2.0 requests from stdin (one per line) and writes
responses to stdout — the standard Cursor/Claude MCP stdio transport.

The event loop is owned here: :func:`_run` is a coroutine driven by
:func:`asyncio.run` so that async MCP tools (build tools, plan_get_labels,
plan_spawn_coordinator) are awaited correctly.  Using the sync
:func:`~agentception.mcp.server.handle_request` path caused those tools to
return an error instead of executing.

Usage (via Cursor mcp.json):
    docker compose exec -T agentception python -m agentception.mcp.stdio_server

Or directly:
    python -m agentception.mcp.stdio_server
"""

import asyncio
import json
import logging
import sys

from agentception.db.engine import init_db
from agentception.mcp.server import handle_request_async

logger = logging.getLogger(__name__)


async def _run() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        stream=sys.stderr,
        format="%(asctime)s [MCP] %(levelname)s %(name)s — %(message)s",
    )
    logger.warning("🚀 MCP stdio_server starting — initialising DB")
    try:
        await init_db()
        logger.warning("✅ MCP stdio_server DB ready")
    except Exception as exc:
        logger.warning("❌ MCP stdio_server init_db FAILED: %s", exc)
        raise

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            request: dict[str, object] = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            response: dict[str, object] = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        method = request.get("method", "<no-method>")
        params = request.get("params", {})
        tool_name = params.get("name", "") if isinstance(params, dict) else ""
        logger.warning(
            "📨 MCP request: method=%r tool=%r id=%r",
            method,
            tool_name or "(n/a)",
            request.get("id"),
        )

        maybe_response: dict[str, object] | None = await handle_request_async(request)
        if maybe_response is None:
            # JSON-RPC notification — no response on the wire.
            logger.warning("📭 MCP notification (no response): method=%r", method)
            continue
        logger.warning("📤 MCP response: method=%r error=%r", method, maybe_response.get("error"))
        sys.stdout.write(json.dumps(maybe_response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_run())
