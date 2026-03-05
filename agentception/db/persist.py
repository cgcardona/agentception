from __future__ import annotations

"""DB persistence layer called by the AgentCeption poller after each tick.

Strategy per entity type
------------------------
ACPipelineSnapshot  — one row per tick, always (lightweight scalars only).
ACIssue / ACPullRequest — upsert on hash-diff: only write when content changes.
ACAgentRun          — upsert on every tick so status transitions are recorded.
ACAgentMessage      — fire-and-forget async task, never blocks the tick loop.
ACRoleVersion       — insert-if-not-exists on role file content hash.

All writes are wrapped in a single ``try/except`` so a DB outage never takes
down the poller — the dashboard degrades gracefully to filesystem-only mode.
"""

import asyncio
import datetime
import hashlib
import json
import logging
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import select

from agentception.db.engine import get_session
from agentception.db.models import (
    ACAgentEvent,
    ACAgentMessage,
    ACAgentRun,
    ACInitiativePhase,
    ACIssue,
    ACPipelineSnapshot,
    ACPullRequest,
    ACRoleVersion,
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
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  DB persist_tick failed (non-fatal): %s", exc)


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

        if existing is None:
            session.add(
                ACIssue(
                    github_number=num,
                    repo=repo,
                    title=title,
                    body=str(raw.get("body", "")) or None,
                    state=state_str,
                    phase_label=active_label,
                    labels_json=labels_json,
                    content_hash=content_hash,
                    closed_at=closed_at,
                    first_seen_at=now,
                    last_synced_at=now,
                )
            )
        elif existing.content_hash != content_hash or existing.state != state_str:
            # Update when content changed OR state transitioned (open → closed).
            existing.title = title
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


# ---------------------------------------------------------------------------
# PRs (hash-diff upsert)
# ---------------------------------------------------------------------------


async def _upsert_prs(
    session: object,
    prs: list[dict[str, object]],
    repo: str,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = _now()

    for raw in prs:
        num = raw.get("number")
        if not isinstance(num, int):
            continue
        title = str(raw.get("title", ""))
        # Normalise GitHub's uppercase GraphQL state values (OPEN/MERGED/CLOSED) to lowercase.
        state_str = str(raw.get("state", "open")).lower()
        head_ref = raw.get("headRefName")
        labels_raw = raw.get("labels", [])
        label_names: list[str] = []
        if isinstance(labels_raw, list):
            label_names = [
                lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                for lbl in labels_raw
                if isinstance(lbl, (str, dict))
            ]
        labels_json = json.dumps(sorted(label_names))
        content_hash = _hash(title, state_str, labels_json, str(head_ref))

        result = await session.execute(
            select(ACPullRequest).where(
                ACPullRequest.github_number == num, ACPullRequest.repo == repo
            )
        )
        existing = result.scalar_one_or_none()

        merged_at_raw = raw.get("mergedAt")
        merged_at: datetime.datetime | None = None
        if isinstance(merged_at_raw, str):
            try:
                merged_at = datetime.datetime.fromisoformat(
                    merged_at_raw.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        if existing is None:
            session.add(
                ACPullRequest(
                    github_number=num,
                    repo=repo,
                    title=title,
                    state=state_str,
                    head_ref=str(head_ref) if isinstance(head_ref, str) else None,
                    labels_json=labels_json,
                    content_hash=content_hash,
                    merged_at=merged_at,
                    first_seen_at=now,
                    last_synced_at=now,
                )
            )
        elif existing.content_hash != content_hash or existing.state != state_str:
            # Update when content changed OR state transitioned (open → merged/closed).
            existing.title = title
            existing.state = state_str
            existing.head_ref = str(head_ref) if isinstance(head_ref, str) else None
            existing.labels_json = labels_json
            existing.content_hash = content_hash
            if merged_at is not None and existing.merged_at is None:
                existing.merged_at = merged_at
            existing.last_synced_at = now


# ---------------------------------------------------------------------------
# Agent runs (status upsert)
# ---------------------------------------------------------------------------


_ACTIVE_STATUSES = {"implementing", "reviewing", "stale"}
"""Statuses that indicate a run has a live, poller-visible worktree.

Runs in these states that are absent from the current poller tick are
orphaned (the worktree was removed without a clean status transition)
and are flipped to ``unknown`` so the UI does not show phantom agents.

``pending_launch`` is intentionally excluded: pending runs live only in
the DB (the Dispatcher hasn't claimed them yet) and have no poller
presence.  Including them in the sweep would immediately flip them to
``unknown`` before the Dispatcher ever reads them.  The only valid
transition out of ``pending_launch`` is via the acknowledge endpoint.
"""


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
            if existing.status != "pending_launch":
                existing.status = agent.status.value
            existing.pr_number = agent.pr_number
            existing.last_activity_at = now
            # Backfill cognitive_arch when the poller first picks up a live run.
            if existing.cognitive_arch is None and agent.cognitive_arch is not None:
                existing.cognitive_arch = agent.cognitive_arch

    # Orphan sweep: any run that was active in a previous tick but is no
    # longer backed by a live worktree gets flipped to "unknown".  This
    # prevents phantom "implementing" rows from persisting in the Run
    # History after a worktree is removed without a clean shutdown.
    orphan_result = await session.execute(
        select(ACAgentRun).where(
            ACAgentRun.status.in_(_ACTIVE_STATUSES),
        )
    )
    for orphan in orphan_result.scalars().all():
        if orphan.id not in live_ids:
            orphan.status = "unknown"
            orphan.last_activity_at = now
            logger.debug("🧹 Orphan run %s flipped to unknown", orphan.id)


# ---------------------------------------------------------------------------
# Role version snapshot (content-addressed insert-if-new)
# ---------------------------------------------------------------------------


async def persist_role_version(role_name: str, content: str) -> None:
    """Snapshot a role file if its content has changed since last seen.

    Called at startup and whenever a role file is read.  Safe to call
    concurrently — the unique constraint on (role_name, content_hash) acts
    as the idempotency guard.
    """
    content_hash = _hash(content)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACRoleVersion).where(
                    ACRoleVersion.role_name == role_name,
                    ACRoleVersion.content_hash == content_hash,
                )
            )
            if result.scalar_one_or_none() is None:
                session.add(
                    ACRoleVersion(
                        role_name=role_name,
                        content_hash=content_hash,
                        content=content,
                        first_seen_at=_now(),
                    )
                )
                await session.commit()
                logger.info("📸 Role version snapshot: %s (%s…)", role_name, content_hash[:8])
    except Exception as exc:
        logger.warning("⚠️  persist_role_version failed (non-fatal): %s", exc)


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
    node_type: str | None = None,
    logical_tier: str | None = None,
    parent_run_id: str | None = None,
) -> None:
    """Insert an ACAgentRun row with status ``pending_launch`` at dispatch time.

    Called by ``POST /api/build/dispatch`` immediately after the worktree and
    ``.agent-task`` file are created.  The coordinator agent reads these rows
    via ``build_get_pending_launches`` and transitions them to ``implementing``
    when it claims the work.

    ``host_worktree_path`` is stored in the ``spawn_mode`` field as a JSON
    blob because ACAgentRun has no dedicated host-path column — this is the
    least-invasive way to pass it to the coordinator without a migration.

    ``cognitive_arch`` is written to the DB column added in migration 0005 so
    the arch string survives beyond the worktree filesystem lifetime.

    ``node_type`` is the structural position of this agent in the tree:
    ``"coordinator"`` (surveys scope and spawns children) or ``"leaf"``
    (works one issue/PR).  Added in migration 0009.

    ``logical_tier`` is the organisational domain for UI visualisation — e.g.
    ``"qa"``, ``"engineering"``, ``"c-suite"``.  A chain-spawned PR reviewer
    can have ``node_type="leaf"`` and ``logical_tier="qa"`` even though its
    ``parent_run_id`` points to an engineering leaf.  Added in migration 0006,
    repurposed for org domain in migration 0009.

    ``parent_run_id`` records the run_id of the agent that spawned this one,
    enabling spawn-lineage tracing in the org chart.  Added in migration 0006.

    Best-effort — swallows exceptions so a DB outage never blocks dispatch.
    """
    import json as _json

    spawn_mode_json = _json.dumps({"host_worktree": host_worktree_path})
    logger.warning(
        "💾 persist_agent_run_dispatch: run_id=%r role=%r worktree_path=%r "
        "host_worktree_path=%r spawn_mode=%r cognitive_arch=%r "
        "node_type=%r logical_tier=%r parent_run_id=%r",
        run_id, role, worktree_path, host_worktree_path, spawn_mode_json,
        cognitive_arch, node_type, logical_tier, parent_run_id,
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
                # Already exists (re-dispatch or orphan sweep reset). Re-arm.
                existing.status = "pending_launch"
                existing.spawn_mode = spawn_mode_json
                existing.last_activity_at = _now()
                if cognitive_arch is not None:
                    existing.cognitive_arch = cognitive_arch
                if node_type is not None:
                    existing.node_type = node_type
                if logical_tier is not None:
                    existing.logical_tier = logical_tier
                if parent_run_id is not None:
                    existing.parent_run_id = parent_run_id
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
                        pr_number=None,
                        branch=branch,
                        worktree_path=worktree_path,
                        role=role,
                        status="pending_launch",
                        attempt_number=0,
                        spawn_mode=spawn_mode_json,
                        batch_id=batch_id,
                        cognitive_arch=cognitive_arch,
                        node_type=node_type,
                        logical_tier=logical_tier,
                        parent_run_id=parent_run_id,
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

    Called by the coordinator agent via ``POST /api/build/acknowledge/{run_id}``
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


class PhaseEntry(TypedDict):
    """One phase entry for :func:`persist_initiative_phases`."""

    label: str
    order: int
    depends_on: list[str]


async def persist_initiative_phases(
    initiative: str,
    phases: list[PhaseEntry],
) -> None:
    """Upsert the phase DAG and display order for an initiative.

    Called by ``file_issues`` after all GitHub issues have been created.
    Each entry must have:
    - ``"label"``      — scoped phase label, e.g. ``"ac-auth/0-foundation"``
    - ``"order"``      — 0-indexed display position (canonical ordering source)
    - ``"depends_on"`` — list of scoped phase labels that must complete first

    Uses upsert semantics so re-running a plan with the same initiative
    replaces the old dep graph rather than failing with a duplicate key.
    Best-effort — swallows exceptions so a DB outage never blocks filing.
    """
    import json as _json

    try:
        now = _now()
        async with get_session() as session:
            for phase in phases:
                label = phase["label"]
                order = phase["order"]
                depends_on = phase["depends_on"]
                result = await session.execute(
                    select(ACInitiativePhase).where(
                        ACInitiativePhase.initiative == initiative,
                        ACInitiativePhase.phase_label == label,
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is not None:
                    existing.phase_order = order
                    existing.depends_on_json = _json.dumps(depends_on)
                else:
                    session.add(
                        ACInitiativePhase(
                            initiative=initiative,
                            phase_label=label,
                            phase_order=order,
                            depends_on_json=_json.dumps(depends_on),
                            created_at=now,
                        )
                    )
            await session.commit()
        logger.info(
            "✅ persist_initiative_phases: %s — %d phases written",
            initiative,
            len(phases),
        )
    except Exception as exc:
        logger.warning("⚠️  persist_initiative_phases failed (non-fatal): %s", exc)


async def persist_agent_event(
    issue_number: int,
    event_type: str,
    payload: dict[str, object],
    agent_run_id: str | None = None,
) -> None:
    """Write one structured agent event row to ``agent_events``.

    Called by the ``build_report_*`` HTTP endpoints when a running agent
    signals a lifecycle change.  The optional ``agent_run_id`` is included
    if the caller can resolve it; otherwise only ``issue_number`` is set.
    """
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
            await session.commit()
    except Exception as exc:
        logger.warning("⚠️  persist_agent_event failed (non-fatal): %s", exc)


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
