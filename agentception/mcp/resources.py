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
    ac://system/config        — pipeline label config (claim label, active label, etc.)
    ac://plan/schema          — PlanSpec JSON Schema (changes only on deploy)
    ac://plan/labels          — GitHub label catalogue for the configured repo
    ac://roles/list           — available role slugs in the team taxonomy

Templated resources (RFC 6570)
    ac://runs/{run_id}                  — lightweight metadata for one run
    ac://runs/{run_id}/children         — child runs spawned by this run
    ac://runs/{run_id}/events           — full structured event log
    ac://runs/{run_id}/events?after_id={n} — paginated event log (n > 0)

    ac://batches/{batch_id}/tree        — all runs in a batch, flat list
    ac://plan/figures/{role}            — cognitive-arch figures for a role
    ac://roles/{slug}                   — role definition Markdown for a slug
"""

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from agentception.config import settings
from agentception.mcp.plan_tools import (
    plan_get_cognitive_figures,
    plan_get_labels,
    plan_get_schema,
)
from agentception.mcp.query_tools import (
    query_active_runs,
    query_children,
    query_dispatcher_state,
    query_pending_runs,
    query_run,
    query_run_context,
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

# Root of the compiled .agentception/ directory.  Works both inside Docker
# (/app is the repo root) and during local development.
_APP_ROOT = Path(__file__).parent.parent.parent
_AGENTCEPTION_DIR = _APP_ROOT / ".agentception"

# Cognitive architecture corpus root.
_ARCH_ROOT = _APP_ROOT / "scripts" / "gen_prompts" / "cognitive_archetypes"
_ARCH_SUBDIRS: dict[str, str] = {
    "figures": "figures",
    "archetypes": "archetypes",
    "skills": "skill_domains",
    "atoms": "atoms",
}

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
    ACResourceDef(
        uri="ac://system/config",
        name="Pipeline config",
        description=(
            "Current pipeline label configuration: claim_label, active_label, "
            "gated_label, and the configured GitHub repo. "
            "Read before writing labels to ensure you are using the canonical names."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://roles/list",
        name="Available roles",
        description=(
            "All role slugs defined in the team taxonomy. "
            "Returns {roles: [str, ...]} sorted alphabetically. "
            "Use a slug from this list when calling build_spawn_adhoc_child or "
            "reading ac://roles/{slug}."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://arch/figures",
        name="Cognitive figure index",
        description=(
            "Index of all cognitive figures in the corpus. "
            "Returns {figures: [{id, display_name, description}]} sorted by id. "
            "Use an id to fetch the full profile at ac://arch/figures/{figure_id}."
        ),
        mimeType=_MIME,
    ),
    ACResourceDef(
        uri="ac://arch/archetypes",
        name="Cognitive archetype index",
        description=(
            "Index of all cognitive archetypes in the corpus. "
            "Returns {archetypes: [{id, display_name, description}]}. "
            "Use an id to fetch the full profile at ac://arch/archetypes/{archetype_id}."
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
        name="Run raw task context",
        description=(
            "Raw task_context string for a run, as stored in ac_agent_runs. "
            "Returns the plain text task context. "
            "Returns ResourceNotFound error for nonexistent run_id."
        ),
        mimeType="text/plain",
    ),
    ACResourceTemplate(
        uriTemplate="ac://runs/{run_id}/context",
        name="Run task context",
        description=(
            "Full task context for a run — the authoritative DB-sourced RunContextRow. "
            "Includes run_id, status, role, cognitive_arch, task_description, "
            "issue_number, pr_number, worktree_path, branch, tier, org_domain, "
            "batch_id, parent_run_id, coord_fingerprint, gh_repo, is_resumed, "
            "spawned_at, last_activity_at, completed_at. "
            "Returns ok=false when the run does not exist."
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
    ACResourceTemplate(
        uriTemplate="ac://roles/{slug}",
        name="Role definition",
        description=(
            "Full role definition Markdown for a given role slug. "
            "Returns {slug, content: str} where content is the raw Markdown. "
            "Use ac://roles/list to discover available slugs. "
            "Returns ok=false when the slug is not found."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://arch/figures/{figure_id}",
        name="Cognitive figure profile",
        description=(
            "Full cognitive profile of a named figure — the human being whose "
            "reasoning style, heuristics, failure modes, and skill affinities "
            "shape this agent's identity. "
            "Returns the parsed YAML as structured JSON: id, display_name, "
            "description, overrides (atom values), skill_domains, heuristic, "
            "failure_modes, and prompt_injection text. "
            "Read this to internalize who you are before starting any task. "
            "Your figure_id comes from the cognitive_arch field in your briefing "
            "(the token before the first colon, e.g. 'guido_van_rossum' from "
            "'guido_van_rossum:python')."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://arch/archetypes/{archetype_id}",
        name="Cognitive archetype profile",
        description=(
            "Full definition of a cognitive archetype — the behavioural template "
            "that a figure extends (e.g. 'the_pragmatist', 'the_hacker'). "
            "Returns id, display_name, description, default atom values, and "
            "characteristic traits. "
            "Your archetype is the 'extends' field in your figure profile."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://arch/skills/{skill_id}",
        name="Skill domain profile",
        description=(
            "Full definition of a skill domain — the technical expertise area "
            "assigned to this agent (e.g. 'python', 'htmx', 'fastapi'). "
            "Returns id, display_name, description, and characteristic patterns. "
            "Your skill_ids come from the tokens after the first colon in your "
            "cognitive_arch string (e.g. ['python', 'fastapi'] from "
            "'guido_van_rossum:python:fastapi')."
        ),
        mimeType=_MIME,
    ),
    ACResourceTemplate(
        uriTemplate="ac://arch/atoms/{atom_id}",
        name="Cognitive atom profile",
        description=(
            "Full definition of a cognitive atom — a single dimension of reasoning "
            "style such as 'epistemic_style', 'quality_bar', or 'error_posture'. "
            "Returns id, display_name, description, and all possible values with "
            "their meanings. "
            "Atom values are overridden per-figure and visible in the figure profile."
        ),
        mimeType=_MIME,
    ),
]

# ---------------------------------------------------------------------------
# URI dispatcher
# ---------------------------------------------------------------------------


def _arch_dir(category: str) -> Path:
    """Resolve the filesystem directory for a given arch category key."""
    subdir = _ARCH_SUBDIRS.get(category, category)
    return _ARCH_ROOT / subdir


def _read_arch_yaml(category: str, item_id: str) -> dict[str, object]:
    """Read and parse a single cognitive architecture YAML file.

    Args:
        category: One of ``"figures"``, ``"archetypes"``, ``"skills"``, ``"atoms"``.
        item_id:  Filename stem (e.g. ``"guido_van_rossum"``).

    Returns:
        Parsed YAML content as a dict, plus ``ok=True``.
        ``{"ok": False, "error": str}`` when not found or unreadable.
    """
    path = _arch_dir(category) / f"{item_id}.yaml"
    if not path.exists():
        return {"ok": False, "error": f"Arch item '{category}/{item_id}' not found"}
    try:
        raw = path.read_text(encoding="utf-8")
        parsed: dict[str, object] = yaml.safe_load(raw) or {}
        parsed["ok"] = True
        return parsed
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ _read_arch_yaml %s/%s: %s", category, item_id, exc)
        return {"ok": False, "error": str(exc)}


def _list_arch_items(category: str) -> dict[str, object]:
    """Return a compact index of all items in an arch category directory.

    Each item includes ``id``, ``display_name``, and ``description`` (first line
    only) so the agent can browse without fetching each individual resource.

    Returns:
        ``{"ok": True, category: [...], "count": N}``
    """
    d = _arch_dir(category)
    if not d.is_dir():
        return {"ok": False, "error": f"Arch directory '{category}' not found at {d}"}
    items: list[dict[str, object]] = []
    for path in sorted(d.glob("*.yaml")):
        try:
            data: dict[str, object] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            desc_raw = data.get("description", "")
            first_line = str(desc_raw).split("\n")[0].strip() if desc_raw else ""
            items.append({
                "id": path.stem,
                "display_name": str(data.get("display_name", path.stem)),
                "description": first_line,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ _list_arch_items: skipping %s — %s", path.name, exc)
    return {"ok": True, category: items, "count": len(items)}


def _get_system_config() -> dict[str, object]:
    """Return current pipeline label configuration from settings."""
    return {
        "gh_repo": settings.gh_repo,
        "claim_label": "agent/wip",
        "active_label": "pipeline/active",
        "gated_label": "pipeline/gated",
        "blocked_label": "blocked/deps",
    }


def _get_roles_list() -> dict[str, object]:
    """Return sorted list of all available role slugs."""
    roles_dir = _AGENTCEPTION_DIR / "roles"
    if not roles_dir.is_dir():
        logger.warning("⚠️  ac://roles/list: roles directory not found at %s", roles_dir)
        return {"roles": [], "error": "roles directory not found"}
    slugs = sorted(p.stem for p in roles_dir.glob("*.md"))
    return {"roles": slugs, "count": len(slugs)}


def _get_role(uri: str, slug: str) -> dict[str, object]:
    """Return the Markdown content of a role definition file."""
    path = _AGENTCEPTION_DIR / "roles" / f"{slug}.md"
    if not path.exists():
        return {"ok": False, "error": f"Role {slug!r} not found"}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("❌ ac://roles/%s: could not read file — %s", slug, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "slug": slug, "content": content}


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


async def _handle_run_task(uri: str, run_id: str) -> ACResourceResult:
    """Return the task_description field for a run as a plain-text resource."""
    from sqlalchemy import select
    from agentception.db.engine import get_session
    from agentception.db.models import ACAgentRun

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            row = result.scalar_one_or_none()
    except Exception as exc:
        logger.error("❌ _handle_run_task: DB error for %r: %s", run_id, exc)
        return _content(uri, {"error": f"Internal error: {exc}"})

    if row is None:
        return _content(uri, {"error": f"Run {run_id!r} not found"})

    task_description: str = row.task_description or ""
    return ACResourceResult(
        contents=[
            ACResourceContent(
                uri=uri,
                mimeType="text/plain",
                text=task_description,
            )
        ]
    )


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
        if path_parts == ["config"]:
            return _content(uri, _get_system_config())
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

            if sub == "context" and len(path_parts) == 2:
                return _content(uri, await query_run_context(run_id))

            if sub == "task" and len(path_parts) == 2:
                return await _handle_run_task(uri, run_id)

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

    # ── ac://roles/* ─────────────────────────────────────────────────────────
    if domain == "roles":
        if path_parts == ["list"]:
            return _content(uri, _get_roles_list())
        if len(path_parts) == 1:
            return _content(uri, _get_role(uri, path_parts[0]))
        return _not_found(uri)

    # ── ac://arch/* ──────────────────────────────────────────────────────────
    if domain == "arch":
        # ac://arch/figures  (index — no trailing segment)
        if path_parts == ["figures"]:
            return _content(uri, _list_arch_items("figures"))

        # ac://arch/archetypes  (index)
        if path_parts == ["archetypes"]:
            return _content(uri, _list_arch_items("archetypes"))

        # ac://arch/figures/{figure_id}
        if len(path_parts) == 2 and path_parts[0] == "figures":
            return _content(uri, _read_arch_yaml("figures", path_parts[1]))

        # ac://arch/archetypes/{archetype_id}
        if len(path_parts) == 2 and path_parts[0] == "archetypes":
            return _content(uri, _read_arch_yaml("archetypes", path_parts[1]))

        # ac://arch/skills/{skill_id}
        if len(path_parts) == 2 and path_parts[0] == "skills":
            return _content(uri, _read_arch_yaml("skills", path_parts[1]))

        # ac://arch/atoms/{atom_id}
        if len(path_parts) == 2 and path_parts[0] == "atoms":
            return _content(uri, _read_arch_yaml("atoms", path_parts[1]))

        return _not_found(uri)

    return _not_found(uri)
