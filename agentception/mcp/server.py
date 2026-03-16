from __future__ import annotations

"""AgentCeption MCP JSON-RPC 2.0 server.

Implements a spec-compliant JSON-RPC 2.0 dispatcher for both MCP Tools
(actions with side effects) and MCP Resources (stateless, cacheable reads).

Supported methods:
  ``initialize``              — MCP handshake; declares tools + resources + prompts capabilities
  ``initialized``             — MCP notification (no response; acknowledged silently)
  ``ping``                    — keepalive/liveness check (responds with empty result)
  ``tools/list``              — lists all registered :class:`~agentception.mcp.types.ACToolDef`
  ``tools/call``              — dispatches to the named tool function
  ``resources/list``          — lists all static :class:`~agentception.mcp.types.ACResourceDef`
  ``resources/templates/list``— lists all :class:`~agentception.mcp.types.ACResourceTemplate`
  ``resources/read``          — reads a resource by ``ac://`` URI
  ``prompts/list``            — lists all MCP Prompt definitions
  ``prompts/get``             — returns the content of a named prompt by name

Tool vs Resource design
  Pure reads (no side effects) are Resources, accessed via ``resources/read``.
  Actions that mutate state (build_*, log_*, github_*, plan mutations) remain Tools.
  See :mod:`agentception.mcp.resources` for the full URI catalogue.

Run-level resource templates (ac://runs/{run_id}/*)
  ac://runs/{run_id}          — lightweight metadata for one run
  ac://runs/{run_id}/children — child runs spawned by this run
  ac://runs/{run_id}/context  — full task context (RunContextRow)
  ac://runs/{run_id}/events   — structured event log (paginated via ?after_id=N)
  ac://runs/{run_id}/status   — current status and completed_at timestamp
  ac://runs/{run_id}/task     — task_description field for the run

Error handling follows the JSON-RPC 2.0 specification:
  - Parse errors     → code -32700 (never raised here; caller parses JSON)
  - Invalid Request  → code -32600 (missing required fields)
  - Method not found → code -32601
  - Invalid params   → code -32602 (wrong or missing tool name / arguments)
  - Internal error   → code -32603 (unexpected exception in tool handler)

Boundary constraint: zero imports from external packages.
"""

import json
import logging

from agentception.types import JsonValue
from agentception.mcp.types import (
    InitializeResult,
    McpCapabilities,
    McpResultPayload,
    McpServerInfo,
    PromptListResult,
    ResourceListResult,
    ResourceTemplateListResult,
    ToolListResult,
)
from agentception.mcp.build_commands import (
    build_block_run,
    build_cancel_run,
    build_claim_run,
    build_complete_run,
    build_resume_run,
    build_spawn_adhoc_child,
    build_stop_run,
)
from agentception.mcp.log_tools import (
    log_run_error,
    log_run_step,
)
from agentception.mcp.github_tools import (
    github_add_comment,
    github_add_label,
    github_remove_label,
)
from agentception.mcp.prompts import PROMPTS, get_prompt, get_static_prompt
from agentception.mcp.plan_advance_phase import plan_advance_phase
from agentception.mcp.plan_tools import (
    plan_validate_manifest,
    plan_validate_spec,
)
from agentception.mcp.resources import (
    RESOURCES,
    RESOURCE_TEMPLATES,
    read_resource,
)
from agentception.mcp.types import (
    ACPromptDef,
    ACPromptResult,
    ACResourceDef,
    ACResourceTemplate,
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

#: MCP protocol version this server implements.
_MCP_PROTOCOL_VERSION = "2025-03-26"

#: Server identity advertised in the ``initialize`` response.
_SERVER_INFO: McpServerInfo = {"name": "agentception", "version": "0.1.1"}

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

#: All tools exposed by this MCP server.  Each entry is an :class:`ACToolDef`
#: mapping the tool name to its description and input JSON Schema.
#:
#: Read-only state inspection is exposed as MCP Resources (see :data:`RESOURCES`
#: and :data:`RESOURCE_TEMPLATES`), not as Tools.  Tools are for actions
#: that mutate state (build_*, log_*, github_*) or that require validation
#: input (plan_validate_*, plan_advance_phase).
TOOLS: list[ACToolDef] = [
    # ── Plan tools — validation and mutations only (reads are Resources) ──────
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
        name="plan_advance_phase",
        description=(
            "Atomically advance a phase gate: verify all from_phase issues for the "
            "given initiative are closed, then unlock all to_phase issues by removing "
            "the blocked label and adding the active label. "
            "Returns {advanced: true, unlocked_count: N} on success or "
            "{advanced: false, error: str, open_issues: [int, ...]} when open issues remain."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "initiative": {
                    "type": "string",
                    "description": (
                        "The initiative label shared by all phase issues "
                        "(e.g. 'agentception-ux-phase1b-to-phase3')."
                    ),
                },
                "from_phase": {
                    "type": "string",
                    "description": (
                        "Phase label that must be fully closed before advancing "
                        "(e.g. 'phase-1')."
                    ),
                },
                "to_phase": {
                    "type": "string",
                    "description": (
                        "Phase label whose issues become active on success "
                        "(e.g. 'phase-2')."
                    ),
                },
            },
            "required": ["initiative", "from_phase", "to_phase"],
            "additionalProperties": False,
        },
    ),
    # ── GitHub tools — cached reads + write-through mutations ────────────────
    ACToolDef(
        name="github_add_label",
        description=(
            "Add a label to a GitHub issue. Invalidates the read cache. "
            "Returns {ok, issue_number, added}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "GitHub issue number."},
                "label": {"type": "string", "description": "Label name to add."},
            },
            "required": ["issue_number", "label"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="github_remove_label",
        description=(
            "Remove a label from a GitHub issue. Idempotent — no error if the label "
            "is not present. Invalidates the read cache. "
            "Returns {ok, issue_number, removed}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "GitHub issue number."},
                "label": {"type": "string", "description": "Label name to remove."},
            },
            "required": ["issue_number", "label"],
            "additionalProperties": False,
        },
    ),
    # ── Build commands — explicit state transitions only ──────────────────────
    ACToolDef(
        name="build_claim_run",
        description=(
            "Atomically claim a pending run before spawning its Task agent. "
            "Call this with the run_id from the ac://runs/pending resource immediately "
            "before firing the Task so the run cannot be double-claimed by a concurrent "
            "Dispatcher. Transitions the run from pending_launch to implementing. "
            "Returns {ok: true, run_id, previous_state} on success, or "
            "{ok: false, reason} if the run was already claimed — skip that item."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "run_id returned by query_pending_runs",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_spawn_adhoc_child",
        description=(
            "Spawn a child agent run from within a coordinator's tool loop. "
            "This is the MCP-native way for a coordinator to dispatch engineer agents. "
            "It creates a git worktree, a DB row with parent_run_id linking it to this "
            "coordinator, and fires the agent loop immediately as an asyncio task. "
            "The child receives its context entirely "
            "via the task/briefing MCP prompt and ac://runs/{run_id}/context resource. "
            "Returns {ok, child_run_id, worktree_path, cognitive_arch}. "
            "After calling this tool, use resources/read with ac://runs/{run_id}/status to poll for completion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "parent_run_id": {
                    "type": "string",
                    "description": "run_id of this coordinator — links the child in the DB hierarchy.",
                },
                "role": {
                    "type": "string",
                    "description": "Role slug for the child agent (e.g. 'developer').",
                },
                "task_description": {
                    "type": "string",
                    "description": (
                        "Plain-language description of the child's task. "
                        "Be specific: files to touch, expected output, constraints. "
                        "This is the first thing the child agent reads."
                    ),
                },
                "figure": {
                    "type": "string",
                    "description": "Cognitive figure slug override (e.g. 'guido_van_rossum'). Omit to use the role default.",
                },
                "base_branch": {
                    "type": "string",
                    "description": "Git ref to branch the child worktree from. Defaults to 'origin/dev'.",
                },
            },
            "required": ["parent_run_id", "role", "task_description"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_complete_run",
        description=(
            "Record that the agent has finished work and transition the run to completed. "
            "Persists the done event (linking the PR and updating workflow state). "
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
                "agent_run_id": {
                    "type": "string",
                    "description": (
                        "Your own run ID exactly as it appears in your task briefing "
                        "(e.g. 'issue-858' or 'review-900'). Required — omitting it "
                        "leaves your run stuck in implementing state."
                    ),
                },
                "grade": {
                    "type": "string",
                    "description": (
                        "Reviewer grade (A/B/C/D/F). Required when called by a reviewer "
                        "agent — A/B merges the PR, C/D/F rejects it with feedback. "
                        "Omit when called by a developer or other non-reviewer role."
                    ),
                },
                "reviewer_feedback": {
                    "type": "string",
                    "description": (
                        "Detailed feedback posted as an issue comment when the grade is "
                        "C, D, or F (rejection). Omit for A/B grades and non-reviewer roles."
                    ),
                },
            },
            "required": ["issue_number", "pr_url", "agent_run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_cancel_run",
        description=(
            "Transition any active run to cancelled (terminal — cannot resume). "
            "Valid from any non-terminal state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID to cancel."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_block_run",
        description=(
            "Transition an implementing run to blocked. "
            "Use when the agent cannot proceed until a dependency resolves or a human intervenes. "
            "A blocked run can be resumed later with build_resume_run. "
            "Only valid from implementing state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID to block."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_resume_run",
        description=(
            "Transition a blocked or stopped run back to implementing. "
            "Idempotent: if the run is already implementing with the same agent_run_id, "
            "the call succeeds so a crashed-and-restarted agent can call this safely on startup."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID to resume."},
                "agent_run_id": {
                    "type": "string",
                    "description": "The caller's own run ID (used for idempotency check).",
                },
            },
            "required": ["run_id", "agent_run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_stop_run",
        description=(
            "Transition any active run to stopped (non-terminal — can be resumed). "
            "Unlike build_cancel_run, a stopped run can be resumed with build_resume_run. "
            "Valid from any non-terminal state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID to stop."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    # ── Log tools — append-only telemetry, no state change ───────────────────
    ACToolDef(
        name="log_run_step",
        description=(
            "Signal that you are starting a new execution step. "
            "Call this whenever you begin a distinct phase of work so the "
            "mission-control dashboard can track your progress in real time. "
            "This tool never changes run state."
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
        name="log_run_error",
        description=(
            "Record an unrecoverable error or crash with semantic distinction from a message. "
            "Use this when the agent is aborting due to an "
            "exception, API failure, or any condition it cannot recover from. "
            "The dashboard surfaces error events differently for operator triage. "
            "After calling this, also call build_cancel_run. "
            "Never changes run state on its own."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "error": {
                    "type": "string",
                    "description": "Human-readable description of the failure. Include exception type and message.",
                },
                "agent_run_id": {"type": "string"},
            },
            "required": ["issue_number", "error"],
            "additionalProperties": False,
        },
    ),
    # ── GitHub tools ────────────────────────────────────────────────────────────
    ACToolDef(
        name="github_add_comment",
        description=(
            "Post a Markdown comment on a GitHub issue. "
            "Use this for fingerprint comments, status updates, handoff notes, and "
            "any other issue comment — do NOT shell out to 'gh issue comment' directly. "
            "Routing comments through this tool keeps them observable, logged, and "
            "auditable. Returns {ok, issue_number, comment_url}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue number to comment on.",
                },
                "body": {
                    "type": "string",
                    "description": "Markdown body for the comment. Supports GitHub-flavoured Markdown.",
                },
            },
            "required": ["issue_number", "body"],
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
    data: JsonValue = None,
) -> JsonRpcErrorResponse:
    """Build a well-formed JSON-RPC 2.0 error response."""
    error: JsonRpcError = JsonRpcError(code=code, message=message, data=data)
    return JsonRpcErrorResponse(jsonrpc="2.0", id=request_id, error=error)


def _make_success_response(
    request_id: int | str | None,
    result: McpResultPayload,
) -> JsonRpcSuccessResponse:
    """Build a well-formed JSON-RPC 2.0 success response."""
    return JsonRpcSuccessResponse(jsonrpc="2.0", id=request_id, result=result)


def _tool_result_to_text(result: dict[str, JsonValue]) -> str:
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


def list_resources() -> list[ACResourceDef]:
    """Return all registered static MCP resource definitions."""
    return list(RESOURCES)


def list_resource_templates() -> list[ACResourceTemplate]:
    """Return all registered MCP resource template definitions."""
    return list(RESOURCE_TEMPLATES)


def list_prompts() -> list[ACPromptDef]:
    """Return all registered MCP prompt definitions.

    Returns:
        A list of :class:`~agentception.mcp.types.ACPromptDef` objects,
        one per compiled role or agent prompt file discovered at import time.
    """
    return list(PROMPTS)


def call_tool(name: str, arguments: dict[str, JsonValue]) -> ACToolResult:
    """Dispatch a ``tools/call`` request to the named tool function.

    Note: all tools that require async I/O (build_*, log_*, github_*, plan
    mutations) cannot be invoked here directly — they return an error directing
    the caller to use the async path.  Use :func:`call_tool_async` instead.

    Read-only state inspection is exposed as MCP Resources (``ac://`` scheme).

    Args:
        name:      The tool name as it appears in the ``tools/list`` response.
        arguments: The tool arguments dict from the JSON-RPC params.

    Returns:
        An :class:`~agentception.mcp.types.ACToolResult` with ``isError=False``
        on success or ``isError=True`` when the tool name is unknown or
        arguments are invalid.

    Never raises — all errors are returned as ``isError=True`` results.
    """
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
        spec_result = plan_validate_spec(spec_json)
        text = json.dumps(spec_result, ensure_ascii=False)
        is_error = not bool(spec_result.get("valid", False))
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
        manifest_result = plan_validate_manifest(json_text)
        text = json.dumps(manifest_result, ensure_ascii=False)
        is_error = not bool(manifest_result.get("valid", False))
        return ACToolResult(
            content=[ACToolContent(type="text", text=text)],
            isError=is_error,
        )

    if name in (
        "plan_advance_phase",
        "build_claim_run",
        "build_spawn_adhoc_child",
        "build_complete_run",
        "build_cancel_run",
        "build_block_run",
        "build_resume_run",
        "build_stop_run",
        "log_run_step",
        "log_run_error",
        "github_add_label",
        "github_remove_label",
        "github_add_comment",
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
    arguments: dict[str, JsonValue],
) -> ACToolResult:
    """Async dispatcher for tools that require async I/O.

    Handles all async tools (plan, build, log, and query tools).
    Falls through to :func:`call_tool` for synchronous tools.

    Args:
        name:      The tool name.
        arguments: The tool arguments dict.

    Returns:
        An :class:`~agentception.mcp.types.ACToolResult`.  Never raises.
    """
    if name == "plan_advance_phase":
        initiative = arguments.get("initiative")
        from_phase = arguments.get("from_phase")
        to_phase = arguments.get("to_phase")
        if (
            not isinstance(initiative, str)
            or not isinstance(from_phase, str)
            or not isinstance(to_phase, str)
        ):
            err_text = _tool_result_to_text(
                {"error": "initiative, from_phase, and to_phase (strings) are required"}
            )
            return ACToolResult(
                content=[ACToolContent(type="text", text=err_text)],
                isError=True,
            )
        result = await plan_advance_phase(initiative, from_phase, to_phase)
        is_error = not bool(result.get("advanced", False))
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=is_error,
        )

    # ── Build commands ───────────────────────────────────────────────────────

    if name == "build_claim_run":
        run_id_arg = arguments.get("run_id")
        if not isinstance(run_id_arg, str) or not run_id_arg:
            return ACToolResult(
                content=[ACToolContent(
                    type="text",
                    text=_tool_result_to_text({"error": "build_claim_run requires a non-empty string run_id"}),
                )],
                isError=True,
            )
        result = await build_claim_run(run_id_arg)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "build_spawn_adhoc_child":
        parent_run_id = arguments.get("parent_run_id")
        role = arguments.get("role")
        task_description = arguments.get("task_description")
        if (
            not isinstance(parent_run_id, str) or not parent_run_id
            or not isinstance(role, str) or not role
            or not isinstance(task_description, str) or not task_description
        ):
            err_text = _tool_result_to_text(
                {"error": "parent_run_id, role, task_description (non-empty strings) are required"}
            )
            return ACToolResult(
                content=[ACToolContent(type="text", text=err_text)],
                isError=True,
            )
        figure_raw = arguments.get("figure", "")
        figure: str = str(figure_raw) if isinstance(figure_raw, str) else ""
        base_branch_raw = arguments.get("base_branch", "origin/dev")
        base_branch: str = str(base_branch_raw) if isinstance(base_branch_raw, str) else "origin/dev"
        result = await build_spawn_adhoc_child(
            parent_run_id=parent_run_id,
            role=role,
            task_description=task_description,
            figure=figure,
            base_branch=base_branch,
        )
        is_error = not bool(result.get("ok", False))
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=is_error,
        )

    if name == "build_complete_run":
        issue_num = arguments.get("issue_number")
        pr_url = arguments.get("pr_url")
        if not isinstance(issue_num, int) or not isinstance(pr_url, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and pr_url (str) are required"}')],
                isError=True,
            )
        summary = arguments.get("summary", "")
        run_id = arguments.get("agent_run_id")
        grade = arguments.get("grade", "")
        reviewer_feedback = arguments.get("reviewer_feedback", "")
        result = await build_complete_run(
            issue_num,
            pr_url,
            str(summary),
            str(run_id) if run_id else None,
            str(grade),
            str(reviewer_feedback),
        )
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", True)),
        )

    if name == "build_cancel_run":
        run_id_arg5 = arguments.get("run_id")
        if not isinstance(run_id_arg5, str) or not run_id_arg5:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_cancel_run requires a non-empty run_id"}')],
                isError=True,
            )
        result = await build_cancel_run(run_id_arg5)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "build_block_run":
        run_id_block = arguments.get("run_id")
        if not isinstance(run_id_block, str) or not run_id_block:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_block_run requires a non-empty run_id"}')],
                isError=True,
            )
        result = await build_block_run(run_id_block)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "build_resume_run":
        run_id_resume = arguments.get("run_id")
        agent_run_id_resume = arguments.get("agent_run_id")
        if not isinstance(run_id_resume, str) or not run_id_resume:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_resume_run requires a non-empty run_id"}')],
                isError=True,
            )
        if not isinstance(agent_run_id_resume, str) or not agent_run_id_resume:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_resume_run requires a non-empty agent_run_id"}')],
                isError=True,
            )
        result = await build_resume_run(run_id_resume, agent_run_id_resume)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "build_stop_run":
        run_id_stop = arguments.get("run_id")
        if not isinstance(run_id_stop, str) or not run_id_stop:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_stop_run requires a non-empty run_id"}')],
                isError=True,
            )
        result = await build_stop_run(run_id_stop)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    # ── Log tools ────────────────────────────────────────────────────────────

    if name == "log_run_step":
        issue_num = arguments.get("issue_number")
        step = arguments.get("step_name")
        if not isinstance(issue_num, int) or not isinstance(step, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and step_name (str) are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await log_run_step(issue_num, step, str(run_id) if run_id else None)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "log_run_error":
        issue_num = arguments.get("issue_number")
        err_msg = arguments.get("error")
        if not isinstance(issue_num, int) or not isinstance(err_msg, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and error (str) are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await log_run_error(issue_num, err_msg, str(run_id) if run_id else None)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    # ── GitHub tools ─────────────────────────────────────────────────────────

    if name == "github_add_label":
        issue_num = arguments.get("issue_number")
        lbl = arguments.get("label")
        if not isinstance(issue_num, int) or not isinstance(lbl, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and label (str) are required"}')],
                isError=True,
            )
        result = await github_add_label(issue_num, lbl)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "github_remove_label":
        issue_num = arguments.get("issue_number")
        lbl = arguments.get("label")
        if not isinstance(issue_num, int) or not isinstance(lbl, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and label (str) are required"}')],
                isError=True,
            )
        result = await github_remove_label(issue_num, lbl)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "github_add_comment":
        issue_num = arguments.get("issue_number")
        body = arguments.get("body")
        if not isinstance(issue_num, int) or not isinstance(body, str) or not body:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and body (non-empty str) are required"}')],
                isError=True,
            )
        result = await github_add_comment(issue_num, body)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    # Delegate sync tools
    return call_tool(name, arguments)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 request handler
# ---------------------------------------------------------------------------


def handle_request(
    raw: dict[str, JsonValue],
) -> JsonRpcSuccessResponse | JsonRpcErrorResponse | None:
    """Dispatch a JSON-RPC 2.0 request dict and return a response dict.

    This is the single entry point for the MCP layer.  The caller is
    responsible for JSON parsing (converting the wire bytes to a ``dict``);
    this function handles everything from field extraction through to
    building the response envelope.

    Returns ``None`` for JSON-RPC notifications (messages with no ``id``
    field, such as ``initialized``) — the caller must not write anything to
    the wire for a ``None`` return value.

    Args:
        raw: A ``dict[str, JsonValue]`` parsed from a JSON-RPC 2.0 request body.

    Returns:
        A :class:`~agentception.mcp.types.JsonRpcSuccessResponse`,
        a :class:`~agentception.mcp.types.JsonRpcErrorResponse`, or ``None``
        for notifications that require no response.

    Never raises.
    """
    _raw_id: JsonValue = raw.get("id")
    request_id: int | str | None = (
        _raw_id if isinstance(_raw_id, (int, str)) else None
    )

    jsonrpc = raw.get("jsonrpc")
    if jsonrpc != "2.0":
        return _make_error_response(
            request_id,
            JSONRPC_ERR_INVALID_REQUEST,
            "jsonrpc must be '2.0'",
        )

    method = raw.get("method")
    if not isinstance(method, str):
        return _make_error_response(
            request_id,
            JSONRPC_ERR_INVALID_REQUEST,
            "method must be a string",
        )

    logger.debug("🔧 handle_request: method=%r id=%r", method, request_id)

    # ── MCP lifecycle handshake ──────────────────────────────────────────────

    if method == "initialize":
        init_result: InitializeResult = {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": McpCapabilities(tools={}, resources={}, prompts={}),
            "serverInfo": _SERVER_INFO,
        }
        return _make_success_response(request_id, init_result)

    if method == "initialized":
        logger.debug("✅ MCP initialized notification received")
        return None

    if method == "ping":
        return _make_success_response(request_id, {})

    # ── Tool methods ─────────────────────────────────────────────────────────

    if method == "tools/list":
        tools = list_tools()
        return _make_success_response(request_id, ToolListResult(tools=tools))

    if method == "tools/call":
        params = raw.get("params")
        if not isinstance(params, dict):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params must be an object for tools/call",
            )

        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.name must be a string",
            )

        arguments_raw = params.get("arguments", {})
        if not isinstance(arguments_raw, dict):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.arguments must be an object",
            )

        arguments: dict[str, JsonValue] = {k: v for k, v in arguments_raw.items()}

        try:
            tool_result = call_tool(tool_name, arguments)
        except Exception as exc:
            logger.error("❌ handle_request: internal error in call_tool — %s", exc, exc_info=True)
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INTERNAL_ERROR,
                f"Internal error: {exc}",
            )

        return _make_success_response(request_id, tool_result)

    # ── Prompt methods (sync — static prompts only) ───────────────────────────
    # Parameterized prompts (task/*) require async DB access; callers using
    # the sync path should use handle_request_async for those.

    if method == "prompts/list":
        return _make_success_response(
            request_id, PromptListResult(prompts=list_prompts())
        )

    if method == "prompts/get":
        params_p = raw.get("params")
        if not isinstance(params_p, dict):
            return _make_error_response(
                request_id, JSONRPC_ERR_INVALID_PARAMS, "params must be an object"
            )
        prompt_name = params_p.get("name")
        if not isinstance(prompt_name, str) or not prompt_name:
            return _make_error_response(
                request_id, JSONRPC_ERR_INVALID_PARAMS, "params.name must be a non-empty string"
            )
        if prompt_name.startswith("task/"):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                f"Prompt {prompt_name!r} requires async resolution — use handle_request_async.",
            )
        prompt_result: ACPromptResult | None = get_static_prompt(prompt_name)
        if prompt_result is None:
            return _make_error_response(
                request_id, JSONRPC_ERR_INVALID_PARAMS, f"Unknown prompt: {prompt_name!r}"
            )
        return _make_success_response(request_id, prompt_result)

    # ── Resource methods (sync server returns method-not-found for reads) ─────
    # Resource reads require async I/O — callers must use handle_request_async.

    if method in ("resources/list", "resources/templates/list", "resources/read"):
        return _make_error_response(
            request_id,
            JSONRPC_ERR_METHOD_NOT_FOUND,
            f"Method '{method}' requires the async path — use handle_request_async",
        )

    return _make_error_response(
        request_id,
        JSONRPC_ERR_METHOD_NOT_FOUND,
        f"Method not found: {method!r}",
    )


async def handle_request_async(
    raw: dict[str, JsonValue],
) -> JsonRpcSuccessResponse | JsonRpcErrorResponse | None:
    """Async variant of :func:`handle_request` — routes ``tools/call`` through
    :func:`call_tool_async` so that async tools (all build tools and
    ``plan_get_labels`` / ``plan_advance_phase``) are awaited correctly.

    The stdio transport must use this function instead of
    :func:`handle_request`; the sync version hard-returns an error for every
    async tool.

    Returns ``None`` for JSON-RPC notifications (no ``id`` field).
    Never raises.
    """
    _raw_id: JsonValue = raw.get("id")
    request_id: int | str | None = (
        _raw_id if isinstance(_raw_id, (int, str)) else None
    )

    jsonrpc = raw.get("jsonrpc")
    if jsonrpc != "2.0":
        return _make_error_response(
            request_id,
            JSONRPC_ERR_INVALID_REQUEST,
            "jsonrpc must be '2.0'",
        )

    method = raw.get("method")
    if not isinstance(method, str):
        return _make_error_response(
            request_id,
            JSONRPC_ERR_INVALID_REQUEST,
            "method must be a string",
        )

    logger.debug("🔧 handle_request_async: method=%r id=%r", method, request_id)

    if method == "initialize":
        init_result_a: InitializeResult = {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": McpCapabilities(tools={}, resources={}, prompts={}),
            "serverInfo": _SERVER_INFO,
        }
        return _make_success_response(request_id, init_result_a)

    if method == "initialized":
        logger.debug("✅ MCP initialized notification received")
        return None

    if method == "ping":
        return _make_success_response(request_id, {})

    # ── Tool methods ─────────────────────────────────────────────────────────

    if method == "tools/list":
        tools = list_tools()
        return _make_success_response(request_id, ToolListResult(tools=tools))

    if method == "tools/call":
        params = raw.get("params")
        if not isinstance(params, dict):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params must be an object for tools/call",
            )

        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.name must be a string",
            )

        arguments_raw = params.get("arguments", {})
        if not isinstance(arguments_raw, dict):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.arguments must be an object",
            )

        arguments: dict[str, JsonValue] = {k: v for k, v in arguments_raw.items()}

        try:
            tool_result = await call_tool_async(tool_name, arguments)
        except Exception as exc:
            logger.error(
                "❌ handle_request_async: internal error in call_tool_async — %s",
                exc,
                exc_info=True,
            )
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INTERNAL_ERROR,
                f"Internal error: {exc}",
            )

        return _make_success_response(request_id, tool_result)

    # ── Prompt methods ────────────────────────────────────────────────────────

    if method == "prompts/list":
        return _make_success_response(
            request_id, PromptListResult(prompts=list_prompts())
        )

    if method == "prompts/get":
        params_pa = raw.get("params")
        if not isinstance(params_pa, dict):
            return _make_error_response(
                request_id, JSONRPC_ERR_INVALID_PARAMS, "params must be an object"
            )
        prompt_name_a = params_pa.get("name")
        if not isinstance(prompt_name_a, str) or not prompt_name_a:
            return _make_error_response(
                request_id, JSONRPC_ERR_INVALID_PARAMS, "params.name must be a non-empty string"
            )
        raw_args = params_pa.get("arguments")
        prompt_args: dict[str, str] = (
            {k: str(v) for k, v in raw_args.items() if isinstance(v, str)}
            if isinstance(raw_args, dict) else {}
        )
        prompt_result_a: ACPromptResult | None = await get_prompt(prompt_name_a, prompt_args)
        if prompt_result_a is None:
            return _make_error_response(
                request_id, JSONRPC_ERR_INVALID_PARAMS, f"Unknown prompt: {prompt_name_a!r}"
            )
        return _make_success_response(request_id, prompt_result_a)

    # ── Resource methods ──────────────────────────────────────────────────────

    if method == "resources/list":
        resources = list_resources()
        return _make_success_response(
            request_id, ResourceListResult(resources=resources)
        )

    if method == "resources/templates/list":
        templates = list_resource_templates()
        return _make_success_response(
            request_id, ResourceTemplateListResult(resourceTemplates=templates)
        )

    if method == "resources/read":
        params_r = raw.get("params")
        if not isinstance(params_r, dict):
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params must be an object for resources/read",
            )
        uri = params_r.get("uri")
        if not isinstance(uri, str) or not uri:
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INVALID_PARAMS,
                "params.uri must be a non-empty string",
            )
        try:
            resource_result = await read_resource(uri)
        except Exception as exc:
            logger.error(
                "❌ handle_request_async: internal error in read_resource — %s",
                exc,
                exc_info=True,
            )
            return _make_error_response(
                request_id,
                JSONRPC_ERR_INTERNAL_ERROR,
                f"Internal error: {exc}",
            )
        return _make_success_response(request_id, resource_result)

    return _make_error_response(
        request_id,
        JSONRPC_ERR_METHOD_NOT_FOUND,
        f"Method not found: {method!r}",
    )
