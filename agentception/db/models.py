from __future__ import annotations

"""AgentCeption ORM models — all tables — no prefix needed since this is a standalone app.

Entity hierarchy
----------------
ACWave
    One row per "Start Wave" click (or any batch spawn).  Groups ACAgentRuns.

ACAgentRun
    One row per agent working one issue in one wave.  Tracks the full lifecycle
    from spawn to completion.  FK to ACWave (nullable for manually-spawned runs).

ACIssue
    Mirror of a GitHub issue, refreshed on every tick via hash-diff so we only
    write when fields change.  Preserves history across state transitions.

ACPullRequest
    Mirror of a GitHub PR, same hash-diff strategy as ACIssue.

ACAgentMessage
    One row per message in an agent's Cursor transcript.  Written async so it
    never blocks the tick loop.  Enables full-text search and heuristics.

ACRoleVersion
    Content-addressed snapshot of a role prompt file.  New row only when the
    SHA-256 hash of the file content changes — tracks prompt evolution over time.

ACPipelineSnapshot
    Time-series: one row per poller tick.  Lightweight (no text blobs).
    Enables trend charts, SLA analysis, and anomaly detection.

ACTaskRun
    One row per agent task dispatched outside the GitHub issue workflow
    (e.g. cognitive-arch enrichment, batch file editing).  Created pending
    when the task file is generated; updated to completed/failed when the
    agent commits or times out.  Physical task file deleted on completion.
"""

import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentception.db.base import Base


# ---------------------------------------------------------------------------
# ACWave — one per batch spawn
# ---------------------------------------------------------------------------


class ACWave(Base):
    """A batch spawn operation — one "Start Wave" click = one ACWave."""

    __tablename__ = "waves"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    """BATCH_ID from the .agent-task file (e.g. ``eng-20260302T084507Z-16da``)."""

    phase_label: Mapped[str] = mapped_column(String(256), nullable=False)
    """Active phase label at spawn time (e.g. ``ac-ui/0-critical-bugs``)."""

    role: Mapped[str] = mapped_column(String(128), nullable=False)
    """Agent role used for this wave (e.g. ``python-developer``)."""

    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    spawn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Number of agents successfully spawned."""

    skip_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Number of issues skipped (already claimed or worktree existed)."""

    agent_runs: Mapped[list[ACAgentRun]] = relationship(
        "ACAgentRun", back_populates="wave", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# ACAgentRun — one per agent / issue
# ---------------------------------------------------------------------------


class ACAgentRun(Base):
    """Lifecycle of one agent working one issue."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    """Worktree basename (e.g. ``issue-732``) or generated UUID for manual spawns."""

    wave_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("waves.id"), nullable=True, index=True
    )
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    """IMPLEMENTING | REVIEWING | DONE | STALE | UNKNOWN"""

    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spawn_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    """JSON blob written by persist_agent_run_dispatch: {"host_worktree": "/path/..."}."""

    batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    cognitive_arch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Cognitive architecture string at spawn time, e.g. ``guido_van_rossum:python``."""

    node_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """Structural position in the agent tree.

    Values: ``coordinator`` | ``leaf``.
    A coordinator surveys its scope and spawns children; a leaf works one issue/PR.
    Null for rows created before migration 0009.  Populated by migration 0009 from
    the ``coordinator | leaf`` values that were previously stored in ``logical_tier``.
    """

    logical_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """Organisational domain for UI visualisation.

    Free string written by the spawning agent — e.g. ``"qa"``, ``"engineering"``,
    ``"c-suite"``.  Allows the UI to place a chain-spawned PR reviewer under the
    QA branch even though its physical ``parent_run_id`` points to an engineering
    leaf.  Null for rows created before migration 0009.
    """

    parent_run_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    """Run ID of the agent that physically spawned this one (spawn-lineage tracking).

    Null for top-level dispatches and legacy rows.
    """

    spawned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_activity_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    wave: Mapped[ACWave | None] = relationship("ACWave", back_populates="agent_runs")
    messages: Mapped[list[ACAgentMessage]] = relationship(
        "ACAgentMessage", back_populates="agent_run", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# ACIssue — GitHub issue mirror (hash-diff sync)
# ---------------------------------------------------------------------------


class ACIssue(Base):
    """Mirror of a GitHub issue, refreshed on every poller tick via hash-diff.

    Only written when ``content_hash`` changes, so the row always reflects the
    latest GitHub state without hammering the DB on every tick.
    """

    __tablename__ = "issues"

    github_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo: Mapped[str] = mapped_column(String(256), nullable=False, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    """open | closed"""

    phase_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Active phase label at the time of last sync."""

    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON-serialised list of label name strings."""

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 of (title + state + labels_json) — write guard for hash-diff."""

    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_issues_state", "state"),
        Index("ix_issues_phase_label", "phase_label"),
    )


# ---------------------------------------------------------------------------
# ACPullRequest — GitHub PR mirror (hash-diff sync)
# ---------------------------------------------------------------------------


class ACPullRequest(Base):
    """Mirror of a GitHub pull request, refreshed on every tick via hash-diff."""

    __tablename__ = "pull_requests"

    github_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo: Mapped[str] = mapped_column(String(256), nullable=False, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    """open | closed | merged"""

    head_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    closes_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_pull_requests_state", "state"),)


# ---------------------------------------------------------------------------
# ACAgentMessage — full transcript (written async)
# ---------------------------------------------------------------------------


class ACAgentMessage(Base):
    """One message from a Cursor agent transcript.

    Written asynchronously so reading + persisting transcripts never blocks
    the 5-second tick loop.  Enables full-text search and ML feature extraction.
    """

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    """user | assistant | tool_call | tool_result | thinking"""

    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    agent_run: Mapped[ACAgentRun] = relationship("ACAgentRun", back_populates="messages")

    __table_args__ = (
        Index("ix_agent_messages_run_seq", "agent_run_id", "sequence_index"),
    )


# ---------------------------------------------------------------------------
# ACRoleVersion — content-addressed role prompt snapshots
# ---------------------------------------------------------------------------


class ACRoleVersion(Base):
    """Content-addressed snapshot of a role prompt file.

    A new row is inserted only when the SHA-256 hash of the file content
    changes, making this a full audit trail of every prompt change over time.
    """

    __tablename__ = "role_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("role_name", "content_hash", name="uq_role_versions"),
    )


# ---------------------------------------------------------------------------
# ACPipelineSnapshot — time-series tick state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ACAgentEvent — structured MCP callback events
# ---------------------------------------------------------------------------


class ACAgentEvent(Base):
    """One row per deliberate signal pushed by a running agent.

    Agents call the ``build_report_*`` HTTP endpoints (or MCP tools once the
    stdio transport is wired) to push typed events: step start, blocker
    encountered, architectural decision made, or work done.

    This is separate from the raw thinking stream in :class:`ACAgentMessage` —
    these are *intentional* structured reports, not passive transcript reads.
    """

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str | None] = mapped_column(
        String(512), ForeignKey("agent_runs.id"), nullable=True, index=True
    )
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """step_start | blocker | decision | done"""

    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """JSON-encoded dict — schema varies by event_type."""

    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# ACInitiativePhase — phase dependency graph per initiative
# ---------------------------------------------------------------------------


class ACInitiativePhase(Base):
    """One row per phase per initiative — the DAG declared in the PlanSpec.

    Written by ``persist_initiative_phases`` when ``file_issues`` completes.
    Read by ``get_initiative_phase_deps`` to compute the ``locked`` flag on
    the Build board swim lanes.

    When no rows exist for an initiative every phase is shown as unlocked,
    which is the correct default for plans created before this feature.
    """

    __tablename__ = "initiative_phases"

    initiative: Mapped[str] = mapped_column(String(256), primary_key=True)
    phase_label: Mapped[str] = mapped_column(String(256), primary_key=True)
    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON list of phase label strings, e.g. ``'["phase-0"]'``."""
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_initiative_phases_initiative", "initiative"),
    )


# ---------------------------------------------------------------------------
# ACTaskRun — ephemeral agent task lifecycle record
# ---------------------------------------------------------------------------


class ACTaskRun(Base):
    """One row per agent task dispatched outside the GitHub issue workflow.

    Created when a task file is generated (status=pending), updated to
    completed when the agent commits, or failed if the physical file is
    found without a corresponding commit after a timeout.  The physical
    .agent-task file is deleted on transition to completed/failed so the
    tasks directory never accumulates stale files.

    Intentionally separate from ACAgentRun, which is tightly coupled to
    the GitHub issue/PR lifecycle.  ACTaskRun covers batch file-editing
    jobs, cognitive-arch enrichment runs, and any future non-issue tasks.
    """

    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    """Stable task identifier, e.g. ``cog-arch-systems-language-designers``."""

    task_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    """Category of task, e.g. ``cognitive-arch-enrichment``."""

    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Git branch the agent committed to, e.g. ``agent/cog-arch-systems-language-designers``."""

    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    """SHA of the agent's commit when status=completed."""

    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """Task-specific metadata as JSON (figures list, batch name, etc.)."""

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    """pending | completed | failed"""

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ACPipelineSnapshot — tick-level time series
# ---------------------------------------------------------------------------


class ACPipelineSnapshot(Base):
    """One row per poller tick — lightweight time-series of pipeline health.

    No text blobs; stores only scalar counts and the active label.
    Use for trend charts, SLA analysis, and anomaly detection.
    """

    __tablename__ = "pipeline_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    polled_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    active_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    issues_open: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prs_open: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agents_active: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alerts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON array of alert strings from the tick."""
