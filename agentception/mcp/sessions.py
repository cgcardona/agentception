from __future__ import annotations

"""MCP session store for the HTTP Streamable transport.

Each session represents a persistent connection from an MCP client (typically
the AgentCeption dashboard) that has performed the MCP initialization handshake
and opened a GET SSE stream to receive server-initiated messages.

Session lifecycle
-----------------
1. Client POSTs ``initialize`` → server creates a :class:`McpSession`, returns
   ``MCP-Session-Id`` response header.
2. Client GETs ``/api/mcp`` with that header and ``Accept: text/event-stream``
   → server streams SSE events from the session's outbound queue.
3. When an agent calls ``request_human_input``, the tool puts an
   ``elicitation/create`` JSON-RPC request into the outbound queue.
4. The dashboard receives the SSE event and shows a form modal to the human.
5. Human submits the form → dashboard POSTs the JSON-RPC response back to
   ``POST /api/mcp``.
6. The HTTP route calls :meth:`McpSessionStore.resolve_response` which resolves
   the pending :class:`asyncio.Future` so the tool call returns to the agent.
7. Client DELETEs ``/api/mcp`` or disconnects → session is cleaned up.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass, field

from agentception.types import JsonValue

logger = logging.getLogger(__name__)


@dataclass
class McpSession:
    """State for one active MCP client session.

    All attributes except ``session_id`` are mutable and updated as the
    session progresses through initialization and elicitation.
    """

    session_id: str

    #: Client declared support for form-mode elicitation.
    elicitation_form: bool = False
    #: Client declared support for URL-mode elicitation.
    elicitation_url: bool = False

    #: Server→client message queue.  The GET SSE handler reads from here,
    #: yielding each item as an SSE ``data:`` event.
    outbound: asyncio.Queue[dict[str, JsonValue]] = field(
        default_factory=asyncio.Queue
    )

    #: Pending server-initiated RPC calls awaiting client responses.
    #: Keys are the ``id`` values the server put in its JSON-RPC requests.
    pending: dict[str | int, asyncio.Future[dict[str, JsonValue]]] = field(
        default_factory=dict
    )


class McpSessionStore:
    """Thread-safe (asyncio-safe) in-memory session registry.

    Instantiated once at import time as the module-level singleton ``_store``.
    Access via :func:`get_store`.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, McpSession] = {}

    def create(
        self,
        *,
        elicitation_form: bool = False,
        elicitation_url: bool = False,
    ) -> McpSession:
        """Create a new session and register it in the store."""
        session_id = secrets.token_urlsafe(32)
        session = McpSession(
            session_id=session_id,
            elicitation_form=elicitation_form,
            elicitation_url=elicitation_url,
        )
        self._sessions[session_id] = session
        logger.info(
            "✅ MCP session created: %s (form=%s url=%s)",
            session_id[:8],
            elicitation_form,
            elicitation_url,
        )
        return session

    def update_capabilities(
        self,
        session_id: str,
        *,
        elicitation_form: bool,
        elicitation_url: bool,
    ) -> None:
        """Update elicitation capability flags after initialization."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.elicitation_form = elicitation_form
        session.elicitation_url = elicitation_url
        logger.info(
            "✅ MCP session capabilities updated: %s (form=%s url=%s)",
            session_id[:8],
            elicitation_form,
            elicitation_url,
        )

    def get(self, session_id: str) -> McpSession | None:
        """Return the session for *session_id*, or ``None`` if not found."""
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        """Remove a session and cancel any pending elicitation futures."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        for fut in session.pending.values():
            if not fut.done():
                fut.cancel()
        logger.info("🧹 MCP session deleted: %s", session_id[:8])

    def elicitation_sessions(self, *, mode: str = "form") -> list[McpSession]:
        """Return all sessions that declared support for *mode* elicitation."""
        result: list[McpSession] = []
        for s in self._sessions.values():
            if mode == "form" and s.elicitation_form:
                result.append(s)
            elif mode == "url" and s.elicitation_url:
                result.append(s)
        return result

    def resolve_response(
        self,
        session_id: str,
        request_id: str | int,
        result: dict[str, JsonValue],
    ) -> bool:
        """Resolve a pending server→client RPC call with the client's response.

        Called by the POST handler when it receives a JSON-RPC *response*
        (a message with ``id`` but no ``method``).

        Returns ``True`` if the future was found and resolved, ``False`` when
        the session or the pending request ID was not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        fut = session.pending.pop(request_id, None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(result)
        return True


#: Module-level singleton — the only session store in the process.
_store = McpSessionStore()


def get_store() -> McpSessionStore:
    """Return the module-level :class:`McpSessionStore` singleton."""
    return _store
