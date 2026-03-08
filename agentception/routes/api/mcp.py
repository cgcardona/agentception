from __future__ import annotations

"""HTTP Streamable MCP endpoint.

Exposes the AgentCeption MCP server over HTTP in addition to the stdio transport,
following the MCP 2025-03-26 Streamable HTTP transport specification.

Endpoint
--------
POST /api/mcp
    Accepts a JSON-RPC 2.0 request (single object or array of objects) and
    returns the corresponding response.

    Request body:  ``application/json`` — a JSON-RPC 2.0 message or batch
    Response body: ``application/json`` — a JSON-RPC 2.0 response or batch

    Notifications (messages without an ``id`` field) return ``202 Accepted``
    with no body.

Why this matters
----------------
The stdio transport works well for Cursor IDE sessions, where the MCP server is
spawned as a child process of the client.  The HTTP transport makes the same MCP
surface available to:
  - Web agents running outside Cursor
  - CI/CD pipelines that call MCP tools via ``curl`` or an HTTP client
  - Any MCP-aware client that supports the Streamable HTTP transport
  - Integration tests that use ``httpx.AsyncClient`` without Docker

The HTTP endpoint calls ``handle_request_async`` directly, so all async tools,
resource reads, and prompt fetches work identically over both transports.

Notes
-----
- No session management: each HTTP request is stateless.
- No server-sent events: the current MCP surface is request/response only.
  SSE streaming can be added in a future iteration when subscriptions land.
- No authentication: the endpoint is protected only by network access controls.
  Add API-key middleware when exposing outside a trusted network.
"""

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from agentception.mcp.server import handle_request_async

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])


@router.post("/mcp")
async def mcp_http_endpoint(request: Request) -> Response:
    """Handle a JSON-RPC 2.0 MCP request over HTTP.

    Supports single requests and JSON-RPC batch arrays.  Notifications
    (requests with no ``id``) return ``202 Accepted`` immediately.

    Args:
        request: The incoming FastAPI request object.

    Returns:
        - ``200 OK`` with JSON body for requests that produce a result.
        - ``202 Accepted`` with no body for JSON-RPC notifications.
        - ``400 Bad Request`` when the body is not valid JSON.
        - ``500 Internal Server Error`` for unexpected processing failures.
    """
    try:
        body = await request.json()
    except Exception as exc:
        logger.warning("⚠️ mcp_http: could not parse JSON body — %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            },
        )

    if isinstance(body, list):
        return await _handle_batch(body)

    return await _handle_single(body)


async def _handle_single(raw: object) -> Response:
    """Process a single JSON-RPC 2.0 request dict."""
    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: body must be an object or array"},
            },
        )
    request_dict: dict[str, object] = {k: v for k, v in raw.items()}
    try:
        result = await handle_request_async(request_dict)
    except Exception as exc:
        logger.error("❌ mcp_http: unexpected error — %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "jsonrpc": "2.0",
                "id": request_dict.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            },
        )
    if result is None:
        return Response(status_code=202)
    return JSONResponse(content=result)


async def _handle_batch(items: list[object]) -> Response:
    """Process a JSON-RPC 2.0 batch request array.

    Processes each item sequentially and collects results.  Notifications
    within a batch are silently dropped (no ``None`` entries in the output).
    Returns ``202 Accepted`` only when every item in the batch is a notification.
    """
    results: list[object] = []
    for item in items:
        if not isinstance(item, dict):
            results.append({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: batch item must be an object"},
            })
            continue
        item_dict: dict[str, object] = {k: v for k, v in item.items()}
        try:
            result = await handle_request_async(item_dict)
        except Exception as exc:
            logger.error("❌ mcp_http batch item error — %s", exc, exc_info=True)
            results.append({
                "jsonrpc": "2.0",
                "id": item_dict.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            })
            continue
        if result is not None:
            results.append(result)

    if not results:
        return Response(status_code=202)
    return JSONResponse(content=results)
