from __future__ import annotations

"""MCP resource definitions and URI dispatcher for the AgentCeption server.

All read-only state inspection that was previously expressed as ``query_*``
tools (and the ``plan_get_*`` tools) is now exposed as MCP Resources — the
correct abstraction in the MCP spec for stateless, side-effect-free reads.

URI scheme
----------
All resources live under the ``ac://`` scheme.  The authority (netloc) acts
as a resource *domain*, and the path identifies the specific resource within
that domain.  Parameterised resources follow RFC 6570 Level 1 URI templates.

Static resources
    ac://runs/active          — all live runs (pending, implementing, blocked)
    ac://runs/pending         — runs queued for Dispatcher launch
    ac://system/dispatcher    — dispatcher run counters and active batch_id
    ac://system/health        — DB reachability and per-status run counts
    ac://plan/schema          — PlanSpec JSON Schema (changes only on deploy)
    ac://plan/labels          — GitHub label catalogue for the configured repo

Templated resources (RFC 6570)
    ac://runs/{run_id}                  — lightweight metadata for one run
    ac://runs/{run_id}/children         — child runs spawned by this run
    ac://runs/{run_id}/events           — full structured event log
    ac://runs/{run_id}/events?after_id={n} — paginated event log (n > 0)
    ac://runs/{run_id}/task             — raw .agent-task TOML text
    ac://batches/{batch_id}/tree        — all runs in a batch, flat list
    ac://plan/figures/{role}            — cognitive-arch figures for a role
"""

import json
import logging
from urllib.parse import parse_qs, urlparse

from agentception.mcp.plan_tools import (
    plan_get_cognitive_figures,
    plan_get_labels,
    plan_get_schema,
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
from agentception.mcp.types import (
    ACResourceContent,
    ACResourceDef,
    ACResourceResult,
    ACResourceTemplate,
)

logger = logging.getLogger(__name__)

_MIME = "application/json"

# ---------------------------------------------------------------------------
# Static resource catalogue
# ---------------------------------------------------------------------------

RESOURCES: list[ACResourceDef] = [
    ACResourceDef(
        uri="ac://runs/active",
        name="Active runs",
        description=(
            "All runs currently in a live or blocked state "
            "(pending_launch, implementing, reviewing, blocked). "
            "Use for a system-wide snapshot of the agent fleet."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://runs/pending",
        name="Pending runs",
        description=(
            "Runs queued for launch from the AgentCeption UI. "
            "The Dispatcher reads this once to discover what the UI has queued. "
            "Each item has run_id, issue_number, role, host_worktree_path, and batch_id."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://system/dispatcher",
        name="Dispatcher state",
        description=(
            "Current dispatcher state: run counts per status, active run total, "
            "and the latest active batch_id. "
            "For supervisory agents that need a high-level view of the system."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://system/health",
        name="System health",
        description=(
            "System-health snapshot: DB reachability, total runs per status. "
            "Always returns a result — db_ok=false signals a degraded database."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://plan/schema",
        name="PlanSpec schema",
        description=(
            "JSON Schema for PlanSpec — the plan-step-v2 YAML contract. "
            "Read this to understand the required structure before calling "
            "plan_validate_spec."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://plan/labels",
        name="GitHub labels",
        description=(
            "Full GitHub label list for the configured repository. "
            "Returns {labels: [{name: str, description: str}, ...]}."
        ),
        mimeType=_MIME,
    ),
]

# ---------------------------------------------------------------------------
# Resource template catalogue
# ---------------------------------------------------------------------------

RESOURCE_TEMPLATES: list[ACResourceTemplate] = [
    ACResourceTemplate(
        uriTemplate="ac://runs/{run_id}",
        name="Run metadata",
        description=(
            "Lightweight metadata for a single run: status, issue_number, "
            "parent_run_id, worktree_path, tier, role, batch_id. "
            "Read on startup to determine current state after a crash or restart. "
            "Returns ok=false when the run does not exist."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://runs/{run_id}/children",
        name="Child runs",
        description=(
            "All runs spawned by a given parent run_id, ordered by spawn time. "
            "Coordinator agents use this to track the state of workers they dispatched."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://runs/{run_id}/events",
        name="Run event log",
        description=(
            "Structured MCP events for a run (log_run_step, log_run_blocker, etc.). "
            "Read to reconstruct what happened in a previous session after a crash. "
            "Append ?after_id=N to page through events incrementally "
            "(returns only events with DB id > N)."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://runs/{run_id}/task",
        name="Agent task file",
        description=(
            "Raw text content of the .agent-task TOML file for a run. "
            "Read to verify configuration on startup or after a restart. "
            "Returns ok=false if the worktree has been torn down."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://batches/{batch_id}/tree",
        name="Batch run tree",
        description=(
            "All runs in a batch as a flat list with parent_run_id references. "
            "Assemble into a tree by following parent_run_id links. "
            "Used by the Dispatcher and supervisory agents to visualise the run hierarchy."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://plan/figures/{role}",
        name="Cognitive-arch figures",
        description=(
            "Cognitive architecture figures compatible with a given role slug. "
            "Returns {role, figures: [{id, display_name, description}]}. "
            "Read before assigning cognitive_arch fields in a PlanSpec."
        ),
        mimeType=_MIME,
    ),
]

# ---------------------------------------------------------------------------
# URI dispatcher
# ---------------------------------------------------------------------------


def _content(uri: str, data: dict[str, object]) -> ACResourceResult:
    """Wrap a result dict into a single-item ACResourceResult."""
    return ACResourceResult(
        contents=[
            ACResourceContent(
                uri=uri,
                mimeType=_MIME,
                text=json.dumps(data, ensure_ascii=False),
            )
        ]
    )


def _not_found(uri: str) -> ACResourceResult:
    return _content(uri, {"error": f"Unknown resource URI: {uri!r}"})


async def read_resource(uri: str) -> ACResourceResult:
    """Dispatch a ``resources/read`` request by URI.

    Parses the ``ac://`` URI, routes to the appropriate query/plan function,
    and wraps the result as an :class:`ACResourceResult`.

    Returns a result with ``error`` key when the URI is unknown or when a
    required path segment is missing.  Never raises.
    """
    try:
        parsed = urlparse(uri)
    except Exception as exc:
        logger.warning("⚠️ read_resource: could not parse URI %r: %s", uri, exc)
        return _content(uri, {"error": f"Invalid URI: {exc}"})

    if parsed.scheme != "ac":
        return _content(uri, {"error": f"Unsupported URI scheme {parsed.scheme!r} — expected 'ac'"})

    domain = parsed.netloc  # e.g. "runs", "system", "plan", "batches"
    # path starts with '/' — strip it and split on remaining '/'
    path_parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)  # e.g. {"after_id": ["5"]}

    try:
        return await _dispatch(uri, domain, path_parts, query)
    except Exception as exc:
        logger.error("❌ read_resource: unexpected error for %r: %s", uri, exc, exc_info=True)
        return _content(uri, {"error": f"Internal error: {exc}"})


async def _dispatch(
    uri: str,
    domain: str,
    path_parts: list[str],
    query: dict[str, list[str]],
) -> ACResourceResult:
    """Route a parsed ``ac://`` URI to the appropriate handler."""

    # ── ac://system/* ────────────────────────────────────────────────────────
    if domain == "system":
        if path_parts == ["health"]:
            return _content(uri, await query_system_health())
        if path_parts == ["dispatcher"]:
            return _content(uri, await query_dispatcher_state())
        return _not_found(uri)

    # ── ac://plan/* ──────────────────────────────────────────────────────────
    if domain == "plan":
        if path_parts == ["schema"]:
            return _content(uri, plan_get_schema())
        if path_parts == ["labels"]:
            return _content(uri, await plan_get_labels())
        if len(path_parts) == 2 and path_parts[0] == "figures":
            role = path_parts[1]
            return _content(uri, plan_get_cognitive_figures(role))
        return _not_found(uri)

    # ── ac://batches/* ───────────────────────────────────────────────────────
    if domain == "batches":
        # ac://batches/{batch_id}/tree
        if len(path_parts) == 2 and path_parts[1] == "tree":
            batch_id = path_parts[0]
            return _content(uri, await query_run_tree(batch_id))
        return _not_found(uri)

    # ── ac://runs/* ──────────────────────────────────────────────────────────
    if domain == "runs":
        # ac://runs/active  (static — must check before treating "active" as run_id)
        if path_parts == ["active"]:
            return _content(uri, await query_active_runs())

        # ac://runs/pending
        if path_parts == ["pending"]:
            return _content(uri, await query_pending_runs())

        # ac://runs/{run_id}  and  ac://runs/{run_id}/*
        if len(path_parts) >= 1:
            run_id = path_parts[0]

            if len(path_parts) == 1:
                # ac://runs/{run_id}
                return _content(uri, await query_run(run_id))

            sub = path_parts[1]

            if sub == "children" and len(path_parts) == 2:
                return _content(uri, await query_children(run_id))

            if sub == "task" and len(path_parts) == 2:
                return _content(uri, await query_agent_task(run_id))

            if sub == "events" and len(path_parts) == 2:
                after_id_vals = query.get("after_id", [])
                after_id = 0
                if after_id_vals:
                    try:
                        after_id = int(after_id_vals[0])
                    except ValueError:
                        pass
                return _content(uri, await query_run_events(run_id, after_id))

        return _not_found(uri)

    return _not_found(uri)
