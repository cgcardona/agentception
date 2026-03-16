# Dispatching Agents

This guide covers the one canonical way to launch an agent in AgentCeption:
`POST /api/dispatch/issue`. The endpoint is called via `curl`, a script, or the
Build dashboard. It returns immediately once the agent loop is live.

---

## The three-tier pipeline

Every implementation task moves through three tiers automatically. You trigger
the first one; the others fire themselves.

```
POST /api/dispatch/issue  (role: "developer")
        │
        ├─ 1. git worktree add   (isolated checkout at /worktrees/issue-{N},
        │                         branched from origin/dev)
        ├─ 2. configure worktree auth  (GITHUB_TOKEN embedded so git push works
        │                              inside the container)
        ├─ 3. Qdrant pre-inject  (top-3 semantically relevant code chunks added
        │                         to task_description at dispatch time)
        ├─ 4. Planner LLM call   (synchronous — one structured LLM call produces
        │                         an ExecutionPlan stored in the DB)
        │        • success → effective role becomes "executor"
        │        • failure → falls back to "developer" (full tool surface)
        ├─ 5. persist DB row     (status = pending_launch, effective role stored,
        │                         pr_number written if provided)
        ├─ 6. acknowledge        (pending_launch → implementing)
        ├─ 7. asyncio.create_task(run_agent_loop)   ← Executor agent starts here
        └─ 8. asyncio.create_task(_index_worktree)  ← background Qdrant index
                │
                ▼
        response: {"status": "implementing", "run_id": "issue-35", ...}

        [Executor runs, writes code, opens PR, calls build_complete_run]
                │
                ▼
        build_complete_run fires auto_dispatch_reviewer
                │
                ├─ release executor worktree  (directory removed, branch kept for PR)
                └─ POST /api/dispatch/issue  (role: "reviewer", internal)
                        │
                        ├─ git fetch origin feat/issue-{N}
                        ├─ git worktree add /worktrees/review-{N} feat/issue-{N}
                        └─ asyncio.create_task(run_agent_loop)
                                │
                                ▼
                        Reviewer verifies mypy + pytest, grades, merges or rejects
```

By the time you get the JSON response, the context assembler has already run and
the developer agent is live against Anthropic. The reviewer fires automatically
at the end — you never need to dispatch it manually.

---

## Request shape

### Implementation dispatch (the one you call)

```bash
curl -s -X POST http://localhost:1337/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 35,
    "issue_title":  "Idempotent GitHub wrappers: ensure_branch, ensure_worktree",
    "issue_body":   "## Context\n...",
    "role":         "developer",
    "repo":         "cgcardona/agentception"
  }'
```

> **Always pass `issue_body`** — it drives the planner's ExecutionPlan and the
> cognitive architecture selection. Pass `""` only for doc-only tickets where
> the body is short enough for the agent to fetch itself via `issue_read`.

### Manual PR reviewer dispatch (rare — auto-fires on completion normally)

```bash
curl -s -X POST http://localhost:1337/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 35,
    "issue_title":  "PR review for feat/issue-35 (#436)",
    "issue_body":   "Review PR #436 (feat/issue-35). Run mypy, typing_audit, pytest. Merge if acceptable.",
    "role":         "reviewer",
    "repo":         "cgcardona/agentception",
    "pr_number":    436
  }'
```

If the PR branch does **not** follow the `feat/issue-{N}` convention, pass
`pr_branch` explicitly:

```bash
curl -s -X POST http://localhost:1337/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 35,
    "issue_title":  "PR review for feat/custom-branch (#437)",
    "issue_body":   "Review PR #437.",
    "role":         "reviewer",
    "repo":         "cgcardona/agentception",
    "pr_number":    437,
    "pr_branch":    "feat/custom-branch"
  }'
```

> **Reviewers must be dispatched before the PR is merged.** Once a branch is
> deleted, `git fetch` returns `fatal: couldn't find remote ref …` and the
> endpoint returns HTTP 422.

### Request fields

| Field | Required | Notes |
|-------|----------|-------|
| `issue_number` | yes | GitHub issue number — used for `run_id = "issue-{N}"` and the worktree slug |
| `issue_title` | yes | Injected into the task briefing; used as the Qdrant search query |
| `issue_body` | no | Full issue body text; drives context assembly and cognitive arch selection. Pass `""` to let the agent read it via `issue_read` |
| `role` | yes | `"developer"` for implementation. `"reviewer"` for review. |
| `repo` | yes | `owner/repo` string — e.g. `"cgcardona/agentception"` |
| `pr_number` | no | PR number to associate with this run. Required for `reviewer` dispatches. Omit for implementers — the executor self-reports it via `build_complete_run` |
| `pr_branch` | no | Exact remote branch name for the PR. `reviewer` only. Omit when the branch follows `feat/issue-{N}` naming |

---

## Response shape

```json
{
  "run_id":        "issue-35",
  "worktree":      "/worktrees/issue-35",
  "host_worktree": "/Users/you/.agentception/worktrees/agentception/issue-35",
  "branch":        "feat/issue-35",
  "batch_id":      "issue-35-20260310T010149Z-37c6",
  "status":        "implementing"
}
```

`status: "implementing"` confirms the executor agent loop is live.

---

## Watching a run

```bash
python3 -u scripts/watch_run.py issue-35
```

Each iteration line shows:

```
╔══ ITER 3/100  [claude…]
    in=24,007  cache_write=0  cache_read=19,777  history=14msgs
💬 (71ch) Now I'll write the implementation.
╚══ → 2 tool calls
```

| Field | Meaning |
|-------|---------|
| `in=` | Input tokens this turn (tool definitions + history + system prompt) |
| `cache_write=` | Tokens written to Anthropic's prompt cache (first turn only) |
| `cache_read=` | Tokens served from cache (turns 2-N at ~10% cost) |
| `history=Nmsgs` | Pruned conversation window size |

A `cache_read` ≥ 18 000 on turn 2 confirms caching is active.

---

## Killing an agent

### Soft kill — mark failed and clean up (preferred)

Stops all active agents, removes all worktrees, clears all `agent/wip` labels,
and resets all active DB runs to `failed`. **Idempotent.**

```bash
curl -s -X POST http://localhost:1337/api/control/reset-build \
  -H "Content-Type: application/json" | python3 -m json.tool
```

Response:

```json
{
  "removed_worktrees": ["issue-35", "review-35"],
  "cleared_wip_labels": [35],
  "runs_reset": 2,
  "errors": []
}
```

`reset-build` sets all `pending_launch`, `implementing`, and `reviewing` runs to
`failed`. Runs in `failed` can be re-dispatched. Runs in `cancelled` cannot.

> `reset-build` does **not** delete remote branches or close open PRs. The
> worktree directory is removed; the branch lives on GitHub so any open PR
> survives.

### Hard kill — restart the container

Use this when the agent loop is stuck and `reset-build` is not enough to stop
it (e.g. the process is sleeping inside `asyncio.sleep`):

```bash
docker compose restart agentception
```

The container restarts in ~5 s. All in-flight agent loops are killed. The DB
rows are left in whatever state they were — run `reset-build` after restart to
clean them up, or leave them as `failed` and re-dispatch.

### Single-run manual cleanup

When you want to cancel one specific run without touching others:

```bash
# 1. Mark the run failed in the DB
docker compose exec agentception-postgres psql -U agentception agentception \
  -c "UPDATE agent_runs SET status='failed' WHERE id='issue-35';"

# 2. Remove the worktree directory
docker compose exec agentception \
  git -C /app worktree remove --force /worktrees/issue-35

# 3. Prune stale worktree refs
docker compose exec agentception git -C /app worktree prune

# 4. Delete the local branch (if it exists)
docker compose exec agentception git -C /app branch -D feat/issue-35

# 5. Delete the remote branch ONLY if no PR is open for it
#    (deleting a branch with an open PR closes the PR on GitHub)
git push origin --delete feat/issue-35
```

> **Do not delete the remote branch if a PR is open.** GitHub closes the PR
> automatically when its head branch is deleted. If you want to preserve the
> PR, skip step 5.

---

## Re-dispatching a failed run

```bash
curl -s -X POST http://localhost:1337/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 35, "issue_title": "...", "issue_body": "...", "role": "developer", "repo": "cgcardona/agentception"}'
```

The dispatch endpoint handles stale worktrees automatically: if the worktree
directory already exists for a `failed` or `cancelled` run, `ensure_worktree`
resets it to a clean state from `origin/dev` before the agent loop starts.

---

## What happens inside the agent

**Context assembler (synchronous, before the agent loop starts):** Zero LLM
calls.  Runs 3 targeted Qdrant queries in parallel (~300 ms), then uses Python
`ast` to extract the exact enclosing function/class scope for each match.
Imports for each file are prepended.  The assembled context is appended to the
task briefing so the developer starts implementation from turn 1 with precise
code context — no file reads, no discovery loop.

**Developer (agent loop, role = `"developer"`):** Receives the issue body,
pre-injected Qdrant chunks, AC-referenced file content, and the pre-extracted
AST scope bodies.  Implements the changes, runs `mypy` + `pytest`, commits,
opens a PR, and calls `build_complete_run`.

**Auto-reviewer (fires on `build_complete_run`):** `build_complete_run`
releases the developer's worktree (directory only — branch kept for the open
PR), then fires `auto_dispatch_reviewer` as a background task. The reviewer
gets its own worktree on the same branch, verifies correctness, and merges or
rejects.

Typical turn counts per tier:

| Tier | Turns | What happens |
|------|-------|--------------|
| Context assembler | 0 (pre-loop, zero LLM calls, ~300 ms) | Qdrant + AST → scope bodies in briefing |
| Developer | 5–20 | Implements, runs mypy + pytest, commits, opens PR |
| Reviewer | 3–10 | Reads diff, verifies tools, grades, merges or rejects |

---

## What the developer must do to finish

1. Implement every requirement in the issue using `write_file`, `replace_in_file`, or `insert_after_in_file`.
2. Run `mypy agentception/ agentception/tests/` — zero errors.
3. Run `python3 tools/typing_audit.py --dirs agentception/ agentception/tests/ --max-any 0` — passes.
4. Run `pytest agentception/tests/ -v` — all green.
5. Run `python3 /app/scripts/gen_prompts/generate.py --check` — no drift (if `.j2` templates were edited, run without `--check` first then re-run with `--check`).
6. `git_commit_and_push` on `feat/issue-{N}`.
7. `create_pull_request` — head `feat/issue-{N}`, base `dev`.
8. Call `build_complete_run` — triggers the auto-reviewer and marks the run `completed`.

---

## Run states

```
pending_launch  →  implementing  →  completed   (happy path)
                              ↓
                           blocked  →  implementing  (resumed after blocker clears)
                              ↓
                           failed                    (limit hit, crash, or explicit cancel)
                              ↓
                           cancelled                 (human-terminated, not retryable)
```

`reset-build` sets all active runs to `failed` (retryable). Use direct SQL to
set a run to `cancelled` if you want to prevent re-dispatch.

---

## Iteration budget

Default: 100 turns (`_DEFAULT_MAX_ITERATIONS`). An agent that hits the limit is
killed and marked `failed`. The worktree reaper cleans up its directory (but
does **not** delete the remote branch if a PR is open).

If a run hits the limit:
- Check `watch_run` output to see where it got stuck.
- Look for repeated reads, search loops, or stalled `mypy` fixes.
- Re-dispatch — the recon phase re-runs with fresh context.

---

## Source map

| Concern | File |
|---------|------|
| Dispatch endpoint | `agentception/routes/api/dispatch.py` |
| Context assembler | `agentception/services/context_assembler.py` |
| Developer agent loop | `agentception/services/agent_loop.py::run_agent_loop` |
| Auto-reviewer trigger | `agentception/mcp/build_commands.py::build_complete_run` |
| Auto-reviewer dispatch | `agentception/services/auto_reviewer.py::auto_dispatch_reviewer` |
| Worktree creation | `agentception/readers/git.py::ensure_worktree` |
| Worktree release (dir only) | `agentception/services/teardown.py::release_worktree` |
| Full worktree teardown | `agentception/services/teardown.py::teardown_agent_worktree` |
| Worktree reaper | `agentception/services/worktree_reaper.py` |
| Reset-build endpoint | `agentception/routes/api/control.py::reset_build` |
| Worktree auth | `agentception/services/run_factory.py::_configure_worktree_auth` |
| DB persistence | `agentception/db/persist.py::persist_agent_run_dispatch` |
| Watch script | `scripts/watch_run.py` |

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

## Agent-task format for plan coordinator spawning

When `POST /api/plan/launch` spawns a coordinator agent via `build_spawn_child()`, it writes a DB context row to the coordinator's git worktree. That file uses **TOML format** (spec version `0.1.1`) with a special `[enriched]` section containing the JSON manifest payload.

### File format

```toml
[task]
version = "0.1.1"
workflow = "bugs-to-issues"
id = "3f4a9c2e-1b8d-4e7f-a6c5-9d2e8f0b1a3c"
created_at = 2026-03-05T14:22:01Z
attempt_n = 0
required_output = "phase_plan"
on_block = "stop"

[agent]
role = "coordinator"
tier = "coordinator"
cognitive_arch = "von_neumann:python"

[repo]
gh_repo = "cgcardona/agentception"
base = "dev"

[pipeline]
batch_id = "coordinator-20260305-142201"
wave = "coordinator-20260305-142201"

[spawn]
mode = "chain"
sub_agents = true

[worktree]
path = "/tmp/worktrees/coordinator-20260305-142201"
branch = "coordinator/20260305-142201"

[enriched]
total_issues = 12
estimated_waves = 4
manifest_json = """
{
  "initiative": "my-feature",
  "phases": [...],
  "total_issues": 12,
  "estimated_waves": 4
}
"""
```

The file is parsed by `tomllib.loads()`. The `[enriched]` section carries the full JSON manifest as a TOML multiline basic string. Parse `enriched.manifest_json` as `EnrichedManifest`.

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

- **No dependency cycles** — the `depends_on` graph is a DAG. The API validates this before writing the DB context row.
- **No intra-group dependencies** — no title in a `parallel_groups` entry may appear in the `depends_on` list of any other title in the same group.
- **`total_issues` and `estimated_waves` are computed** — they are derived by `EnrichedManifest` model validators and are always consistent with the actual data.

Coordinator agents reading this block must **execute** — not re-validate or re-interpret. See `coordinator.md` for the execution loop.

### Producing the enriched coordinator task file

Use the `build_spawn_child` MCP tool:

```python
result = await build_spawn_child(manifest_json)
# Returns: {"worktree": str, "branch": str, "agent_task_path": str, "batch_id": str}
# Note: agent_task_path now holds the worktree path only (DB context row is the canonical record)
```

Or call `POST /api/plan/launch` from the Build dashboard — it invokes `build_spawn_child` internally and returns the same shape.

Do not write DB context rows with `[enriched]` sections by hand. Always go through `build_spawn_child` so the manifest is validated before it reaches the coordinator.

