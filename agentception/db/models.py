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

    tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """Behavioral execution tier for this agent run.

    Values: ``executive`` | ``coordinator`` | ``engineer`` | ``reviewer``.
    Written from the ``TIER=`` field in the ``.agent-task`` file.
    Null for rows spawned before migration 0012.
    """

    org_domain: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """Organisational slot for UI hierarchy visualisation.

    Values: ``c-suite`` | ``engineering`` | ``qa``.
    Written from the ``ORG_DOMAIN=`` field in the ``.agent-task`` file.
    Allows the UI to place a chain-spawned PR reviewer under the QA column
    even though its physical ``parent_run_id`` points to an engineering leaf.
    Null for rows spawned before migration 0012.
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

    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON-serialised list of GitHub issue numbers this issue depends on (must merge first)."""

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
    base_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Target branch (e.g. ``dev``, ``main``).  Added for base-mismatch detection."""

    is_draft: Mapped[bool] = mapped_column(Integer, nullable=False, default=False)
    """Whether the PR is a GitHub draft.  Stored as 0/1 for SQLite compat."""

    closes_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closes_issue_numbers_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    """JSON array of all issue numbers referenced by Closes/Fixes/Resolves keywords."""

    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """SHA-256 of normalised body text — enables body-change detection independently of content_hash."""

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
    Read by ``get_initiative_phase_meta`` to compute the display order and
    ``locked`` flag on the Build board swim lanes.

    ``phase_order`` is the canonical display position (0-indexed).  It is the
    single source of truth for phase ordering — the board always sorts by this
    column, never by label strings.

    When no rows exist for an initiative every phase is shown as unlocked and
    ordering falls back to lexicographic sort of the scoped phase labels —
    correct for plans that use the ``{N}-{slug}`` label convention.
    """

    __tablename__ = "initiative_phases"

    initiative: Mapped[str] = mapped_column(String(256), primary_key=True)
    phase_label: Mapped[str] = mapped_column(String(256), primary_key=True)
    phase_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """0-indexed display position within the initiative."""
    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON list of scoped phase label strings this phase waits for."""
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


# ---------------------------------------------------------------------------
# ACPRIssueLink — explicit, auditable PR↔Issue linkage
# ---------------------------------------------------------------------------


class ACPRIssueLink(Base):
    """One row per candidate PR↔Issue association, with provenance.

    Multiple link methods may produce rows for the same (repo, pr_number,
    issue_number) triple; the unique constraint deduplicates.  The linker
    picks the best link per issue based on confidence and PR state.
    """

    __tablename__ = "pr_issue_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String(256), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)

    link_method: Mapped[str] = mapped_column(String(64), nullable=False)
    """How the link was discovered.

    Values: ``explicit`` | ``body_closes`` | ``branch_regex`` |
    ``run_pr_number`` | ``title_mention`` | ``unknown``
    """

    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """0–100 score — higher means more reliable signal."""

    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """JSON blob describing the evidence (matched text, run_id, regex capture, etc.)."""

    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "repo", "pr_number", "issue_number", "link_method",
            name="uq_pr_issue_links",
        ),
        Index("ix_pr_issue_links_issue", "repo", "issue_number"),
        Index("ix_pr_issue_links_pr", "repo", "pr_number"),
    )


# ---------------------------------------------------------------------------
# ACIssueWorkflowState — canonical, persisted swim-lane state per issue
# ---------------------------------------------------------------------------


class ACIssueWorkflowState(Base):
    """One row per (repo, issue_number) — the UI's source of truth for swim lanes.

    Computed idempotently each tick from DB signals (issues, PRs, runs,
    pr_issue_links).  The board reads this table instead of re-inferring
    lanes at request time.
    """

    __tablename__ = "issue_workflow_state"

    repo: Mapped[str] = mapped_column(String(256), primary_key=True)
    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)

    initiative: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Denormalised for fast initiative-scoped queries."""

    phase_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Scoped phase label (e.g. ``ac-ui/0-critical-bugs``)."""

    lane: Mapped[str] = mapped_column(String(32), nullable=False, default="todo")
    """Canonical swim lane: ``todo`` | ``active`` | ``pr_open`` | ``reviewing`` | ``done``."""

    issue_state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    """GitHub issue state (``open`` | ``closed``), with stabilisation rules applied."""

    run_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """FK-like reference to the most relevant ``ac_agent_runs.id``."""

    agent_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Computed agent status (implementing, reviewing, pending_launch, stale, done, unknown)."""

    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Best-linked PR number, if any."""

    pr_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """State of the best-linked PR (open, merged, closed, draft, unknown)."""

    pr_base: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Target branch of the best-linked PR."""

    pr_head_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Source branch of the best-linked PR."""

    pr_link_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """How the PR was linked to this issue (see ACPRIssueLink.link_method)."""

    pr_link_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Confidence score of the PR link (0–100)."""

    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON array of warning strings surfaced to the maintainer."""

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """Hash of canonical state fields — update guard to avoid no-op writes."""

    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_issue_workflow_state_lane", "lane"),
        Index("ix_issue_workflow_state_initiative", "initiative"),
        Index("ix_issue_workflow_state_phase", "phase_key"),
    )
