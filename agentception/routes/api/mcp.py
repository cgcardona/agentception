"""HTTP Streamable MCP endpoint.

Exposes the AgentCeption MCP server over HTTP in addition to the stdio transport,
following the MCP 2025-11-25 Streamable HTTP transport specification.

Endpoints
---------
POST /api/mcp
    Accepts a JSON-RPC 2.0 request (single object or array of objects) and
    returns the corresponding response.

    Request body:  ``application/json`` — a JSON-RPC 2.0 message or batch
    Response body: ``application/json`` — a JSON-RPC 2.0 response or batch

    Notifications (messages without an ``id`` field) return ``202 Accepted``
    with no body.

GET /api/mcp
    Returns ``405 Method Not Allowed``.  The AgentCeption MCP surface is
    request/response only — no persistent SSE streams are offered.  Returning
    405 (rather than 404) is the correct signal for 2025-11-25-aware clients
    distinguishing this transport from the deprecated 2024-11-05 HTTP+SSE
    transport, which opened its stream via GET.

Why two transports
------------------
The stdio transport works well for local MCP client sessions, where the MCP
server is spawned as a child process.  The HTTP transport makes the same MCP
surface available to:
  - Agents running server-side without a local MCP client
  - CI/CD pipelines that call MCP tools via ``curl`` or an HTTP client
  - Any MCP-aware client that supports the Streamable HTTP transport
  - Integration tests that use ``httpx.AsyncClient`` without Docker

The HTTP endpoint calls ``handle_request_async`` directly, so all async tools,
resource reads, and prompt fetches work identically over both transports.

Security
--------
Origin validation (§ Streamable HTTP, 2025-11-25)
  The MCP spec requires servers to validate the ``Origin`` header on all HTTP
  connections to prevent DNS rebinding attacks.  If the header is present and
  the host is not ``localhost`` or ``127.0.0.1``, the server responds with
  ``403 Forbidden``.  Programmatic MCP clients (agents, CI) never send an
  ``Origin`` header, so legitimate callers are unaffected.

MCP-Protocol-Version header (§ Streamable HTTP, 2025-11-25)
  Clients MUST include ``MCP-Protocol-Version`` on all requests after
  initialization.  If present, the server validates it against the set of
  supported versions.  Unsupported versions return ``400 Bad Request``.
  Absent headers are accepted for backwards compatibility (spec allows servers
  to assume ``2025-03-26`` in that case).

Notes
-----
- No session management: each HTTP request is stateless.  Session IDs are
  optional for stateless servers per the spec.
- No server-sent events: the current MCP surface is request/response only.
- Authentication: when ``AC_API_KEY`` is set, this endpoint is protected by
  ``ApiKeyMiddleware`` (all ``/api/*`` routes). See the Security guide for
  client configuration.
"""

from __future__ import annotations


import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from agentception.mcp.server import handle_request_async
from agentception.mcp.types import JsonRpcErrorResponse, JsonRpcSuccessResponse
from agentception.types import JsonValue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

#: Protocol versions this server accepts on the ``MCP-Protocol-Version`` header.
#: Older versions are allowed for backwards compatibility; unknown future versions
#: are rejected with 400 so clients learn they need to negotiate downward.
_SUPPORTED_PROTOCOL_VERSIONS: frozenset[str] = frozenset({"2025-11-25", "2025-03-26"})

#: Hostnames accepted in the ``Origin`` header.  Any other host triggers a 403
#: to block DNS rebinding attacks from browser-based pages.
_ALLOWED_ORIGIN_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


def _check_origin(request: Request) -> Response | None:
    """Return a 403 response if the ``Origin`` header is present and invalid.

    Programmatic clients (agents, curl, httpx) never set ``Origin``, so this
    guard has zero impact on legitimate API use.  It blocks only browser pages
    attempting cross-origin requests — the DNS rebinding attack vector described
    in the MCP 2025-11-25 security requirements.

    Returns ``None`` when the request should proceed normally.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return None
    try:
        host = urlparse(origin).hostname or ""
    except Exception:
        host = ""
    if host not in _ALLOWED_ORIGIN_HOSTS:
        logger.warning("⚠️ mcp_http: rejected request with invalid Origin %r", origin)
        return JSONResponse(
            status_code=403,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": f"Forbidden: invalid Origin {origin!r}"},
            },
        )
    return None


def _check_protocol_version(request: Request) -> Response | None:
    """Return a 400 response if ``MCP-Protocol-Version`` is present but unsupported.

    The header is optional (absent → backwards-compatible 2025-03-26 assumed).
    Returns ``None`` when the request should proceed normally.
    """
    version = request.headers.get("mcp-protocol-version")
    if version is None:
        return None
    if version not in _SUPPORTED_PROTOCOL_VERSIONS:
        logger.warning("⚠️ mcp_http: unsupported MCP-Protocol-Version %r", version)
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": (
                        f"Unsupported MCP-Protocol-Version {version!r}. "
                        f"Supported: {sorted(_SUPPORTED_PROTOCOL_VERSIONS)}"
                    ),
                },
            },
        )
    return None


@router.get("/mcp")
async def mcp_http_get(_request: Request) -> Response:
    """Reject GET requests with 405 Method Not Allowed.

    The AgentCeption HTTP transport is request/response only — no persistent
    SSE stream is offered.  Returning 405 (not 404) is the correct signal for
    2025-11-25-aware clients that use GET to distinguish Streamable HTTP from
    the deprecated 2024-11-05 HTTP+SSE transport.
    """
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_http_endpoint(request: Request) -> Response:
    """Handle a JSON-RPC 2.0 MCP request over HTTP.

    Supports single requests and JSON-RPC batch arrays.  Notifications
    (requests with no ``id``) return ``202 Accepted`` immediately.

    Security guards run first:
      - Invalid ``Origin`` → 403 (DNS rebinding protection)
      - Unsupported ``MCP-Protocol-Version`` → 400

    Args:
        request: The incoming FastAPI request object.

    Returns:
        - ``200 OK`` with JSON body for requests that produce a result.
        - ``202 Accepted`` with no body for JSON-RPC notifications.
        - ``400 Bad Request`` when the body is not valid JSON or the protocol version is unsupported.
        - ``403 Forbidden`` when the ``Origin`` header is present but invalid.
        - ``500 Internal Server Error`` for unexpected processing failures.
    """
    if (guard := _check_origin(request)) is not None:
        return guard
    if (guard := _check_protocol_version(request)) is not None:
        return guard

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


async def _handle_single(raw: JsonValue) -> Response:
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
    request_dict: dict[str, JsonValue] = {k: v for k, v in raw.items()}
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


async def _handle_batch(items: list[JsonValue]) -> Response:
    """Process a JSON-RPC 2.0 batch request array.

    Processes each item sequentially and collects results.  Notifications
    within a batch are silently dropped (no ``None`` entries in the output).
    Returns ``202 Accepted`` only when every item in the batch is a notification.
    """
    results: list[JsonRpcSuccessResponse | JsonRpcErrorResponse | dict[str, JsonValue]] = []
    for item in items:
        if not isinstance(item, dict):
            results.append({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: batch item must be an object"},
            })
            continue
        item_dict: dict[str, JsonValue] = {k: v for k, v in item.items()}
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
