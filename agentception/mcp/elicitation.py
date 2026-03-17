from __future__ import annotations

"""MCP elicitation — server-initiated human-in-the-loop requests.

Implements the ``elicitation/create`` protocol from MCP 2025-11-25.  The
server sends an ``elicitation/create`` JSON-RPC request to a connected
dashboard session and blocks until the human responds, declines, or the
timeout expires.

``request_human_input`` is the MCP *tool* implementation — agents call it via
``tools/call`` when they need a decision that only the human can provide:
architectural choices, credential approval, branching strategy, etc.

Flow
----
1. Agent calls ``request_human_input(message=..., fields=[...])``.
2. Tool selects the first dashboard session that declared
   ``elicitation_form`` capability (MCP session in the browser).
3. Server puts an ``elicitation/create`` JSON-RPC request into the session's
   outbound queue; the SSE stream delivers it to the dashboard.
4. Dashboard renders a form modal.  Human fills it in and clicks Submit.
5. Dashboard POSTs the JSON-RPC response back to ``POST /api/mcp``.
6. HTTP route calls :func:`~agentception.mcp.sessions.McpSessionStore.resolve_response`
   which resolves the pending :class:`asyncio.Future`.
7. :func:`send_form_elicitation` returns the result to the tool call handler.
8. Agent receives a dict with ``action`` and (optionally) ``content``.
"""

import asyncio
import logging
import secrets

from agentception.mcp.sessions import McpSession, get_store
from agentception.mcp.types import ElicitationField, ElicitationResult
from agentception.types import JsonValue

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT: float = 300.0  # seconds before the tool auto-cancels


def _build_json_schema(fields: list[ElicitationField]) -> dict[str, JsonValue]:
    """Convert the simplified ``ElicitationField`` list to a JSON Schema.

    The spec requires a flat ``"object"`` schema with only primitive-typed
    properties (``string``, ``number``, ``integer``, ``boolean``).  We pass
    the schema verbatim in the ``elicitation/create`` params so the
    dashboard can render an appropriate form.
    """
    properties: dict[str, JsonValue] = {}
    required_keys: list[JsonValue] = []

    for field in fields:
        prop: dict[str, JsonValue] = {"type": field["type"]}

        if "title" in field:
            prop["title"] = field["title"]
        if "description" in field:
            prop["description"] = field["description"]
        if "default" in field:
            prop["default"] = field["default"]

        ftype = field.get("type")
        if ftype == "string":
            if "enum" in field:
                prop["enum"] = list(field["enum"])
            if "format" in field:
                prop["format"] = field["format"]
        if ftype in ("number", "integer"):
            if "minimum" in field:
                prop["minimum"] = field["minimum"]
            if "maximum" in field:
                prop["maximum"] = field["maximum"]

        properties[field["name"]] = prop
        if field.get("required", False):
            required_keys.append(field["name"])

    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": properties,
    }
    if required_keys:
        schema["required"] = required_keys
    return schema


async def send_form_elicitation(
    session: McpSession,
    message: str,
    schema: dict[str, JsonValue],
    timeout_seconds: float = _DEFAULT_TIMEOUT,
) -> ElicitationResult:
    """Send ``elicitation/create`` (form mode) to *session* and await reply.

    The server generates a unique ``id`` for the JSON-RPC request, registers
    a :class:`asyncio.Future` in ``session.pending``, then puts the request
    into the session's outbound queue.  The SSE stream delivers it to the
    dashboard within milliseconds.

    The coroutine suspends until either:
    - The client POSTs a JSON-RPC response with the matching ``id``, or
    - *timeout_seconds* elapses (raises :class:`asyncio.TimeoutError`).

    Raises
    ------
    asyncio.TimeoutError
        When the human does not respond within *timeout_seconds*.
    """
    elicitation_id = secrets.token_urlsafe(16)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, JsonValue]] = loop.create_future()
    session.pending[elicitation_id] = fut

    rpc_request: dict[str, JsonValue] = {
        "jsonrpc": "2.0",
        "id": elicitation_id,
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "requestedSchema": schema,
        },
    }
    await session.outbound.put(rpc_request)
    logger.info(
        "🙋 elicitation/create → session %s (elicitation_id=%s)",
        session.session_id[:8],
        elicitation_id[:8],
    )

    try:
        raw: dict[str, JsonValue] = await asyncio.wait_for(
            asyncio.shield(fut), timeout=float(timeout_seconds)
        )
    except asyncio.TimeoutError:
        session.pending.pop(elicitation_id, None)
        if not fut.done():
            fut.cancel()
        raise

    raw_content = raw.get("content")
    content: dict[str, JsonValue] = (
        {k: v for k, v in raw_content.items()}
        if isinstance(raw_content, dict)
        else {}
    )

    action = str(raw.get("action", "cancel"))
    result = ElicitationResult(action=action)
    if action == "accept" and content:
        result["content"] = content
    return result


async def request_human_input(
    message: str,
    fields: list[ElicitationField],
    run_id: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
) -> dict[str, JsonValue]:
    """MCP tool: block until a human operator provides structured input.

    Selects the first session with ``elicitation_form`` capability, sends an
    ``elicitation/create`` request, and returns the human's response.

    Returns a dict with:
        ``action``  — ``"accept"`` | ``"decline"`` | ``"cancel"`` |
                      ``"timeout"`` | ``"no_client"``
        ``content`` — submitted form data (only when action == "accept")
        ``message`` — human-readable outcome summary

    When no elicitation-capable session is connected (no browser tab open on
    Mission Control) the tool returns immediately with ``action="no_client"``
    rather than blocking indefinitely.
    """
    store = get_store()
    sessions = store.elicitation_sessions(mode="form")

    if not sessions:
        logger.warning(
            "⚠️ request_human_input: no elicitation-capable session (run=%r)",
            run_id,
        )
        return {
            "action": "no_client",
            "message": (
                "No dashboard session with elicitation capability is connected. "
                "Open Mission Control in your browser to enable real-time "
                "human-in-the-loop input for running agents."
            ),
        }

    session = sessions[0]
    schema = _build_json_schema(fields)
    ctx = f" (run: {run_id})" if run_id else ""
    logger.info(
        "🙋 request_human_input%s → session %s",
        ctx,
        session.session_id[:8],
    )

    try:
        result = await send_form_elicitation(
            session,
            message,
            schema,
            timeout_seconds=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "⚠️ request_human_input: timeout after %ds (session=%s run=%r)",
            timeout_seconds,
            session.session_id[:8],
            run_id,
        )
        return {
            "action": "timeout",
            "message": (
                f"Human did not respond within {timeout_seconds}s. "
                "Proceed with your best judgment or configured defaults."
            ),
        }

    action = result["action"]
    out: dict[str, JsonValue] = {"action": action}
    if action == "accept":
        out["content"] = result.get("content") or {}
        out["message"] = "Human provided input — proceed with the submitted values."
    elif action == "decline":
        out["message"] = "Human declined to provide input."
    else:
        out["message"] = "Human dismissed the request without acting."

    logger.info(
        "✅ request_human_input: action=%r (session=%s run=%r)",
        action,
        session.session_id[:8],
        run_id,
    )
    return out
