# AgentCeption — Type Contracts Reference

This document lists the public Pydantic models and functions that form the API contracts between AgentCeption's internal layers. Each entry includes its source path, field table, and producer/consumer annotations.

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
