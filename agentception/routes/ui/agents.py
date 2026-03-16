"""UI routes: agent listing, spawn forms, and agent detail."""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TypedDict

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request
from starlette.responses import Response

from agentception.db.queries import (
    AgentMessageRow,
    AgentRunDetail,
    AgentRunRow,
    BoardIssueRow,
    IssueDetailRow,
    SiblingRunRow,
    get_issue_detail,
)
from agentception.models import AgentNode, PipelineState, VALID_ROLES
from agentception.poller import get_state
from agentception.readers.pipeline_config import read_pipeline_config
from agentception.types import JsonValue
from ._shared import (
    _TEMPLATES,
    _CATEGORY_ORDER,
    _ROLE_CATEGORY_MAP,
    _fmt_duration,
    _parse_iso,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# View-layer TypedDicts — enriched structures built in route handlers.
# ---------------------------------------------------------------------------


class AgentEnrichedRow(TypedDict):
    """Live agent node enriched with persona and timing metadata."""

    node: AgentNode
    persona: dict[str, JsonValue]
    elapsed: str
    is_stale_idle: bool


class EnrichedAgentRunRow(TypedDict):
    """AgentRunRow with computed duration and formatted timestamp fields."""

    id: str
    wave_id: str | None
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    worktree_path: str | None
    role: str
    status: str
    attempt_number: int
    spawn_mode: str | None
    batch_id: str | None
    spawned_at: str
    last_activity_at: str | None
    completed_at: str | None
    duration_s: float | None
    duration_str: str
    spawned_fmt: str
    completed_fmt: str
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None


class BatchRow(TypedDict):
    """History rows grouped by batch_id for the agents listing view."""

    batch_id: str
    runs: list[EnrichedAgentRunRow]
    spawned_at: str | None
    spawned_fmt: str | None
    phase_label: str
    success_rate: int


class RoleGroupRow(TypedDict):
    """Role cards grouped by UI category for the spawn form."""

    category: str
    roles: list[dict[str, str]]


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request) -> HTMLResponse:
    """Agent listing page — live agents (in-memory) plus historical runs (Postgres).

    Live agents come from the in-memory poller state (real-time, filesystem
    backed).  Postgres ``ac_agent_runs`` provides the historical run list so
    completed agents are visible even after their worktrees are removed.

    Data enrichments served to the template:
    - ``agents``        — live in-memory agents enriched with persona + elapsed/staleness.
    - ``stats``         — KPI counts (total, active, done, success_rate, avg_duration_s).
    - ``batches``       — run history grouped by batch_id with per-batch success rate.
    - ``all_roles``     — unique roles seen in history (for filter dropdown).
    - ``all_statuses``  — unique statuses seen (for filter chips).
    """
    from agentception.db.queries import get_agent_run_history
    from agentception.routes.roles import resolve_cognitive_arch

    state = get_state() or PipelineState.empty()
    now_utc = datetime.datetime.now(datetime.UTC)

    # Flatten root + children into one list for the listing view.
    all_agents: list[AgentNode] = []
    for agent in state.agents:
        all_agents.append(agent)
        all_agents.extend(agent.children)

    # Enrich each live agent with persona + runtime context.
    agents_enriched: list[AgentEnrichedRow] = []
    for ag in all_agents:
        persona = resolve_cognitive_arch(ag.cognitive_arch)
        elapsed = ""
        is_stale_idle = False
        spawned_dt = _parse_iso(ag.spawned_at.isoformat() if hasattr(ag, "spawned_at") and ag.spawned_at else None)
        if spawned_dt:
            elapsed = _fmt_duration((now_utc - spawned_dt).total_seconds())
        last_activity_dt = _parse_iso(
            ag.last_activity_at.isoformat()
            if hasattr(ag, "last_activity_at") and ag.last_activity_at
            else None
        )
        if last_activity_dt:
            idle_s = (now_utc - last_activity_dt).total_seconds()
            is_stale_idle = idle_s > 900  # >15 min without activity
        agents_enriched.append(AgentEnrichedRow(
            node=ag,
            persona=persona,
            elapsed=elapsed,
            is_stale_idle=is_stale_idle,
        ))

    # Fetch full history from Postgres.
    run_history: list[AgentRunRow] = []
    try:
        run_history = await get_agent_run_history(limit=200)
    except Exception as exc:
        logger.debug("DB agent run history fetch skipped: %s", exc)

    # ── Compute duration + enrich each history row ────────────────────────
    enriched_history: list[EnrichedAgentRunRow] = []
    total_duration_s = 0.0
    completed_count = 0
    for run in run_history:
        spawned = _parse_iso(run.get("spawned_at"))
        completed = _parse_iso(run.get("completed_at")) or _parse_iso(run.get("last_activity_at"))
        duration_s: float | None = None
        duration_str = "—"
        if spawned and completed and completed > spawned:
            duration_s = (completed - spawned).total_seconds()
            duration_str = _fmt_duration(duration_s)
            if run.get("status") == "completed":
                total_duration_s += duration_s
                completed_count += 1

        enriched_history.append(EnrichedAgentRunRow(
            id=run["id"],
            wave_id=run["wave_id"],
            issue_number=run["issue_number"],
            pr_number=run["pr_number"],
            branch=run["branch"],
            worktree_path=run["worktree_path"],
            role=run["role"],
            status=run["status"],
            attempt_number=run["attempt_number"],
            spawn_mode=run["spawn_mode"],
            batch_id=run["batch_id"],
            spawned_at=run["spawned_at"],
            last_activity_at=run["last_activity_at"],
            completed_at=run["completed_at"],
            duration_s=duration_s,
            duration_str=duration_str,
            spawned_fmt=run["spawned_at"][:16].replace("T", " "),
            completed_fmt=run["completed_at"][:16].replace("T", " ") if run["completed_at"] else "—",
            tier=run["tier"],
            org_domain=run["org_domain"],
            parent_run_id=run["parent_run_id"],
        ))

    # ── Group history by batch_id, newest batch first ─────────────────────
    batches: list[BatchRow] = []
    seen_batches: dict[str, BatchRow] = {}
    for run in enriched_history:
        bid = str(run.get("batch_id") or "ungrouped")
        if bid not in seen_batches:
            batch_entry = BatchRow(
                batch_id=bid,
                runs=[],
                spawned_at=run.get("spawned_at"),
                spawned_fmt=run.get("spawned_fmt"),
                # phase_label is derived from the batch_id string (e.g. "eng-20260302T…")
                phase_label=str(bid).split("-")[0] if bid != "ungrouped" else "",
                success_rate=0,
            )
            seen_batches[bid] = batch_entry
            batches.append(batch_entry)
        seen_batches[bid]["runs"].append(run)

    # Add per-batch success rate.
    for batch in batches:
        b_runs = batch["runs"]
        b_total = len(b_runs)
        b_done = sum(1 for r in b_runs if r.get("status") == "completed")
        batch["success_rate"] = round(b_done / b_total * 100) if b_total else 0

    # ── Aggregate KPI stats ───────────────────────────────────────────────
    total = len(enriched_history)
    done_count = sum(1 for r in enriched_history if r.get("status") == "completed")
    failed_count = sum(1 for r in enriched_history if r.get("status") in ("stale", "failed"))
    success_rate = round(done_count / total * 100) if total else 0
    avg_duration_str = _fmt_duration(total_duration_s / completed_count) if completed_count else "—"

    stats = {
        "total": total,
        "active": len(all_agents),
        "done": done_count,
        "failed": failed_count,
        "success_rate": success_rate,
        "avg_duration": avg_duration_str,
    }

    all_roles = sorted({str(r.get("role") or "") for r in enriched_history if r.get("role")})
    all_statuses = sorted({str(r.get("status") or "") for r in enriched_history if r.get("status")})

    return _TEMPLATES.TemplateResponse(
        request,
        "agents.html",
        {
            "agents": agents_enriched,
            "state": state,
            "batches": batches,
            "stats": stats,
            "all_roles": all_roles,
            "all_statuses": all_statuses,
            "run_count": total,
        },
    )


@router.get("/partials/agents", response_class=HTMLResponse)
async def agents_partial(request: Request) -> HTMLResponse:
    """HTMX partial — returns only the live agents grid for polling.

    Called every 15 s by hx-trigger on the agents page inner div.
    Returns a bare HTML fragment (no base layout, no nav) so HTMX can
    swap just the live-agents card grid without destroying Alpine state.
    """
    from agentception.routes.roles import resolve_cognitive_arch

    state = get_state() or PipelineState.empty()
    now_utc = datetime.datetime.now(datetime.UTC)

    all_agents: list[AgentNode] = []
    for agent in state.agents:
        all_agents.append(agent)
        all_agents.extend(agent.children)

    agents_enriched: list[AgentEnrichedRow] = []
    for ag in all_agents:
        persona = resolve_cognitive_arch(ag.cognitive_arch)
        elapsed = ""
        is_stale_idle = False
        spawned_dt = _parse_iso(ag.spawned_at.isoformat() if hasattr(ag, "spawned_at") and ag.spawned_at else None)
        if spawned_dt:
            elapsed = _fmt_duration((now_utc - spawned_dt).total_seconds())
        last_activity_dt = _parse_iso(
            ag.last_activity_at.isoformat()
            if hasattr(ag, "last_activity_at") and ag.last_activity_at
            else None
        )
        if last_activity_dt:
            idle_s = (now_utc - last_activity_dt).total_seconds()
            is_stale_idle = idle_s > 900
        agents_enriched.append(AgentEnrichedRow(
            node=ag,
            persona=persona,
            elapsed=elapsed,
            is_stale_idle=is_stale_idle,
        ))

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/agents_list.html",
        {"agents": agents_enriched},
    )


@router.get("/controls", response_class=HTMLResponse)
async def controls_hub(request: Request) -> HTMLResponse:
    """Controls hub — central page for all pipeline control actions.

    Renders pause/resume, kill-agent, spawn-agent, and trigger-poll actions.

    Context supplied to the template:
    - ``paused``         — bool: is the pipeline currently paused?
    - ``state``          — PipelineState: current in-memory poller snapshot.
    - ``running_agents`` — list of live agent slugs (worktree directory names).
    - ``kill_history``   — last 10 completed/stale agent runs from Postgres.
    """
    from pathlib import Path as _Path
    from agentception.config import settings as _cfg
    from agentception.db.queries import get_agent_run_history

    sentinel = _cfg.ac_dir / ".pipeline-pause"
    paused: bool = sentinel.exists()

    state = get_state() or PipelineState.empty()

    # Collect live agent slugs from the worktrees directory so the kill form
    # can offer a select of currently running agent directories.
    running_agents: list[str] = []
    try:
        wt_dir = _cfg.worktrees_dir
        running_agents = sorted(
            p.name for p in wt_dir.iterdir() if p.is_dir()
        ) if wt_dir.exists() else []
    except OSError:
        logger.warning("⚠️ Could not list worktrees dir for controls hub")

    # Recent kill history from Postgres — status done/stale = terminated runs.
    kill_history: list[AgentRunRow] = []
    try:
        history = await get_agent_run_history(limit=50)
        kill_history = [
            r for r in history
            if r.get("status") in ("completed", "stale", "failed", "cancelled", "stopped")
        ][:10]
    except Exception as exc:
        logger.debug("DB kill history fetch skipped: %s", exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "controls.html",
        {
            "paused": paused,
            "state": state,
            "running_agents": running_agents,
            "kill_history": kill_history,
        },
    )


@router.get("/agents/spawn", response_class=HTMLResponse)
async def spawn_form(request: Request) -> HTMLResponse:
    """Mission Control — orchestration dashboard for spawning agents.

    Renders all three spawn modes (single agent, wave, coordinator) with a
    visual issue board and role card picker. Fetches issues and board counts
    concurrently; falls back gracefully when the DB is unavailable.
    """
    from agentception.db.queries import (
        get_board_issues as _get_board_issues,
        get_board_counts as _get_board_counts,
    )
    from agentception.config import settings as _cfg

    error: str | None = None
    issues: list[BoardIssueRow] = []
    board_counts: dict[str, int] = {"total": 0, "claimed": 0, "unclaimed": 0}

    try:
        issues_raw, counts = await asyncio.gather(
            _get_board_issues(repo=_cfg.gh_repo, include_claimed=True),
            _get_board_counts(repo=_cfg.gh_repo),
        )
        issues = list(issues_raw)
        board_counts = counts
    except Exception as exc:  # pragma: no cover — DB failure path
        error = f"Could not load issues: {exc}"

    state = get_state()
    active_label: str = (state.active_label or "") if state else ""

    # Fetch role descriptions from the taxonomy YAML (best-effort — gracefully
    # degrades to empty strings when the YAML is absent or the taxonomy API
    # fails, which happens in test environments without the scripts/ tree).
    _role_descriptions: dict[str, str] = {}
    try:
        from agentception.routes.roles import get_taxonomy as _get_taxonomy
        _taxonomy = await _get_taxonomy()
        for _level in _taxonomy.levels:
            for _trole in _level.roles:
                _role_descriptions[_trole.slug] = _trole.description
    except Exception:
        pass  # descriptions stay empty; UI renders without them

    # Build role groups in category order for the Jinja role card grid.
    # We produce a list of {category, roles} dicts to preserve the
    # canonical category ordering (_CATEGORY_ORDER) — Jinja's groupby
    # filter sorts alphabetically and would scramble the order.
    _cat_buckets: dict[str, list[dict[str, str]]] = {c: [] for c in _CATEGORY_ORDER}
    for slug in VALID_ROLES:
        cat, pos = _ROLE_CATEGORY_MAP.get(slug, ("Other", 99))
        entry: dict[str, str] = {
            "slug": slug,
            "label": slug.replace("-", " ").title(),
            "category": cat,
            "description": _role_descriptions.get(slug, ""),
        }
        _cat_buckets.setdefault(cat, []).append(entry)
    # Sort roles within each category by their defined position.
    for cat in _cat_buckets:
        _cat_buckets[cat].sort(key=lambda r: _ROLE_CATEGORY_MAP.get(r["slug"], ("", 99))[1])

    role_groups: list[RoleGroupRow] = [
        RoleGroupRow(category=cat, roles=_cat_buckets[cat])
        for cat in _CATEGORY_ORDER
        if _cat_buckets.get(cat)
    ]
    # Flat roles list for the wave-mode <select> (all roles, sorted by category then position).
    roles_flat: list[dict[str, str]] = [
        r
        for g in role_groups
        for r in g["roles"]
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "spawn.html",
        {
            "issues": issues,
            "role_groups": role_groups,
            "roles_flat": roles_flat,
            "active_label": active_label,
            "board_counts": board_counts,
            "error": error,
        },
    )


@router.get("/agents/spawn/issues", response_class=HTMLResponse)
async def spawn_issues_partial(request: Request) -> HTMLResponse:
    """HTMX partial — refreshes the issue board inside the spawn Mission Control.

    Returns just the issue card list so the browser can swap it in without a
    full page reload (hx-target="#spawn-issue-list").
    """
    from agentception.db.queries import get_board_issues as _get_board_issues
    from agentception.config import settings as _cfg

    issues: list[BoardIssueRow] = []
    try:
        issues_raw = await _get_board_issues(repo=_cfg.gh_repo, include_claimed=True)
        issues = list(issues_raw)
    except Exception as exc:  # pragma: no cover
        logger.warning("⚠️ spawn_issues_partial: DB failure: %s", exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "_spawn_issues.html",
        {"issues": issues},
    )


@router.get("/partials/agents/{agent_id}/transcript", response_class=HTMLResponse)
async def agent_transcript_partial(request: Request, agent_id: str) -> Response:
    """HTMX partial — returns only the transcript message list for live polling.

    Called every 8 seconds by ``hx-trigger="every 8s"`` on the transcript
    section in agent.html.  Returns just the message list fragment so HTMX
    can swap it in without a full page reload.
    """
    from agentception.db.queries import get_agent_run_detail
    from agentception.models import AgentStatus as _AgentStatus
    from ._shared import _find_agent

    state = get_state()
    node = _find_agent(state, agent_id)

    db_messages: list[AgentMessageRow] = []
    if node is None:
        try:
            db_run = await get_agent_run_detail(agent_id)
            if db_run:
                db_messages = db_run.get("messages", [])
                raw_status = str(db_run.get("status", "failed")).lower()
                try:
                    synth_status = _AgentStatus(raw_status)
                except ValueError:
                    synth_status = _AgentStatus.FAILED
                node = AgentNode(
                    id=str(db_run.get("id", agent_id)),
                    role=str(db_run.get("role", "unknown")),
                    status=synth_status,
                    issue_number=db_run.get("issue_number"),
                    pr_number=db_run.get("pr_number"),
                    branch=db_run.get("branch"),
                    batch_id=db_run.get("batch_id"),
                    worktree_path=db_run.get("worktree_path")
                )
        except Exception as exc:
            logger.debug("DB agent run lookup skipped for transcript partial: %s", exc)

    messages: list[dict[str, str]] = []

    if not messages and db_messages:
        messages = [
            {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
            for m in db_messages
        ]

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/agent_transcript.html",
        {
            "messages": messages,
            "agent_id": agent_id,
        },
    )


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_id: str) -> Response:
    """Agent profile page — rich view of a single agent run.

    Data sources (in priority order):
    1. In-memory state — live status, branch, issue number from the poller.
    2. Postgres ``ac_agent_runs`` — historical run metadata and status.
    3. Postgres ``ac_agent_messages`` — transcript messages (stored by the agent loop).
    4. GitHub API — issue body, PR checks, PR reviews (fetched in parallel).

    The activity feed is rendered client-side via the SSE stream
    (``/ship/runs/{run_id}/stream``) rather than pre-fetched here.

    Returns HTTP 404 only when the agent is absent from both in-memory state
    and the Postgres history.
    """
    from agentception.db.queries import (
        get_agent_run_detail,
        get_sibling_runs,
    )
    from agentception.routes.roles import resolve_cognitive_arch
    from ._shared import _find_agent, _fmt_duration, _parse_iso

    state = get_state()
    node = _find_agent(state, agent_id)
    # True only when the agent is live in the poller (worktree exists, killable).
    is_live_agent: bool = node is not None

    # DB run detail — enrichment regardless of live state.
    db_run: AgentRunDetail | None = None
    db_messages: list[AgentMessageRow] = []
    try:
        db_run = await get_agent_run_detail(agent_id)
        if db_run:
            db_messages = db_run.get("messages", [])
    except Exception as exc:
        logger.debug("DB agent run lookup skipped: %s", exc)

    if node is None and db_run is None:
        return _TEMPLATES.TemplateResponse(
            request,
            "agent.html",
            {"node": None, "agent_id": agent_id, "messages": [], "db_run": None,
             "persona": resolve_cognitive_arch(None)},
            status_code=404,
        )

    # Synthesise AgentNode from DB when the live poller no longer holds it.
    if node is None and db_run is not None:
        from agentception.models import AgentStatus as _AgentStatus
        raw_status = str(db_run.get("status", "failed")).lower()
        # If the agent is not live in the poller it cannot still be running.
        # Map any legacy "unknown" value to "failed" for backward compatibility.
        if raw_status == "unknown":
            raw_status = "failed"
        elif raw_status == "done":
            raw_status = "completed"
        try:
            synth_status = _AgentStatus(raw_status)
        except ValueError:
            synth_status = _AgentStatus.FAILED
        node = AgentNode(
            id=str(db_run.get("id", agent_id)),
            role=str(db_run.get("role", "unknown")),
            status=synth_status,
            issue_number=db_run.get("issue_number"),
            pr_number=db_run.get("pr_number"),
            branch=db_run.get("branch"),
            batch_id=db_run.get("batch_id"),
            cognitive_arch=db_run.get("cognitive_arch"),
            tier=db_run.get("tier"),
            org_domain=db_run.get("org_domain"),
            parent_run_id=db_run.get("parent_run_id"),
            worktree_path=db_run.get("worktree_path"),
        )

    # Messages come from DB; filesystem transcript reading is removed.
    messages: list[dict[str, str]] = []
    if not messages and db_messages:
        messages = [
            {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
            for m in db_messages
        ]

    # ── Parallel enrichment fetches ─────────────────────────────────────────
    # All helpers return [] / {} / None on error so the page never fails.
    issue_number: int | None = node.issue_number if node else None
    pr_number: int | None = node.pr_number if node else None
    batch_id: str | None = node.batch_id if node else None

    async def _safe_get_pr_checks(n: int) -> list[dict[str, JsonValue]]:
        try:
            from agentception.readers.github import get_pr_checks as _get_pr_checks
            return await _get_pr_checks(n)
        except Exception:
            return []

    async def _safe_get_pr_reviews(n: int) -> list[dict[str, JsonValue]]:
        try:
            from agentception.readers.github import get_pr_reviews as _get_pr_reviews
            return await _get_pr_reviews(n)
        except Exception:
            return []

    async def _safe_get_siblings(bid: str) -> list[SiblingRunRow]:
        try:
            return await get_sibling_runs(bid, agent_id)
        except Exception:
            return []

    async def _noop_none() -> IssueDetailRow | None:
        return None

    async def _noop_list_dict() -> list[dict[str, JsonValue]]:
        return []

    async def _noop_list_sibling() -> list[SiblingRunRow]:
        return []

    async def _safe_get_parent_run(run_id: str) -> AgentRunDetail | None:
        """Fetch the parent run from DB to extract its own parent_run_id (grandparent)."""
        try:
            return await get_agent_run_detail(run_id)
        except Exception:
            return None

    issue_detail: IssueDetailRow | None
    pr_checks: list[dict[str, JsonValue]]
    pr_reviews: list[dict[str, JsonValue]]
    siblings: list[SiblingRunRow]

    parent_run_id: str | None = node.parent_run_id if node else None

    async def _safe_get_issue_from_db(n: int) -> IssueDetailRow | None:
        try:
            from agentception.config import settings as _settings
            return await get_issue_detail(_settings.gh_repo, n)
        except Exception:
            return None

    (
        issue_detail,
        pr_checks,
        pr_reviews,
        siblings,
    ) = await asyncio.gather(
        _safe_get_issue_from_db(issue_number) if issue_number else _noop_none(),
        _safe_get_pr_checks(pr_number) if pr_number else _noop_list_dict(),
        _safe_get_pr_reviews(pr_number) if pr_number else _noop_list_dict(),
        _safe_get_siblings(batch_id) if batch_id else _noop_list_sibling(),
    )

    # Fetch parent run separately (to avoid the 4-coroutine overload limit in mypy)
    # and extract the grandparent run id for the org chart.
    grandparent_run_id: str | None = None
    if parent_run_id:
        parent_run = await _safe_get_parent_run(parent_run_id)
        if parent_run and isinstance(parent_run, dict):
            gp = parent_run.get("parent_run_id")
            grandparent_run_id = str(gp) if gp else None

    # ── Duration / date strings ─────────────────────────────────────────────
    # Only use completed_at for duration — last_activity_at is updated by the
    # poller and can be arbitrarily recent even for long-dead agents, which
    # would produce a wildly inflated "ran Xh Ym" display.
    spawned_at_iso: str | None = db_run.get("spawned_at") if db_run else None
    finished_iso: str | None = db_run.get("completed_at") if db_run else None
    duration_str = ""
    spawned_date_str = ""
    if spawned_at_iso:
        spawned_dt = _parse_iso(spawned_at_iso)
        if spawned_dt:
            spawned_date_str = spawned_dt.strftime("%b %-d, %Y")
        finished_dt = _parse_iso(finished_iso) if finished_iso else None
        if spawned_dt and finished_dt:
            delta = (finished_dt - spawned_dt).total_seconds()
            duration_str = _fmt_duration(max(0.0, delta))

    persona = resolve_cognitive_arch(node.cognitive_arch if node else None)

    return _TEMPLATES.TemplateResponse(
        request,
        "agent.html",
        {
            "node": node,
            "agent_id": agent_id,
            "messages": messages,
            "db_run": db_run,
            "persona": persona,
            "issue_detail": issue_detail,
            "pr_checks": pr_checks,
            "pr_reviews": pr_reviews,
            "siblings": siblings,
            "grandparent_run_id": grandparent_run_id,
            "is_live_agent": is_live_agent,
            "duration_str": duration_str,
            "spawned_date_str": spawned_date_str,
        },
    )
