"""HTTP Streamable MCP endpoint — MCP 2025-11-25 compliant.

Exposes the AgentCeption MCP server over HTTP following the MCP 2025-11-25
Streamable HTTP transport specification, including session management, SSE
push for server-initiated requests (elicitation), and security guards.

Endpoints
---------
POST /api/mcp
    Accepts a JSON-RPC 2.0 request or batch.  On ``initialize``, creates a
    session and returns ``MCP-Session-Id``.  When a session ID header is present
    and the body is a JSON-RPC *response* (has ``id``, no ``method``), routes it
    to a pending :class:`asyncio.Future` and returns ``202 Accepted``.

GET /api/mcp
    Opens an SSE stream when ``Accept: text/event-stream`` and a valid
    ``MCP-Session-Id`` are present.  The server pushes server-initiated
    JSON-RPC requests (e.g. ``elicitation/create``) down this stream.
    Returns ``405 Method Not Allowed`` when the Accept header is absent —
    the correct signal per the spec for clients distinguishing Streamable HTTP
    from the deprecated 2024-11-05 HTTP+SSE transport.

DELETE /api/mcp
    Terminates the session identified by ``MCP-Session-Id`` and cancels any
    pending elicitation futures.

Session lifecycle
-----------------
1. Dashboard POSTs ``initialize`` with ``capabilities.elicitation``.
2. Server creates an :class:`~agentception.mcp.sessions.McpSession`, returns
   ``MCP-Session-Id`` in the response header.
3. Dashboard GETs ``/api/mcp`` with that header → SSE stream opens.
4. When an agent calls ``request_human_input``, the server puts an
   ``elicitation/create`` request into the session's outbound queue.
5. SSE stream delivers the request to the browser.
6. Human fills the form → browser POSTs the JSON-RPC response.
7. Server resolves the pending future → agent's tool call returns.
8. Dashboard DELETEs ``/api/mcp`` on page unload.

Security
--------
*Origin validation* — requests with an ``Origin`` header from a non-localhost
host return ``403 Forbidden`` (DNS rebinding protection).

*MCP-Protocol-Version* — if present, must be ``2025-11-25`` or ``2025-03-26``;
any other value returns ``400 Bad Request``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from agentception.mcp.server import handle_request_async
from agentception.mcp.sessions import McpSession, get_store
from agentception.mcp.types import JsonRpcErrorResponse, JsonRpcSuccessResponse
from agentception.types import JsonValue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

#: Protocol versions accepted on the ``MCP-Protocol-Version`` header.
_SUPPORTED_PROTOCOL_VERSIONS: frozenset[str] = frozenset({"2025-11-25", "2025-03-26"})

#: Hostnames accepted in the ``Origin`` header.
_ALLOWED_ORIGIN_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})

#: SSE keepalive interval in seconds — prevents proxy/CDN timeouts.
_SSE_KEEPALIVE_SECONDS: float = 15.0


# ---------------------------------------------------------------------------
# Security guards
# ---------------------------------------------------------------------------


def _check_origin(request: Request) -> Response | None:
    """Return 403 if the ``Origin`` header is present and from a non-localhost host.

    Programmatic clients (agents, curl, httpx) never send ``Origin``, so this
    guard does not affect legitimate API callers.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return None
    try:
        host = urlparse(origin).hostname or ""
    except Exception:
        host = ""
    if host not in _ALLOWED_ORIGIN_HOSTS:
        logger.warning("⚠️ mcp_http: rejected invalid Origin %r", origin)
        return JSONResponse(
            status_code=403,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": f"Forbidden: invalid Origin {origin!r}",
                },
            },
        )
    return None


def _check_protocol_version(request: Request) -> Response | None:
    """Return 400 if ``MCP-Protocol-Version`` is present but unsupported."""
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


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _is_jsonrpc_response(body: dict[str, JsonValue]) -> bool:
    """True when *body* is a JSON-RPC response (has ``id``, no ``method``)."""
    return (
        "id" in body
        and "method" not in body
        and ("result" in body or "error" in body)
    )


def _parse_elicitation_caps(body: dict[str, JsonValue]) -> tuple[bool, bool]:
    """Extract elicitation capability flags from an ``initialize`` request body.

    Returns ``(form, url)`` booleans.  An empty ``elicitation`` object ``{}``
    is treated as form-mode support per the spec convention.
    """
    params = body.get("params")
    if not isinstance(params, dict):
        return False, False
    caps = params.get("capabilities")
    if not isinstance(caps, dict):
        return False, False
    elicitation = caps.get("elicitation")
    if not isinstance(elicitation, dict):
        return False, False
    # Empty object {} → form mode; explicit keys override
    if not elicitation:
        return True, False
    return "form" in elicitation, "url" in elicitation


# ---------------------------------------------------------------------------
# SSE stream generator
# ---------------------------------------------------------------------------


async def _sse_generator(session: McpSession, request: Request) -> AsyncIterator[str]:
    """Yield SSE events from *session*'s outbound queue until disconnected.

    Sends a ``: keepalive`` comment every :data:`_SSE_KEEPALIVE_SECONDS` to
    prevent proxy timeouts.  The loop exits cleanly when the client disconnects
    or the session is deleted (queue raises ``asyncio.CancelledError``).
    """
    logger.info("📡 SSE stream opened — session %s", session.session_id[:8])
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(
                    session.outbound.get(), timeout=_SSE_KEEPALIVE_SECONDS
                )
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                logger.debug(
                    "📡 SSE → session %s method=%r",
                    session.session_id[:8],
                    msg.get("method"),
                )
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
    finally:
        logger.info("📡 SSE stream closed — session %s", session.session_id[:8])


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get("/mcp")
async def mcp_http_get(request: Request) -> Response:
    """Open an SSE stream or return 405.

    When ``Accept: text/event-stream`` is present and a valid
    ``MCP-Session-Id`` identifies an existing session, upgrades the connection
    to a persistent SSE stream that delivers server-initiated JSON-RPC requests
    (e.g. ``elicitation/create``) to the browser dashboard.

    Returns ``405 Method Not Allowed`` when the ``Accept`` header does not
    include ``text/event-stream`` — the correct signal for MCP 2025-11-25
    clients that probe for SSE support via GET.
    """
    if (guard := _check_origin(request)) is not None:
        return guard

    accept = request.headers.get("accept", "")
    if "text/event-stream" not in accept:
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    "MCP-Session-Id header required for SSE stream. "
                    "Initialize first via POST /api/mcp."
                )
            },
        )

    store = get_store()
    session = store.get(session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": (
                    f"Session not found: {session_id!r}. "
                    "The session may have expired or been deleted."
                )
            },
        )

    return StreamingResponse(
        _sse_generator(session, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "MCP-Session-Id": session_id,
        },
    )


@router.delete("/mcp")
async def mcp_http_delete(request: Request) -> Response:
    """Terminate the session identified by ``MCP-Session-Id``.

    Cancels any pending elicitation futures so tools blocked on human input
    return immediately with a ``cancel`` action rather than hanging until
    their timeout elapses.
    """
    if (guard := _check_origin(request)) is not None:
        return guard

    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        return Response(status_code=400)

    get_store().delete(session_id)
    logger.info("🗑️  MCP session terminated via DELETE: %s", session_id[:8])
    return Response(status_code=200)


@router.post("/mcp")
async def mcp_http_endpoint(request: Request) -> Response:
    """Handle a JSON-RPC 2.0 MCP request, response, or batch over HTTP.

    Three message types are accepted:

    *JSON-RPC requests* — dispatched through ``handle_request_async``.
    On ``initialize``, a new session is created and its ID is returned in
    the ``MCP-Session-Id`` response header.

    *JSON-RPC responses* — when a session ID is present and the body has
    an ``id`` but no ``method``, the message is a client response to a
    server-initiated request (e.g. ``elicitation/create``).  It is routed
    to the matching pending :class:`asyncio.Future` and returns ``202``.

    *JSON-RPC notifications* — requests with no ``id`` field; acknowledged
    silently with ``202 Accepted`` and no body.
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

    session_id = request.headers.get("mcp-session-id")

    if isinstance(body, list):
        return await _handle_batch(body, session_id=session_id)

    return await _handle_single(body, session_id=session_id)


async def _handle_single(
    raw: JsonValue,
    *,
    session_id: str | None,
) -> Response:
    """Process a single JSON-RPC 2.0 message."""
    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request: body must be an object or array",
                },
            },
        )

    request_dict: dict[str, JsonValue] = {k: v for k, v in raw.items()}
    store = get_store()

    # ── Route client→server JSON-RPC responses ────────────────────────────────
    # A response has ``id`` and ``result``/``error`` but no ``method``.
    # These are replies to server-initiated requests (elicitation/create).
    if _is_jsonrpc_response(request_dict) and session_id:
        rpc_id = request_dict.get("id")
        result_raw = request_dict.get("result")
        if isinstance(rpc_id, (str, int)) and isinstance(result_raw, dict):
            result_dict: dict[str, JsonValue] = {k: v for k, v in result_raw.items()}
            resolved = store.resolve_response(session_id, rpc_id, result_dict)
            if resolved:
                logger.info(
                    "✅ mcp_http: resolved elicitation response id=%r session=%s",
                    rpc_id,
                    session_id[:8],
                )
            else:
                logger.warning(
                    "⚠️ mcp_http: no pending future for response id=%r session=%s",
                    rpc_id,
                    session_id,
                )
        return Response(status_code=202)

    # ── Handle initialize — create session ────────────────────────────────────
    method = request_dict.get("method")
    extra_headers: dict[str, str] = {}

    if method == "initialize":
        elicitation_form, elicitation_url = _parse_elicitation_caps(request_dict)
        session = store.create(
            elicitation_form=elicitation_form,
            elicitation_url=elicitation_url,
        )
        extra_headers["MCP-Session-Id"] = session.session_id
        logger.info(
            "✅ mcp_http: initialize — session %s created (form=%s url=%s)",
            session.session_id[:8],
            elicitation_form,
            elicitation_url,
        )

    # ── Dispatch JSON-RPC request ─────────────────────────────────────────────
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
        return Response(status_code=202, headers=extra_headers)

    return JSONResponse(content=result, headers=extra_headers)


async def _handle_batch(
    items: list[JsonValue],
    *,
    session_id: str | None,
) -> Response:
    """Process a JSON-RPC 2.0 batch request array."""
    results: list[JsonRpcSuccessResponse | JsonRpcErrorResponse | dict[str, JsonValue]] = []
    for item in items:
        if not isinstance(item, dict):
            results.append({
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request: batch item must be an object",
                },
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
