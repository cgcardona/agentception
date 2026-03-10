# Dispatching Agents

This guide covers the one canonical way to launch an agent in AgentCeption: `POST /api/dispatch/issue`. Until the Ship UI is wired to call this endpoint directly, you trigger it manually (via `curl`, a script, or the Build dashboard). This document exists so the flow is never re-invented or confused with the old adhoc path.

---

## The single canonical flow

```
POST /api/dispatch/issue
        │
        ├─ 1. git worktree add   (isolated checkout at /worktrees/issue-{N})
        ├─ 2. configure worktree auth  (_configure_worktree_auth embeds GITHUB_TOKEN
        │                              so git push works inside the container)
        ├─ 3. Qdrant pre-inject  (top-3 semantically relevant code chunks added
        │                         to task_description at dispatch time)
        ├─ 4. persist DB row     (status = pending_launch, all context stored)
        ├─ 5. acknowledge        (pending_launch → implementing)
        ├─ 6. asyncio.create_task(run_agent_loop)   ← Anthropic agent starts here
        └─ 7. asyncio.create_task(_index_worktree)  ← background Qdrant index
                │
                ▼
        response: {"status": "implementing", "run_id": "issue-35", ...}
```

By the time you get the JSON response, the agent is already running against Anthropic. No Cursor session, no external Dispatcher, no second HTTP call required.

---

## Request shape

```bash
curl -s -X POST http://localhost:10003/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 35,
    "issue_title":  "Idempotent GitHub wrappers: ensure_branch, ensure_pull_request, ensure_worktree",
    "issue_body":   "",
    "role":         "developer",
    "repo":         "agentception"
  }'
```

| Field | Required | Notes |
|-------|----------|-------|
| `issue_number` | yes | GitHub issue number — used for `run_id = "issue-{N}"` and the branch `feat/issue-{N}` |
| `issue_title` | yes | Injected into the agent's task briefing and used as the Qdrant search query |
| `issue_body` | no | Full issue body text; drives cognitive arch selection and task briefing. Pass `""` to let the agent read the body itself via `issue_read` |
| `role` | yes | Role slug matching a file in `.agentception/roles/` — typically `"developer"` for leaf workers |
| `repo` | yes | `owner/repo` string — e.g. `"agentception"` (short form resolved against `settings.gh_repo`) |

### Re-dispatching a failed or cancelled run

If a run ended in `failed` or `cancelled`, reset the DB row before calling the endpoint again. The dispatch endpoint 409s on an existing worktree — tear that down first:

```bash
# 1. Remove the stale worktree (inside the container)
docker compose exec agentception git -C /app worktree remove --force /worktrees/issue-35
docker compose exec agentception git -C /app branch -D feat/issue-35

# 2. Reset the DB row so the endpoint doesn't treat it as a duplicate
docker compose exec postgres psql -U agentception agentception \
  -c "UPDATE agent_runs SET status='failed' WHERE id='issue-35';"

# 3. Dispatch again — fresh worktree, fresh agent loop
curl -s -X POST http://localhost:10003/api/dispatch/issue \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 35, "issue_title": "...", "issue_body": "", "role": "developer", "repo": "agentception"}'
```

> **Why reset to `failed` and not `cancelled`?** `cancelled` is a terminal state the endpoint respects as a hard stop. `failed` signals "previous attempt ended badly — retry is allowed."

---

## Response shape

```json
{
  "run_id":       "issue-35",
  "worktree":     "/worktrees/issue-35",
  "host_worktree": "/Users/you/.agentception/worktrees/agentception/issue-35",
  "branch":       "feat/issue-35",
  "batch_id":     "issue-35-20260310T010149Z-37c6",
  "status":       "implementing"
}
```

`status: "implementing"` confirms the agent loop is live. A `status: "pending_launch"` response means you have an older deployment without [PR #434](https://github.com/cgcardona/agentception/pull/434) — update before proceeding.

---

## Watching a run

```bash
python3 -u scripts/watch_run.py issue-35
```

Each iteration line shows:

```
╔══ ITER 3/100  [claude…]
    in=24,007  cache_write=0  cache_read=19,777  history=14msgs
💬 (71ch) Now I'll log my plan and start implementing the three helper functions.
╚══ → 2 tool calls
```

| Field | Meaning |
|-------|---------|
| `in=` | Input tokens this turn (tool definitions + history + system prompt) |
| `cache_write=` | Tokens written to Anthropic's prompt cache (first turn only) |
| `cache_read=` | Tokens served from cache (turns 2-N at ~10% cost) |
| `history=Nmsgs` | Pruned conversation window size |

The tool catalogue and system prompt are cached after turn 1. A `cache_read` ≥ 18 000 on turn 2 is the confirmation that caching is active.

---

## What happens inside the agent

The agent reads its full task context from the DB at startup via the `task/briefing` MCP prompt — it never needs to call `issue_read` for the issue body if `issue_body` was passed at dispatch time. The recon phase (pre-loop) runs one LLM call that produces a JSON exploration plan; the runtime executes all reads and searches concurrently and injects the results into the initial user message, collapsing 5-10 discovery turns into zero.

Typical successful run shape:

| Phase | Turns | What happens |
|-------|-------|--------------|
| Recon | 0 (pre-loop) | Parallel file reads + searches injected as context |
| Understand | 1–3 | Agent reads relevant symbols, confirms plan |
| Implement | 4–N | Writes functions, updates callers |
| Verify | N+1 to N+5 | `mypy`, `typing_audit`, `pytest` inside the container |
| Ship | last 2 | `git_commit_and_push`, `create_pull_request`, `merge_pull_request` |

---

## What the agent must do to finish

Agents are expected to self-complete without human intervention. In order, the agent must:

1. Write the implementation.
2. Run `docker compose exec agentception mypy agentception/ agentception/tests/` — zero errors.
3. Run `docker compose exec agentception python3 tools/typing_audit.py --dirs agentception/ agentception/tests/ --max-any 0` — passes.
4. Run `docker compose exec agentception pytest agentception/tests/ -v` — all green.
5. Run `docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py --check` — no drift (if `.j2` templates were edited, run without `--check` first).
6. `git_commit_and_push` on `feat/issue-{N}`.
7. `create_pull_request` (GitHub MCP) — head `feat/issue-{N}`, base `dev`.
8. `merge_pull_request` (GitHub MCP) — squash, `deleteBranch: true`.
9. Call `build_complete_run` MCP tool to transition the run to `completed`.

---

## Iteration budget

The default limit is 100 turns (`_DEFAULT_MAX_ITERATIONS`). An agent that hits the limit is killed and the worktree is torn down by the reaper. If this happens:

- Check `watch_run` output to see where it got stuck.
- Look for repeated reads of the same file (working memory `files_examined` should prevent this), search loops, or stalled `mypy` fixes.
- Re-dispatch — the per-run Qdrant index will be rebuilt and the recon phase re-runs.

---

## Run states

```
pending_launch  →  implementing  →  completed   (happy path)
                              ↓
                           blocked  →  implementing  (resumed after blocker clears)
                              ↓
                           failed                    (limit hit, crash, or explicit error)
                              ↓
                           cancelled                 (human-terminated, not retryable)
```

`POST /api/dispatch/issue` sets `pending_launch` in the DB and immediately acknowledges to `implementing` before returning. There is no window where the TTL sweep can kill the run.

---

## Source map

| Concern | File |
|---------|------|
| Dispatch endpoint | `agentception/routes/api/dispatch.py::dispatch_agent` |
| Agent loop | `agentception/services/agent_loop.py::run_agent_loop` |
| Worktree auth | `agentception/services/run_factory.py::_configure_worktree_auth` |
| Worktree indexing | `agentception/services/run_factory.py::_index_worktree` |
| DB persistence | `agentception/db/persist.py::persist_agent_run_dispatch` |
| Run acknowledgement | `agentception/db/persist.py::acknowledge_agent_run` |
| Watch script | `scripts/watch_run.py` |
