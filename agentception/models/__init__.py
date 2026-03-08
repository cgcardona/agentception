from __future__ import annotations

"""Domain models for the AgentCeption dashboard.

These types are the shared contract between the background poller, the API
routes, and the frontend templates. Keep them flat — no nested Pydantic
models that reference external services.
"""

import logging
import re
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# Path to the canonical role taxonomy — three directories up from this file (repo root).
# __file__ = agentception/models/__init__.py → .parent×3 = repo root
_TAXONOMY_PATH: Path = (
    Path(__file__).parent.parent.parent / "scripts" / "gen_prompts" / "role-taxonomy.yaml"
)


def _load_valid_roles() -> frozenset[str]:
    """Load spawnable role slugs from role-taxonomy.yaml at import time.

    Extracts every entry with ``spawnable: true`` from the three-tier org
    hierarchy and returns their slugs as an immutable set. This keeps
    ``VALID_ROLES`` automatically in sync with the taxonomy without manual
    list maintenance.

    Logs a warning and returns an empty frozenset when the taxonomy file is
    absent (e.g. during tests that run against an isolated module).
    """
    if not _TAXONOMY_PATH.exists():
        logger.warning(
            "⚠️ role-taxonomy.yaml not found at %s — VALID_ROLES will be empty",
            _TAXONOMY_PATH,
        )
        return frozenset()
    raw: object = yaml.safe_load(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        logger.warning("⚠️ role-taxonomy.yaml has unexpected structure — VALID_ROLES will be empty")
        return frozenset()
    roles: set[str] = set()
    for level in raw.get("levels", []):
        if not isinstance(level, dict):
            continue
        for role in level.get("roles", []):
            if isinstance(role, dict) and role.get("spawnable") is True:
                slug = role.get("slug")
                if isinstance(slug, str):
                    roles.add(slug)
    return frozenset(roles)


#: Roles that can be assigned to a spawned leaf agent via POST /api/control/spawn.
#: Derived dynamically from ``scripts/gen_prompts/role-taxonomy.yaml`` (spawnable: true entries).
#: Orchestration roles (cto, engineering-coordinator, qa-coordinator, coordinator) are
#: spawnable only through the CTO pipeline — their taxonomy entries have spawnable: false.
VALID_ROLES: frozenset[str] = _load_valid_roles()


class AgentStatus(str, Enum):
    """Lifecycle state of a single pipeline agent, derived from filesystem + GitHub signals.

    Mirror of :class:`agentception.workflow.status.AgentStatus` — kept in sync.
    """

    PENDING_LAUNCH = "pending_launch"
    IMPLEMENTING = "implementing"
    BLOCKED = "blocked"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"
    FAILED = "failed"


class AgentNode(BaseModel):
    """A single agent in the pipeline tree.

    Represents one Cursor/Claude agent instance that is either actively working
    or has completed its assigned task. Children are spawned sub-agents.

    ``tier`` is the behavioral execution tier:
    - ``"coordinator"`` — surveys its scope and spawns children; any coordinator
                          can be the tree root.
    - ``"engineer"``    — leaf worker; implements one issue.
    - ``"reviewer"``    — leaf reviewer; reviews one PR.

    ``org_domain`` is the organisational slot for UI hierarchy visualisation —
    ``"c-suite"``, ``"engineering"``, or ``"qa"``.  A chain-spawned PR reviewer
    that was seeded by an engineering leaf will have ``tier="reviewer"`` and
    ``org_domain="qa"`` so the board places it under the QA column rather than
    under its physical parent.

    ``parent_run_id`` is the run_id of the agent that physically spawned this one.
    """

    id: str
    role: str
    status: AgentStatus
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    batch_id: str | None = None
    worktree_path: str | None = None
    transcript_path: str | None = None
    message_count: int = 0
    last_activity_mtime: float = 0.0
    children: list[AgentNode] = []
    cognitive_arch: str | None = None
    tier: str | None = None
    org_domain: str | None = None
    parent_run_id: str | None = None


class StaleClaim(BaseModel):
    """A GitHub issue with ``agent/wip`` label but no corresponding local worktree.

    Produced by :func:`~agentception.intelligence.guards.detect_stale_claims`
    and included in :class:`PipelineState` so the dashboard can surface a
    one-click "Clear Label" fix button for each stale claim.
    """

    issue_number: int
    issue_title: str
    worktree_path: str  # expected path that does not exist


class BoardIssue(BaseModel):
    """Lightweight issue summary for the overview board sidebar.

    Populated from ``ac_issues`` (Postgres) by the poller and carried in every
    SSE broadcast so the sidebar updates live without page reloads or HTMX
    polling.  Only fields needed for the board card are included; full issue
    detail is always available on GitHub.
    """

    number: int
    title: str
    state: str = "open"
    labels: list[str] = []
    claimed: bool = False
    phase_label: str | None = None
    last_synced_at: str | None = None


class PlanDraftEvent(BaseModel):
    """A single plan-draft lifecycle event emitted by the poller.

    Carried in ``PipelineState.plan_draft_events`` so SSE subscribers receive
    exactly-once delivery: the poller adds the event on the tick it is first
    detected and deduplicates via an in-memory set so subsequent ticks carry
    an empty list for the same draft.

    ``event`` is one of ``"plan_draft_ready"`` or ``"plan_draft_timeout"``.
    ``yaml_text`` is the raw YAML written by the Cursor agent (filled for
    ``plan_draft_ready``, empty string for ``plan_draft_timeout``).
    ``output_path`` is the absolute path of the expected output file.
    """

    event: str
    draft_id: str
    yaml_text: str = ""
    output_path: str


class PipelineState(BaseModel):
    """Snapshot of the entire AgentCeption pipeline at a point in time.

    Aggregated by the background poller and served to the dashboard UI.
    ``polled_at`` is a UNIX timestamp — compare with ``time.time()`` to know
    how stale the data is.
    ``stale_claims`` provides structured data for the "Clear Label" UI action;
    the same claims also appear as human-readable strings in ``alerts``.
    ``board_issues`` carries the unclaimed issues for the active phase so the
    sidebar updates via SSE without any extra requests.

    SSE-expanded fields (updated every tick from Postgres):
    ``closed_issues_count`` — issues closed in the last 24 hours.
    ``merged_prs_count`` — PRs merged in the last 24 hours.
    ``stale_branches`` — local git branch names that match feat/issue-N but
    have no corresponding live worktree (leftover from failed/manual runs).

    ``plan_draft_events`` carries new plan-draft lifecycle events for this
    tick only.  The poller deduplicates across ticks via an in-memory set,
    so a given draft_id appears at most once in the SSE stream.
    """

    active_label: str | None
    issues_open: int
    prs_open: int
    agents: list[AgentNode]
    alerts: list[str] = []
    stale_claims: list[StaleClaim] = []
    board_issues: list[BoardIssue] = []
    polled_at: float
    closed_issues_count: int = 0
    merged_prs_count: int = 0
    stale_branches: list[str] = []
    pending_approval: list[dict[str, object]] = []
    plan_draft_events: list[PlanDraftEvent] = []

    @classmethod
    def empty(cls) -> PipelineState:
        """Return a zero-value PipelineState for pre-first-tick callers.

        Routes and the API endpoint use this when ``get_state()`` returns
        ``None`` (i.e. the background poller hasn't completed its first tick).
        Callers should treat ``agents == []`` as "loading", not "no agents."
        """
        import time

        return cls(
            active_label=None,
            issues_open=0,
            prs_open=0,
            agents=[],
            alerts=[],
            stale_claims=[],
            board_issues=[],
            polled_at=time.time(),
            closed_issues_count=0,
            merged_prs_count=0,
            stale_branches=[],
            pending_approval=[],
            plan_draft_events=[],
        )


class IssueSub(BaseModel):
    """One entry from ``[[issue_queue]]`` in a TOML .agent-task file.

    Coordinator agents receive a list of these; each becomes one worktree and
    one leaf agent. All fields except ``branch`` and ``file_ownership`` are
    required in the spec; we allow optional for lenient parsing.
    """

    number: int
    title: str = ""
    role: str = ""
    cognitive_arch: str = ""
    depends_on: list[int] = []
    file_ownership: list[str] = []
    branch: str | None = None


class PRSub(BaseModel):
    """One entry from ``[[pr_queue]]`` in a TOML .agent-task file.

    QA coordinator agents receive a list of these; each is one PR to review
    with merge order and grade threshold. All fields except ``closes_issues``
    are required in the spec; we allow optional for lenient parsing.
    """

    number: int
    title: str = ""
    branch: str = ""
    role: str = ""
    cognitive_arch: str = ""
    grade_threshold: str = ""
    merge_order: int = 0
    closes_issues: list[int] = []


class TaskFile(BaseModel):
    """Parsed content of a ``.agent-task`` file from a worktree.

    Supports both the legacy KEY=value format and the TOML spec
    (see .agentception/agent-task-spec.md). Every field maps from a TOML section
    or a legacy key. Unknown keys are silently ignored. All fields are optional
    to ensure graceful handling of missing or malformed task files.
    """

    # [task]
    task: str | None = None
    id: str | None = None
    attempt_n: int = 0
    is_resumed: bool = False
    required_output: str | None = None
    on_block: str | None = None
    # [agent]
    role: str | None = None
    tier: str | None = None
    """Behavioral execution tier: coordinator | engineer | reviewer."""
    org_domain: str | None = None
    """Organisational slot for UI hierarchy: c-suite | engineering | qa."""
    cognitive_arch: str | None = None
    session_id: str | None = None
    # [repo]
    gh_repo: str | None = None
    base: str | None = None
    # [pipeline]
    batch_id: str | None = None
    parent_run_id: str | None = None
    wave: str | None = None
    vp_fingerprint: str | None = None
    # [spawn]
    spawn_sub_agents: bool = False
    spawn_mode: str | None = None
    # [target]
    issue_number: int | None = None
    pr_number: int | None = None
    depends_on: list[int] = []
    closes_issues: list[int] = []
    file_ownership: list[str] = []
    files_changed: list[str] = []
    grade_threshold: str | None = None
    has_migration: bool = False
    merge_after: str | None = None
    # [worktree]
    worktree: str | None = None
    branch: str | None = None
    linked_pr: int | None = None
    # [output]
    draft_id: str | None = None
    output_path: str | None = None
    output_format: str | None = None
    # [domain]
    domain: str | None = None
    # Queues (TOML repeated tables)
    issue_queue: list[IssueSub] = []
    pr_queue: list[PRSub] = []


class AbModeConfig(BaseModel):
    """A/B mode configuration for role file experimentation (AC-504).

    When ``enabled`` is true the Engineering VP alternates between two role
    files for the ``target_role`` based on whether the BATCH_ID timestamp
    second is even (variant A) or odd (variant B).  This enables controlled
    experiments where successive batches see different role prompts so outcomes
    can be compared with everything else held constant.

    ``variant_a_file`` and ``variant_b_file`` are paths relative to the
    repository root (e.g. ``.agentception/roles/python-developer.md``).
    """

    enabled: bool = False
    target_role: str | None = None
    variant_a_file: str | None = None
    variant_b_file: str | None = None


class ProjectConfig(BaseModel):
    """A single project entry in ``pipeline-config.json``.

    Each project maps to a distinct GitHub repository and local workspace.
    The ``active_project`` field in :class:`PipelineConfig` selects which
    project the AgentCeption dashboard currently targets.

    ``repo_dir`` and ``worktrees_dir`` are optional.  When absent, the values
    from the environment (``REPO_DIR``, ``WORKTREES_DIR``) are used unchanged.
    Set them only when targeting a *different* repository than the one the
    service was started against — e.g. in a multi-repo setup.
    ``worktrees_dir`` supports ``~`` expansion.

    ``cursor_project_id`` is the Cursor project slug used to locate transcript files.
    """

    name: str
    gh_repo: str
    repo_dir: str | None = None
    worktrees_dir: str | None = None
    cursor_project_id: str | None = None


class PipelineConfig(BaseModel):
    """Validated shape of ``.agentception/pipeline-config.json``.

    This is the single source of truth for pipeline allocation.  Coordinator
    role files read this model at the start of every loop/seed cycle.  The
    ``PUT /api/config`` route validates incoming bodies against this schema
    before persisting them to disk.

    ``projects`` lists all configured projects; ``active_project`` is the name
    of the currently active one.  When ``active_project`` is set, the dashboard
    targets the corresponding project's ``gh_repo``, ``repo_dir``, and
    ``worktrees_dir`` instead of the defaults in :class:`AgentCeptionSettings`.

    Allocation fields:
      ``coordinator_limits`` — max concurrent instances per coordinator role slug.
      ``pool_size``          — number of leaf agents per coordinator instance.
    """

    coordinator_limits: dict[str, int] = Field(
        default={"engineering-coordinator": 1, "qa-coordinator": 1},
        description="Max concurrent instances per coordinator role slug.",
    )
    pool_size: int = Field(default=4, gt=0, description="Leaf agents per coordinator.")
    active_labels_order: list[str] = Field(
        default=[],
        description=(
            "Ordered list of scoped phase labels (e.g. 'ac-build/phase-0') used by the "
            "poller to auto-advance the active phase.  Empty disables auto-advance."
        ),
    )
    ab_mode: AbModeConfig = AbModeConfig()
    projects: list[ProjectConfig] = []
    active_project: str | None = None
    approval_required_labels: list[str] = ["db-schema", "security", "api-contract"]
    #: Label removed from to_phase issues when advancing a phase gate.
    phase_advance_blocked_label: str = "blocked"
    #: Label added to to_phase issues when they become eligible for dispatch.
    phase_advance_active_label: str = "pipeline-active"


class SpawnRequest(BaseModel):
    """Request body for ``POST /api/control/spawn``.

    Callers provide the issue number they want an agent to tackle and an
    optional role override. The endpoint verifies the issue is open and
    unclaimed before creating the worktree.
    """

    issue_number: int
    role: str = "python-developer"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        """Reject unknown roles early so errors surface at the boundary, not in git."""
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}, got {v!r}")
        return v


class SpawnResult(BaseModel):
    """Response for a successful ``POST /api/control/spawn``.

    Contains enough information for the user (or a future automation layer)
    to launch a Cursor Task pointed at the new worktree.
    ``agent_task`` is the raw text of the ``.agent-task`` file that was
    written — callers can display it or pass it directly to the Task tool.

    ``worktree`` is the container-side path (``/worktrees/issue-N``).
    ``host_worktree`` is the equivalent host-side path the user can open in
    AgentCeption (``~/.agentception/worktrees/agentception/issue-N``).
    ``spawned_at`` is an ISO-8601 UTC timestamp indicating when the worktree
    was created (included for display in the HTML success panel).
    """

    spawned: int
    worktree: str
    host_worktree: str
    branch: str
    agent_task: str
    spawned_at: str = ""


class SpawnConductorRequest(BaseModel):
    """Request body for ``POST /api/control/spawn-conductor``.

    ``phases`` is a non-empty list of phase label strings to run the conductor
    against.  ``org`` optionally scopes the conductor to a specific org name
    (passed through to the ``.agent-task`` file; leave ``None`` for the default).
    """

    phases: list[str]
    org: str | None = None


class SpawnConductorResult(BaseModel):
    """Response for a successful ``POST /api/control/spawn-conductor``.

    ``wave_id`` is the auto-generated conductor ID (e.g. ``conductor-20260303-142201``).
    ``host_worktree`` is the path the user should open in Cursor to activate the agent.
    ``agent_task`` is the raw ``.agent-task`` content written to disk.
    """

    wave_id: str
    worktree: str
    host_worktree: str
    branch: str
    agent_task: str


class SpawnCoordinatorRequest(BaseModel):
    """Request body for ``POST /api/control/spawn-coordinator``.

    ``plan_text`` is the user's raw unstructured text — feature ideas, bug
    descriptions, or any free-form list of work items.  The coordinator agent
    reads this field from its ``.agent-task`` file and runs Phase 1A (LLM →
    PlanSpec YAML) followed by Phase 1B (human review), then creates labelled
    GitHub issues via the AgentCeption planning pipeline.

    ``label_prefix`` optionally scopes the generated phase labels to a named
    initiative (e.g. ``"q2-rewrite"`` → labels like ``phase-1/q2-rewrite``).
    Leave blank for the default label scheme.
    """

    plan_text: str
    label_prefix: str = ""


class SpawnCoordinatorResult(BaseModel):
    """Response for a successful ``POST /api/control/spawn-coordinator``.

    ``slug`` is the worktree directory name (e.g. ``brain-dump-20260301-143022``).
    ``host_worktree`` is the path the user should open in Cursor to activate
    the coordinator agent.  ``agent_task`` is the raw ``.agent-task`` content
    written to disk — useful for display and debugging.
    """

    slug: str
    worktree: str
    host_worktree: str
    branch: str
    agent_task: str


class SwitchProjectRequest(BaseModel):
    """Request body for ``POST /api/config/switch-project``.

    ``project_name`` must match the ``name`` field of one of the entries in
    ``PipelineConfig.projects``.  If no match is found the endpoint returns
    HTTP 404 rather than silently writing an invalid ``active_project`` value.
    """

    project_name: str


class RoleMeta(BaseModel):
    """Metadata for a managed role or cursor configuration file.

    Used by the Role Studio API (AC-301) to describe each file without
    returning full content — callers fetch content separately via GET /api/roles/{slug}.
    ``last_commit_sha`` and ``last_commit_message`` are empty strings when the
    file has never been committed (e.g. in tests with a temp directory).
    """

    slug: str
    path: str
    line_count: int
    mtime: float
    last_commit_sha: str
    last_commit_message: str


class RoleUpdateRequest(BaseModel):
    """Request body for ``PUT /api/roles/{slug}`` (Role Studio AC-301).

    Wraps the raw ``content`` string so FastAPI can validate and document the
    request body rather than accepting an untyped naked dict.
    """

    content: str


class RoleContent(BaseModel):
    """Response for ``GET /api/roles/{slug}`` — full file content with metadata.

    Returned by the Role Studio reader endpoint so the UI (AC-302/303) has
    both the Markdown source and the git provenance in one round-trip.
    """

    slug: str
    content: str
    meta: RoleMeta


class RoleUpdateResponse(BaseModel):
    """Response for ``PUT /api/roles/{slug}`` — diff and refreshed metadata.

    ``diff`` is the raw output of ``git diff HEAD -- <path>`` immediately after
    writing; an empty string means the written content was identical to what
    was already committed.
    """

    slug: str
    diff: str
    meta: RoleMeta


class RoleDiffRequest(BaseModel):
    """Request body for ``POST /api/roles/{slug}/diff`` (AC-303).

    ``content`` is the proposed file content to diff against the HEAD-committed
    version.  No file is written to disk — this is a pure preview operation.
    Using a POST body avoids URL-length limits for large managed files (e.g.
    PARALLEL_PR_REVIEW.md which exceeds Nginx's default 4 KB URI limit).
    """

    content: str


class RoleDiffResponse(BaseModel):
    """Response for ``POST /api/roles/{slug}/diff`` — diff of proposed vs HEAD.

    ``diff`` is a unified diff string comparing ``content`` against
    the HEAD-committed version.  An empty string means the proposed content is
    identical to the committed file.  No file is written to disk.
    """

    slug: str
    diff: str


class RoleCommitRequest(BaseModel):
    """Request body for ``POST /api/roles/{slug}/commit`` (AC-303).

    ``content`` is written to the managed file and then staged + committed in
    one atomic operation.  The commit message is generated automatically.
    """

    content: str


class RoleCommitResponse(BaseModel):
    """Response for ``POST /api/roles/{slug}/commit`` — resulting commit SHA.

    ``commit_sha`` is the full 40-character SHA of the newly created commit.
    ``message`` is the commit subject line that was used.
    """

    slug: str
    commit_sha: str
    message: str


class RoleVersionEntry(BaseModel):
    """A single entry in a role's version history (AC-503).

    Records the git SHA, human-readable version label, and UNIX timestamp of
    one committed change to the role file.  Entries are ordered chronologically
    (oldest first) inside ``RoleVersionInfo.history``.
    """

    sha: str
    label: str
    timestamp: int


class RoleVersionInfo(BaseModel):
    """Version tracking data for a single role slug (AC-503).

    ``current`` is the label of the most recently recorded version.  ``history``
    is the chronologically ordered list of all version entries (oldest first).
    An empty ``history`` means the slug has never been committed through the
    Role Studio commit endpoint.
    """

    current: str
    history: list[RoleVersionEntry]


class RoleVersionsResponse(BaseModel):
    """Response for ``GET /api/roles/{slug}/versions`` (AC-503).

    Returns structured version history for a single role slug so the Role
    Studio UI can display a timeline of changes and link each version to its
    git commit SHA.
    """

    slug: str
    versions: RoleVersionInfo


class RoleHistoryEntry(BaseModel):
    """One git log entry for a role file's revision history."""

    sha: str
    date: str
    subject: str


# ---------------------------------------------------------------------------
# Cognitive Architecture API — taxonomy, personas, atoms
# ---------------------------------------------------------------------------


class TaxonomyRole(BaseModel):
    """A single role entry in the org hierarchy taxonomy.

    Returned by ``GET /api/roles/taxonomy`` as part of a level's role list.
    Combines metadata from ``role-taxonomy.yaml`` with a live ``file_exists``
    flag so the GUI can show which roles have been authored.
    """

    slug: str
    label: str
    title: str
    category: str
    description: str
    spawnable: bool
    compatible_figures: list[str]
    compatible_skill_domains: list[str]
    file_exists: bool


class TaxonomyLevel(BaseModel):
    """One tier of the org hierarchy (C-Suite, VP Level, Workers).

    Contains the full ordered list of ``TaxonomyRole`` entries for that tier.
    """

    id: str
    label: str
    description: str
    roles: list[TaxonomyRole]


class TaxonomyResponse(BaseModel):
    """Response for ``GET /api/roles/taxonomy``.

    Returns the complete three-tier org hierarchy so the GUI can render the
    hierarchy browser and the primitive composer's compatible-figures dropdowns.
    """

    levels: list[TaxonomyLevel]


class PersonaEntry(BaseModel):
    """A single persona/figure from the cognitive architecture library.

    Returned by ``GET /api/roles/personas``.  Each entry corresponds to one
    YAML file in ``scripts/gen_prompts/cognitive_archetypes/figures/``.
    The ``prompt_prefix`` is the injected context block used at spawn time.
    """

    id: str
    display_name: str
    layer: str
    extends: str
    description: str
    prompt_prefix: str
    overrides: dict[str, str]


class PersonasResponse(BaseModel):
    """Response for ``GET /api/roles/personas``."""

    personas: list[PersonaEntry]


class AtomValue(BaseModel):
    """A single named value within an atom dimension."""

    id: str
    label: str
    description: str


class AtomDimension(BaseModel):
    """One cognitive atom dimension (e.g. epistemic_style, quality_bar).

    Returned by ``GET /api/roles/atoms`` so the primitive composer can render
    dropdowns for each atom and its valid values.
    """

    dimension: str
    description: str
    values: list[AtomValue]


class AtomsResponse(BaseModel):
    """Response for ``GET /api/roles/atoms``."""

    atoms: list[AtomDimension]


# ---------------------------------------------------------------------------
# Template export / import  (AC-602)
# ---------------------------------------------------------------------------


class TemplateExportRequest(BaseModel):
    """Request body for ``POST /api/templates/export``.

    ``name`` and ``version`` are embedded in the manifest inside the archive
    so that importers know what they are applying.
    """

    name: str
    version: str


class TemplateManifest(BaseModel):
    """Metadata record written as ``template-manifest.json`` inside the archive.

    Included in every exported template so importers can surface provenance
    without unpacking the whole tarball.
    """

    name: str
    version: str
    created_at: str
    gh_repo: str
    files: list[str]


class TemplateConflict(BaseModel):
    """A single file that already exists in the target repo's ``.agentception/`` directory.

    Surfaced by the import endpoint before any file is overwritten so the
    caller can decide whether to proceed.
    """

    path: str
    exists: bool


class TemplateImportResult(BaseModel):
    """Response for ``POST /api/templates/import``.

    ``extracted`` lists every file path that was written (relative to the
    target repo root).  ``conflicts`` lists files that already existed — they
    are still overwritten, but the caller is informed so the UI can display
    a warning banner.
    """

    name: str
    version: str
    extracted: list[str]
    conflicts: list[TemplateConflict]


class TemplateListEntry(BaseModel):
    """Summary of one previously exported template stored on disk.

    Represents a single ``.tar.gz`` archive in the templates store directory.
    ``size_bytes`` is the archive size (not uncompressed size).
    """

    filename: str
    name: str
    version: str
    created_at: str
    gh_repo: str
    size_bytes: int


class OrgTreeRole(BaseModel):
    """A single role entry in the org tree returned by ``GET /api/org/tree``.

    ``figures`` holds the first two compatible figures from the taxonomy so the
    D3 tree can render avatar chips without a second round-trip.
    ``assigned_phases`` is reserved for future phase-assignment features and
    is always an empty list in the current implementation.
    """

    slug: str
    name: str
    tier: str
    assigned_phases: list[str]
    figures: list[str]


class OrgTreeNode(BaseModel):
    """One node in the org hierarchy tree returned by ``GET /api/org/tree``.

    The root node represents the active preset; its children are the
    leadership and workers tiers; each tier's ``roles`` list holds the
    individual role cards rendered by the D3 tree.
    """

    name: str
    id: str
    tier: str
    roles: list[OrgTreeRole]
    children: list["OrgTreeNode"]


OrgTreeNode.model_rebuild()


# ---------------------------------------------------------------------------
# PlanSpec — YAML schema contract for plan-step-v2 pipeline (AC-867)
# ---------------------------------------------------------------------------


class PlanIssue(BaseModel):
    """A single GitHub issue to be created within a plan phase.

    ``id`` is a stable kebab-case slug (e.g. ``"auth-001"``) used as the
    canonical dependency reference.  ``depends_on`` lists *IDs* (not titles)
    of other issues — this avoids silent breakage when titles are edited in
    the review editor.  ``title`` is the issue title; ``body`` is the
    Markdown description.

    ``skills`` is an optional list of skill domain IDs (matching filenames in
    ``scripts/gen_prompts/cognitive_archetypes/skill_domains/``) that the LLM
    planner populates at plan time.  These flow through to ``_resolve_cognitive_arch``
    at agent spawn time as the authoritative ``skills_hint``, replacing the
    fragile keyword-extraction fallback with planner-provided signal.

    ``cognitive_arch`` is the fully-resolved arch string (``figure:skill1[:skill2]``)
    assigned by the LLM planner in Phase 1A and optionally edited by the user in
    Phase 1B.  When non-empty, it is embedded in the GitHub issue body as an
    ``<!-- ac:cognitive_arch: ... -->`` comment so that the dispatch layer can read
    it directly — no heuristics required.
    """

    id: str
    title: str
    body: str
    depends_on: list[str] = []  # issue IDs, not titles
    skills: list[str] = []  # skill domain IDs from the cognitive arch catalog
    cognitive_arch: str = ""  # resolved arch string: "figure:skill1[:skill2]"


_PHASE_LABEL_RE = re.compile(r"^[0-9]+-[a-z0-9][a-z0-9-]*$")


class PlanPhase(BaseModel):
    """A sequenced phase grouping related issues in a PlanSpec.

    ``label`` must follow the ``{N}-{slug}`` convention — a numeric prefix
    (the 0-based phase index) followed by a kebab-case semantic descriptor,
    e.g. ``"0-foundation"``, ``"1-api-layer"``, ``"2-ui"``.  This format
    makes lexicographic sort a correct ordering fallback while also being
    human-readable as a GitHub label suffix.

    ``description`` is a one-sentence summary of the phase's gate criterion.
    ``depends_on`` lists sibling phase labels that must complete first.
    ``issues`` is the ordered list of issues to create in this phase.

    Raises ``ValueError`` if ``issues`` is empty — a phase with no issues
    cannot advance the pipeline.
    """

    label: str
    description: str
    depends_on: list[str] = []
    issues: list[PlanIssue]

    @field_validator("label")
    @classmethod
    def label_must_follow_n_slug_convention(cls, v: str) -> str:
        """Enforce the {N}-{slug} label format: digits, hyphen, kebab-case slug."""
        if not _PHASE_LABEL_RE.match(v):
            raise ValueError(
                f"Phase label {v!r} must match '{_PHASE_LABEL_RE.pattern}' "
                f"(e.g. '0-foundation', '1-api-layer'). "
                f"The numeric prefix must equal the phase's 0-based list position."
            )
        return v

    @field_validator("issues")
    @classmethod
    def issues_must_be_non_empty(cls, v: list[PlanIssue]) -> list[PlanIssue]:
        """A phase must contain at least one issue."""
        if not v:
            raise ValueError("phases must each contain at least one issue")
        return v


class PlanSpec(BaseModel):
    """Root schema for the plan-step-v2 YAML contract.

    ``initiative`` is a short human name for the batch (e.g. ``"auth-rewrite"``).
    ``phases`` is an ordered list of :class:`PlanPhase` objects; index 0 is the
    foundation phase that has no dependencies.

    ``coordinator_arch`` maps orchestration role slugs to resolved arch strings
    (``figure:skill1[:skill2]``).  The LLM planner populates this in Phase 1A;
    the user may edit it in the Phase 1B YAML editor before submitting.  Keys
    are role slugs from ``role-taxonomy.yaml`` — e.g. ``"cto"``,
    ``"engineering-coordinator"``, ``"qa-coordinator"``.  New coordinator types
    require no schema changes — just new keys.

    Invariants enforced at construction time:
    - ``phases`` must be non-empty.
    - Phase ``depends_on`` labels must all reference labels that appear
      earlier in the list (strict DAG; no forward references, no cycles).

    Round-trip serialization:
    - :meth:`to_yaml` produces clean YAML with no Pydantic internals.
    - :meth:`from_yaml` parses and validates; raises :class:`ValueError` on
      malformed input.
    """

    initiative: str
    phases: list[PlanPhase]
    coordinator_arch: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps orchestration role slugs to resolved arch strings "
            "(figure:skill1[:skill2]).  Keys: cto, engineering-coordinator, "
            "qa-coordinator, etc.  Populated by the LLM planner in Phase 1A."
        ),
    )

    @field_validator("phases")
    @classmethod
    def phases_must_be_non_empty(cls, v: list[PlanPhase]) -> list[PlanPhase]:
        """At least one phase is required."""
        if not v:
            raise ValueError("PlanSpec must contain at least one phase")
        return v

    @model_validator(mode="after")
    def validate_issue_ids_unique(self) -> "PlanSpec":
        """Ensure all issue IDs are unique across the entire plan."""
        seen: set[str] = set()
        for phase in self.phases:
            for issue in phase.issues:
                if issue.id in seen:
                    raise ValueError(
                        f"Duplicate issue id {issue.id!r} — every issue must have a unique id"
                    )
                seen.add(issue.id)
        return self

    @model_validator(mode="after")
    def validate_issue_depends_on(self) -> "PlanSpec":
        """Ensure issue depends_on references valid issue IDs defined earlier in the plan."""
        all_ids: set[str] = {issue.id for phase in self.phases for issue in phase.issues}
        for phase in self.phases:
            for issue in phase.issues:
                for dep in issue.depends_on:
                    if dep not in all_ids:
                        raise ValueError(
                            f"Issue {issue.id!r} depends_on {dep!r} which is not a "
                            f"known issue id in this plan"
                        )
                    if dep == issue.id:
                        raise ValueError(f"Issue {issue.id!r} cannot depend on itself")
        return self

    @model_validator(mode="after")
    def validate_phase_dag(self) -> "PlanSpec":
        """Ensure phase depends_on references form a valid DAG.

        Each phase may only depend on phases that appear *before* it in the
        list.  Forward references and cycles are both rejected.  This is a
        necessary (though not sufficient) condition for a valid DAG: because
        phases are linearly ordered and can only reference earlier entries,
        the dependency graph is acyclic by construction.
        """
        seen: set[str] = set()
        for phase in self.phases:
            for dep in phase.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"Phase {phase.label!r} depends_on {dep!r} which is not a "
                        f"previously defined phase label (forward reference or cycle)"
                    )
            seen.add(phase.label)
        return self

    def to_yaml(self) -> str:
        """Serialize to a clean YAML string.

        Uses PyYAML ``safe_dump`` with ``default_flow_style=False`` and
        ``sort_keys=False`` so the output preserves insertion order and omits
        Pydantic-internal fields.

        ``coordinator_arch`` is included only when non-empty so that minimal
        plans produced without Phase 1A planner output remain compact.
        Per-issue ``cognitive_arch`` and ``skills`` are always included when
        non-empty so users can inspect and edit them in the Phase 1B editor.
        """
        data: dict[str, object] = {
            "initiative": self.initiative,
        }
        if self.coordinator_arch:
            data["coordinator_arch"] = dict(self.coordinator_arch)
        data["phases"] = [
            {
                "label": phase.label,
                "description": phase.description,
                "depends_on": phase.depends_on,
                "issues": [
                    {
                        "id": issue.id,
                        "title": issue.title,
                        "body": issue.body,
                        "depends_on": issue.depends_on,
                        **({"skills": issue.skills} if issue.skills else {}),
                        **({"cognitive_arch": issue.cognitive_arch} if issue.cognitive_arch else {}),
                    }
                    for issue in phase.issues
                ],
            }
            for phase in self.phases
        ]
        return yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> "PlanSpec":
        """Parse and validate a YAML string into a PlanSpec.

        Raises:
            ValueError: If the YAML is malformed, missing required fields,
                or violates any PlanSpec invariant (empty phases, bad deps).
        """
        try:
            raw: object = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Expected a YAML mapping at the top level, got {type(raw).__name__}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"PlanSpec validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# EnrichedManifest — coordinator input contract for plan-step-v2 (AC-868)
# ---------------------------------------------------------------------------


class EnrichedIssue(BaseModel):
    """A fully-specified GitHub issue generated by the LLM coordinator.

    Carries the complete contract for one unit of work: the Markdown body,
    GitHub label set, phase membership, dependency titles, parallelizability
    assessment, and the three acceptance criteria lists that appear verbatim
    in the issue body.

    ``depends_on`` references *titles* (not numbers) of other issues that must
    be merged before this one can begin.  Titles are used because issue numbers
    are not known until after creation.
    """

    title: str
    body: str
    labels: list[str]
    phase: str
    depends_on: list[str] = []
    can_parallel: bool = True
    acceptance_criteria: list[str]
    tests_required: list[str]
    docs_required: list[str]


class EnrichedPhase(BaseModel):
    """A sequenced execution phase in an EnrichedManifest.

    Groups related :class:`EnrichedIssue` objects that share a lifecycle
    stage.  ``parallel_groups`` partitions issue titles into sets that can
    run concurrently — no title in a group may appear in the ``depends_on``
    list of any other title in the same group.

    Raises ``ValueError`` if any ``parallel_groups`` entry violates the
    no-intra-group-dependency invariant.
    """

    label: str
    description: str
    depends_on: list[str] = []
    issues: list[EnrichedIssue]
    parallel_groups: list[list[str]]

    @model_validator(mode="after")
    def validate_parallel_groups_invariant(self) -> "EnrichedPhase":
        """Enforce that no issue in a parallel group depends on another in the same group.

        For every group G and every title T in G, none of T's ``depends_on``
        entries may also appear in G.  Violating this would schedule a
        depender and its dependency in the same execution wave, which is
        incorrect — the dependency must complete before the depender starts.
        """
        issue_deps: dict[str, set[str]] = {
            issue.title: set(issue.depends_on) for issue in self.issues
        }
        for group in self.parallel_groups:
            group_set = set(group)
            for title in group:
                deps = issue_deps.get(title, set())
                intra_deps = deps & group_set - {title}
                if intra_deps:
                    raise ValueError(
                        f"Issue {title!r} depends on {sorted(intra_deps)} "
                        f"which are in the same parallel group — "
                        f"dependent and dependency cannot run concurrently"
                    )
        return self


def _compute_wave_depths(phases: list[EnrichedPhase]) -> dict[str, int]:
    """Return a mapping from issue title to its critical-path depth (1-indexed).

    Depth 1 = no dependencies (first wave).
    Depth N = all dependencies are in waves <= N-1.

    Uses memoised DFS over the dependency graph built from all issues across
    all phases.  Unknown dependency titles (titles not in the manifest) are
    silently ignored — they are treated as if they have depth 0 so the
    computation remains valid even for cross-manifest references.
    """
    all_issues: dict[str, EnrichedIssue] = {}
    for phase in phases:
        for issue in phase.issues:
            all_issues[issue.title] = issue

    wave_depth: dict[str, int] = {}

    def get_depth(title: str, visiting: frozenset[str]) -> int:
        if title in wave_depth:
            return wave_depth[title]
        if title not in all_issues or title in visiting:
            return 0
        issue = all_issues[title]
        max_dep_depth = max(
            (get_depth(dep, visiting | {title}) for dep in issue.depends_on),
            default=0,
        )
        depth = 1 + max_dep_depth
        wave_depth[title] = depth
        return depth

    for title in all_issues:
        get_depth(title, frozenset())

    return wave_depth


class EnrichedManifest(BaseModel):
    """Root coordinator input contract for the plan-step-v2 pipeline.

    Produced by the LLM coordinator after enriching a :class:`PlanSpec` with
    full issue bodies, labels, acceptance criteria, tests, and docs.  Consumed
    by the issue-creation step that writes each :class:`EnrichedIssue` to
    GitHub.

    ``total_issues`` and ``estimated_waves`` are computed invariants —
    callers do not supply them.  They are derived by the ``model_validator``
    so that downstream consumers can always trust their values.

    Invariants enforced at construction time:
    - ``phases`` must be non-empty.
    - ``total_issues`` equals ``sum(len(p.issues) for p in phases)``.
    - ``estimated_waves`` equals the longest dependency chain across all
      issues in all phases (critical-path length through the dep graph).
    """

    initiative: str | None = None
    phases: list[EnrichedPhase]
    total_issues: int = 0
    estimated_waves: int = 0

    @field_validator("phases")
    @classmethod
    def phases_must_be_non_empty(cls, v: list[EnrichedPhase]) -> list[EnrichedPhase]:
        """At least one phase is required."""
        if not v:
            raise ValueError("EnrichedManifest must contain at least one phase")
        return v

    @model_validator(mode="after")
    def compute_invariants(self) -> "EnrichedManifest":
        """Compute total_issues and estimated_waves from the phases graph.

        Both values are derived — never trust a caller-supplied value.
        Setting them here guarantees they are always consistent with the
        actual phase/issue data regardless of how the model was constructed.
        """
        self.total_issues = sum(len(p.issues) for p in self.phases)

        wave_depths = _compute_wave_depths(self.phases)
        self.estimated_waves = max(wave_depths.values(), default=1)

        return self
