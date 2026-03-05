# Integration Guide

This guide covers how to integrate external tools, scripts, and workflows with AgentCeption's browser UI and localStorage state.

---

## Batch Context Bar

The **batch context bar** is a slim persistent strip rendered immediately below the top navigation on every page. It shows the currently active `batch_id` and provides quick navigation links to the Plan, Build, and Ship pages for that batch.

### How it works

The bar is driven entirely by `localStorage`. No server session is involved. When a batch is started (e.g. from the Plan page or via the MCP `build_kickoff` tool), the following keys are written:

| Key | Type | Description |
|-----|------|-------------|
| `ac_active_batch` | `string` | The active `batch_id` (e.g. `eng-20260304T230644Z-5a86`) |
| `ac_active_initiative` | `string` | The active initiative name used for the Build page link |

The bar renders only when `ac_active_batch` is non-empty. It hides automatically when the key is absent or blank.

### Dismissing the bar

Clicking the **✕** button calls `dismiss()`, which:

1. Removes `ac_active_batch` from `localStorage`.
2. Removes `ac_active_initiative` from `localStorage`.
3. Hides the bar immediately via Alpine's `x-show` binding.

The same effect can be triggered programmatically from any page:

```js
localStorage.removeItem('ac_active_batch');
localStorage.removeItem('ac_active_initiative');
```

### Setting the active batch from code

Any page or script can activate the bar by writing to `localStorage`:

```js
localStorage.setItem('ac_active_batch', 'eng-20260304T230644Z-5a86');
localStorage.setItem('ac_active_initiative', 'my-initiative-name');
```

The Alpine component listens for `storage` events, so the bar updates instantly in any tab that has the page open.

### Navigation links

| Link | Destination |
|------|-------------|
| Plan | `/plan` |
| Build | `/build?initiative=<ac_active_initiative>` |
| Ship | `/ship?batch=<ac_active_batch>` |

Build and Ship pages should call `localStorage.setItem(...)` on `init()` when the corresponding query param is present, so that arriving via a direct link also populates the bar.

### Alpine component

The component lives in `agentception/static/js/base.js` and is exported as `batchBar`. It is registered globally in `app.js` and referenced in `base.html` via:

```html
<div class="batch-bar"
     x-data="batchBar()"
     x-show="batchId"
     x-init="init()"
     x-cloak>
  ...
</div>
```

The `x-cloak` attribute prevents a flash of the bar before Alpine initialises. The `[x-cloak] { display: none !important }` rule is defined in `_foundation.scss`.

---

## ENRICHED_MANIFEST: .agent-task format

When `POST /api/plan/launch` spawns a coordinator agent via `plan_spawn_coordinator()`, it writes an `.agent-task` file to the coordinator's git worktree. That file uses a structured key-value format with one special multi-line block: `ENRICHED_MANIFEST:`.

### File format

```
WORKFLOW=bugs-to-issues
BATCH_ID=coordinator-20260305-142201
BRANCH=coordinator/20260305-142201
WORKTREE=/tmp/worktrees/coordinator-20260305-142201

ENRICHED_MANIFEST:
```json
{
  "initiative": "my-feature",
  "phases": [...],
  "total_issues": 12,
  "estimated_waves": 4
}
```
```

The single-line key-value pairs above `ENRICHED_MANIFEST:` are parsed as `KEY=VALUE`. Everything between the opening ` ```json ` fence and its closing ` ``` ` is the JSON payload — parse it as `EnrichedManifest`.

### EnrichedManifest schema

| Field | Type | Description |
|-------|------|-------------|
| `initiative` | `string \| null` | Human-readable batch name (e.g. `"auth-rewrite"`). Optional. |
| `phases` | `EnrichedPhase[]` | Ordered phase list. Index 0 is the earliest (no deps). |
| `total_issues` | `int` | Computed invariant — sum of all `phases[*].issues` lengths. Do not override. |
| `estimated_waves` | `int` | Computed invariant — critical-path length through the full dependency graph. |

#### EnrichedPhase fields

| Field | Type | Description |
|-------|------|-------------|
| `label` | `string` | Short slug used as the GitHub phase label (e.g. `"0-foundation"`). |
| `description` | `string` | One-sentence summary of the phase. |
| `depends_on` | `string[]` | Labels of phases that must fully complete before this phase begins. Empty for the first phase. |
| `issues` | `EnrichedIssue[]` | Fully-specified GitHub issue payloads for this phase. |
| `parallel_groups` | `string[][]` | Partition of issue titles into concurrent batches. Each sub-list is one execution wave. |

#### EnrichedIssue fields

| Field | Type | Description |
|-------|------|-------------|
| `title` | `string` | GitHub issue title. |
| `body` | `string` | Full Markdown issue body (pre-written by the LLM). |
| `labels` | `string[]` | GitHub label names to apply at creation time. |
| `phase` | `string` | Phase label this issue belongs to. |
| `depends_on` | `string[]` | Titles (not numbers) of issues that must be merged before this one begins. |
| `can_parallel` | `bool` | Whether this issue can run concurrently with others in its `parallel_groups` entry. |
| `acceptance_criteria` | `string[]` | Checklist items that define "done" for this issue. |
| `tests_required` | `string[]` | Test cases that must be written. |
| `docs_required` | `string[]` | Documentation sections that must be updated. |

### Invariants guaranteed by `/api/plan/launch`

- **No dependency cycles** — the `depends_on` graph is a DAG. The API validates this before writing the `.agent-task` file.
- **No intra-group dependencies** — no title in a `parallel_groups` entry may appear in the `depends_on` list of any other title in the same group.
- **`total_issues` and `estimated_waves` are computed** — they are derived by `EnrichedManifest` model validators and are always consistent with the actual data.

Coordinator agents reading this block must **execute** — not re-validate or re-interpret. See `coordinator.md` for the execution loop.

### Producing an ENRICHED_MANIFEST: block

Use the `plan_spawn_coordinator` MCP tool:

```python
result = await plan_spawn_coordinator(manifest_json)
# Returns: {"worktree": str, "branch": str, "agent_task_path": str, "batch_id": str}
```

Or call `POST /api/plan/launch` from the Build dashboard — it invokes `plan_spawn_coordinator` internally and returns the same shape.

Do not write `.agent-task` files with `ENRICHED_MANIFEST:` blocks by hand. Always go through `plan_spawn_coordinator` so the manifest is validated before it reaches the coordinator.
