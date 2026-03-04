# AgentCeption — Type Contracts Reference

This document lists the public Pydantic models, TypedDicts, and functions that form the API contracts between AgentCeption's internal layers. Each entry includes its source path, field table, and producer/consumer annotations.

---

## DB Query TypedDicts

All query functions in `agentception/db/queries.py` return named TypedDicts instead of `dict[str, Any]`. These are the typed row shapes that flow from the DB layer to route handlers, the poller, and the MCP tools. Import them directly from `agentception.db.queries` when annotating callers.

### Issued/board shapes

| TypedDict | Produced by | Key fields |
|-----------|-------------|-----------|
| `BoardIssueRow` | `get_board_issues()` | `number`, `title`, `state`, `labels: list[LabelEntry]`, `claimed`, `phase_label`, `last_synced_at` |
| `AllIssueRow` | `get_all_issues()` | `number`, `title`, `state`, `labels: list[str]`, `phase_label`, `closed_at`, `last_synced_at` |
| `IssueDetailRow` | `get_issue_detail()` | All of `AllIssueRow` + `body`, `claimed`, `first_seen_at`, `linked_prs: list[LinkedPRRow]`, `agent_runs: list[IssueAgentRunRow]` |
| `LabelEntry` | Embedded in board rows | `name: str` — matches the `{name: str}` GitHub API label shape |
| `LinkedPRRow` | Embedded in `IssueDetailRow` | `number`, `title`, `state`, `head_ref`, `merged_at` |
| `IssueAgentRunRow` | Embedded in `IssueDetailRow` | `id`, `role`, `status`, `branch`, `pr_number`, `spawned_at`, `last_activity_at` |
| `PhasedIssueRow` | `get_issues_grouped_by_phase()` | `number`, `title`, `state`, `url`, `labels: list[str]` |
| `PhaseGroupRow` | `get_issues_grouped_by_phase()` | `label`, `issues: list[PhasedIssueRow]`, `locked`, `complete`, `depends_on` |

### PR shapes

| TypedDict | Produced by | Key fields |
|-----------|-------------|-----------|
| `OpenPRRow` | `get_open_prs_db()` | `number`, `title`, `state`, `headRefName`, `labels: list[LabelEntry]` |
| `AllPRRow` | `get_all_prs()` | `number`, `title`, `state`, `head_ref`, `labels: list[str]`, `closes_issue_number`, `merged_at`, `last_synced_at` |
| `PRDetailRow` | `get_pr_detail()` | All of `AllPRRow` + `first_seen_at`, `linked_issue: LinkedIssueRow \| None`, `agent_runs: list[PRAgentRunRow]` |
| `LinkedIssueRow` | Embedded in `PRDetailRow` | `number`, `title`, `state` |
| `PRAgentRunRow` | Embedded in `PRDetailRow` | `id`, `role`, `status`, `branch`, `issue_number`, `spawned_at`, `last_activity_at` |

### Agent run shapes

| TypedDict | Produced by | Key fields |
|-----------|-------------|-----------|
| `AgentRunRow` | `get_agent_run_history()` | `id`, `role`, `status`, `wave_id`, `issue_number`, `pr_number`, `branch`, `worktree_path`, `attempt_number`, `spawn_mode`, `batch_id`, `spawned_at`, `last_activity_at`, `completed_at` |
| `AgentRunDetail` | `get_agent_run_detail()` | Subset of `AgentRunRow` + `messages: list[AgentMessageRow]` |
| `AgentMessageRow` | Embedded in `AgentRunDetail` | `role`, `content`, `tool_name`, `sequence_index`, `recorded_at` |
| `RunForIssueRow` | `get_runs_for_issue_numbers()` | `id`, `role`, `status`, `pr_number`, `branch`, `spawned_at`, `last_activity_at` |
| `PendingLaunchRow` | `get_pending_launches()` | `run_id`, `issue_number`, `role`, `branch`, `worktree_path`, `host_worktree_path`, `batch_id`, `spawned_at` |

### Event and thought shapes

| TypedDict | Produced by | Key fields |
|-----------|-------------|-----------|
| `AgentEventRow` | `get_agent_events_tail()` | `id: int`, `event_type`, `payload: str` (raw JSON — caller must `json.loads`), `recorded_at` |
| `AgentThoughtRow` | `get_agent_thoughts_tail()` | `seq: int`, `role`, `content`, `recorded_at` |

### Pipeline / wave shapes

| TypedDict | Produced by | Key fields |
|-----------|-------------|-----------|
| `PipelineTrendRow` | `get_pipeline_trend()` | `polled_at`, `active_label`, `issues_open`, `prs_open`, `agents_active`, `alert_count` |
| `WaveRow` | `get_waves_from_db()` | `batch_id`, `started_at: float`, `ended_at: float \| None`, `issues_worked`, `prs_opened`, `estimated_tokens`, `estimated_cost_usd`, `agents: list[WaveAgentRow]` |
| `WaveAgentRow` | Embedded in `WaveRow` | `id`, `role`, `status`, `issue_number`, `pr_number`, `branch`, `batch_id`, `worktree_path`, `cognitive_arch`, `message_count` |
| `ConductorHistoryRow` | `get_conductor_history()` | `wave_id`, `worktree`, `host_worktree`, `started_at`, `status` |

> **Note on `AgentEventRow.payload`:** The `payload` field is stored and returned as a raw JSON string. Route handlers that need the parsed structure must call `json.loads(ev["payload"])`. This keeps the DB layer free of schema knowledge about individual event types.

---

## Intelligence

### `ABVariantResult`

**Path:** `agentception/intelligence/ab_results.py`

Pydantic `BaseModel` — Outcome metrics for one A/B role variant across all applicable batches. Produced by `compute_ab_results()` and consumed by the `/ab-testing` dashboard route.

| Field | Type | Description |
|-------|------|-------------|
| `variant` | `Literal["A", "B"]` | Which variant this result represents |
| `role_sha` | `str` | Git SHA of the role file version active during these batches; empty string when unknown |
| `batch_ids` | `list[str]` | All BATCH_IDs attributed to this variant |
| `prs_opened` | `int` | Total PRs opened by engineers in this variant's batches |
| `prs_merged` | `int` | Total merged PRs attributed to this variant |
| `avg_grade` | `str | None` | Mean reviewer letter grade (A–F); `None` when no graded PRs found |
| `merge_rate` | `float` | `prs_merged / prs_opened`; `0.0` when `prs_opened` is zero |

**Produced by:** `agentception.intelligence.ab_results.compute_ab_results()`
**Consumed by:** `GET /ab-testing` route handler in `agentception.routes.ui`

---

## Models

### `ProjectConfig`

**Path:** `agentception/models.py`

Pydantic `BaseModel` — A single project entry in `pipeline-config.json`. Each project maps to a distinct GitHub repository and local workspace. `AgentCeptionSettings._apply_active_project()` reads the active entry at initialisation to override path defaults.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Human-readable project name; must be unique within `PipelineConfig.projects` |
| `gh_repo` | `str` | GitHub repository slug (e.g. `cgcardona/agentception`) |
| `repo_dir` | `str` | Absolute path to the local git repository |
| `worktrees_dir` | `str` | Path to the worktrees root for this project; `~` expansion is applied |
| `cursor_project_id` | `str` | Cursor project slug used to locate transcript files |
| `active_labels_order` | `list[str]` | Ordered list of phase labels for this project |

**Produced by:** `agentception.models.ProjectConfig`
**Consumed by:** `AgentCeptionSettings._apply_active_project()`, `POST /api/config/switch-project`

---

### `SwitchProjectRequest`

**Path:** `agentception/models.py`

Pydantic `BaseModel` — Request body for `POST /api/config/switch-project`.

| Field | Type | Description |
|-------|------|-------------|
| `project_name` | `str` | Must match the `name` of an existing entry in `PipelineConfig.projects` |

**Produced by:** API callers (dashboard project-switcher dropdown)
**Consumed by:** `agentception.routes.api.switch_project_endpoint()`

---

## Readers

### `switch_project` (function)

**Path:** `agentception/readers/pipeline_config.py`

```python
async def switch_project(project_name: str) -> PipelineConfig
```

Sets `active_project` in `pipeline-config.json` and returns the updated config. Raises `ValueError` when `project_name` does not match any entry in `PipelineConfig.projects`.
