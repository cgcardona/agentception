from __future__ import annotations

"""AgentCeption MCP stdio transport entry point.

Reads JSON-RPC 2.0 requests from stdin (one per line) and writes
responses to stdout — the standard Cursor/Claude MCP stdio transport.

Usage (via Cursor mcp.json):
    docker compose exec -T agentception python -m agentception.mcp.stdio_server

Or directly:
    python -m agentception.mcp.stdio_server
"""

import json
import logging
import sys

from agentception.mcp.server import handle_request

logger = logging.getLogger(__name__)


def _run() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
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

        response = handle_request(request)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    _run()
