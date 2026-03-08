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
from agentception.models import AgentNode, AgentStatus, BoardIssue, PipelineState, PlanDraftEvent, StaleClaim, TaskFile
from agentception.readers.github import (
    get_active_label,
    get_closed_issues,
    get_merged_prs_full,
    get_open_issues,
    get_open_prs,
    get_wip_issues,
)
from agentception.readers.worktrees import list_active_worktrees, parse_agent_task, worktree_last_commit_time

logger = logging.getLogger(__name__)

# Agents whose most-recent commit is older than this threshold are flagged.
_STUCK_THRESHOLD_SECONDS: int = 30 * 60  # 30 minutes

# Plan draft output file must appear within this many seconds of .agent-task mtime.
_PLAN_DRAFT_TIMEOUT_SECONDS: int = 120

# ---------------------------------------------------------------------------
# Shared state — module-level singletons, mutated only by tick()
# ---------------------------------------------------------------------------

_state: PipelineState | None = None
_subscribers: list[asyncio.Queue[PipelineState]] = []

# In-memory deduplication for plan draft events.  Each set holds draft_ids
# for which the corresponding SSE event has already been emitted exactly once.
_emitted_ready_drafts: set[str] = set()
_emitted_timeout_drafts: set[str] = set()

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
    2. Worktree issue number appears in ``agent/wip`` issues → IMPLEMENTING
    3. ``WORKFLOW=bugs-to-issues`` (coordinator/brain-dump) → IMPLEMENTING
       These agents have no issue number or PR; they are actively planning.
    4. Otherwise → UNKNOWN
    """
    # Index open PRs by branch name for O(1) lookup.
    # Maps headRefName → PR number so we can propagate pr_number to AgentNode.
    pr_branch_to_number: dict[str, int] = {}
    for pr in github.open_prs:
        head = pr.get("headRefName")
        number = pr.get("number")
        if isinstance(head, str) and isinstance(number, int):
            pr_branch_to_number[head] = number

    # Index WIP issue numbers for O(1) lookup.
    wip_issue_numbers: set[int] = set()
    for issue in github.wip_issues:
        num = issue.get("number")
        if isinstance(num, int):
            wip_issue_numbers.add(num)

    nodes: list[AgentNode] = []
    for tf in worktrees:
        branch = tf.branch or ""
        gh_pr_number: int | None = pr_branch_to_number.get(branch) if branch else None
        if branch and gh_pr_number is not None:
            status = AgentStatus.REVIEWING
        elif tf.issue_number is not None:
            # Any worktree with a valid issue number is implementing. We do not
            # require the agent/wip GitHub label here — the worktree's existence
            # is the authoritative signal. The label is useful for stale-claim
            # detection (a label without a worktree) but should not gate board
            # visibility, because leaf agents may not have claimed the issue yet
            # when the first poller tick fires.
            status = AgentStatus.IMPLEMENTING
        elif tf.task == "bugs-to-issues":
            # Coordinator (brain-dump) agents have no GitHub issue or PR until
            # sub-agents start filing them.  Treat them as IMPLEMENTING so they
            # show as active rather than confusingly UNKNOWN.
            status = AgentStatus.IMPLEMENTING
        else:
            status = AgentStatus.FAILED

        # Agent ID is the worktree basename (e.g. "issue-732"). This is the
        # canonical identifier used in URLs, DB PKs, and API responses.
        node_id = (
            Path(tf.worktree).name if tf.worktree else None
        ) or (f"issue-{tf.issue_number}" if tf.issue_number else None) or "unknown"
        # Prefer PR number derived from live GitHub branch match over the
        # static value in the .agent-task file (which may be 0 or missing
        # until the agent explicitly updates linked_pr).
        resolved_pr_number = gh_pr_number if gh_pr_number is not None else tf.pr_number
        nodes.append(
            AgentNode(
                id=node_id,
                role=tf.role or "unknown",
                status=status,
                issue_number=tf.issue_number,
                pr_number=resolved_pr_number,
                branch=tf.branch,
                batch_id=tf.batch_id,
                worktree_path=tf.worktree,
                cognitive_arch=tf.cognitive_arch,
                tier=tf.tier,
                org_domain=tf.org_domain,
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
    1. **Stale claim** — an ``agent/wip`` issue has no live worktree.
    2. **Out-of-order PR** — an open PR's labels include an agentception phase
       that no longer matches the currently active phase.
    3. **Stuck agent** — the most-recent commit in a worktree is > 30 min old.

    Returns a tuple of (alert strings, stale_claims).  Alert strings include a
    human-readable summary of each stale claim; ``stale_claims`` provides the
    structured data used by the UI "Clear Label" action.
    """
    alerts: list[str] = []
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


async def scan_plan_draft_worktrees() -> list[PlanDraftEvent]:
    """Scan ``plan-draft-*`` worktrees and return new plan-draft lifecycle events.

    Called on each tick to detect when a Cursor agent has written the expected
    output file (``OUTPUT_PATH`` from the ``.agent-task``).  Returns a list
    containing at most one event per draft per tick:

    - ``plan_draft_ready``   — emitted exactly once when ``OUTPUT_PATH`` appears.
    - ``plan_draft_timeout`` — emitted exactly once when ``OUTPUT_PATH`` is still
      absent 120 seconds after the ``.agent-task`` mtime.

    Already-seen drafts are tracked in ``_emitted_ready_drafts`` and
    ``_emitted_timeout_drafts`` so no draft_id appears in the SSE stream more
    than once regardless of how many ticks elapse.
    """
    events: list[PlanDraftEvent] = []
    worktrees_dir: Path = settings.worktrees_dir

    if not worktrees_dir.exists():
        return events

    try:
        entries = list(worktrees_dir.iterdir())
    except OSError as exc:
        logger.warning("⚠️  scan_plan_draft_worktrees: cannot read %s: %s", worktrees_dir, exc)
        return events

    now = time.time()

    for entry in entries:
        if not entry.is_dir():
            continue
        if not entry.name.startswith("plan-draft-"):
            continue

        if not (entry / ".agent-task").exists():
            continue

        task_file_data = await parse_agent_task(entry)
        if task_file_data is None:
            continue

        draft_id = task_file_data.draft_id
        output_path_str = task_file_data.output_path

        if not draft_id or not output_path_str:
            continue

        # Skip if this draft already had an event emitted.
        if draft_id in _emitted_ready_drafts or draft_id in _emitted_timeout_drafts:
            continue

        output_file = Path(output_path_str)

        if output_file.exists():
            # Output file appeared: emit plan_draft_ready exactly once.
            try:
                yaml_text = await asyncio.get_running_loop().run_in_executor(
                    None, output_file.read_text, "utf-8"
                )
            except OSError as exc:
                logger.warning(
                    "⚠️  scan_plan_draft_worktrees: cannot read output file %s: %s",
                    output_file,
                    exc,
                )
                yaml_text = ""
            _emitted_ready_drafts.add(draft_id)
            events.append(
                PlanDraftEvent(
                    event="plan_draft_ready",
                    draft_id=draft_id,
                    yaml_text=yaml_text,
                    output_path=output_path_str,
                )
            )
            logger.info("✅ plan_draft_ready: draft=%s output=%s", draft_id, output_path_str)
        else:
            # Check whether the draft has timed out.
            task_file_path = entry / ".agent-task"
            try:
                task_mtime = task_file_path.stat().st_mtime
            except OSError as exc:
                logger.warning(
                    "⚠️  scan_plan_draft_worktrees: cannot stat task file %s: %s",
                    task_file_path,
                    exc,
                )
                continue
            if (now - task_mtime) >= _PLAN_DRAFT_TIMEOUT_SECONDS:
                _emitted_timeout_drafts.add(draft_id)
                events.append(
                    PlanDraftEvent(
                        event="plan_draft_timeout",
                        draft_id=draft_id,
                        yaml_text="",
                        output_path=output_path_str,
                    )
                )
                logger.warning(
                    "⚠️ plan_draft_timeout: draft=%s output=%s", draft_id, output_path_str
                )

    return events


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

    worktrees = await list_active_worktrees()
    github = await build_github_board()
    agents = await merge_agents(worktrees, github)
    alerts, stale_claims = await detect_alerts(worktrees, github)
    plan_draft_events = await scan_plan_draft_worktrees()

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
