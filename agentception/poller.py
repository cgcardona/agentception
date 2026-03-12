from __future__ import annotations

"""AgentCeption background poller — pipeline state aggregation and SSE broadcast.

This module owns the single shared ``PipelineState`` that the dashboard
displays.  A background task calls ``polling_loop()`` on startup; it wakes
every ``poll_interval_seconds``, calls ``tick()``, and broadcasts the new
state to every connected SSE client via a per-client ``asyncio.Queue``.

Public surface used by API routes:
- ``subscribe()`` / ``unsubscribe()``  — SSE client lifecycle
- ``get_state()``                      — synchronous snapshot for HTTP /state

Public surface used by ``app.py`` lifespan:
- ``polling_loop()``  — the long-running background coroutine
"""

import asyncio
import dataclasses
import logging
import time
from pathlib import Path

from agentception.config import settings
from agentception.intelligence.guards import detect_out_of_order_prs, detect_stale_claims
from agentception.db.queries import RunContextRow, list_active_runs
from agentception.models import AgentNode, AgentStatus, BoardIssue, PipelineState, PlanDraftEvent, StaleClaim, StalledAgentEvent
from agentception.readers.github import (
    get_active_label,
    get_closed_issues,
    get_merged_prs_full,
    get_open_issues,
    get_open_prs,
    get_wip_issues,
)
from agentception.readers.git import worktree_last_commit_time

logger = logging.getLogger(__name__)

# Default fallback used when PipelineConfig cannot be read.  The live value
# comes from PipelineConfig.stall_threshold_minutes at tick time.
_DEFAULT_STALL_THRESHOLD_SECONDS: int = 30 * 60

# ---------------------------------------------------------------------------
# Shared state — module-level singletons, mutated only by tick()
# ---------------------------------------------------------------------------

_state: PipelineState | None = None
_subscribers: list[asyncio.Queue[PipelineState]] = []

# In-memory deduplication for auto phase-advance transitions.
# Each entry is (initiative, from_phase, to_phase) and is added only after a
# successful plan_advance_phase call.  This prevents repeated GitHub API calls
# on every tick for transitions that have already been applied.
_auto_advanced: set[tuple[str, str, str]] = set()


# ---------------------------------------------------------------------------
# GitHub board aggregation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GitHubBoard:
    """Aggregated GitHub data for a single polling tick.

    Fetched in parallel so one slow GitHub API call doesn't block the others.
    All fields come directly from the readers; the poller merges them with
    filesystem data to produce ``PipelineState``.
    """

    active_label: str | None
    open_issues: list[dict[str, object]]
    open_prs: list[dict[str, object]]
    wip_issues: list[dict[str, object]]
    closed_issues: list[dict[str, object]] = dataclasses.field(default_factory=list)
    merged_prs: list[dict[str, object]] = dataclasses.field(default_factory=list)


async def build_github_board() -> GitHubBoard:
    """Fetch all required GitHub data in parallel and return a ``GitHubBoard``.

    Using ``asyncio.gather`` keeps the wall-clock cost equal to the slowest
    individual request rather than the sum of all requests.  Closed issues and
    merged PRs are fetched with a limit cap so each tick stays bounded.
    """
    (
        active_label,
        open_issues,
        open_prs,
        wip_issues,
        closed_issues,
        merged_prs,
    ) = await asyncio.gather(
        get_active_label(),
        get_open_issues(),
        get_open_prs(),
        get_wip_issues(),
        get_closed_issues(limit=1000),
        get_merged_prs_full(limit=100),
    )
    return GitHubBoard(
        active_label=active_label,
        open_issues=open_issues,
        open_prs=open_prs,
        wip_issues=wip_issues,
        closed_issues=closed_issues,
        merged_prs=merged_prs,
    )


# ---------------------------------------------------------------------------
# Agent merging — correlate filesystem worktrees with GitHub signals
# ---------------------------------------------------------------------------


async def merge_agents(
    active_runs: list[RunContextRow],
    github: GitHubBoard,
) -> list[AgentNode]:
    """Build an ``AgentNode`` list by correlating DB run rows with GitHub.

    Status derivation rules (applied in priority order):
    1. Run branch matches an open PR ``headRefName`` → REVIEWING
    2. Run has an issue_number → IMPLEMENTING
    3. Run is a coordinator (tier == coordinator, no issue/PR) → IMPLEMENTING
    4. Otherwise → UNKNOWN
    """
    pr_branch_to_number: dict[str, int] = {}
    for pr in github.open_prs:
        head = pr.get("headRefName")
        number = pr.get("number")
        if isinstance(head, str) and isinstance(number, int):
            pr_branch_to_number[head] = number

    nodes: list[AgentNode] = []
    for run in active_runs:
        branch = run["branch"] or ""
        gh_pr_number: int | None = pr_branch_to_number.get(branch) if branch else None
        if branch and gh_pr_number is not None:
            status = AgentStatus.REVIEWING
        elif run["issue_number"] is not None:
            status = AgentStatus.IMPLEMENTING
        elif run["tier"] == "coordinator":
            # Coordinator agents may have no issue/PR during planning.
            status = AgentStatus.IMPLEMENTING
        else:
            # Ad-hoc runs (issue_number is None, not a coordinator) are
            # managed by their asyncio task lifecycle, not by GitHub signals.
            # Treat them as IMPLEMENTING so the poller never stamps FAILED
            # onto a live adhoc run that simply has no associated issue.
            status = AgentStatus.IMPLEMENTING

        worktree = run["worktree_path"]
        node_id = (
            Path(worktree).name if worktree else None
        ) or (f"issue-{run['issue_number']}" if run["issue_number"] else None) or run["run_id"]
        resolved_pr_number = gh_pr_number if gh_pr_number is not None else run["pr_number"]
        nodes.append(
            AgentNode(
                id=node_id,
                role=run["role"] or "unknown",
                status=status,
                issue_number=run["issue_number"],
                pr_number=resolved_pr_number,
                branch=run["branch"],
                batch_id=run["batch_id"],
                worktree_path=worktree,
                cognitive_arch=run["cognitive_arch"],
                tier=run["tier"],
                org_domain=run["org_domain"],
                parent_run_id=run["parent_run_id"],
            )
        )

    return nodes


# ---------------------------------------------------------------------------
# Alert detection
# ---------------------------------------------------------------------------


async def detect_alerts(
    active_runs: list[RunContextRow],
    github: GitHubBoard,
    stall_threshold_seconds: int = _DEFAULT_STALL_THRESHOLD_SECONDS,
) -> tuple[list[str], list[StaleClaim], list[StalledAgentEvent]]:
    """Detect pipeline problems and return alerts, stale claims, and stalled-agent events.

    Three alert classes:
    1. **Stale claim** — an ``agent/wip`` issue has no live worktree.
    2. **Out-of-order PR** — an open PR's labels include an agentception phase
       that no longer matches the currently active phase.
    3. **Stalled agent** — two-signal detection:
       - *Primary (DB heartbeat):* ``last_activity_at`` is older than
         ``stall_threshold_seconds``.  Promotes the run to ``AgentStatus.STALLED``
         and emits a ``StalledAgentEvent``.
       - *Secondary (git commit):* ``worktree_last_commit_time()`` is older than
         the threshold while ``last_activity_at`` is still fresh.  Surfaces an
         advisory warning only — no STALLED promotion.

    Returns ``(alerts, stale_claims, stalled_agents)``.
    """
    alerts: list[str] = []
    stalled_agents: list[StalledAgentEvent] = []
    now = time.time()

    # ── Alert 1: agent/wip issue with no matching worktree ─────────────────
    # Self-heal: automatically clear the agent/wip label when there is no live
    # worktree for the issue.  The worktree is the source of truth — if it is
    # gone, the claim is orphaned and can be safely released so the issue
    # becomes available for re-spawn.
    from agentception.readers.github import clear_wip_label
    stale_claims = await detect_stale_claims(github.wip_issues, settings.worktrees_dir)
    for claim in stale_claims:
        # Always surface the stale claim in alerts so the UI shows it regardless
        # of whether auto-heal succeeds.  The alert text is the same in both
        # cases; auto-heal is best-effort and silent on success.
        alerts.append(f"Stale claim on #{claim.issue_number}")
        try:
            await clear_wip_label(claim.issue_number)
            logger.info("✅ Auto-healed stale claim: removed agent/wip from #%d", claim.issue_number)
        except Exception as exc:
            logger.warning("⚠️  Auto-heal failed for #%d: %s", claim.issue_number, exc)

    # ── Alert 2: open PR labelled with a non-active agentception phase ──────
    active = github.active_label
    for pr in github.open_prs:
        pr_labels = pr.get("labels", [])
        if not isinstance(pr_labels, list):
            continue
        for lbl in pr_labels:
            if not isinstance(lbl, dict):
                continue
            label_name = lbl.get("name", "")
            if (
                isinstance(label_name, str)
                and label_name.startswith("agentception/")
                and label_name != active
            ):
                pr_num = pr.get("number")
                if isinstance(pr_num, int):
                    alerts.append(f"Out-of-order PR #{pr_num}")
                break  # one alert per PR is enough

    # ── Alert 3: two-signal stall detection ────────────────────────────────
    # Skip coordinator runs — they have no commits of their own.
    # Skip freshly-spawned runs — a brand-new worktree has no activity yet.
    import datetime as _dt
    for run in active_runs:
        worktree = run["worktree_path"]
        if worktree is None:
            continue
        if run["tier"] == "coordinator":
            continue
        path = Path(worktree)
        if not path.exists():
            continue

        # Skip if spawned within the threshold window.
        spawned_at_str = run["spawned_at"]
        try:
            spawned_ts = _dt.datetime.fromisoformat(spawned_at_str).timestamp()
            if (now - spawned_ts) < stall_threshold_seconds:
                continue
        except (ValueError, OSError):
            pass

        issue_num: int = run["issue_number"] or 0
        label = f"issue #{issue_num}" if issue_num else path.name
        run_id = run["run_id"]

        # ── Primary signal: DB heartbeat (last_activity_at) ────────────────
        # Agents update last_activity_at on every LLM turn; silence here means
        # the LLM loop itself has stalled — promote to STALLED immediately.
        last_activity_raw = run.get("last_activity_at")
        heartbeat_cold = False
        last_activity_iso = ""
        stalled_for_minutes = 0

        if last_activity_raw is not None:
            try:
                last_activity_ts = _dt.datetime.fromisoformat(str(last_activity_raw)).timestamp()
                silence_seconds = now - last_activity_ts
                if silence_seconds > stall_threshold_seconds:
                    heartbeat_cold = True
                    stalled_for_minutes = int(silence_seconds / 60)
                    last_activity_iso = str(last_activity_raw)
            except (ValueError, OSError):
                pass
        else:
            # No heartbeat recorded yet; agent has been running past threshold —
            # treat as cold heartbeat (agent may have crashed before first turn).
            heartbeat_cold = True
            last_activity_iso = ""
            # spawned_ts was already parsed above; reparse defensively.
            try:
                spawned_ts_fallback = _dt.datetime.fromisoformat(spawned_at_str).timestamp()
                stalled_for_minutes = int((now - spawned_ts_fallback) / 60)
            except (ValueError, OSError):
                stalled_for_minutes = 0

        if heartbeat_cold:
            alerts.append(f"Possible stuck agent on {label}")

            from agentception.db.persist import update_agent_status  # noqa: PLC0415
            from agentception.workflow.status import AgentStatus  # noqa: PLC0415
            await update_agent_status(run_id, AgentStatus.STALLED)

            stale_claims.append(StaleClaim(
                issue_number=issue_num,
                issue_title=label,
                worktree_path=str(path),
            ))

            stalled_agents.append(StalledAgentEvent(
                run_id=run_id,
                issue_number=issue_num,
                worktree_path=str(path),
                last_activity_at=last_activity_iso,
                stalled_for_minutes=stalled_for_minutes,
            ))

            logger.warning(
                "⚠️  agent_stalled — run_id=%s issue=%s stalled_for=%dm (DB heartbeat cold)",
                run_id,
                label,
                stalled_for_minutes,
            )
            continue  # Primary signal fired — skip secondary check for this run.

        # ── Secondary signal: git commit age (advisory only) ───────────────
        # Agent is turning (heartbeat warm) but hasn't committed in a while.
        # Surface a soft warning; do NOT promote to STALLED.
        last_commit = await worktree_last_commit_time(path)
        if last_commit > 0.0 and (now - last_commit) > stall_threshold_seconds:
            advisory_minutes = int((now - last_commit) / 60)
            logger.warning(
                "⚠️  agent_active_no_commit — run_id=%s issue=%s no_commit_for=%dm (heartbeat warm)",
                run_id,
                label,
                advisory_minutes,
            )

    # ── Alert 4: structured out-of-order PR violations (linked-issue check) ─
    # Complements Alert 2: while Alert 2 checks the PR's own labels, this
    # check inspects the issue the PR closes — more precise for the common
    # case where the PR body contains a 'Closes #N' reference.
    try:
        violations = await detect_out_of_order_prs()
        for v in violations:
            alerts.append(
                f"Out-of-order PR #{v.pr_number} — "
                f"expected {v.expected_label}, got {v.actual_label}"
            )
    except Exception as exc:
        logger.warning("⚠️  detect_out_of_order_prs failed: %s", exc)

    return alerts, stale_claims, stalled_agents


# ---------------------------------------------------------------------------
# Pub/sub — SSE client registry
# ---------------------------------------------------------------------------


def subscribe() -> asyncio.Queue[PipelineState]:
    """Register a new SSE client and return its dedicated queue.

    The caller owns the queue for the duration of the connection.  Call
    ``unsubscribe()`` in a ``finally`` block to prevent queue accumulation.
    """
    q: asyncio.Queue[PipelineState] = asyncio.Queue()
    _subscribers.append(q)
    logger.debug("✅ SSE subscriber added (total=%d)", len(_subscribers))
    return q


def unsubscribe(q: asyncio.Queue[PipelineState]) -> None:
    """Remove a client queue after disconnect.

    Idempotent — calling this for a queue that was already removed is safe.
    """
    try:
        _subscribers.remove(q)
        logger.debug("✅ SSE subscriber removed (total=%d)", len(_subscribers))
    except ValueError:
        pass  # Already removed — benign race on disconnect.


def get_state() -> PipelineState | None:
    """Return the most recently computed ``PipelineState`` (synchronous).

    Returns ``None`` before the first tick completes.  API routes should
    return a 503 or an empty state object when this is ``None``.
    """
    return _state


async def broadcast(state: PipelineState) -> None:
    """Push the new state to every connected SSE subscriber.

    Iterates over a snapshot of the list so that a concurrent ``unsubscribe``
    during iteration does not raise ``RuntimeError``.
    """
    for q in list(_subscribers):
        await q.put(state)
    logger.debug("📡 Broadcast to %d subscriber(s)", len(_subscribers))


# ---------------------------------------------------------------------------
# Core polling functions
# ---------------------------------------------------------------------------




async def _build_board_issues(
    active_label: str | None,
    gh_repo: str,
) -> list[BoardIssue]:
    """Query ``issues`` for unclaimed issues in the active phase.

    Called after ``persist_tick`` so the DB already has the freshest data.
    Returns ``[]`` on any error — poller continues without board data.
    """
    try:
        from agentception.db.queries import get_board_issues
        rows = await get_board_issues(
            repo=gh_repo,
            label=active_label,
            include_claimed=False,
        )
        return [
            BoardIssue(
                number=int(r["number"]),
                title=str(r["title"]),
                state=str(r.get("state", "open")),
                labels=[lbl["name"] for lbl in r.get("labels", []) if isinstance(lbl, dict)],
                claimed=bool(r.get("claimed", False)),
                phase_label=r.get("phase_label") if isinstance(r.get("phase_label"), str) else None,
                last_synced_at=r.get("last_synced_at") if isinstance(r.get("last_synced_at"), str) else None,
            )
            for r in rows
        ]
    except Exception as exc:
        logger.warning("⚠️  Board issues query failed (non-fatal): %s", exc)
        return []


async def _auto_advance_phases(repo: str) -> None:
    """Automatically remove the ``pipeline/gated`` label and add ``pipeline/active`` when a phase gate opens.

    Called on every tick after ``persist_tick`` and ``reseed_missing_initiative_phases``.
    Reads the DB-computed phase completion state, finds any phases whose
    dependencies are now all closed, and calls ``plan_advance_phase`` for each
    newly unlocked transition.

    Uses ``_auto_advanced`` (a module-level set of
    ``(initiative, from_phase, to_phase)`` tuples) to deduplicate — a
    transition that has already succeeded is never retried within the same
    process run.  Failures are logged at WARNING level and retried on the
    next tick.
    """
    from agentception.db.queries import get_initiatives, get_issues_grouped_by_phase
    from agentception.mcp.plan_advance_phase import plan_advance_phase

    try:
        initiatives = await get_initiatives(repo)
    except Exception as exc:
        logger.debug("⚠️  _auto_advance_phases: get_initiatives failed: %s", exc)
        return

    for initiative in initiatives:
        try:
            phases = await get_issues_grouped_by_phase(repo, initiative)
        except Exception as exc:
            logger.debug(
                "⚠️  _auto_advance_phases: get_issues_grouped_by_phase(%s) failed: %s",
                initiative,
                exc,
            )
            continue

        complete_set: set[str] = {p["label"] for p in phases if p["complete"]}

        for phase in phases:
            # Only consider phases that have dependencies and are not yet done.
            if phase["complete"] or not phase["depends_on"]:
                continue

            # Check whether all dependencies are now complete in the DB.
            if not all(dep in complete_set for dep in phase["depends_on"]):
                continue

            # All deps complete — fire plan_advance_phase for each dep→phase pair.
            for dep_label in phase["depends_on"]:
                key = (initiative, dep_label, phase["label"])
                if key in _auto_advanced:
                    continue  # already applied this tick-cycle
                try:
                    result = await plan_advance_phase(initiative, dep_label, phase["label"])
                    if result.get("advanced") is True:
                        _auto_advanced.add(key)
                        logger.info(
                            "✅ auto-advance: %s %r → %r (%s issue(s) unlocked)",
                            initiative,
                            dep_label,
                            phase["label"],
                            result.get("unlocked_count", 0),
                        )
                    else:
                        # from_phase not yet fully closed on GitHub (DB ahead of GH).
                        logger.debug(
                            "⚠️  auto-advance: %s %r → %r gate not yet open on GitHub: %s",
                            initiative,
                            dep_label,
                            phase["label"],
                            result.get("open_issues", []),
                        )
                except Exception as exc:
                    logger.warning(
                        "⚠️  auto-advance: %s %r → %r failed: %s",
                        initiative,
                        dep_label,
                        phase["label"],
                        exc,
                    )


async def _auto_unblock_deps(repo: str) -> None:
    """Remove ``blocked/deps`` from issues whose all ticket-level deps have closed.

    Called on every tick after ``_auto_advance_phases``.  For each open issue
    that still carries the ``blocked/deps`` label, checks whether every issue
    in its ``depends_on_json`` list is now ``closed`` in the DB.  If so,
    removes the label so the engineering coordinator will pick it up on the
    next dispatch.

    DB is the source of truth here — the poller has already written the latest
    GitHub state into ``ACIssue.state`` before this function runs.
    """
    from agentception.db.queries import get_blocked_deps_open_issues, get_closed_issue_numbers
    from agentception.readers.github import remove_label_from_issue

    try:
        candidates = await get_blocked_deps_open_issues(repo)
        if not candidates:
            return
        closed = await get_closed_issue_numbers(repo)
        for row in candidates:
            if all(dep in closed for dep in row["dep_numbers"]):
                try:
                    await remove_label_from_issue(row["github_number"], "blocked/deps")
                    logger.info(
                        "✅ _auto_unblock_deps: #%d all deps closed — removed blocked/deps",
                        row["github_number"],
                    )
                except Exception as exc:
                    logger.warning(
                        "⚠️  _auto_unblock_deps: could not remove label from #%d: %s",
                        row["github_number"],
                        exc,
                    )
    except Exception as exc:
        logger.warning("⚠️  _auto_unblock_deps: failed: %s", exc)


async def _stamp_missing_blocked_deps(repo: str) -> None:
    """Re-apply ``blocked/deps`` to issues whose deps are recorded but the label is absent.

    This is the server-side safety net for a silent failure mode in
    ``file_issues``: if ``add_label_to_issue`` threw a ``RuntimeError`` that was
    caught by the (now-fixed) shared try/except, the body was edited but the
    label was never applied.  On the next poller tick this function detects the
    gap — ``depends_on_json`` non-empty but ``blocked/deps`` missing — and
    re-stamps the label provided at least one dep is still open.

    Called on every tick immediately before ``_auto_unblock_deps`` so the
    unblock pass always operates on a correct label set.
    """
    from agentception.db.queries import get_closed_issue_numbers, get_issues_missing_blocked_deps
    from agentception.readers.github import add_label_to_issue

    try:
        candidates = await get_issues_missing_blocked_deps(repo)
        if not candidates:
            return
        closed = await get_closed_issue_numbers(repo)
        for row in candidates:
            if all(dep in closed for dep in row["dep_numbers"]):
                continue  # All deps already closed — no need to block
            try:
                await add_label_to_issue(row["github_number"], "blocked/deps")
                logger.info(
                    "✅ _stamp_missing_blocked_deps: re-stamped blocked/deps on #%d (deps: %s)",
                    row["github_number"],
                    row["dep_numbers"],
                )
            except Exception as exc:
                logger.warning(
                    "⚠️  _stamp_missing_blocked_deps: could not stamp #%d: %s",
                    row["github_number"],
                    exc,
                )
    except Exception as exc:
        logger.warning("⚠️  _stamp_missing_blocked_deps: failed: %s", exc)


async def tick() -> PipelineState:
    """Execute a single polling cycle: collect → merge → detect → persist → enrich → broadcast.

    Pipeline:
    1. Read filesystem (worktrees) + GitHub (issues, PRs, WIP labels) in parallel.
    2. Merge into AgentNode tree.
    3. Detect stale claims / stuck agents / out-of-order PRs.
    4. Persist raw data to Postgres via ``persist_tick``.
    5. Read board_issues back from Postgres (freshest data, owned by us).
    6. Build final ``PipelineState`` with board_issues embedded.
    7. Broadcast to all SSE subscribers.

    Steps 4-5 decouple the write path (GitHub → Postgres) from the read
    path (Postgres → SSE stream), so the UI never reads directly from GitHub.
    """
    global _state

    # Reload active project from pipeline-config.json so a project switch
    # via the GUI takes effect within one polling interval — no restart needed.
    settings.reload()

    active_runs = await list_active_runs()
    github = await build_github_board()
    agents = await merge_agents(active_runs, github)
    plan_draft_events: list[PlanDraftEvent] = []

    # ── Read PipelineConfig once — drives loop guard and stall thresholds ──
    loop_guard_triggered: list[str] = []
    stall_threshold_seconds = _DEFAULT_STALL_THRESHOLD_SECONDS
    try:
        from agentception.readers.pipeline_config import read_pipeline_config
        config = await read_pipeline_config()
        max_attempts = config.max_attempts
        stall_threshold_seconds = config.stall_threshold_minutes * 60
        for run in active_runs:
            msg_count_raw = run.get("message_count", 0)
            msg_count = int(msg_count_raw) if isinstance(msg_count_raw, (int, float)) else 0
            if msg_count > max_attempts:
                run_id = run["run_id"]
                issue_num = run.get("issue_number")
                label = f"issue #{issue_num}" if issue_num else run_id
                loop_guard_triggered.append(label)
                logger.warning(
                    "⚠️  agent_loop_guard_triggered — run_id=%s issue=%s message_count=%d max_attempts=%d",
                    run_id,
                    label,
                    msg_count,
                    max_attempts,
                )
    except Exception as exc:
        logger.warning("⚠️  Loop guard / config read failed (non-fatal): %s", exc)

    # ── Detect stale claims / stalled agents / out-of-order PRs ────────────
    alerts, stale_claims, stalled_agents = await detect_alerts(
        active_runs, github, stall_threshold_seconds
    )

    # ── Persist raw tick data to Postgres ────────────────────────────────────
    # Non-blocking: a DB outage cannot crash the poller or stall the SSE stream.
    try:
        from agentception.db.persist import persist_tick, reseed_missing_initiative_phases
        await persist_tick(
            state=PipelineState(
                active_label=github.active_label,
                issues_open=len(github.open_issues),
                prs_open=len(github.open_prs),
                agents=agents,
                alerts=alerts,
                stale_claims=stale_claims,
                board_issues=[],
                polled_at=time.time(),
                plan_draft_events=plan_draft_events,
                loop_guard_triggered=loop_guard_triggered,
                stalled_agents=stalled_agents,
            ),
            open_issues=github.open_issues,
            open_prs=github.open_prs,
            closed_issues=github.closed_issues,
            merged_prs=github.merged_prs,
            gh_repo=settings.gh_repo,
        )
        # Re-seed initiative_phases from issue labels if the table is empty
        # (e.g. after a DB reset).  Idempotent: skips initiatives that already
        # have stored phase metadata.  Initiative slugs are derived from the
        # scoped labels on the issues themselves — no config file needed.
        await reseed_missing_initiative_phases(settings.gh_repo)
        # Auto-unblock next-phase issues whenever a phase gate closes.
        await _auto_advance_phases(settings.gh_repo)
        # Re-stamp blocked/deps on issues whose label was lost (e.g. silent
        # API failure during file_issues).  Must run before _auto_unblock_deps so
        # the unblock pass always sees a correct label set.
        await _stamp_missing_blocked_deps(settings.gh_repo)
        # Auto-remove blocked/deps label when all ticket-level deps have closed.
        await _auto_unblock_deps(settings.gh_repo)
    except Exception as exc:
        logger.warning("⚠️  DB persist skipped (non-fatal): %s", exc)

    # ── Read board_issues back from Postgres (Postgres is the source of truth) ─
    board_issues = await _build_board_issues(github.active_label, settings.gh_repo)

    # ── SSE expansion: closed/merged counts and stale branch detection ────────
    closed_issues_count = 0
    merged_prs_count = 0
    stale_branches: list[str] = []
    try:
        from agentception.db.queries import get_closed_issues_count, get_merged_prs_count
        from agentception.readers.git import list_git_branches, list_git_worktrees

        closed_issues_count, merged_prs_count = await asyncio.gather(
            get_closed_issues_count(settings.gh_repo),
            get_merged_prs_count(settings.gh_repo),
        )

        # Stale branches: feat/issue-N local branches with no live worktree path.
        live_branches: set[str] = {
            str(wt.get("branch", ""))
            for wt in await list_git_worktrees()
            if wt.get("branch")
        }
        for branch in await list_git_branches():
            name = str(branch.get("name", ""))
            if branch.get("is_agent_branch") and name not in live_branches:
                stale_branches.append(name)
    except Exception as exc:
        logger.debug("⚠️  SSE expansion data fetch skipped: %s", exc)

    state = PipelineState(
        active_label=github.active_label,
        issues_open=len(github.open_issues),
        prs_open=len(github.open_prs),
        agents=agents,
        alerts=alerts,
        stale_claims=stale_claims,
        board_issues=board_issues,
        polled_at=time.time(),
        closed_issues_count=closed_issues_count,
        merged_prs_count=merged_prs_count,
        stale_branches=stale_branches,
        plan_draft_events=plan_draft_events,
        loop_guard_triggered=loop_guard_triggered,
        stalled_agents=stalled_agents,
    )
    _state = state

    await broadcast(state)
    return state


async def polling_loop() -> None:
    """Run the tick/broadcast cycle on a fixed interval until cancelled.

    Designed to be launched as an ``asyncio.Task`` from the FastAPI lifespan.
    Errors inside a single tick are logged and swallowed so one bad GitHub
    response cannot kill the entire dashboard.

    Rate-limit backoff: when GitHub returns a rate-limit error we back off
    exponentially (60 s → 120 s → 240 s, capped at 300 s) rather than
    hammering the API on every loop iteration.
    """
    _RATE_LIMIT_BACKOFF_INITIAL: int = 60
    _RATE_LIMIT_BACKOFF_MAX: int = 300
    _rate_limit_backoff: int = 0  # 0 = not in backoff

    logger.info(
        "✅ AgentCeption polling loop started (interval=%ds)",
        settings.poll_interval_seconds,
    )
    while True:
        try:
            await tick()
            _rate_limit_backoff = 0  # reset on success
            await asyncio.sleep(settings.poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("✅ Polling loop stopped cleanly")
            return
        except Exception as exc:
            exc_str = str(exc).lower()
            if "rate limit" in exc_str or "rate_limit" in exc_str:
                if _rate_limit_backoff == 0:
                    _rate_limit_backoff = _RATE_LIMIT_BACKOFF_INITIAL
                else:
                    _rate_limit_backoff = min(
                        _rate_limit_backoff * 2, _RATE_LIMIT_BACKOFF_MAX
                    )
                logger.warning(
                    "⚠️  GitHub rate limit hit — backing off %ds before retry",
                    _rate_limit_backoff,
                )
                await asyncio.sleep(_rate_limit_backoff)
            else:
                logger.warning("⚠️  Polling loop error: %s", exc)
                await asyncio.sleep(settings.poll_interval_seconds)
