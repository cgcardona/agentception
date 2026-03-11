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

By the time you get the JSON response, the planner has already run and the
executor agent is live against Anthropic. The reviewer fires automatically at
the end — you never need to dispatch it manually.

---

## Request shape

### Implementation dispatch (the one you call)

```bash
curl -s -X POST http://localhost:10003/api/dispatch/issue \
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
curl -s -X POST http://localhost:10003/api/dispatch/issue \
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
curl -s -X POST http://localhost:10003/api/dispatch/issue \
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
| `issue_body` | no | Full issue body text; drives the planner and cognitive arch selection. Pass `""` to let the agent read it via `issue_read` |
| `role` | yes | `"developer"` for implementation (planner → executor pipeline). `"reviewer"` for review. Never pass `"executor"` or `"planner"` — those are internal |
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
curl -s -X POST http://localhost:10003/api/control/reset-build \
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
curl -s -X POST http://localhost:10003/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 35, "issue_title": "...", "issue_body": "...", "role": "developer", "repo": "cgcardona/agentception"}'
```

The dispatch endpoint handles stale worktrees automatically: if the worktree
directory already exists for a `failed` or `cancelled` run, `ensure_worktree`
resets it to a clean state from `origin/dev` before the planner runs.

---

## What happens inside the agent

**Planner (synchronous, before the agent loop starts):** One structured LLM
call reads the issue body and the pre-injected code chunks, then outputs an
`ExecutionPlan` — a list of atomic file operations (`write_file`,
`replace_in_file`, `insert_after_in_file`) with all parameters pre-filled.
The plan is stored in the DB.

**Executor (agent loop, role = `"executor"`):** Receives the formatted
`ExecutionPlan` in its task briefing. Applies each operation mechanically,
runs `mypy` + `pytest`, commits, opens a PR, and calls `build_complete_run`.
The executor never reads the codebase speculatively — all context was
pre-supplied by the planner.

**Auto-reviewer (fires on `build_complete_run`):** `build_complete_run`
releases the executor's worktree (directory only — branch kept for the open
PR), then fires `auto_dispatch_reviewer` as a background task. The reviewer
gets its own worktree on the same branch, verifies correctness, and merges or
rejects.

Typical turn counts per tier:

| Tier | Turns | What happens |
|------|-------|--------------|
| Planner | 0 (pre-loop, 1 LLM call) | Reads issue + code chunks, outputs ExecutionPlan |
| Executor | 5–20 | Applies plan, runs mypy + pytest, commits, opens PR |
| Reviewer | 3–10 | Reads diff, verifies tools, grades, merges or rejects |

---

## What the executor must do to finish

1. Apply every operation in the `ExecutionPlan` using `write_file` or `replace_in_file`.
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
| Planner | `agentception/services/planner.py` |
| Executor agent loop | `agentception/services/agent_loop.py::run_agent_loop` |
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
