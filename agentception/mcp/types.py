from __future__ import annotations

"""Protocol TypedDicts for the AgentCeption MCP layer.

These types map 1-to-1 with the MCP JSON-RPC 2.0 wire protocol so callers
can rely on them for type-safe serialisation.

JSON-RPC 2.0 error codes (JSONRPC_ERR_*) are defined as module-level
constants rather than an Enum so they remain plain ``int`` values that
serialise to JSON without adaptation.

Resource URIs follow the ``ac://`` scheme with REST-like path segments:

  Static resources (no parameters):
    ac://runs/active          — all live runs
    ac://runs/pending         — runs queued for dispatch
    ac://system/dispatcher    — dispatcher counters and active batch
    ac://system/health        — DB reachability and status counts
    ac://plan/schema          — PlanSpec JSON Schema
    ac://plan/labels          — GitHub label catalogue

  Templated resources (parameters in braces, RFC 6570):
    ac://runs/{run_id}                — single run metadata
    ac://runs/{run_id}/children       — child runs spawned by this run
    ac://runs/{run_id}/context        — full task context (DB-sourced RunContextRow)
    ac://runs/{run_id}/events         — structured event log
    ac://runs/{run_id}/events?after_id={n} — paginated event log
    ac://batches/{batch_id}/tree      — full batch run tree
    ac://plan/figures/{role}          — cognitive-arch figures for a role
"""

from typing import NotRequired, TypeAlias, TypedDict

from agentception.types import JsonSchemaObj, JsonValue

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

    ``icon`` is optional per the 2025-11-25 spec — a URL to an image
    that clients may display alongside the tool name.
    """

    name: str
    description: str
    inputSchema: JsonSchemaObj
    icon: NotRequired[str]


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
# MCP resource protocol types
# ---------------------------------------------------------------------------


class ACResourceDef(TypedDict):
    """Definition of a single static MCP resource.

    Conforms to the ``resources/list`` response item shape.
    Static resources have a fixed URI — no template expansion required.

    ``icon`` is optional per the 2025-11-25 spec.
    """

    uri: str
    name: str
    description: str
    mimeType: str
    icon: NotRequired[str]


class ACResourceTemplate(TypedDict):
    """Definition of a parameterised MCP resource template.

    Conforms to the ``resources/templates/list`` response item shape.
    ``uriTemplate`` follows RFC 6570 Level 1 (``{variable}`` expansion) and
    may include a query component (``{?param}``).

    ``icon`` is optional per the 2025-11-25 spec.
    """

    uriTemplate: str
    name: str
    description: str
    mimeType: str
    icon: NotRequired[str]


class ACResourceContent(TypedDict):
    """A single content item in a ``resources/read`` response.

    ``uri`` echoes the requested URI.  ``mimeType`` is always
    ``"application/json"`` for AgentCeption resources.  ``text`` carries
    the UTF-8 JSON payload.
    """

    uri: str
    mimeType: str
    text: str


class ACResourceResult(TypedDict):
    """Result of a ``resources/read`` invocation.

    ``contents`` is a list with exactly one item for every AgentCeption
    resource (resources are atomic; no multi-part responses).
    """

    contents: list[ACResourceContent]


# ---------------------------------------------------------------------------
# MCP prompt protocol types
# ---------------------------------------------------------------------------


class ACPromptArgument(TypedDict):
    """Definition of a single argument accepted by a prompt template.

    ``required`` is ``True`` when the argument must be provided to
    ``prompts/get``.  Optional arguments are omitted from this field
    when the prompt has sensible defaults.
    """

    name: str
    description: str
    required: bool


class ACPromptDef(TypedDict):
    """Definition of a single MCP prompt.

    Conforms to the ``prompts/list`` response item shape.  ``arguments``
    is an empty list for static prompts that require no parameters.

    ``icon`` is optional per the 2025-11-25 spec.
    """

    name: str
    description: str
    arguments: list[ACPromptArgument]
    icon: NotRequired[str]


class ACPromptContent(TypedDict):
    """Text content carried inside an :class:`ACPromptMessage`.

    ``type`` is always ``"text"`` for AgentCeption prompts.
    """

    type: str
    text: str


class ACPromptMessage(TypedDict):
    """A single message in a ``prompts/get`` result.

    ``role`` is ``"user"`` for every AgentCeption prompt — the prompts
    are system instructions delivered as the first user turn.
    """

    role: str
    content: ACPromptContent


class ACPromptResult(TypedDict):
    """Result of a ``prompts/get`` invocation.

    ``messages`` always contains exactly one item — a ``user`` message
    carrying the full prompt text.  Callers may prepend this message to
    their conversation context.
    """

    description: str
    messages: list[ACPromptMessage]


# ---------------------------------------------------------------------------
# MCP method-level result TypedDicts
# ---------------------------------------------------------------------------


class McpServerInfo(TypedDict):
    """Server identity advertised in the ``initialize`` response.

    ``description`` is optional per the 2025-11-25 spec — a human-readable
    summary used by MCP registries and client UIs.
    """

    name: str
    version: str
    description: NotRequired[str]


class McpCapabilities(TypedDict):
    """Server capability flags advertised in the ``initialize`` response."""

    tools: dict[str, str]
    resources: dict[str, str]
    prompts: dict[str, str]


class InitializeResult(TypedDict):
    """Result payload of the ``initialize`` MCP handshake."""

    protocolVersion: str
    capabilities: McpCapabilities
    serverInfo: McpServerInfo


class ToolListResult(TypedDict):
    """Result payload of ``tools/list``."""

    tools: list[ACToolDef]


class PromptListResult(TypedDict):
    """Result payload of ``prompts/list``."""

    prompts: list[ACPromptDef]


class ResourceListResult(TypedDict):
    """Result payload of ``resources/list``."""

    resources: list[ACResourceDef]


class ResourceTemplateListResult(TypedDict):
    """Result payload of ``resources/templates/list``."""

    resourceTemplates: list[ACResourceTemplate]


# ---------------------------------------------------------------------------
# MCP result union — covers every type that can appear as a JSON-RPC result
# ---------------------------------------------------------------------------

McpResultPayload: TypeAlias = (
    ACToolResult
    | ACPromptResult
    | ACResourceResult
    | InitializeResult
    | ToolListResult
    | PromptListResult
    | ResourceListResult
    | ResourceTemplateListResult
    | dict[str, str | int | float | bool | None]
)

# ---------------------------------------------------------------------------
# MCP elicitation types (2025-11-25)
# ---------------------------------------------------------------------------


class ElicitationField(TypedDict):
    """One form field in a ``request_human_input`` tool call.

    ``name`` is the key that appears in the submitted ``content`` dict.
    ``type`` is the primitive JSON type — ``"string"``, ``"number"``,
    ``"integer"``, or ``"boolean"``.
    All other fields are optional hints for the dashboard's form renderer.
    """

    name: str
    type: str  # "string" | "number" | "integer" | "boolean"
    title: NotRequired[str]
    description: NotRequired[str]
    required: NotRequired[bool]
    default: NotRequired[JsonValue]
    enum: NotRequired[list[str]]
    format: NotRequired[str]  # e.g. "email" | "uri" | "date"
    minimum: NotRequired[float]
    maximum: NotRequired[float]


class ElicitationResult(TypedDict):
    """Result of an ``elicitation/create`` round-trip.

    ``action`` is one of ``"accept"`` (human submitted the form),
    ``"decline"`` (human explicitly declined), or ``"cancel"`` (human
    dismissed without acting).  ``content`` carries the submitted form
    data and is present only when ``action == "accept"``.
    """

    action: str  # "accept" | "decline" | "cancel"
    content: NotRequired[dict[str, JsonValue]]


class ClientElicitationFormCapability(TypedDict):
    """Capability token: client supports form-mode elicitation.

    Per the 2025-11-25 spec the object is currently empty — its presence
    is the signal.
    """


class ClientElicitationUrlCapability(TypedDict):
    """Capability token: client supports URL-mode elicitation.

    Per the 2025-11-25 spec the object is currently empty — its presence
    is the signal.
    """


class ClientElicitationCapabilities(TypedDict):
    """Client-declared elicitation capabilities.

    Sent inside ``initialize`` params.  Any key whose value is an empty
    ``{}`` object signals support for that mode.  An entirely absent key
    means the client does not support that mode.
    """

    form: NotRequired[ClientElicitationFormCapability]
    url: NotRequired[ClientElicitationUrlCapability]


class ClientCapabilities(TypedDict):
    """Client capability block sent in ``initialize`` params.

    Only ``elicitation`` is parsed today.  Other MCP capability blocks
    (``sampling``, ``roots``, ``tasks``) are not used by AgentCeption.
    """

    elicitation: NotRequired[ClientElicitationCapabilities]


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
    data: JsonValue


class JsonRpcSuccessResponse(TypedDict):
    """JSON-RPC 2.0 success response envelope."""

    jsonrpc: str
    id: int | str | None
    result: McpResultPayload


class JsonRpcErrorResponse(TypedDict):
    """JSON-RPC 2.0 error response envelope."""

    jsonrpc: str
    id: int | str | None
    error: JsonRpcError
