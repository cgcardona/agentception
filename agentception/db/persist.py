from __future__ import annotations

"""DB persistence layer called by the AgentCeption poller after each tick.

Strategy per entity type
------------------------
ACPipelineSnapshot  — one row per tick, always (lightweight scalars only).
ACIssue / ACPullRequest — upsert on hash-diff: only write when content changes.
ACAgentRun          — upsert on every tick so status transitions are recorded.
ACAgentMessage      — fire-and-forget async task, never blocks the tick loop.

All writes are wrapped in a single ``try/except`` so a DB outage never takes
down the poller — the dashboard degrades gracefully to filesystem-only mode.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import select, update

# Label namespace prefixes that are part of the taxonomy and must never be
# interpreted as initiative slugs.  Any label whose prefix-before-"/" matches
# one of these is a taxonomy label, not a plan-pipeline initiative.
_TAXONOMY_NAMESPACES: frozenset[str] = frozenset(
    {"agent", "batch", "blocked", "gate", "phase", "pipeline", "priority", "team", "type"}
)

from agentception.db.engine import get_session
from agentception.db.models import (
    ACAgentEvent,
    ACAgentMessage,
    ACAgentRun,
    ACExecutionPlan,
    ACInitiativePhase,
    ACIssue,
    ACIssueWorkflowState,
    ACPipelineSnapshot,
    ACPRIssueLink,
    ACPullRequest,
    ACWave,
)

if TYPE_CHECKING:
    from agentception.models import AgentNode, PipelineState

logger = logging.getLogger(__name__)

_UTC = datetime.timezone.utc


def _now() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def _hash(*parts: str) -> str:
    """SHA-256 of the concatenation of all parts — used as the change sentinel."""
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def _parse_blocked_by(body: str) -> list[int]:
    """Extract blocker issue numbers from a '**Blocked by:** #N, #M' line in an issue body.

    Called during every upsert so depends_on_json is populated even when
    persist_issue_depends_on loses the race with the DB row not yet existing.
    Only parses the first matching line; returns [] if no match.
    """
    m = re.search(r"\*\*Blocked by:\*\*\s*((?:#\d+(?:,\s*)?)+)", body)
    if not m:
        return []
    return [int(n) for n in re.findall(r"#(\d+)", m.group(1))]


# ---------------------------------------------------------------------------
# Public entry point — called by poller.tick()
# ---------------------------------------------------------------------------


async def persist_tick(
    state: PipelineState,
    open_issues: list[dict[str, object]],
    open_prs: list[dict[str, object]],
    gh_repo: str,
    closed_issues: list[dict[str, object]] | None = None,
    merged_prs: list[dict[str, object]] | None = None,
) -> None:
    """Persist everything derived from one polling tick.

    Open + closed issues are upserted together so the DB retains full history.
    Open + merged PRs likewise.  Swallows all exceptions so a DB outage never
    crashes the poller.
    """
    try:
        async with get_session() as session:
            await _upsert_snapshot(session, state)
            all_issues = list(open_issues) + list(closed_issues or [])
            await _upsert_issues(session, all_issues, state.active_label, gh_repo)
            all_prs = list(open_prs) + list(merged_prs or [])
            await _upsert_prs(session, all_prs, gh_repo)
            await _upsert_agent_runs(session, state.agents)
            await _auto_close_pr_linked_issues(session, gh_repo)
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  DB persist_tick failed (non-fatal): %s", exc)

    # Workflow state recomputation runs in a separate transaction so a
    # failure here never rolls back the critical PR/issue/run upserts above.
    try:
        async with get_session() as session:
            await _recompute_workflow_state(session, gh_repo)
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  Workflow state recomputation failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def _upsert_snapshot(session: object, state: PipelineState) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    snap = ACPipelineSnapshot(
        polled_at=datetime.datetime.fromtimestamp(state.polled_at, tz=_UTC),
        active_label=state.active_label,
        issues_open=state.issues_open,
        prs_open=state.prs_open,
        agents_active=len(state.agents),
        alerts_json=json.dumps(state.alerts),
    )
    session.add(snap)


# ---------------------------------------------------------------------------
# Issues (hash-diff upsert)
# ---------------------------------------------------------------------------


async def _upsert_issues(
    session: object,
    issues: list[dict[str, object]],
    active_label: str | None,
    repo: str,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = _now()

    for raw in issues:
        num = raw.get("number")
        if not isinstance(num, int):
            continue
        title = str(raw.get("title", ""))
        # Normalise GitHub's uppercase GraphQL state values (OPEN/CLOSED) to lowercase.
        state_str = str(raw.get("state", "open")).lower()
        labels_raw = raw.get("labels", [])
        label_names: list[str] = []
        if isinstance(labels_raw, list):
            for lbl in labels_raw:
                if isinstance(lbl, str):
                    label_names.append(lbl)
                elif isinstance(lbl, dict):
                    n = lbl.get("name")
                    if isinstance(n, str):
                        label_names.append(n)
        labels_json = json.dumps(sorted(label_names))
        content_hash = _hash(title, state_str, labels_json)

        # Parse closedAt timestamp when present (closed issues only).
        closed_at: datetime.datetime | None = None
        closed_at_raw = raw.get("closedAt")
        if isinstance(closed_at_raw, str):
            try:
                closed_at = datetime.datetime.fromisoformat(
                    closed_at_raw.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        result = await session.execute(
            select(ACIssue).where(ACIssue.github_number == num, ACIssue.repo == repo)
        )
        existing = result.scalar_one_or_none()

        body_str = str(raw.get("body", ""))
        if existing is None:
            session.add(
                ACIssue(
                    github_number=num,
                    repo=repo,
                    title=title,
                    body=body_str or None,
                    state=state_str,
                    phase_label=active_label,
                    labels_json=labels_json,
                    # Parse "Blocked by" on first insert so depends_on_json is
                    # populated even when persist_issue_depends_on loses the race.
                    depends_on_json=json.dumps(_parse_blocked_by(body_str)),
                    content_hash=content_hash,
                    closed_at=closed_at,
                    first_seen_at=now,
                    last_synced_at=now,
                )
            )
        elif existing.content_hash != content_hash or existing.state != state_str:
            # Update when content changed OR state transitioned (open → closed).
            existing.title = title
            existing.body = body_str or None
            existing.state = state_str
            existing.phase_label = active_label
            existing.labels_json = labels_json
            existing.content_hash = content_hash
            existing.last_synced_at = now
            # Preserve existing closed_at if already set; use parsed value on transition.
            if closed_at is not None and existing.closed_at is None:
                existing.closed_at = closed_at
            elif state_str == "closed" and existing.closed_at is None:
                existing.closed_at = now

        # Backfill depends_on_json from the issue body for rows where
        # persist_issue_depends_on lost the race (row did not exist yet).
        # Safe to run unconditionally: only writes when the field is still empty.
        if existing is not None and existing.depends_on_json == "[]":
            parsed = _parse_blocked_by(body_str)
            if parsed:
                existing.depends_on_json = json.dumps(parsed)


async def upsert_issues(
    issues: list[dict[str, object]],
    active_label: str | None,
    repo: str,
) -> int:
    """Public entry point for upserting a batch of issues outside a poller tick.

    Opens its own DB session, delegates to :func:`_upsert_issues`, commits,
    and returns the number of issues passed to the upsert.  The underlying
    upsert is hash-diff idempotent — rows are only written when content has
    changed — so concurrent calls with identical data are safe.

    Parameters
    ----------
    issues:
        Raw GitHub issue dicts (same shape as the poller feed).
    active_label:
        Current pipeline phase label, or ``None`` when called outside a tick.
    repo:
        GitHub repository slug (e.g. ``"owner/repo"``).

    Returns
    -------
    int
        Total number of issues passed to the upsert (not the number of rows
        actually written — the hash-diff logic may skip unchanged rows).
    """
    async with get_session() as session:
        await _upsert_issues(session, issues, active_label, repo)
        await session.commit()
    return len(issues)


# ---------------------------------------------------------------------------
# PRs (hash-diff upsert)
# ---------------------------------------------------------------------------


async def _upsert_prs(
    session: object,
    prs: list[dict[str, object]],
    repo: str,
) -> None:
    """Hash-diff upsert for PR rows.

    Key changes vs. the old implementation:

    1. **No tombstone poisoning** — PRs absent from the feed are NOT flipped to
       ``closed``.  Missing data is unknown, not closed.  Only explicit GitHub
       state transitions change the state column.
    2. **Body always re-parsed** — ``closes_issue_number`` and the new
       ``closes_issue_numbers_json`` are recomputed from body text every tick,
       not just on first insert.
    3. **Body hash in content_hash** — ensures body changes trigger a row update
       even when title/labels/state are unchanged.
    4. **New columns** — ``base_ref``, ``is_draft``, ``body_hash``.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = _now()

    for raw in prs:
        num = raw.get("number")
        if not isinstance(num, int):
            continue
        title = str(raw.get("title", ""))
        state_str = str(raw.get("state", "open")).lower()
        head_ref = raw.get("headRefName")
        base_ref = raw.get("baseRefName")
        is_draft_raw = raw.get("isDraft", False)
        is_draft = bool(is_draft_raw) if isinstance(is_draft_raw, bool) else False

        labels_raw = raw.get("labels", [])
        label_names: list[str] = []
        if isinstance(labels_raw, list):
            label_names = [
                lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                for lbl in labels_raw
                if isinstance(lbl, (str, dict))
            ]
        labels_json = json.dumps(sorted(label_names))

        body_str = str(raw.get("body") or "")
        body_hash = _hash(body_str) if body_str else ""

        content_hash = _hash(
            title, state_str, labels_json, str(head_ref),
            str(base_ref), body_hash,
        )

        merged_at_raw = raw.get("mergedAt")
        merged_at: datetime.datetime | None = None
        if isinstance(merged_at_raw, str):
            try:
                merged_at = datetime.datetime.fromisoformat(
                    merged_at_raw.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Always re-derive ALL closing references from body (not just the first).
        closes_issue_numbers: list[int] = [
            int(m.group(1))
            for m in re.finditer(
                r"(?i)(?:closes|fixes|resolves)\s+(?:[\w\-]+/[\w\-]+)?#(\d+)",
                body_str,
            )
        ]
        closes_issue_number: int | None = (
            closes_issue_numbers[0] if closes_issue_numbers else None
        )
        closes_issue_numbers_json = json.dumps(closes_issue_numbers)

        result = await session.execute(
            select(ACPullRequest).where(
                ACPullRequest.github_number == num, ACPullRequest.repo == repo
            )
        )
        existing = result.scalar_one_or_none()

        if existing is None:
            session.add(
                ACPullRequest(
                    github_number=num,
                    repo=repo,
                    title=title,
                    state=state_str,
                    head_ref=str(head_ref) if isinstance(head_ref, str) else None,
                    base_ref=str(base_ref) if isinstance(base_ref, str) else None,
                    is_draft=is_draft,
                    closes_issue_number=closes_issue_number,
                    closes_issue_numbers_json=closes_issue_numbers_json,
                    labels_json=labels_json,
                    content_hash=content_hash,
                    body_hash=body_hash or None,
                    merged_at=merged_at,
                    first_seen_at=now,
                    last_synced_at=now,
                )
            )
        elif existing.content_hash != content_hash or existing.state != state_str:
            existing.title = title
            existing.state = state_str
            existing.head_ref = str(head_ref) if isinstance(head_ref, str) else None
            existing.base_ref = str(base_ref) if isinstance(base_ref, str) else None
            existing.is_draft = is_draft
            existing.labels_json = labels_json
            existing.content_hash = content_hash
            existing.body_hash = body_hash or None
            if merged_at is not None and existing.merged_at is None:
                existing.merged_at = merged_at
            # Always update closes references from body — deterministic recomputation.
            existing.closes_issue_number = closes_issue_number
            existing.closes_issue_numbers_json = closes_issue_numbers_json
            existing.last_synced_at = now
        else:
            # Content unchanged — still mark as seen this tick.
            existing.last_synced_at = now


# ---------------------------------------------------------------------------
# Auto-close issues whose PRs have been merged
# ---------------------------------------------------------------------------

#: Regex that matches GitHub closing keywords in PR body text.
_CLOSES_RE: re.Pattern[str] = re.compile(
    r"(?i)(?:closes|fixes|resolves)\s+#(\d+)"
)


async def _auto_close_pr_linked_issues(session: object, repo: str) -> None:
    """Close issues in the DB (and on GitHub) when their linked PR is merged.

    Agents open PRs against the ``dev`` branch rather than ``main``, which
    bypasses GitHub's native auto-close.  This function bridges the gap by
    detecting two kinds of linkage and closing the issue on GitHub + in the DB:

    1. **Agent-run linkage** — ``agent_runs.pr_number`` → ``agent_runs.issue_number``
    2. **PR-body linkage** — ``pull_requests.closes_issue_number`` (parsed from
       the PR body's "Closes #N" / "Fixes #N" / "Resolves #N" keyword).

    The DB row is updated immediately so the board reflects the change within the
    current tick.  The GitHub close runs as a fire-and-forget subprocess; the next
    ``get_closed_issues`` fetch will confirm and solidify the change.
    """
    from sqlalchemy import or_
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = _now()

    # Collect issue numbers to auto-close from both linkage methods.
    to_close: set[int] = set()

    # Method 1 — agent_runs linking issue ↔ merged PR.
    result = await session.execute(
        select(ACAgentRun.issue_number)
        .join(ACPullRequest, ACPullRequest.github_number == ACAgentRun.pr_number)
        .join(ACIssue, ACIssue.github_number == ACAgentRun.issue_number)
        .where(
            ACAgentRun.pr_number.is_not(None),
            ACAgentRun.issue_number.is_not(None),
            ACPullRequest.state == "merged",
            ACPullRequest.repo == repo,
            ACIssue.state == "open",
            ACIssue.repo == repo,
        )
    )
    for row in result.all():
        if isinstance(row.issue_number, int):
            to_close.add(row.issue_number)

    # Method 2 — pull_requests.closes_issue_number (parsed from PR body).
    result2 = await session.execute(
        select(ACPullRequest.closes_issue_number)
        .join(
            ACIssue,
            ACIssue.github_number == ACPullRequest.closes_issue_number,
        )
        .where(
            ACPullRequest.closes_issue_number.is_not(None),
            ACPullRequest.state == "merged",
            ACPullRequest.repo == repo,
            ACIssue.state == "open",
            ACIssue.repo == repo,
        )
    )
    for row in result2.all():
        if isinstance(row.closes_issue_number, int):
            to_close.add(row.closes_issue_number)

    if not to_close:
        return

    logger.info(
        "✅ auto-closing %d issue(s) whose PRs are merged: %s",
        len(to_close),
        sorted(to_close),
    )

    # Update the DB immediately so the board reflects the change this tick.
    for issue_num in to_close:
        issue_result = await session.execute(
            select(ACIssue).where(
                ACIssue.github_number == issue_num, ACIssue.repo == repo
            )
        )
        issue = issue_result.scalar_one_or_none()
        if issue is None:
            continue
        issue.state = "closed"
        issue.last_synced_at = now
        if issue.closed_at is None:
            issue.closed_at = now
        # Recompute content_hash so the next _upsert_issues doesn't re-open it
        # if GitHub still reports it as open during the close propagation window.
        issue.content_hash = _hash(issue.title, "closed", issue.labels_json)

    # Fire-and-forget: close the issues on GitHub so the source of truth stays
    # consistent and subsequent poller ticks don't flip the state back to open.
    for issue_num in to_close:
        asyncio.ensure_future(
            _gh_close_issue(repo, issue_num)
        )


async def _gh_close_issue(repo: str, issue_number: int) -> None:
    """Close a GitHub issue via the ``gh`` CLI (fire-and-forget helper)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "close", str(issue_number), "--repo", repo,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("✅ gh issue close %d (exit %d)", issue_number, proc.returncode or 0)
    except Exception as exc:
        logger.warning("⚠️  _gh_close_issue(%d) failed: %s", issue_number, exc)


# ---------------------------------------------------------------------------
# Workflow state recomputation (linker + state machine)
# ---------------------------------------------------------------------------


async def _recompute_workflow_state(session: object, repo: str) -> list[str]:
    """Recompute PR↔Issue links and canonical workflow state for all issues.

    Called within ``persist_tick`` after issues, PRs, and runs are upserted.
    Returns a list of invariant-violation alert strings (empty if clean).
    """
    from agentception.workflow.invariants import InvariantContext, WorkflowSnapshot, check_invariants
    from agentception.workflow.linking import (
        BestPR,
        CandidateLink,
        PRInfo,
        PRRow,
        RunRow,
        best_pr_for_issue,
        discover_links_for_pr,
    )
    from agentception.workflow.state_machine import IssueInput, RunInput, compute_workflow_state
    from agentception.workflow.status import compute_agent_status
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = _now()

    # --- Load all data ---
    issues_result = await session.execute(
        select(ACIssue).where(ACIssue.repo == repo)
    )
    all_issues = issues_result.scalars().all()

    prs_result = await session.execute(
        select(ACPullRequest).where(ACPullRequest.repo == repo)
    )
    all_prs = prs_result.scalars().all()

    runs_result = await session.execute(
        select(ACAgentRun).order_by(ACAgentRun.spawned_at.desc())
    )
    all_runs = runs_result.scalars().all()

    # --- Build lookup structures ---
    runs_by_pr: dict[int, list[RunRow]] = {}
    latest_run_by_issue: dict[int, ACAgentRun] = {}
    run_pr_numbers: dict[str, int | None] = {}

    for run in all_runs:
        run_pr_numbers[run.id] = run.pr_number
        if run.pr_number is not None:
            run_row = RunRow(
                id=run.id,
                issue_number=run.issue_number,
                pr_number=run.pr_number,
            )
            runs_by_pr.setdefault(run.pr_number, []).append(run_row)
        if run.issue_number is not None and run.issue_number not in latest_run_by_issue:
            latest_run_by_issue[run.issue_number] = run

    pr_info_map: dict[int, PRInfo] = {}
    pr_states: dict[int, str] = {}
    pr_bases: dict[int, str | None] = {}
    closes_refs_by_pr: dict[int, list[int]] = {}

    for pr in all_prs:
        pr_info_map[pr.github_number] = PRInfo(
            number=pr.github_number,
            state=pr.state,
            base_ref=pr.base_ref,
            head_ref=pr.head_ref,
        )
        pr_states[pr.github_number] = pr.state
        pr_bases[pr.github_number] = pr.base_ref
        # Parse closes refs from the stored JSON array.
        try:
            refs = json.loads(pr.closes_issue_numbers_json or "[]")
        except (json.JSONDecodeError, TypeError):
            refs = []
        if isinstance(refs, list):
            closes_refs_by_pr[pr.github_number] = [r for r in refs if isinstance(r, int)]

    # --- Discover links for every PR ---
    all_candidates: list[CandidateLink] = []
    for pr in all_prs:
        labels: list[str] = json.loads(pr.labels_json or "[]")
        pr_row = PRRow(
            number=pr.github_number,
            title=pr.title,
            head_ref=pr.head_ref,
            base_ref=pr.base_ref,
            body="",  # body not stored on ACPullRequest — linkage comes from closes_issue_numbers_json
            labels=labels,
        )
        candidates = discover_links_for_pr(pr_row, repo, runs_by_pr)

        # Also add body_closes candidates from the stored closes_issue_numbers.
        for issue_num in closes_refs_by_pr.get(pr.github_number, []):
            already_has = any(
                c["issue_number"] == issue_num and c["link_method"] == "body_closes"
                for c in candidates
            )
            if not already_has:
                candidates.append(CandidateLink(
                    repo=repo,
                    pr_number=pr.github_number,
                    issue_number=issue_num,
                    link_method="body_closes",
                    confidence=95,
                    evidence_json=json.dumps({"source": "closes_issue_numbers_json"}),
                ))

        all_candidates.extend(candidates)

    # --- Persist link rows (upsert) ---
    link_issue_numbers_by_pr: dict[int, list[int]] = {}
    for candidate in all_candidates:
        pr_num = candidate["pr_number"]
        issue_num = candidate["issue_number"]
        link_issue_numbers_by_pr.setdefault(pr_num, []).append(issue_num)

        existing_link = await session.execute(
            select(ACPRIssueLink).where(
                ACPRIssueLink.repo == repo,
                ACPRIssueLink.pr_number == pr_num,
                ACPRIssueLink.issue_number == issue_num,
                ACPRIssueLink.link_method == candidate["link_method"],
            )
        )
        link_row = existing_link.scalar_one_or_none()
        if link_row is None:
            session.add(ACPRIssueLink(
                repo=repo,
                pr_number=pr_num,
                issue_number=issue_num,
                link_method=candidate["link_method"],
                confidence=candidate["confidence"],
                evidence_json=candidate["evidence_json"],
                first_seen_at=now,
                last_seen_at=now,
            ))
        else:
            link_row.confidence = candidate["confidence"]
            link_row.evidence_json = candidate["evidence_json"]
            link_row.last_seen_at = now

    # --- Compute workflow state for each issue ---
    issue_number_set = {i.github_number for i in all_issues}
    workflow_snapshots: dict[int, WorkflowSnapshot] = {}

    for issue in all_issues:
        issue_num = issue.github_number
        issue_labels: list[str] = json.loads(issue.labels_json or "[]")

        # Derive initiative and phase from labels — skip taxonomy namespaces.
        initiative: str | None = None
        phase_key: str | None = None
        for lbl in issue_labels:
            if "/" in lbl:
                slug = lbl.split("/")[0]
                if slug not in _TAXONOMY_NAMESPACES:
                    initiative = slug
                    phase_key = lbl
                    break

        # Get best PR for this issue.
        issue_candidates = [c for c in all_candidates if c["issue_number"] == issue_num]
        best = best_pr_for_issue(issue_num, issue_candidates, pr_info_map)

        # Get latest run for this issue.
        run_obj = latest_run_by_issue.get(issue_num)
        run_input: RunInput | None = None
        if run_obj is not None:
            computed_status = compute_agent_status(run_obj.status, run_obj.last_activity_at, now=now)
            # Promote to "reviewing" if an active reviewer run exists for this PR.
            if best and best["pr_state"] in ("open", "draft"):
                for r in all_runs:
                    if (
                        r.role == "reviewer"
                        and r.pr_number == best["pr_number"]
                        and r.status in ("implementing", "reviewing")
                    ):
                        computed_status = "reviewing"
                        break
            run_input = RunInput(
                id=run_obj.id,
                status=run_obj.status,
                agent_status=computed_status,
                pr_number=run_obj.pr_number,
            )

        # Detect merged-recently for stabilisation.
        pr_merged_recently = False
        if best and best["pr_state"] == "merged" and issue.state == "open":
            pr_merged_recently = True

        issue_input = IssueInput(
            number=issue_num,
            state=issue.state,
            labels=issue_labels,
            phase_key=phase_key,
            initiative=initiative,
        )

        wf_state = compute_workflow_state(
            issue_input, run_input, best, pr_merged_recently=pr_merged_recently,
        )

        # Persist to ac_issue_workflow_state.
        existing_wf = await session.execute(
            select(ACIssueWorkflowState).where(
                ACIssueWorkflowState.repo == repo,
                ACIssueWorkflowState.issue_number == issue_num,
            )
        )
        wf_row = existing_wf.scalar_one_or_none()

        if wf_row is None:
            session.add(ACIssueWorkflowState(
                repo=repo,
                issue_number=issue_num,
                initiative=initiative,
                phase_key=phase_key,
                lane=wf_state["lane"],
                issue_state=wf_state["issue_state"],
                run_id=wf_state["run_id"],
                agent_status=wf_state["agent_status"],
                pr_number=wf_state["pr_number"],
                pr_state=wf_state["pr_state"],
                pr_base=wf_state["pr_base"],
                pr_head_ref=wf_state["pr_head_ref"],
                pr_link_method=wf_state["pr_link_method"],
                pr_link_confidence=wf_state["pr_link_confidence"],
                warnings_json=json.dumps(wf_state["warnings"]),
                content_hash=wf_state["content_hash"],
                first_seen_at=now,
                last_computed_at=now,
            ))
        elif wf_row.content_hash != wf_state["content_hash"]:
            wf_row.initiative = initiative
            wf_row.phase_key = phase_key
            wf_row.lane = wf_state["lane"]
            wf_row.issue_state = wf_state["issue_state"]
            wf_row.run_id = wf_state["run_id"]
            wf_row.agent_status = wf_state["agent_status"]
            wf_row.pr_number = wf_state["pr_number"]
            wf_row.pr_state = wf_state["pr_state"]
            wf_row.pr_base = wf_state["pr_base"]
            wf_row.pr_head_ref = wf_state["pr_head_ref"]
            wf_row.pr_link_method = wf_state["pr_link_method"]
            wf_row.pr_link_confidence = wf_state["pr_link_confidence"]
            wf_row.warnings_json = json.dumps(wf_state["warnings"])
            wf_row.content_hash = wf_state["content_hash"]
            wf_row.last_computed_at = now

        workflow_snapshots[issue_num] = WorkflowSnapshot(
            lane=wf_state["lane"],
            pr_number=wf_state["pr_number"],
            pr_state=wf_state["pr_state"],
            agent_status=wf_state["agent_status"],
            issue_state=wf_state["issue_state"],
        )

    # --- Run invariants ---
    inv_ctx = InvariantContext(
        repo=repo,
        issue_numbers=list(issue_number_set),
        pr_numbers_in_db={pr.github_number for pr in all_prs},
        run_pr_numbers=run_pr_numbers,
        link_issue_numbers_by_pr=link_issue_numbers_by_pr,
        workflow_states=workflow_snapshots,
        pr_states=pr_states,
        pr_bases=pr_bases,
        closes_refs_by_pr=closes_refs_by_pr,
    )
    alerts = check_invariants(inv_ctx)

    if alerts:
        logger.warning(
            "⚠️  Workflow invariant alerts (%d): %s",
            len(alerts),
            "; ".join(alerts[:3]),
        )

    return alerts


# ---------------------------------------------------------------------------
# Agent runs (status upsert)
# ---------------------------------------------------------------------------


from agentception.workflow.status import ACTIVE_STATUSES as _ACTIVE_STATUSES  # noqa: E402


async def _upsert_agent_runs(
    session: object,
    agents: list[AgentNode],
) -> None:
    from sqlalchemy import or_

    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = _now()

    live_ids: set[str] = set()

    for agent in agents:
        run_id = agent.id
        live_ids.add(run_id)
        result = await session.execute(
            select(ACAgentRun).where(ACAgentRun.id == run_id)
        )
        existing = result.scalar_one_or_none()

        if existing is None:
            session.add(
                ACAgentRun(
                    id=run_id,
                    wave_id=None,
                    issue_number=agent.issue_number,
                    pr_number=agent.pr_number,
                    branch=agent.branch,
                    worktree_path=agent.worktree_path,
                    role=agent.role,
                    status=agent.status.value,
                    batch_id=agent.batch_id,
                    cognitive_arch=agent.cognitive_arch,
                    spawned_at=now,
                    last_activity_at=now,
                )
            )
        else:
            # Never overwrite pending_launch — only the Dispatcher's acknowledge
            # endpoint may transition out of that state.  The poller can see the
            # worktree on disk and would clobber it with "stale" otherwise, which
            # would drain the queue before the Dispatcher ever reads it.
            #
            # Never overwrite adhoc runs (issue_number is None) — they are
            # managed entirely by their asyncio task lifecycle.  The poller
            # derives a synthetic display status for them that is not an accurate
            # reflection of real run state, so writing it back would corrupt the
            # DB row and cause the agent loop's terminal-state guard to fire.
            if existing.status != "pending_launch" and existing.issue_number is not None:
                existing.status = agent.status.value
            # Only advance pr_number — never regress it to None.
            # persist_agent_event(done) writes pr_number from the agent's
            # build_report_done call; the DB row never contains a
            # PR number (it was written before the PR existed).  Without this
            # guard the worktree-derived None would overwrite the saved value
            # on every subsequent tick, collapsing the Kanban card back to
            # "todo" immediately after the engineer completes.
            if agent.pr_number is not None:
                existing.pr_number = agent.pr_number
            existing.last_activity_at = now
            # Backfill cognitive_arch when the poller first picks up a live run.
            if existing.cognitive_arch is None and agent.cognitive_arch is not None:
                existing.cognitive_arch = agent.cognitive_arch

    # Orphan sweep: any run that was active in a previous tick but is no
    # longer backed by a live worktree gets flipped to "completed" (PR exists)
    # or "failed" (no PR).  This prevents phantom "implementing" rows from
    # persisting in the Run History after a worktree is removed without a
    # clean shutdown.
    #
    # Exception: runs that have already opened a PR are not orphaned.  Their
    # lifecycle is now driven by the PR state (GitHub), not by a live worktree.
    # They stay "reviewing" until the PR merges and the issue closes, at which
    # point the issue moves to the "completed" bucket naturally.
    #
    # Grace period: do not orphan a run that transitioned to active very
    # recently (last_activity_at within _ORPHAN_GRACE_SECONDS).  The poller
    # builds live_ids from list_active_runs() at tick start; dispatch can
    # commit acknowledge_agent_run after that, so the run is in the DB as
    # implementing but not yet in live_ids.  Without the grace period the
    # orphan sweep would immediately mark it failed.
    _ORPHAN_GRACE_SECONDS = 60
    orphan_cutoff = now - datetime.timedelta(seconds=_ORPHAN_GRACE_SECONDS)
    orphan_result = await session.execute(
        select(ACAgentRun).where(
            ACAgentRun.status.in_(_ACTIVE_STATUSES),
        )
    )
    for orphan in orphan_result.scalars().all():
        # Ad-hoc runs (issue_number is None) are managed by the asyncio task
        # lifecycle, not by the GitHub polling loop.  Exclude them from the
        # orphan sweep so the polling tick never flips an in-progress adhoc
        # run to "failed" just because it isn't backed by a GitHub issue.
        #
        # Reviewer runs are also excluded: unlike developer runs (where
        # pr_number is set only after the PR is opened and the run is done),
        # reviewer runs have pr_number set AT DISPATCH TIME because the PR
        # already exists.  Applying the pr_number → completed heuristic to a
        # reviewer would kill it immediately after creation.  Reviewer
        # lifecycle is always driven by build_complete_run, never by poller
        # inference.
        if (
            orphan.id not in live_ids
            and orphan.issue_number is not None
            and orphan.role != "reviewer"
            and (orphan.last_activity_at is None or orphan.last_activity_at <= orphan_cutoff)
        ):
            with session.no_autoflush:
                # Use the build_complete_run event as the authoritative completion
                # gate — not pr_number.  An agent can open a PR and then crash
                # before calling build_complete_run; pr_number alone is not a
                # reliable signal that the agent finished cleanly.
                #
                # no_autoflush prevents SQLAlchemy from flushing a pending
                # ACAgentEvent row from the previous iteration when this SELECT
                # fires, which would fail if the referenced ACAgentRun was
                # concurrently re-created with the same run_id.
                from sqlalchemy import func  # noqa: PLC0415

                has_complete_event = await session.scalar(
                    select(func.count()).select_from(ACAgentEvent).where(
                        ACAgentEvent.agent_run_id == orphan.id,
                        ACAgentEvent.event_type == "build_complete_run",
                    )
                )
                if has_complete_event:
                    pass  # already completed — do not mutate
                else:
                    orphan.status = "failed"
                    orphan.last_activity_at = now
                    session.add(ACAgentEvent(
                        agent_run_id=orphan.id,
                        issue_number=orphan.issue_number,
                        event_type="orphan_failed",
                        payload=json.dumps({"reason": "worktree_gone_no_build_complete"}),
                        recorded_at=now,
                   ))
                    logger.warning(
                        "🧹 Orphan run %s → failed (worktree gone, no build_complete_run event)",
                        orphan.id,
                    )

    # Pending-launch TTL sweep: a pending_launch run that was never acknowledged
    # within 15 minutes is presumed abandoned (Dispatcher aborted before claiming
    # it).  Mark it failed so it doesn't permanently lock the issue in "active".
    _PENDING_LAUNCH_TTL = datetime.timedelta(minutes=15)
    ttl_cutoff = now - _PENDING_LAUNCH_TTL
    pending_result = await session.execute(
        select(ACAgentRun).where(
            ACAgentRun.status == "pending_launch",
            ACAgentRun.spawned_at <= ttl_cutoff,
        )
    )
    for stale_pending in pending_result.scalars().all():
        stale_pending.status = "failed"
        stale_pending.last_activity_at = now
        logger.debug(
            "🧹 Pending-launch TTL expired: %s → failed (spawned_at=%s)",
            stale_pending.id,
            stale_pending.spawned_at,
        )


# ---------------------------------------------------------------------------
# Wave lifecycle (conductor-spawn entry points)
# ---------------------------------------------------------------------------


async def persist_wave_start(wave_id: str, phase_label: str, role: str) -> None:
    """Insert a new ACWave row at conductor-spawn time.

    Best-effort — all exceptions are swallowed so a DB outage never blocks
    the spawn endpoint.  The wave_id is the primary key; duplicate inserts
    are silently ignored via the except clause.
    """
    try:
        async with get_session() as session:
            session.add(
                ACWave(
                    id=wave_id,
                    phase_label=phase_label,
                    role=role,
                    started_at=_now(),
                    completed_at=None,
                    spawn_count=0,
                    skip_count=0,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  persist_wave_start failed (non-fatal): %s", exc)


async def persist_wave_complete(wave_id: str, spawn_count: int, skip_count: int) -> None:
    """Update an existing ACWave row with final spawn/skip counts and completed_at.

    Best-effort — all exceptions are swallowed so a DB outage never blocks
    the spawn endpoint.  No-ops when the row does not exist.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACWave).where(ACWave.id == wave_id)
            )
            wave = result.scalar_one_or_none()
            if wave is not None:
                wave.spawn_count = spawn_count
                wave.skip_count = skip_count
                wave.completed_at = _now()
                await session.commit()
    except Exception as exc:
        logger.warning("⚠️  persist_wave_complete failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Agent messages (async fire-and-forget)
# ---------------------------------------------------------------------------


async def persist_agent_run_dispatch(
    run_id: str,
    issue_number: int,
    role: str,
    branch: str,
    worktree_path: str,
    batch_id: str,
    host_worktree_path: str,
    cognitive_arch: str | None = None,
    tier: str | None = None,
    org_domain: str | None = None,
    parent_run_id: str | None = None,
    gh_repo: str | None = None,
    is_resumed: bool = False,
    coord_fingerprint: str | None = None,
    task_description: str | None = None,
    pr_number: int | None = None,
    prompt_variant: str | None = None,
) -> None:
    """Insert an ``ACAgentRun`` row with status ``pending_launch`` at dispatch time.

    Called by dispatch routes and ``spawn_child`` immediately after the worktree
    is created.  All task context is stored here —
    written.  Agents read their briefing from ``ac://runs/{run_id}/context`` and
    the ``task/briefing`` MCP prompt, both of which are DB-backed.

    ``host_worktree_path`` is stored in the ``spawn_mode`` field as a JSON
    blob (backward compat — no dedicated column yet).

    Best-effort — swallows exceptions so a DB outage never blocks dispatch.
    """
    import json as _json

    spawn_mode_json = _json.dumps({"host_worktree": host_worktree_path})
    logger.warning(
        "💾 persist_agent_run_dispatch: run_id=%r role=%r worktree_path=%r "
        "host_worktree_path=%r cognitive_arch=%r tier=%r org_domain=%r "
        "parent_run_id=%r gh_repo=%r is_resumed=%r coord_fingerprint=%r",
        run_id, role, worktree_path, host_worktree_path, cognitive_arch,
        tier, org_domain, parent_run_id, gh_repo, is_resumed, coord_fingerprint,
    )
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                logger.warning(
                    "💾 persist_agent_run_dispatch: run_id=%r already exists (status=%r) — re-arming to pending_launch",
                    run_id, existing.status,
                )
                existing.status = "pending_launch"
                existing.role = role  # always update — role field is persisted as-is
                existing.spawn_mode = spawn_mode_json
                # Reset spawned_at so the pending_launch TTL sweep (15 min window)
                # does not immediately re-fail a re-dispatched run whose original
                # spawned_at is older than the cutoff.
                existing.spawned_at = _now()
                existing.last_activity_at = _now()
                if cognitive_arch is not None:
                    existing.cognitive_arch = cognitive_arch
                if tier is not None:
                    existing.tier = tier
                if org_domain is not None:
                    existing.org_domain = org_domain
                if parent_run_id is not None:
                    existing.parent_run_id = parent_run_id
                if gh_repo is not None:
                    existing.gh_repo = gh_repo
                existing.is_resumed = is_resumed
                if coord_fingerprint is not None:
                    existing.coord_fingerprint = coord_fingerprint
                if task_description is not None:
                    existing.task_description = task_description
                if pr_number is not None:
                    existing.pr_number = pr_number
                if prompt_variant is not None:
                    existing.prompt_variant = prompt_variant
            else:
                logger.warning(
                    "💾 persist_agent_run_dispatch: run_id=%r is new — inserting with status=pending_launch",
                    run_id,
                )
                session.add(
                    ACAgentRun(
                        id=run_id,
                        wave_id=None,
                        issue_number=issue_number,
                        pr_number=pr_number,
                        branch=branch,
                        worktree_path=worktree_path,
                        role=role,
                        status="pending_launch",
                        attempt_number=0,
                        spawn_mode=spawn_mode_json,
                        batch_id=batch_id,
                        cognitive_arch=cognitive_arch,
                        tier=tier,
                        org_domain=org_domain,
                        parent_run_id=parent_run_id,
                        gh_repo=gh_repo,
                        is_resumed=is_resumed,
                        coord_fingerprint=coord_fingerprint,
                        task_description=task_description,
                        prompt_variant=prompt_variant,
                        spawned_at=_now(),
                        last_activity_at=_now(),
                    )
                )
            await session.commit()
        logger.warning("✅ persist_agent_run_dispatch: committed — run_id=%r is pending_launch", run_id)
    except Exception as exc:
        logger.warning("❌ persist_agent_run_dispatch FAILED: %s", exc, exc_info=True)


async def acknowledge_agent_run(run_id: str) -> bool:
    """Transition a ``pending_launch`` run to ``implementing``.

    Called by the coordinator agent via ``POST /api/runs/{run_id}/acknowledge``
    to atomically claim the run before spawning its Task worker.

    Returns ``True`` when the transition succeeded, ``False`` when the run was
    not found or was not in ``pending_launch`` state (idempotency guard).
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None or run.status != "pending_launch":
                return False
            run.status = "implementing"
            run.last_activity_at = _now()
            await session.commit()
        logger.info("✅ acknowledge_agent_run: %s → implementing", run_id)
        return True
    except Exception as exc:
        logger.warning("⚠️  acknowledge_agent_run failed: %s", exc)
        return False


from agentception.workflow.status import (  # noqa: E402
    ACTIVE_STATUSES as _ACTIVE_STATUSES_SM,
    RESET_STATUSES as _RESET_STATUSES,
    RESUMABLE_STATUSES as _RESUMABLE_STATUSES,
)

# ---------------------------------------------------------------------------
# Explicit state-transition persist functions (called by MCP build commands)
# ---------------------------------------------------------------------------


async def complete_agent_run(run_id: str) -> bool:
    """Transition an ``implementing`` run to ``completed``.

    Called by ``build_complete_run`` MCP tool after the agent has opened a PR
    and all work is done.  Only succeeds from ``implementing`` state.

    Also inserts an ``ACAgentEvent`` row with ``event_type = 'build_complete_run'``
    so the orphan sweep can distinguish a clean completion from a crash.

    Returns ``True`` on success, ``False`` if the run was not found or was not
    in a valid source state.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None or run.status != "implementing":
                return False
            run.status = "completed"
            run.last_activity_at = _now()
            run.completed_at = _now()
            session.add(ACAgentEvent(
                agent_run_id=run_id,
                event_type="build_complete_run",
                payload="{}",
                recorded_at=_now(),
            ))
            await session.commit()
        logger.info("✅ complete_agent_run: %s → completed", run_id)
        return True
    except Exception as exc:
        logger.warning("⚠️  complete_agent_run failed: %s", exc)
        return False


async def block_agent_run(run_id: str) -> bool:
    """Transition an ``implementing`` run to ``blocked``.

    Called by ``build_block_run`` MCP tool when an agent cannot proceed without
    human intervention or a dependency resolving.  Only succeeds from
    ``implementing`` state.

    Returns ``True`` on success, ``False`` if the run was not found or not in
    a valid source state.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None or run.status != "implementing":
                return False
            run.status = "blocked"
            run.last_activity_at = _now()
            await session.commit()
        logger.info("✅ block_agent_run: %s → blocked", run_id)
        return True
    except Exception as exc:
        logger.warning("⚠️  block_agent_run failed: %s", exc)
        return False


async def resume_agent_run(run_id: str, agent_run_id: str) -> bool:
    """Transition a ``blocked`` or ``stopped`` run back to ``implementing``.

    Called by ``build_resume_run`` MCP tool.  Idempotent: if the run is already
    ``implementing`` and the caller's ``agent_run_id`` matches the run id, the
    call succeeds (safe restart behaviour).

    Returns ``True`` on success, ``False`` if the run was not found, not in a
    resumable state, or the agent_run_id does not match.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                return False
            # Idempotency: already implementing with same agent — allow restart
            if run.status == "implementing" and run.id == agent_run_id:
                return True
            if run.status not in _RESUMABLE_STATUSES:
                return False
            run.status = "implementing"
            run.last_activity_at = _now()
            await session.commit()
        logger.info("✅ resume_agent_run: %s → implementing", run_id)
        return True
    except Exception as exc:
        logger.warning("⚠️  resume_agent_run failed: %s", exc)
        return False


async def cancel_agent_run(run_id: str) -> bool:
    """Transition any active run to ``cancelled``.

    Called by ``build_cancel_run`` MCP tool (or UI cancel button).  Valid from
    any non-terminal state.

    Returns ``True`` on success, ``False`` if run not found or already terminal.
    """
    from agentception.workflow.status import TERMINAL_STATUSES as _TERMINAL  # noqa: PLC0415
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None or run.status in _TERMINAL:
                return False
            run.status = "cancelled"
            run.last_activity_at = _now()
            await session.commit()
        logger.info("✅ cancel_agent_run: %s → cancelled", run_id)
        return True
    except Exception as exc:
        logger.warning("⚠️  cancel_agent_run failed: %s", exc)
        return False


async def stop_agent_run(run_id: str) -> bool:
    """Transition any active run to ``stopped``.

    Called by ``build_stop_run`` MCP tool (or the UI stop button).  Unlike
    ``cancel_agent_run``, a stopped run can be resumed via ``build_resume_run``.

    Returns ``True`` on success, ``False`` if run not found or already terminal.
    """
    from agentception.workflow.status import TERMINAL_STATUSES as _TERMINAL  # noqa: PLC0415
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None or run.status in _TERMINAL:
                return False
            run.status = "stopped"
            run.last_activity_at = _now()
            await session.commit()
        logger.info("✅ stop_agent_run: %s → stopped", run_id)
        return True
    except Exception as exc:
        logger.warning("⚠️  stop_agent_run failed: %s", exc)
        return False


async def update_agent_status(run_id: str, status: str) -> bool:
    """Set the status of an existing :class:`ACAgentRun` by *run_id*.

    Accepts a plain status string (``AgentStatus.STALLED.value``) or the
    ``AgentStatus`` enum value directly (both are ``str`` at runtime because
    ``AgentStatus`` inherits from ``str``).

    Guards against overwriting terminal states — if the run is already in a
    terminal state (completed, cancelled, stopped, failed) the write is skipped
    and ``False`` is returned.

    Returns ``True`` on success, ``False`` if run not found or already terminal.
    """
    from agentception.workflow.status import TERMINAL_STATUSES as _TERMINAL  # noqa: PLC0415

    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None or run.status in _TERMINAL:
                return False
            run.status = str(status)
            run.last_activity_at = _now()
            await session.commit()
        logger.info("✅ update_agent_status: %s → %s", run_id, status)
        return True
    except Exception as exc:
        logger.warning("⚠️  update_agent_status failed: %s", exc)
        return False


async def clear_run_worktree_path(run_id: str) -> bool:
    """Set worktree_path to None for *run_id* so the reaper stops re-finding it.

    Called by the worktree reaper after successfully releasing a worktree dir.
    Returns True on success, False if run not found or DB error.
    """
    try:
        async with get_session() as session:
            await session.execute(
                update(ACAgentRun).where(ACAgentRun.id == run_id).values(worktree_path=None)
            )
            await session.commit()
        return True
    except Exception as exc:
        logger.warning("⚠️  clear_run_worktree_path failed for %s: %s", run_id, exc)
        return False


async def accumulate_token_usage(
    run_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> None:
    """Add per-turn token counts to the cumulative totals on an ACAgentRun.

    Called after every successful LLM turn.  Uses an atomic SQL increment so
    concurrent writes from the same run cannot race — the cumulative columns
    always reflect the true total regardless of iteration order.

    Silently skips when the run is not found (e.g. tests that stub the loop).
    """
    try:
        async with get_session() as session:
            await session.execute(
                update(ACAgentRun)
                .where(ACAgentRun.id == run_id)
                .values(
                    total_input_tokens=ACAgentRun.total_input_tokens + input_tokens,
                    total_output_tokens=ACAgentRun.total_output_tokens + output_tokens,
                    total_cache_write_tokens=ACAgentRun.total_cache_write_tokens + cache_write_tokens,
                    total_cache_read_tokens=ACAgentRun.total_cache_read_tokens + cache_read_tokens,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  accumulate_token_usage failed for run_id=%r — %s", run_id, exc)


async def reset_build_runs_to_failed() -> int:
    """Set all agent runs in active states to ``failed``.

    Used by the full reset-build flow so the launch queue is empty and no
    run appears as in progress.  Returns the number of runs updated.
    """
    try:
        now = _now()
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.status.in_(_RESET_STATUSES))
            )
            rows = result.scalars().all()
            for run in rows:
                run.status = "failed"
                run.last_activity_at = now
            await session.commit()
            count = len(rows)
        logger.info("✅ reset_build_runs_to_failed: %d run(s) set to failed", count)
        return count
    except Exception as exc:
        logger.warning("⚠️  reset_build_runs_to_failed failed: %s", exc)
        return 0


class PhaseEntry(TypedDict):
    """One phase entry for :func:`persist_initiative_phases`."""

    label: str
    order: int
    depends_on: list[str]


async def persist_initiative_phases(
    repo: str,
    initiative: str,
    batch_id: str,
    phases: list[PhaseEntry],
) -> None:
    """Insert phase DAG rows for a specific (repo, initiative, batch_id) filing.

    Called by ``file_issues`` after all GitHub issues have been created.
    Each entry must have:
    - ``"label"``      — scoped phase label, e.g. ``"ac-auth/0-foundation"``
    - ``"order"``      — 0-indexed display position (canonical ordering source)
    - ``"depends_on"`` — list of scoped phase labels that must complete first

    Each call produces a distinct batch of rows identified by ``batch_id``.
    Re-filing the same initiative creates new rows under the new batch_id;
    no existing rows are modified.  Best-effort — swallows exceptions so a DB
    outage never blocks filing.
    """
    import json as _json

    try:
        now = _now()
        async with get_session() as session:
            for phase in phases:
                session.add(
                    ACInitiativePhase(
                        repo=repo,
                        initiative=initiative,
                        batch_id=batch_id,
                        phase_label=phase["label"],
                        phase_order=phase["order"],
                        depends_on_json=_json.dumps(phase["depends_on"]),
                        created_at=now,
                    )
                )
            await session.commit()
        logger.info(
            "✅ persist_initiative_phases: %s/%s batch=%s — %d phases written",
            repo,
            initiative,
            batch_id,
            len(phases),
        )
    except Exception as exc:
        logger.warning("⚠️  persist_initiative_phases failed (non-fatal): %s", exc)


async def reseed_missing_initiative_phases(repo: str) -> None:
    """Re-derive and persist the phase dep graph for any initiative that has no DB entry.

    Called by the poller after every ``persist_tick`` so that a DB reset never
    leaves the board in a permanently-unlocked state.  When ``file_issues`` ran
    originally it wrote the authoritative dep graph; this function recovers it
    from the scoped phase labels that are already on the GitHub issues (and now
    in the ``issues`` table).

    Initiative slugs are derived directly from the issues in the DB — any label
    containing a ``/`` contributes an initiative slug (the part before the
    slash).  This avoids a dependency on ``pipeline-config.json`` and makes the
    recovery self-contained: the source of truth is the issues themselves.

    Recovery rules:
    - Scoped phase labels follow ``{initiative}/{N}-{slug}`` (e.g.
      ``ac-workflow/1-toml-migration``).  Lexicographic sort preserves the
      ``N-`` prefix ordering.
    - Dependencies are assumed sequential: phase[0] has none; phase[i] depends
      on phase[i-1].  This matches what ``file_issues`` writes for a linear plan.
    - If ``initiative_phases`` already has rows for an initiative, skip it —
      the authoritative data is in place and should not be overwritten.
    """
    try:
        async with get_session() as session:
            # Collect all scoped phase labels grouped by initiative slug.
            issues_result = await session.execute(
                select(ACIssue).where(ACIssue.repo == repo)
            )
            initiative_phase_labels: dict[str, set[str]] = {}
            for issue in issues_result.scalars().all():
                labels: list[str] = json.loads(issue.labels_json or "[]")
                for lbl in labels:
                    if "/" in lbl:
                        slug, _, _ = lbl.partition("/")
                        if slug not in _TAXONOMY_NAMESPACES:
                            initiative_phase_labels.setdefault(slug, set()).add(lbl)

            for initiative, phase_label_set in initiative_phase_labels.items():
                # Skip if the dep graph already exists for this repo+initiative.
                existing = await session.execute(
                    select(ACInitiativePhase).where(
                        ACInitiativePhase.repo == repo,
                        ACInitiativePhase.initiative == initiative,
                    ).limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    continue

                sorted_phases = sorted(phase_label_set)
                phases: list[PhaseEntry] = [
                    PhaseEntry(
                        label=label,
                        order=idx,
                        depends_on=[sorted_phases[idx - 1]] if idx > 0 else [],
                    )
                    for idx, label in enumerate(sorted_phases)
                ]
                recovery_batch_id = f"batch-{uuid.uuid4().hex[:12]}"

                logger.info(
                    "✅ reseed_missing_initiative_phases: %s/%s — recovered %d phases, batch=%s",
                    repo,
                    initiative,
                    len(phases),
                    recovery_batch_id,
                )
                await persist_initiative_phases(repo, initiative, recovery_batch_id, phases)
    except Exception as exc:
        logger.warning("⚠️  reseed_missing_initiative_phases failed (non-fatal): %s", exc)


async def persist_issue_depends_on(
    repo: str,
    issue_deps: dict[int, list[int]],
) -> None:
    """Write ticket-level dependency lists into the ``issues`` table.

    Called by ``file_issues`` after all ``PlanIssue.depends_on`` references
    have been resolved to real GitHub issue numbers.  Only updates rows that
    exist in the DB (issues created by this plan run will already be present
    from the poller's upsert on the same tick, or we write optimistically and
    accept that a very short race window may mean the row isn't there yet —
    the next poller tick will not overwrite this field).

    Best-effort — swallows exceptions so a DB outage never blocks filing.
    """
    import json as _json

    if not issue_deps:
        return
    try:
        async with get_session() as session:
            for number, blockers in issue_deps.items():
                result = await session.execute(
                    select(ACIssue).where(
                        ACIssue.github_number == number,
                        ACIssue.repo == repo,
                    )
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    row.depends_on_json = _json.dumps(blockers)
            await session.commit()
        logger.info(
            "✅ persist_issue_depends_on: %d issues updated for %s",
            len(issue_deps),
            repo,
        )
    except Exception as exc:
        logger.warning("⚠️  persist_issue_depends_on failed (non-fatal): %s", exc)


def _pr_number_from_url(pr_url: str) -> int | None:
    """Extract PR number from a GitHub PR URL (e.g. .../pull/123 or .../pulls/123)."""
    if not pr_url or not isinstance(pr_url, str):
        return None
    try:
        # Accept .../pull/123 or .../pulls/123
        parts = pr_url.rstrip("/").split("/")
        if len(parts) >= 1 and parts[-1].isdigit():
            return int(parts[-1])
        return None
    except (ValueError, IndexError):
        return None


async def persist_pr_link_and_recompute(
    pr_number: int,
    issue_number: int,
    gh_repo: str,
) -> None:
    """Write an explicit PR↔Issue link and immediately recompute workflow state.

    Called from ``persist_agent_event`` when an agent reports ``build_report_done``
    with a ``pr_url``.  The agent knows exactly which PR belongs to which issue —
    no regex, no inference, no poller cycle needed.

    Writes a confidence-100 ``ACPRIssueLink`` row and a stub ``ACPullRequest``
    row (if none exists yet), then runs ``_recompute_workflow_state`` so the
    board card moves on the next board refresh (every 5 s) rather than waiting
    for the next poller tick.

    The stub ``ACPullRequest`` uses empty-string placeholders for fields only
    available from the GitHub API (title, head_ref).  The poller overwrites the
    stub with real data on its next tick via the normal content-hash diff path.
    The stub's only job is to satisfy ``INV-RUN-PR-1`` immediately so the
    invariant monitor stays green between agent completion and the first poller
    tick.
    """
    now = _now()
    try:
        async with get_session() as session:
            existing_link = (
                await session.execute(
                    select(ACPRIssueLink).where(
                        ACPRIssueLink.repo == gh_repo,
                        ACPRIssueLink.pr_number == pr_number,
                        ACPRIssueLink.issue_number == issue_number,
                        ACPRIssueLink.link_method == "explicit",
                    )
                )
            ).scalar_one_or_none()
            if existing_link is None:
                session.add(
                    ACPRIssueLink(
                        repo=gh_repo,
                        pr_number=pr_number,
                        issue_number=issue_number,
                        link_method="explicit",
                        confidence=100,
                        evidence_json=json.dumps({"source": "build_report_done"}),
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                existing_link.last_seen_at = now

            # Upsert a stub ACPullRequest so INV-RUN-PR-1 is satisfied before
            # the poller's next GitHub sync.  The stub content_hash uses empty
            # placeholders; the poller detects the mismatch and overwrites on
            # its next tick.
            existing_pr = (
                await session.execute(
                    select(ACPullRequest).where(
                        ACPullRequest.github_number == pr_number,
                        ACPullRequest.repo == gh_repo,
                    )
                )
            ).scalar_one_or_none()
            if existing_pr is None:
                stub_head = f"feat/issue-{issue_number}"
                stub_hash = _hash("", "open", "[]", stub_head, "dev", "")
                session.add(
                    ACPullRequest(
                        github_number=pr_number,
                        repo=gh_repo,
                        title="",
                        state="open",
                        head_ref=stub_head,
                        base_ref="dev",
                        is_draft=False,
                        closes_issue_number=issue_number,
                        closes_issue_numbers_json=json.dumps([issue_number]),
                        labels_json="[]",
                        content_hash=stub_hash,
                        body_hash=None,
                        merged_at=None,
                        first_seen_at=now,
                        last_synced_at=now,
                    )
                )

            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  persist_pr_link_and_recompute (link upsert) failed: %s", exc)
        return

    try:
        async with get_session() as session:
            await _recompute_workflow_state(session, gh_repo)
            await session.commit()
        logger.info(
            "✅ persist_pr_link_and_recompute: pr=%d → issue=%d, workflow recomputed",
            pr_number,
            issue_number,
        )
    except Exception as exc:
        logger.warning("⚠️  persist_pr_link_and_recompute (recompute) failed: %s", exc)


async def persist_run_heartbeat(run_id: str) -> datetime.datetime | None:
    """Set last_activity_at = now() for the given run.

    Uses a single UPDATE … RETURNING query — does not load the full row.
    Returns the new timestamp, or None if run_id was not found.
    """
    try:
        now = _now()
        async with get_session() as session:
            result = await session.execute(
                update(ACAgentRun)
                .where(ACAgentRun.id == run_id)
                .values(last_activity_at=now)
                .returning(ACAgentRun.last_activity_at)
            )
            row = result.fetchone()
            await session.commit()
        if row is None:
            return None
        ts: datetime.datetime = row[0]
        logger.info("✅ persist_run_heartbeat: %s last_activity_at=%s", run_id, ts)
        return ts
    except Exception as exc:
        logger.warning("⚠️  persist_run_heartbeat failed: %s", exc)
        return None


async def persist_agent_event(
    issue_number: int,
    event_type: str,
    payload: dict[str, object],
    agent_run_id: str | None = None,
) -> None:
    """Write one structured agent event row to ``agent_events``.

    When ``event_type == "done"`` and ``payload`` contains ``pr_url``, the
    PR number is parsed from the URL and ``persist_pr_link_and_recompute`` is
    called immediately so the board card moves without waiting for the poller.
    If a matching ``ACAgentRun`` exists it is also updated with ``pr_number``.
    """
    from agentception.config import settings

    try:
        async with get_session() as session:
            session.add(
                ACAgentEvent(
                    agent_run_id=agent_run_id,
                    issue_number=issue_number,
                    event_type=event_type,
                    payload=json.dumps(payload),
                    recorded_at=_now(),
                )
            )
            if event_type == "done":
                pr_url = payload.get("pr_url")
                pr_num = _pr_number_from_url(str(pr_url)) if pr_url else None
                if pr_num is not None:
                    # Update ACAgentRun.pr_number when a run exists for this issue.
                    run_id = agent_run_id
                    run_row: ACAgentRun | None = None
                    if run_id:
                        run_row = (
                            await session.execute(
                                select(ACAgentRun).where(ACAgentRun.id == run_id)
                            )
                        ).scalar_one_or_none()
                    if run_row is None and issue_number:
                        run_row = (
                            await session.execute(
                                select(ACAgentRun)
                                .where(ACAgentRun.issue_number == issue_number)
                                .order_by(ACAgentRun.spawned_at.desc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                    if run_row is not None:
                        run_row.pr_number = pr_num
                        run_row.last_activity_at = _now()
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  persist_agent_event failed (non-fatal): %s", exc)

    # Immediately wire the explicit PR↔Issue link and recompute workflow state.
    # This is the authoritative path — no regex or poller cycle needed.
    if event_type == "done":
        pr_url = payload.get("pr_url")
        pr_num = _pr_number_from_url(str(pr_url)) if pr_url else None
        if pr_num is not None and issue_number:
            await persist_pr_link_and_recompute(pr_num, issue_number, settings.gh_repo)


async def persist_agent_messages_async(
    agent_run_id: str,
    messages: list[dict[str, object]],
) -> None:
    """Persist transcript messages without blocking the caller.

    Launched as a background asyncio Task so the tick loop is never delayed
    by transcript I/O.  Errors are swallowed — message loss is preferable to
    a crashed poller.
    """
    asyncio.create_task(_write_messages(agent_run_id, messages))


async def _write_messages(
    agent_run_id: str,
    messages: list[dict[str, object]],
) -> None:
    try:
        async with get_session() as session:
            # Determine the next sequence index to avoid duplicates.
            result = await session.execute(
                select(ACAgentMessage.sequence_index)
                .where(ACAgentMessage.agent_run_id == agent_run_id)
                .order_by(ACAgentMessage.sequence_index.desc())
                .limit(1)
            )
            last_seq = result.scalar_one_or_none()
            start_idx = (last_seq + 1) if last_seq is not None else 0
            now = _now()

            for i, msg in enumerate(list(messages)[start_idx:], start=start_idx):
                session.add(
                    ACAgentMessage(
                        agent_run_id=agent_run_id,
                        role=str(msg.get("role", "unknown")),
                        content=str(msg.get("content", "")) or None,
                        tool_name=str(msg.get("tool_name", "")) or None,
                        sequence_index=i,
                        recorded_at=now,
                    )
                )
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  _write_messages failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Execution plan persistence
# ---------------------------------------------------------------------------


async def persist_execution_plan(run_id: str, plan_json: str, issue_number: int) -> None:
    """Store the serialised ExecutionPlan for *run_id*.

    Called once by the planner before the developer starts.  Subsequent calls
    for the same ``run_id`` are silently ignored (the plan is immutable).

    Args:
        run_id: Agent run identifier (e.g. ``"issue-501"``).
        plan_json: Serialised ``ExecutionPlan`` (``model.model_dump_json()``).
        issue_number: GitHub issue number — stored for efficient index lookups.
    """
    try:
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(ACExecutionPlan).where(ACExecutionPlan.run_id == run_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                logger.info(
                    "✅ persist_execution_plan: plan already exists for run_id=%s — skipping",
                    run_id,
                )
                return
            session.add(
                ACExecutionPlan(
                    run_id=run_id,
                    issue_number=issue_number,
                    plan_json=plan_json,
                    created_at=_now(),
                )
            )
            await session.commit()
            logger.info(
                "✅ persist_execution_plan: stored plan for run_id=%s issue=%d",
                run_id,
                issue_number,
            )
    except Exception as exc:
        logger.warning("⚠️  persist_execution_plan failed (non-fatal): %s", exc)
