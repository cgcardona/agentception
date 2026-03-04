"""Protocol TypedDicts for the AgentCeption MCP layer.

All types defined here are self-contained — zero imports from maestro,
muse, kly, or storpheus.  They map 1-to-1 with the MCP JSON-RPC 2.0
wire protocol so callers can rely on them for type-safe serialisation.

JSON-RPC 2.0 error codes (JSONRPC_ERR_*) are defined as module-level
constants rather than an Enum so they remain plain ``int`` values that
serialise to JSON without adaptation.
"""
from __future__ import annotations

from typing import TypedDict

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 error codes
# ---------------------------------------------------------------------------

JSONRPC_ERR_PARSE_ERROR: int = -32700
JSONRPC_ERR_INVALID_REQUEST: int = -32600
JSONRPC_ERR_METHOD_NOT_FOUND: int = -32601
JSONRPC_ERR_INVALID_PARAMS: int = -32602
JSONRPC_ERR_INTERNAL_ERROR: int = -32603


# ---------------------------------------------------------------------------
# MCP tool protocol types
# ---------------------------------------------------------------------------


class ACToolDef(TypedDict):
    """Definition of a single AgentCeption MCP tool.

    Conforms to the MCP JSON-RPC 2.0 ``tools/list`` protocol shape.
    ``inputSchema`` is a JSON Schema object describing the tool's accepted
    parameters.  An empty schema (``{"type": "object", "properties": {}}``)
    signals that the tool accepts no parameters.
    """

    name: str
    description: str
    inputSchema: dict[str, object]


class ACToolContent(TypedDict):
    """A single content item in a tool call result.

    ``type`` is always ``"text"`` in the current implementation.  ``text``
    is the UTF-8 string payload — typically a JSON-encoded result or a
    human-readable error message.
    """

    type: str
    text: str


class ACToolResult(TypedDict):
    """Result of a ``tools/call`` invocation.

    ``content`` carries one or more content items (always non-empty).
    ``isError`` is ``True`` when the tool encountered a semantic error
    (e.g. validation failure) as opposed to a JSON-RPC protocol error.
    """

    content: list[ACToolContent]
    isError: bool


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelope types
# ---------------------------------------------------------------------------


class JsonRpcError(TypedDict):
    """JSON-RPC 2.0 error object embedded in an error response.

    ``code`` is one of the ``JSONRPC_ERR_*`` constants defined above.
    ``message`` is a short human-readable description.
    ``data`` carries additional context (may be ``None``).
    """

    code: int
    message: str
    data: object


class JsonRpcSuccessResponse(TypedDict):
    """JSON-RPC 2.0 success response envelope."""

    jsonrpc: str
    id: int | str | None
    result: object


class JsonRpcErrorResponse(TypedDict):
    """JSON-RPC 2.0 error response envelope."""

    jsonrpc: str
    id: int | str | None
    error: JsonRpcError
