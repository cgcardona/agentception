from __future__ import annotations

"""Domain: shared type definitions for query return values."""

from typing import TypedDict



class LabelEntry(TypedDict):
    """Single label object as returned by the GitHub API shape."""

    name: str



class BoardIssueRow(TypedDict):
    """One row from get_board_issues."""

    number: int
    title: str
    state: str
    labels: list[LabelEntry]
    claimed: bool
    phase_label: str | None
    last_synced_at: str



class PipelineTrendRow(TypedDict):
    """One snapshot row from get_pipeline_trend."""

    polled_at: str
    active_label: str | None
    issues_open: int
    prs_open: int
    agents_active: int
    alert_count: int



class AgentRunRow(TypedDict):
    """One row from get_agent_run_history."""

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
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None



class AgentMessageRow(TypedDict):
    """One transcript message row from get_agent_run_detail."""

    role: str
    content: str | None
    tool_name: str | None
    sequence_index: int
    recorded_at: str



class AgentRunDetail(TypedDict):
    """Full detail dict from get_agent_run_detail."""

    id: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    role: str
    status: str
    spawned_at: str
    last_activity_at: str | None
    completed_at: str | None
    batch_id: str | None
    cognitive_arch: str | None
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None
    messages: list[AgentMessageRow]



class SiblingRunRow(TypedDict):
    """Minimal sibling agent run info for the lineage panel."""

    id: str
    role: str
    status: str
    issue_number: int | None
    tier: str | None



class AgentRunTeardownRow(TypedDict, total=False):
    """Minimal agent run fields needed to tear down a worktree after completion."""

    worktree_path: str | None
    branch: str | None
    plan_branch: str | None



class OpenPRRow(TypedDict):
    """One row from get_open_prs_db."""

    number: int
    title: str
    state: str
    headRefName: str | None
    labels: list[LabelEntry]



class LinkedPRRow(TypedDict):
    """Linked PR summary embedded in IssueDetailRow."""

    number: int
    title: str
    state: str
    head_ref: str | None
    merged_at: str | None



class IssueAgentRunRow(TypedDict):
    """Agent run summary embedded in IssueDetailRow."""

    id: str
    role: str
    status: str
    branch: str | None
    pr_number: int | None
    spawned_at: str
    last_activity_at: str | None



class IssueDetailRow(TypedDict):
    """Full detail dict from get_issue_detail."""

    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    phase_label: str | None
    claimed: bool
    first_seen_at: str
    last_synced_at: str
    closed_at: str | None
    linked_prs: list[LinkedPRRow]
    agent_runs: list[IssueAgentRunRow]



class AllIssueRow(TypedDict):
    """One row from get_all_issues."""

    number: int
    title: str
    state: str
    labels: list[str]
    phase_label: str | None
    closed_at: str | None
    last_synced_at: str



class LinkedIssueRow(TypedDict):
    """Linked issue summary embedded in PRDetailRow."""

    number: int
    title: str
    state: str



class PRAgentRunRow(TypedDict):
    """Agent run summary embedded in PRDetailRow."""

    id: str
    role: str
    status: str
    branch: str | None
    issue_number: int | None
    spawned_at: str
    last_activity_at: str | None



class PRDetailRow(TypedDict):
    """Full detail dict from get_pr_detail."""

    number: int
    title: str
    state: str
    head_ref: str | None
    labels: list[str]
    closes_issue_number: int | None
    merged_at: str | None
    first_seen_at: str
    last_synced_at: str
    linked_issue: LinkedIssueRow | None
    agent_runs: list[PRAgentRunRow]



class AllPRRow(TypedDict):
    """One row from get_all_prs."""

    number: int
    title: str
    state: str
    head_ref: str | None
    labels: list[str]
    closes_issue_number: int | None
    merged_at: str | None
    last_synced_at: str



class ShipReviewerRunRow(TypedDict):
    """Latest reviewer run attached to a PR on the Ship board."""

    id: str
    status: str
    spawned_at: str
    last_activity_at: str | None



class ShipPRRow(TypedDict):
    """Enriched PR entry for the Ship board."""

    number: int
    title: str
    state: str
    head_ref: str | None
    url: str
    labels: list[str]
    closes_issue_number: int | None
    merged_at: str | None
    phase_label: str | None
    reviewer_run: ShipReviewerRunRow | None



class ShipPhaseGroupRow(TypedDict):
    """PRs grouped by phase label for the Ship board."""

    label: str
    prs: list[ShipPRRow]



class WaveAgentRow(TypedDict):
    """One agent entry inside a WaveRow."""

    id: str
    role: str
    status: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    batch_id: str | None
    worktree_path: str | None
    cognitive_arch: str | None
    message_count: int



class WaveRow(TypedDict):
    """One wave from get_waves_from_db."""

    batch_id: str
    started_at: float
    ended_at: float | None
    issues_worked: list[int]
    prs_opened: int
    prs_merged: int
    estimated_tokens: int
    estimated_cost_usd: float
    agents: list[WaveAgentRow]



class ConductorHistoryRow(TypedDict):
    """One entry from get_conductor_history."""

    wave_id: str
    worktree: str
    host_worktree: str
    started_at: str
    status: str



class PhasedIssueRow(TypedDict):
    """One issue entry inside a PhaseGroupRow."""

    number: int
    title: str
    body_excerpt: str
    """First ~120 chars of the issue body, markdown stripped — used as a card subtitle."""
    state: str
    url: str
    labels: list[str]
    depends_on: list[int]
    """GitHub issue numbers this issue must wait for (ticket-level dependencies)."""



class PhaseGroupRow(TypedDict):
    """One phase bucket from get_issues_grouped_by_phase."""

    label: str
    issues: list[PhasedIssueRow]
    locked: bool
    complete: bool
    depends_on: list[str]



class OpenPRForIssueRow(TypedDict):
    """An open GitHub PR associated with a board issue.

    Returned by ``get_open_prs_by_issue`` which uses this as the authoritative
    signal for placing issues in the ``pr_open`` or ``reviewing`` swim lane.
    Two matching strategies are used (either is sufficient):
    1. ``closes_issue_number`` — explicit ``Closes #N`` link in the PR body.
    2. ``head_ref`` matching ``feat/issue-{N}-*`` — branch naming convention.
    """

    pr_number: int
    head_ref: str | None



class WorkflowStateRow(TypedDict):
    """Canonical workflow state for a board issue, read from ``ac_issue_workflow_state``.

    This is the UI's source of truth for swim lanes — no ad-hoc inference.
    """

    lane: str
    issue_state: str
    run_id: str | None
    agent_status: str | None
    pr_number: int | None
    pr_state: str | None
    pr_base: str | None
    pr_head_ref: str | None
    pr_link_method: str | None
    pr_link_confidence: int | None
    warnings: list[str]



class RunForIssueRow(TypedDict):
    """Most-recent run entry from get_runs_for_issue_numbers.

    ``agent_status`` is a normalized, stale-aware status string suitable for
    CSS class suffixes: ``implementing`` | ``reviewing`` | ``done`` | ``stale``
    | ``unknown`` | other DB values lower-cased.  A run is ``stale`` when its
    status is active but ``last_activity_at`` is older than
    ``_STALE_THRESHOLD_SECONDS``.
    """

    id: str
    role: str
    cognitive_arch: str | None
    status: str
    agent_status: str
    pr_number: int | None
    branch: str | None
    spawned_at: str
    last_activity_at: str | None
    current_step: str | None
    steps_completed: int
    tier: str | None
    org_domain: str | None
    batch_id: str | None



class RunTreeNodeRow(TypedDict):
    """One node in the agent run tree, returned by ``get_run_tree_by_batch_id``.

    The flat list can be assembled into a tree client-side by following
    ``parent_run_id`` references.
    """

    id: str
    role: str
    status: str
    agent_status: str
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None
    issue_number: int | None
    pr_number: int | None
    batch_id: str | None
    spawned_at: str
    last_activity_at: str | None
    current_step: str | None


class BatchSummaryRow(TypedDict):
    """Summary for one dispatch batch, used by the org live API.

    Returned by ``get_batch_summaries_for_initiative``.  Ordered newest-first.
    """

    batch_id: str
    spawned_at: str  # ISO datetime of the earliest run in the batch
    total_count: int  # total number of runs in the batch
    active_count: int  # runs currently in a live status



class _RunStepData(TypedDict):
    """Internal shape returned by ``_get_step_data_for_runs``."""

    current_step: str | None
    steps_completed: int



class PendingLaunchRow(TypedDict):
    """One pending launch from get_pending_launches."""

    run_id: str
    issue_number: int | None
    role: str
    branch: str | None
    worktree_path: str | None
    host_worktree_path: str | None
    batch_id: str | None
    spawned_at: str
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None



class AgentEventRow(TypedDict):
    """One structured event from get_agent_events_tail.

    ``payload`` is the raw JSON string stored in the DB — callers must
    parse it with ``json.loads`` if they need the structured payload.
    """

    id: int
    event_type: str
    payload: str
    recorded_at: str



class AgentThoughtRow(TypedDict):
    """One transcript message from get_agent_thoughts_tail."""

    seq: int
    role: str
    content: str
    tool_name: str  # empty string when absent
    recorded_at: str



class InitiativePhaseMeta(TypedDict):
    """Metadata for one phase of an initiative, read from ``initiative_phases``."""

    label: str
    """Scoped phase label, e.g. ``"ac-auth/0-foundation"``."""
    order: int
    """0-indexed canonical display position."""
    depends_on: list[str]
    """Scoped phase labels that must be complete before this phase unlocks."""




class PhaseSummary(TypedDict):
    """A phase sub-label and its open-issue count, for the launch modal picker."""

    label: str
    count: int
    blocked: bool  # True when every open issue in the phase carries "blocked/deps"



class IssueSummary(TypedDict):
    """A minimal open-issue descriptor, for the launch modal single-issue picker."""

    number: int
    title: str
    blocked: bool  # True when the issue carries the "blocked/deps" label



class LabelContext(TypedDict):
    """Data package returned by ``get_label_context`` to populate the launch modal."""

    phases: list[PhaseSummary]
    issues: list[IssueSummary]



class BlockedDepsRow(TypedDict):
    """One open issue that carries the ``blocked/deps`` label and has dependencies."""

    github_number: int
    dep_numbers: list[int]



class InitiativeIssueRow(TypedDict):
    """One issue entry inside an InitiativePhaseRow, for the shareable plan view."""

    number: int
    title: str
    url: str
    state: str
    """``"open"`` or ``"closed"``."""



class InitiativePhaseRow(TypedDict):
    """One phase in an InitiativeSummary, for the shareable plan view."""

    label: str
    """Scoped phase label, e.g. ``"auth-rewrite/0-foundation"``."""
    short_label: str
    """Unscoped display label, e.g. ``"0-foundation"``."""
    order: int
    is_active: bool
    """Not locked by unmet deps and not yet complete."""
    is_complete: bool
    """All issues in this phase are closed."""
    issues: list[InitiativeIssueRow]



class InitiativeSummary(TypedDict):
    """Full summary for the shareable /plan/{org}/{repo}/{initiative}/{batch_id} page."""

    repo: str
    initiative: str
    batch_id: str
    phase_count: int
    issue_count: int
    open_count: int
    closed_count: int
    filed_at: str | None
    """ISO datetime of the earliest phase creation — the filing timestamp."""
    phases: list[InitiativePhaseRow]



class TerminalRunRow(TypedDict):
    """Minimal run fields needed by the worktree reaper."""

    id: str
    worktree_path: str
    branch: str | None



class RunSummaryRow(TypedDict):
    """Lightweight run summary for MCP query tools.

    Intentionally omits transcript messages (use get_agent_run_detail when
    the full message history is needed).
    """

    run_id: str
    status: str
    role: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    worktree_path: str | None
    batch_id: str | None
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None
    spawned_at: str
    last_activity_at: str | None
    completed_at: str | None



class RunContextRow(TypedDict):
    """Full task context for an agent run — the authoritative DB-sourced record.

    Used by ``ac://runs/{run_id}/context`` and the ``task/briefing`` MCP prompt.
    All fields an agent needs to understand its assignment are present here;
    context is read exclusively from the DB.
    """

    run_id: str
    status: str
    role: str
    cognitive_arch: str | None
    task_description: str | None
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    worktree_path: str | None
    batch_id: str | None
    tier: str | None
    org_domain: str | None
    parent_run_id: str | None
    gh_repo: str | None
    is_resumed: bool
    coord_fingerprint: str | None
    spawned_at: str
    last_activity_at: str | None
    completed_at: str | None
    pr_base_branch: str | None
    """When set (plan-scoped run), open the PR against this branch instead of dev."""


class StatusCountRow(TypedDict):
    """Status → count pair for aggregate queries."""

    status: str
    count: int



class DailyMetrics(TypedDict):
    """KPI snapshot for a single calendar day."""

    date: str
    issues_closed: int
    prs_merged: int
    reviewer_runs: int
    grade_a_count: int
    grade_b_count: int
    grade_c_count: int
    grade_d_count: int
    grade_f_count: int
    first_pass_rate: float
    rework_rate: float
    avg_iterations: float
    max_iter_hit_count: int
    avg_cycle_time_seconds: float
    cost_usd: float
    cost_per_issue_usd: float
    redispatch_count: int
    auto_merge_rate: float

