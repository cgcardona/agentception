from __future__ import annotations

"""AgentCeption MCP JSON-RPC 2.0 server.

Implements a minimal but spec-compliant JSON-RPC 2.0 dispatcher for the
AgentCeption MCP tool layer.  The dispatcher is synchronous and stateless —
it handles exactly one request per call to :func:`handle_request`.

Supported methods:
  ``initialize``  — MCP protocol handshake (returns server capabilities)
  ``initialized`` — MCP notification (no response; acknowledged silently)
  ``tools/list``  — returns all registered :class:`~agentception.mcp.types.ACToolDef`
  ``tools/call``  — dispatches to the named tool function

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
from typing import cast

from agentception.mcp.build_commands import (
    build_block_run,
    build_cancel_run,
    build_claim_run,
    build_complete_run,
    build_resume_run,
    build_spawn_child_run,
    build_stop_run,
    build_teardown_worktree,
)
from agentception.mcp.log_tools import (
    log_run_blocker,
    log_run_decision,
    log_run_message,
    log_run_step,
)
from agentception.mcp.query_tools import (
    query_active_runs,
    query_agent_task,
    query_children,
    query_dispatcher_state,
    query_pending_runs,
    query_run,
    query_run_events,
    query_run_tree,
    query_system_health,
)
from agentception.mcp.github_tools import (
    github_add_label,
    github_claim_issue,
    github_remove_label,
    github_unclaim_issue,
)
from agentception.mcp.plan_advance_phase import plan_advance_phase
from agentception.mcp.plan_tools import (
    plan_get_cognitive_figures,
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

#: MCP protocol version this server implements.
_MCP_PROTOCOL_VERSION = "2024-11-05"

#: Server identity advertised in the ``initialize`` response.
_SERVER_INFO: dict[str, object] = {"name": "agentception", "version": "0.1.1"}

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
        name="plan_get_cognitive_figures",
        description=(
            "Return the catalog of cognitive architecture figures compatible with a given role slug. "
            "Reads role-taxonomy.yaml to filter figures by role, then returns each figure's id, "
            "display name, and one-line description. "
            "Use this before assigning cognitive_arch fields in a PlanSpec so assignments are "
            "grounded in the actual available figures for each role. "
            "Returns {role: str, figures: [{id: str, display_name: str, description: str}, ...]} "
            "or {role: str, figures: [], error: str} when the role is unknown."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": (
                        "Role slug from role-taxonomy.yaml — e.g. 'cto', "
                        "'engineering-coordinator', 'qa-coordinator', 'python-developer'."
                    ),
                }
            },
            "required": ["role"],
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
    ACToolDef(
        name="github_claim_issue",
        description=(
            "Claim a GitHub issue for this agent by adding the 'agent/wip' label. "
            "Call this before starting work to prevent double-claiming. "
            "Invalidates the read cache. Returns {ok, issue_number, claimed: true}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "GitHub issue number to claim."},
            },
            "required": ["issue_number"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="github_unclaim_issue",
        description=(
            "Release an issue claim by removing the 'agent/wip' label. "
            "Call this when finishing or aborting work. "
            "Invalidates the read cache. Returns {ok, issue_number, claimed: false}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "GitHub issue number to unclaim."},
            },
            "required": ["issue_number"],
            "additionalProperties": False,
        },
    ),
    # ── Query tools — read-only state inspection ──────────────────────────────
    ACToolDef(
        name="query_pending_runs",
        description=(
            "Return all issues queued for launch from the AgentCeption UI. "
            "The Dispatcher calls this once to discover what the UI has queued. "
            "Each item has run_id, issue_number, role, host_worktree_path, and batch_id. "
            "The role tells you what kind of agent to spawn — a leaf worker implements "
            "one issue directly; a coordinator reads its role file and spawns its own "
            "children via the Task tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_run",
        description=(
            "Return lightweight metadata for a single run by run_id. "
            "Agents call this on startup to determine their current state (status, "
            "issue_number, parent_run_id, worktree_path, tier, role, batch_id). "
            "Returns ok=false when the run does not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID to look up."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_children",
        description=(
            "Return all runs spawned by a given parent run_id, ordered by spawn time. "
            "Coordinator and VP-tier agents use this to track the state of engineers they dispatched."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The parent run ID."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_run_events",
        description=(
            "Return structured MCP events for a run (log_run_step, log_run_blocker, etc.). "
            "Agents use this to reconstruct what happened in a previous session after a crash. "
            "Pass after_id to page through events incrementally."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run to query events for."},
                "after_id": {
                    "type": "integer",
                    "description": "Return only events with DB id > this value. Defaults to 0.",
                    "default": 0,
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_agent_task",
        description=(
            "Return the raw text content of the .agent-task TOML file for a run. "
            "Agents use this to verify their own configuration on startup or after a restart. "
            "Returns ok=false if the worktree has been torn down or the file does not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run to read the agent task for."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_active_runs",
        description=(
            "Return all runs currently in a live or blocked state "
            "(pending_launch, implementing, reviewing, blocked). "
            "Supervisory agents and the Dispatcher use this for a system-wide snapshot."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_run_tree",
        description=(
            "Return all runs in a batch as a flat list with parent_run_id references. "
            "Assemble into a tree by following parent_run_id. "
            "Used by the Dispatcher and supervisory agents to visualise the run hierarchy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "The batch fingerprint to query."},
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_dispatcher_state",
        description=(
            "Return current dispatcher state: run counts per status, active run total, "
            "and the latest active batch_id. "
            "Designed for supervisory agents that need a high-level view of the system."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="query_system_health",
        description=(
            "Return a system-health snapshot: DB reachability, total runs per status. "
            "Always returns a result — db_ok=false signals a degraded database. "
            "Use for diagnostics and health checks."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    # ── Build commands — explicit state transitions only ──────────────────────
    ACToolDef(
        name="build_claim_run",
        description=(
            "Atomically claim a pending run before spawning its Task agent. "
            "Call this with the run_id from query_pending_runs immediately before "
            "firing the Task so the run cannot be double-claimed by a concurrent "
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
        name="build_spawn_child_run",
        description=(
            "Create a child agent node in the agent tree. "
            "Any coordinator agent calls this to atomically create a worktree, "
            "write a .agent-task file with TIER, COGNITIVE_ARCH, and full "
            "lineage fields, register a DB record, and auto-claim the run. "
            "Returns {ok, child_run_id, worktree_path, tier, org_domain, role, "
            "cognitive_arch, agent_task_path, scope_type, scope_value}. "
            "After calling this tool, immediately fire a Task with the briefing: "
            "'Read your .agent-task at {worktree_path}/.agent-task and follow "
            "the instructions for your role.' "
            "This is the canonical way to grow the agent tree at runtime."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "parent_run_id": {
                    "type": "string",
                    "description": "run_id of the calling agent (lineage tracking).",
                },
                "role": {
                    "type": "string",
                    "description": "Child role slug (e.g. 'engineering-coordinator', 'python-developer').",
                },
                "tier": {
                    "type": "string",
                    "enum": ["coordinator", "engineer", "reviewer"],
                    "description": "Behavioral execution tier.",
                },
                "org_domain": {
                    "type": "string",
                    "enum": ["c-suite", "engineering", "qa"],
                    "description": "Organisational slot for UI hierarchy. Optional.",
                },
                "scope_type": {
                    "type": "string",
                    "enum": ["label", "issue", "pr"],
                    "description": "'label' for coordinator nodes, 'issue' for engineer nodes, 'pr' for reviewer nodes.",
                },
                "scope_value": {
                    "type": "string",
                    "description": "Label string, issue number (as string), or PR number (as string).",
                },
                "gh_repo": {
                    "type": "string",
                    "description": "'owner/repo' string.",
                },
                "issue_body": {
                    "type": "string",
                    "description": "Issue body for COGNITIVE_ARCH skill extraction (issue-scoped children).",
                },
                "issue_title": {
                    "type": "string",
                    "description": "Issue title written to ISSUE_TITLE field.",
                },
                "skills_hint": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit skill list override for COGNITIVE_ARCH (bypasses keyword extraction).",
                },
                "coord_fingerprint": {
                    "type": "string",
                    "description": (
                        "The spawning coordinator's fingerprint string. Written as "
                        "COORD_FINGERPRINT in the child's .agent-task so leaf agents "
                        "can include it in their GitHub fingerprint comments."
                    ),
                },
                "cognitive_arch": {
                    "type": "string",
                    "description": (
                        "When provided, forward this exact cognitive architecture string "
                        "to the child without re-resolving. Coordinators must pass their "
                        "own cognitive_arch here so the field propagates unchanged through "
                        "every tier of the agent tree."
                    ),
                },
            },
            "required": ["parent_run_id", "role", "tier", "scope_type", "scope_value", "gh_repo"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_complete_run",
        description=(
            "Record that the agent has finished work and transition the run to completed. "
            "Persists the done event (linking the PR and updating workflow state). "
            "Does NOT tear down the worktree — call build_teardown_worktree after this "
            "if cleanup is needed (the Dispatcher controls teardown timing). "
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
    ACToolDef(
        name="build_teardown_worktree",
        description=(
            "Clean up the git worktree for a completed or stopped run. "
            "Fires teardown as a background task and returns immediately. "
            "The Dispatcher or orchestration layer should call this after build_complete_run. "
            "Engineers should not call this directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_run_id": {
                    "type": "string",
                    "description": "The run ID of the completed agent (must have a worktree).",
                },
            },
            "required": ["agent_run_id"],
            "additionalProperties": False,
        },
    ),
    ACToolDef(
        name="build_block_run",
        description=(
            "Transition an implementing run to blocked. "
            "Call when the agent cannot proceed without external input. "
            "The run stays blocked until build_resume_run is called. "
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
            "Idempotent: if the run is already implementing and agent_run_id matches, "
            "returns ok=true without state change (restart-safe). "
            "Valid from blocked or stopped states only."
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
        name="build_cancel_run",
        description=(
            "Transition any active run to cancelled (terminal — cannot resume). "
            "Use build_stop_run if you want to pause and later resume. "
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
        name="build_stop_run",
        description=(
            "Transition any active run to stopped (resumable via build_resume_run). "
            "Use this to pause a run for inspection without permanently closing it. "
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
            "This tool never changes run state — use build_block_run for state transitions."
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
        name="log_run_blocker",
        description=(
            "Append a blocker event to the run's event log. "
            "This tool only records the event — it does NOT change run state. "
            "To also transition the run to blocked state, call build_block_run separately."
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
        name="log_run_decision",
        description=(
            "Record a significant architectural or implementation decision you made. "
            "Use this for choices that affect code structure, dependencies, or approach "
            "so the team can review your reasoning. Never changes run state."
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
        name="log_run_message",
        description=(
            "Append a free-form message to the agent's event log. "
            "Use for noteworthy information that doesn't fit a structured event type. "
            "Never changes run state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "message": {
                    "type": "string",
                    "description": "The message text to log.",
                },
                "agent_run_id": {"type": "string"},
            },
            "required": ["issue_number", "message"],
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

    if name == "plan_get_cognitive_figures":
        role = arguments.get("role")
        if not isinstance(role, str) or not role:
            err_text = _tool_result_to_text(
                {"error": "Missing or invalid required argument 'role' (must be a non-empty string)"}
            )
            return ACToolResult(
                content=[ACToolContent(type="text", text=err_text)],
                isError=True,
            )
        result_figures = plan_get_cognitive_figures(role)
        text = _tool_result_to_text(result_figures)
        is_error = "error" in result_figures
        return ACToolResult(
            content=[ACToolContent(type="text", text=text)],
            isError=is_error,
        )

    if name in (
        "plan_get_labels",
        "plan_spawn_coordinator",
        "plan_advance_phase",
        # Query tools
        "query_pending_runs",
        "query_run",
        "query_children",
        "query_run_events",
        "query_agent_task",
        "query_active_runs",
        "query_run_tree",
        "query_dispatcher_state",
        "query_system_health",
        # Build commands
        "build_claim_run",
        "build_spawn_child_run",
        "build_complete_run",
        "build_teardown_worktree",
        "build_block_run",
        "build_resume_run",
        "build_cancel_run",
        "build_stop_run",
        # Log tools
        "log_run_step",
        "log_run_blocker",
        "log_run_decision",
        "log_run_message",
        # GitHub tools
        "github_add_label",
        "github_remove_label",
        "github_claim_issue",
        "github_unclaim_issue",
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

    # ── Query tools ─────────────────────────────────────────────────────────

    if name == "query_pending_runs":
        result = await query_pending_runs()
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "query_run":
        qr_run_id = arguments.get("run_id")
        if not isinstance(qr_run_id, str) or not qr_run_id:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"query_run requires a non-empty run_id"}')],
                isError=True,
            )
        result = await query_run(qr_run_id)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "query_children":
        qc_run_id = arguments.get("run_id")
        if not isinstance(qc_run_id, str) or not qc_run_id:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"query_children requires a non-empty run_id"}')],
                isError=True,
            )
        result = await query_children(qc_run_id)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "query_run_events":
        qre_run_id = arguments.get("run_id")
        if not isinstance(qre_run_id, str) or not qre_run_id:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"query_run_events requires a non-empty run_id"}')],
                isError=True,
            )
        after_id_raw = arguments.get("after_id", 0)
        after_id = int(after_id_raw) if isinstance(after_id_raw, int) else 0
        result = await query_run_events(qre_run_id, after_id)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "query_agent_task":
        qat_run_id = arguments.get("run_id")
        if not isinstance(qat_run_id, str) or not qat_run_id:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"query_agent_task requires a non-empty run_id"}')],
                isError=True,
            )
        result = await query_agent_task(qat_run_id)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "query_active_runs":
        result = await query_active_runs()
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "query_run_tree":
        qrt_batch_id = arguments.get("batch_id")
        if not isinstance(qrt_batch_id, str) or not qrt_batch_id:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"query_run_tree requires a non-empty batch_id"}')],
                isError=True,
            )
        result = await query_run_tree(qrt_batch_id)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "query_dispatcher_state":
        result = await query_dispatcher_state()
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "query_system_health":
        result = await query_system_health()
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
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

    if name == "build_spawn_child_run":
        parent_run_id = arguments.get("parent_run_id")
        role = arguments.get("role")
        tier_arg = arguments.get("tier")
        scope_type = arguments.get("scope_type")
        scope_value = arguments.get("scope_value")
        gh_repo = arguments.get("gh_repo")
        if (
            not isinstance(parent_run_id, str)
            or not isinstance(role, str)
            or not isinstance(tier_arg, str)
            or not isinstance(scope_type, str)
            or not isinstance(scope_value, str)
            or not isinstance(gh_repo, str)
        ):
            err_text = _tool_result_to_text(
                {"error": "parent_run_id, role, tier, scope_type, scope_value, gh_repo (strings) are required"}
            )
            return ACToolResult(
                content=[ACToolContent(type="text", text=err_text)],
                isError=True,
            )
        issue_body_raw = arguments.get("issue_body", "")
        issue_body = str(issue_body_raw) if issue_body_raw else ""
        issue_title_raw = arguments.get("issue_title", "")
        issue_title = str(issue_title_raw) if issue_title_raw else ""
        org_domain_raw = arguments.get("org_domain", "")
        org_domain = str(org_domain_raw) if org_domain_raw else ""
        skills_raw = arguments.get("skills_hint")
        skills_hint: list[str] | None = None
        if isinstance(skills_raw, list):
            skills_hint = [str(s) for s in skills_raw]
        coord_fp_raw = arguments.get("coord_fingerprint")
        coord_fingerprint: str | None = str(coord_fp_raw) if isinstance(coord_fp_raw, str) else None
        cognitive_arch_raw = arguments.get("cognitive_arch", "")
        cognitive_arch: str = str(cognitive_arch_raw) if isinstance(cognitive_arch_raw, str) else ""
        result = await build_spawn_child_run(
            parent_run_id=parent_run_id,
            role=role,
            tier=tier_arg,
            scope_type=scope_type,
            scope_value=scope_value,
            gh_repo=gh_repo,
            org_domain=org_domain,
            issue_body=issue_body,
            issue_title=issue_title,
            skills_hint=skills_hint,
            coord_fingerprint=coord_fingerprint,
            cognitive_arch=cognitive_arch,
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
        result = await build_complete_run(
            issue_num, pr_url, str(summary), str(run_id) if run_id else None
        )
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "build_teardown_worktree":
        run_id_arg2 = arguments.get("agent_run_id")
        if not isinstance(run_id_arg2, str) or not run_id_arg2:
            return ACToolResult(
                content=[ACToolContent(
                    type="text",
                    text=_tool_result_to_text({"error": "build_teardown_worktree requires a non-empty agent_run_id"}),
                )],
                isError=True,
            )
        result = await build_teardown_worktree(run_id_arg2)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "build_block_run":
        run_id_arg3 = arguments.get("run_id")
        if not isinstance(run_id_arg3, str) or not run_id_arg3:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_block_run requires a non-empty run_id"}')],
                isError=True,
            )
        result = await build_block_run(run_id_arg3)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "build_resume_run":
        run_id_arg4 = arguments.get("run_id")
        agent_run_id_arg = arguments.get("agent_run_id")
        if (
            not isinstance(run_id_arg4, str)
            or not run_id_arg4
            or not isinstance(agent_run_id_arg, str)
            or not agent_run_id_arg
        ):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_resume_run requires run_id and agent_run_id (non-empty strings)"}')],
                isError=True,
            )
        result = await build_resume_run(run_id_arg4, agent_run_id_arg)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
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

    if name == "build_stop_run":
        run_id_arg6 = arguments.get("run_id")
        if not isinstance(run_id_arg6, str) or not run_id_arg6:
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"build_stop_run requires a non-empty run_id"}')],
                isError=True,
            )
        result = await build_stop_run(run_id_arg6)
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

    if name == "log_run_blocker":
        issue_num = arguments.get("issue_number")
        desc = arguments.get("description")
        if not isinstance(issue_num, int) or not isinstance(desc, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and description (str) are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await log_run_blocker(issue_num, desc, str(run_id) if run_id else None)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "log_run_decision":
        issue_num = arguments.get("issue_number")
        decision = arguments.get("decision")
        rationale = arguments.get("rationale")
        if not isinstance(issue_num, int) or not isinstance(decision, str) or not isinstance(rationale, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number, decision, rationale are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await log_run_decision(
            issue_num, decision, rationale, str(run_id) if run_id else None
        )
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=False,
        )

    if name == "log_run_message":
        issue_num = arguments.get("issue_number")
        msg = arguments.get("message")
        if not isinstance(issue_num, int) or not isinstance(msg, str):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) and message (str) are required"}')],
                isError=True,
            )
        run_id = arguments.get("agent_run_id")
        result = await log_run_message(issue_num, msg, str(run_id) if run_id else None)
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

    if name == "github_claim_issue":
        issue_num = arguments.get("issue_number")
        if not isinstance(issue_num, int):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) is required"}')],
                isError=True,
            )
        result = await github_claim_issue(issue_num)
        return ACToolResult(
            content=[ACToolContent(type="text", text=_tool_result_to_text(result))],
            isError=not bool(result.get("ok", False)),
        )

    if name == "github_unclaim_issue":
        issue_num = arguments.get("issue_number")
        if not isinstance(issue_num, int):
            return ACToolResult(
                content=[ACToolContent(type="text", text='{"error":"issue_number (int) is required"}')],
                isError=True,
            )
        result = await github_unclaim_issue(issue_num)
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
    raw: dict[str, object],
) -> dict[str, object] | None:
    """Dispatch a JSON-RPC 2.0 request dict and return a response dict.

    This is the single entry point for the MCP layer.  The caller is
    responsible for JSON parsing (converting the wire bytes to a ``dict``);
    this function handles everything from field extraction through to
    building the response envelope.

    Returns ``None`` for JSON-RPC notifications (messages with no ``id``
    field, such as ``initialized``) — the caller must not write anything to
    the wire for a ``None`` return value.

    Args:
        raw: A ``dict[str, object]`` parsed from a JSON-RPC 2.0 request body.

    Returns:
        A :class:`~agentception.mcp.types.JsonRpcSuccessResponse`,
        a :class:`~agentception.mcp.types.JsonRpcErrorResponse`, or ``None``
        for notifications that require no response.

    Never raises.
    """
    _raw_id: object = raw.get("id")
    request_id: int | str | None = (
        _raw_id if isinstance(_raw_id, (int, str)) else None
    )

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

    # ── MCP lifecycle handshake ──────────────────────────────────────────────

    if method == "initialize":
        # Respond with our protocol version and tool capability declaration.
        result: dict[str, object] = {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _SERVER_INFO,
        }
        return cast(dict[str, object], _make_success_response(request_id, result))

    if method == "initialized":
        # JSON-RPC notification — no id, no response required.
        logger.debug("✅ MCP initialized notification received")
        return None

    # ── Tool methods ─────────────────────────────────────────────────────────

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


async def handle_request_async(
    raw: dict[str, object],
) -> dict[str, object] | None:
    """Async variant of :func:`handle_request` — routes ``tools/call`` through
    :func:`call_tool_async` so that async tools (all build tools and
    ``plan_get_labels`` / ``plan_spawn_coordinator``) are awaited correctly.

    The stdio transport must use this function instead of
    :func:`handle_request`; the sync version hard-returns an error for every
    async tool.

    Returns ``None`` for JSON-RPC notifications (no ``id`` field).
    Never raises.
    """
    _raw_id: object = raw.get("id")
    request_id: int | str | None = (
        _raw_id if isinstance(_raw_id, (int, str)) else None
    )

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

    logger.debug("🔧 handle_request_async: method=%r id=%r", method, request_id)

    if method == "initialize":
        result: dict[str, object] = {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _SERVER_INFO,
        }
        return cast(dict[str, object], _make_success_response(request_id, result))

    if method == "initialized":
        logger.debug("✅ MCP initialized notification received")
        return None

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
            tool_result = await call_tool_async(tool_name, arguments)
        except Exception as exc:
            logger.error(
                "❌ handle_request_async: internal error in call_tool_async — %s",
                exc,
                exc_info=True,
            )
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
