from __future__ import annotations

"""AgentCeption MCP JSON-RPC 2.0 server.

Implements a minimal but spec-compliant JSON-RPC 2.0 dispatcher for the
AgentCeption MCP tool layer.  The dispatcher is synchronous and stateless —
it handles exactly one request per call to :func:`handle_request`.

Supported methods:
  ``tools/list``  — returns all registered :class:`~agentception.mcp.types.ACToolDef`
  ``tools/call``  — dispatches to the named tool function

Error handling follows the JSON-RPC 2.0 specification:
  - Parse errors     → code -32700 (never raised here; caller parses JSON)
  - Invalid Request  → code -32600 (missing required fields)
  - Method not found → code -32601
  - Invalid params   → code -32602 (wrong or missing tool name / arguments)
  - Internal error   → code -32603 (unexpected exception in tool handler)

Boundary constraint: zero imports from maestro, muse, kly, or storpheus.
"""

import json
import logging
from typing import cast

from agentception.mcp.build_tools import (
    build_get_pending_launches,
    build_report_blocker,
    build_report_decision,
    build_report_done,
    build_report_step,
)
from agentception.mcp.plan_tools import (
    plan_get_labels,
    plan_get_schema,
    plan_validate_manifest,
    plan_validate_spec,
)
from agentception.mcp.types import (
    ACToolContent,
    ACToolDef,
    ACToolResult,
    JSONRPC_ERR_INTERNAL_ERROR,
    JSONRPC_ERR_INVALID_PARAMS,
    JSONRPC_ERR_INVALID_REQUEST,
    JSONRPC_ERR_METHOD_NOT_FOUND,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcSuccessResponse,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

#: All tools exposed by this MCP server.  Each entry is an :class:`ACToolDef`
#: mapping the tool name to its description and input JSON Schema.
TOOLS: list[ACToolDef] = [
    ACToolDef(
        name="plan_get_schema",
        description=(
            "Return the JSON Schema for PlanSpec — the plan-step-v2 YAML contract. "
            "Use this to understand the required structure before calling plan_validate_spec."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="plan_validate_spec",
        description=(
            "Validate a JSON string against the PlanSpec schema. "
            "Returns {valid: true, spec: {...}} on success or "
            "{valid: false, errors: [...]} on failure."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "spec_json": {
                    "type": "string",
                    "description": "A JSON-encoded PlanSpec object to validate.",
                }
            },
            "required": ["spec_json"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="plan_get_labels",
        description=(
            "Fetch the full GitHub label list for the configured repository. "
            "Returns {labels: [{name: str, description: str}, ...]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="plan_validate_manifest",
        description=(
            "Validate a JSON string against the EnrichedManifest schema. "
            "Returns {valid: true, manifest: {...}, total_issues: int, estimated_waves: int} "
            "or {valid: false, errors: [...]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "json_text": {
                    "type": "string",
                    "description": "A JSON-encoded EnrichedManifest object to validate.",
                }
            },
            "required": ["json_text"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="plan_spawn_coordinator",
        description=(
            "Validate a manifest and create a coordinator git worktree with a .agent-task file. "
            "Returns {worktree, branch, agent_task_path, batch_id}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "manifest_json": {
                    "type": "string",
                    "description": "A JSON-encoded EnrichedManifest for the coordinator.",
                }
            },
            "required": ["manifest_json"],
            "additionalProperties": False,
        },
    ),
    # ── Build tools — Dispatcher reads queue; agents report lifecycle events ─
    ACToolDef(
        name="build_get_pending_launches",
        description=(
            "Return all issues queued for launch from the AgentCeption UI. "
            "Call this once to discover your work. Each item has run_id, issue_number, "
            "role, host_worktree_path, and batch_id. The role tells you what kind of "
            "agent to spawn — a leaf worker implements one issue directly; a manager "
            "(VP, CTO) reads its role file and spawns its own children via the Task tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_report_step",
        description=(
            "Signal that you are starting a new execution step. "
            "Call this whenever you begin a distinct phase of work so the "
            "mission-control dashboard can track your progress in real time."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue number you are working on.",
                },
                "step_name": {
                    "type": "string",
                    "description": "Short label for the step (e.g. 'Reading codebase').",
                },
                "agent_run_id": {
                    "type": "string",
                    "description": "Optional: your worktree id (e.g. 'issue-938').",
                },
            },
            "required": ["issue_number", "step_name"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_report_blocker",
        description=(
            "Signal that you are blocked and cannot proceed without human input. "
            "Describe what is blocking you — this creates a visible alert on the dashboard."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "description": {
                    "type": "string",
                    "description": "What is blocking you and what you need to proceed.",
                },
                "agent_run_id": {"type": "string"},
            },
            "required": ["issue_number", "description"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_report_decision",
        description=(
            "Record a significant architectural or implementation decision you made. "
            "Use this for choices that affect code structure, dependencies, or approach "
            "so the team can review your reasoning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "decision": {
                    "type": "string",
                    "description": "One-sentence description of the decision.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why you made this decision.",
                },
                "agent_run_id": {"type": "string"},
            },
            "required": ["issue_number", "decision", "rationale"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_report_done",
        description=(
            "Signal that you have finished the issue and opened a pull request. "
            "Call this as your final action after pushing your branch and opening the PR."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "pr_url": {
                    "type": "string",
                    "description": "Full URL of the pull request you opened.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional one-sentence summary of what you did.",
                },
                "agent_run_id": {"type": "string"},
            },
            "required": ["issue_number", "pr_url"],
            "additionalProperties": False,
        },
    ),
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_error_response(
    request_id: int | str | None,
    code: int,
    message: str,
    data: object = None,
) -> JsonRpcErrorResponse:
    """Build a well-formed JSON-RPC 2.0 error response."""
    error: JsonRpcError = JsonRpcError(code=code, message=message, data=data)
    return JsonRpcErrorResponse(jsonrpc="2.0", id=request_id, error=error)


def _make_success_response(
    request_id: int | str | None,
    result: object,
) -> JsonRpcSuccessResponse:
    """Build a well-formed JSON-RPC 2.0 success response."""
    return JsonRpcSuccessResponse(jsonrpc="2.0", id=request_id, result=result)


def _tool_result_to_text(result: dict[str, object]) -> str:
    """Serialise a tool result dict to a compact JSON string."""
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


def list_tools() -> list[ACToolDef]:
    """Return all registered MCP tool definitions.

    Returns:
        A list of :class:`~agentception.mcp.types.ACToolDef` objects,
        one per registered tool.
    """
    return list(TOOLS)


def call_tool(name: str, arguments: dict[str, object]) -> ACToolResult:
    """Dispatch a ``tools/call`` request to the named tool function.

    Note: ``plan_get_labels`` and ``plan_spawn_coordinator`` are async and
    cannot be invoked here directly.  Callers that need those tools must use
    the async variants directly or wrap this dispatcher in an async context.

    Args:
        name:      The tool name as it appears in the ``tools/list`` response.
        arguments: The tool arguments dict from the JSON-RPC params.

    Returns:
        An :class:`~agentception.mcp.types.ACToolResult` with ``isError=False``
        on success or ``isError=True`` when the tool name is unknown or
        arguments are invalid.

    Never raises — all errors are returned as ``isError=True`` results.
    """
    if name == "plan_get_schema":
        schema = plan_get_schema()
        text = _tool_result_to_text(schema)
        content: list[ACToolContent] = [ACToolContent(type="text", text=text)]
        return ACToolResult(content=content, isError=False)

    if name == "plan_validate_spec":
        spec_json = arguments.get("spec_json")
        if not isinstance(spec_json, str):
            err_text = _tool_result_to_text(
                {"error": "Missing or invalid required argument 'spec_json' (must be a string)"}
            )
            return ACToolResult(
                content=[ACToolContent(type="text", text=err_text)],
                isError=True,
            )
        result = plan_validate_spec(spec_json)
        text = _tool_result_to_text(result)
        is_error = not bool(result.get("valid", False))
        return ACToolResult(
            content=[ACToolContent(type="text", text=text)],
            isError=is_error,
        )

    if name == "plan_validate_manifest":
        json_text = arguments.get("json_text")
        if not isinstance(json_text, str):
            err_text = _tool_result_to_text(
                {"error": "Missing or invalid required argument 'json_text' (must be a string)"}
            )
            return ACToolResult(
                content=[ACToolContent(type="text", text=err_text)],
                isError=True,
            )
        result = plan_validate_manifest(json_text)
        text = _tool_result_to_text(result)
        is_error = not bool(result.get("valid", False))
        return ACToolResult(
            content=[ACToolContent(type="text", text=text)],
            isError=is_error,
        )

    if name in (
        "plan_get_labels",
        "plan_spawn_coordinator",
        "build_get_pending_launches",
        "build_report_step",
        "build_report_blocker",
        "build_report_decision",
        "build_report_done",
    ):
        err_text = _tool_result_to_text(
            {"error": f"Tool {name!r} is async — use the async call path"}
        )
        return ACToolResult(
            content=[ACToolContent(type="text", text=err_text)],
            isError=True,
        )

    err_text = _tool_result_to_text({"error": f"Unknown tool: {name!r}"})
    logger.warning("⚠️ call_tool: unknown tool %r", name)
    return ACToolResult(
        content=[ACToolContent(type="text", text=err_text)],
        isError=True,
    )


async def call_tool_async(
    name: str,
    arguments: dict[str, object],
) -> ACToolResult:
    """Async dispatcher for tools that require async I/O.

    Handles all async tools (``plan_get_labels``, ``plan_spawn_coordinator``,
    and the four ``build_report_*`` tools).  Falls through to :func:`call_tool`
    for synchronous tools.

    Args:
        name:      The tool name.
        arguments: The tool arguments dict.

    Returns:
        An :class:`~agentception.mcp.types.ACToolResult`.  Never raises.
    """
    if name == "build_get_pending_launches":
        result = await build_get_pending_launches()
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "build_report_step":
        issue_num = arguments.get("issue_number")
        step = arguments.get("step_name")
        if not isinstance(issue_num, int) or not isinstance(step, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and step_name (str) are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await build_report_step(issue_num, step, str(run_id) if run_id else None)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "build_report_blocker":
        issue_num = arguments.get("issue_number")
        desc = arguments.get("description")
        if not isinstance(issue_num, int) or not isinstance(desc, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and description (str) are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await build_report_blocker(issue_num, desc, str(run_id) if run_id else None)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "build_report_decision":
        issue_num = arguments.get("issue_number")
        decision = arguments.get("decision")
        rationale = arguments.get("rationale")
        if not isinstance(issue_num, int) or not isinstance(decision, str) or not isinstance(rationale, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number, decision, rationale are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await build_report_decision(
            issue_num, decision, rationale, str(run_id) if run_id else None
        )
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "build_report_done":
        issue_num = arguments.get("issue_number")
        pr_url = arguments.get("pr_url")
        if not isinstance(issue_num, int) or not isinstance(pr_url, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and pr_url (str) are required"}')],
                isError=True,
            )
        summary = arguments.get("summary", "")
        run_id = arguments.get("agent_run_id")
        result = await build_report_done(
            issue_num, pr_url, str(summary), str(run_id) if run_id else None
        )
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    # Delegate sync tools
    return call_tool(name, arguments)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 request handler
# ---------------------------------------------------------------------------


def handle_request(
    raw: dict[str, object],
) -> dict[str, object]:
    """Dispatch a JSON-RPC 2.0 request dict and return a response dict.

    This is the single entry point for the MCP layer.  The caller is
    responsible for JSON parsing (converting the wire bytes to a ``dict``);
    this function handles everything from field extraction through to
    building the response envelope.

    Args:
        raw: A ``dict[str, object]`` parsed from a JSON-RPC 2.0 request body.

    Returns:
        Either a :class:`~agentception.mcp.types.JsonRpcSuccessResponse` or
        a :class:`~agentception.mcp.types.JsonRpcErrorResponse`.  The caller
        should serialise the return value back to JSON for the wire.

    Never raises.
    """
    request_id: int | str | None = raw.get("id")  # type: ignore[assignment]
    # Narrow: id must be int | str | None per spec; cast safely.
    if not isinstance(request_id, (int, str, type(None))):
        request_id = None

    jsonrpc = raw.get("jsonrpc")
    if jsonrpc != "2.0":
        return cast(dict[str, object], _make_error_response(
            request_id,
            JSONRPC_ERR_INVALID_REQUEST,
            "jsonrpc must be '2.0'",
        ))

    method = raw.get("method")
    if not isinstance(method, str):
        return cast(dict[str, object], _make_error_response(
            request_id,
            JSONRPC_ERR_INVALID_REQUEST,
            "method must be a string",
        ))

    logger.debug("🔧 handle_request: method=%r id=%r", method, request_id)

    if method == "tools/list":
        tools = list_tools()
        return cast(dict[str, object], _make_success_response(request_id, {"tools": tools}))

    if method == "tools/call":
        params = raw.get("params")
        if not isinstance(params, dict):
            return cast(dict[str, object], _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params must be an object for tools/call",
            ))

        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            return cast(dict[str, object], _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.name must be a string",
            ))

        arguments_raw = params.get("arguments", {})
        if not isinstance(arguments_raw, dict):
            return cast(dict[str, object], _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.arguments must be an object",
            ))

        arguments: dict[str, object] = {k: v for k, v in arguments_raw.items()}

        try:
            tool_result = call_tool(tool_name, arguments)
        except Exception as exc:
            logger.error("❌ handle_request: internal error in call_tool — %s", exc, exc_info=True)
            return cast(dict[str, object], _make_error_response(
                request_id,
                JSONRPC_ERR_INTERNAL_ERROR,
                f"Internal error: {exc}",
            ))

        return cast(dict[str, object], _make_success_response(request_id, tool_result))

    return cast(dict[str, object], _make_error_response(
        request_id,
        JSONRPC_ERR_METHOD_NOT_FOUND,
        f"Method not found: {method!r}",
    ))
