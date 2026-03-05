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
from agentception.models import AgentNode, AgentStatus, BoardIssue, PipelineState, StaleClaim, TaskFile
from agentception.readers.github import (
    get_active_label,
    get_closed_issues,
    get_merged_prs_full,
    get_open_issues,
    get_open_prs,
    get_wip_issues,
)
from agentception.readers.worktrees import list_active_worktrees, worktree_last_commit_time

logger = logging.getLogger(__name__)

# Agents whose most-recent commit is older than this threshold are flagged.
_STUCK_THRESHOLD_SECONDS: int = 30 * 60  # 30 minutes

# ---------------------------------------------------------------------------
# Shared state — module-level singletons, mutated only by tick()
# ---------------------------------------------------------------------------

_state: PipelineState | None = None
_subscribers: list[asyncio.Queue[PipelineState]] = []


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
        get_closed_issues(limit=100),
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
    worktrees: list[TaskFile],
    github: GitHubBoard,
) -> list[AgentNode]:
    """Build an ``AgentNode`` list by correlating worktree task files with GitHub.

    Status derivation rules (applied in priority order):
    1. Worktree branch matches an open PR ``headRefName`` → REVIEWING
    2. Worktree issue number appears in ``agent:wip`` issues → IMPLEMENTING
    3. ``WORKFLOW=bugs-to-issues`` (coordinator/brain-dump) → IMPLEMENTING
       These agents have no issue number or PR; they are actively planning.
    4. Otherwise → UNKNOWN
    """
    # Index open PRs by branch name for O(1) lookup.
    pr_branches: set[str] = {
        str(pr["headRefName"])
        for pr in github.open_prs
        if isinstance(pr.get("headRefName"), str)
    }

    # Index WIP issue numbers for O(1) lookup.
    wip_issue_numbers: set[int] = set()
    for issue in github.wip_issues:
        num = issue.get("number")
        if isinstance(num, int):
            wip_issue_numbers.add(num)

    nodes: list[AgentNode] = []
    for tf in worktrees:
        branch = tf.branch or ""
        if branch and branch in pr_branches:
            status = AgentStatus.REVIEWING
        elif tf.issue_number is not None and tf.issue_number in wip_issue_numbers:
            status = AgentStatus.IMPLEMENTING
        elif tf.task == "bugs-to-issues":
            # Coordinator (brain-dump) agents have no GitHub issue or PR until
            # sub-agents start filing them.  Treat them as IMPLEMENTING so they
            # show as active rather than confusingly UNKNOWN.
            status = AgentStatus.IMPLEMENTING
        else:
            status = AgentStatus.UNKNOWN

        # Agent ID is the worktree basename (e.g. "issue-732"). This is the
        # canonical identifier used in URLs, DB PKs, and API responses.
        node_id = (
            Path(tf.worktree).name if tf.worktree else None
        ) or (f"issue-{tf.issue_number}" if tf.issue_number else None) or "unknown"
        nodes.append(
            AgentNode(
                id=node_id,
                role=tf.role or "unknown",
                status=status,
                issue_number=tf.issue_number,
                branch=tf.branch,
                batch_id=tf.batch_id,
                worktree_path=tf.worktree,
                cognitive_arch=tf.cognitive_arch,
                logical_tier=tf.logical_tier,
                parent_run_id=tf.parent_run_id,
            )
        )

    return nodes


# ---------------------------------------------------------------------------
# Alert detection
# ---------------------------------------------------------------------------


async def detect_alerts(
    worktrees: list[TaskFile],
    github: GitHubBoard,
) -> tuple[list[str], list[StaleClaim]]:
    """Detect pipeline problems and return human-readable alert strings plus structured stale claims.

    Three alert classes:
    1. **Stale claim** — an ``agent:wip`` issue has no live worktree.
    2. **Out-of-order PR** — an open PR's labels include an agentception phase
       that no longer matches the currently active phase.
    3. **Stuck agent** — the most-recent commit in a worktree is > 30 min old.

    Returns a tuple of (alert strings, stale_claims).  Alert strings include a
    human-readable summary of each stale claim; ``stale_claims`` provides the
    structured data used by the UI "Clear Label" action.
    """
    alerts: list[str] = []
    now = time.time()

    # ── Alert 1: agent:wip issue with no matching worktree ─────────────────
    # Self-heal: automatically clear the agent:wip label when there is no live
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
            logger.info("✅ Auto-healed stale claim: removed agent:wip from #%d", claim.issue_number)
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

    # ── Alert 3: worktree last commit > 30 min ago (async path) ────────────
    # Skip coordinator (brain-dump) worktrees — they have no commits of their
    # own; all work happens in sub-agent worktrees they spawn.  Applying the
    # stuck check to them would always fire immediately.
    #
    # Also skip worktrees whose .agent-task file is newer than the threshold —
    # a freshly spawned worktree has no agent activity yet and is not stuck.
    for tf in worktrees:
        if tf.worktree is None:
            continue
        if tf.task == "bugs-to-issues":
            continue
        path = Path(tf.worktree)
        if not path.exists():
            continue
        # Skip if the worktree was created (task file written) within the threshold.
        task_file = path / ".agent-task"
        if task_file.exists():
            task_mtime = task_file.stat().st_mtime
            if (now - task_mtime) < _STUCK_THRESHOLD_SECONDS:
                continue
        last_commit = await worktree_last_commit_time(path)
        if last_commit > 0.0 and (now - last_commit) > _STUCK_THRESHOLD_SECONDS:
            label = f"issue #{tf.issue_number}" if tf.issue_number else path.name
            alerts.append(f"Possible stuck agent on {label}")

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

    return alerts, stale_claims


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

    worktrees = await list_active_worktrees()
    github = await build_github_board()
    agents = await merge_agents(worktrees, github)
    alerts, stale_claims = await detect_alerts(worktrees, github)

    # ── Persist raw tick data to Postgres ────────────────────────────────────
    # Non-blocking: a DB outage cannot crash the poller or stall the SSE stream.
    try:
        from agentception.db.persist import persist_tick
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
            ),
            open_issues=github.open_issues,
            open_prs=github.open_prs,
            closed_issues=github.closed_issues,
            merged_prs=github.merged_prs,
            gh_repo=settings.gh_repo,
        )
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
    )
    _state = state

    await broadcast(state)
    return state


async def polling_loop() -> None:
    """Run the tick/broadcast cycle on a fixed interval until cancelled.

    Designed to be launched as an ``asyncio.Task`` from the FastAPI lifespan.
    Errors inside a single tick are logged and swallowed so one bad GitHub
    response cannot kill the entire dashboard.
    """
    logger.info(
        "✅ AgentCeption polling loop started (interval=%ds)",
        settings.poll_interval_seconds,
    )
    while True:
        try:
            await tick()
            await asyncio.sleep(settings.poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("✅ Polling loop stopped cleanly")
            return
        except Exception as exc:
            logger.warning("⚠️  Polling loop error: %s", exc)
