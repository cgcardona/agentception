# Poller Reference

The AgentCeption poller is the heartbeat of the pipeline.  It runs on a fixed
cadence, reads live state from GitHub and the filesystem, persists it to the
database, and drives all automated lifecycle transitions.

---

## Polling loop architecture and tick cadence

The polling loop lives in `agentception/poller.py`.  It runs as a long-lived
`asyncio` task started during application lifespan (`agentception/app.py`).

Each **tick** performs the following steps in order:

1. **GitHub reads** — fetch open issues, open PRs, closed issues, and merged PRs
   via the `gh` CLI wrappers in `agentception/readers/github.py`.
2. **Filesystem reads** — enumerate live git worktrees via
   `agentception/readers/git.py`.
3. **State assembly** — build a `PipelineState` snapshot from the combined
   GitHub + filesystem data.
4. **Alert detection** — call `detect_alerts()` to find stale claims, stalled
   agents, and out-of-order PRs.
5. **DB persist** — call `persist_tick()` in `agentception/db/persist.py` to
   upsert all entities and run the orphan sweep.
6. **Phase advance** — optionally advance the active phase label when all issues
   in the current phase are resolved.

Default tick cadence: **30 seconds** (configurable via `settings.poll_interval`).

---

## Orphan sweep

### What triggers it

The orphan sweep runs inside `_upsert_agent_runs()`, which is called on every
tick.  It fires after the live-agent upsert loop so `live_ids` (the set of run
IDs backed by a live worktree) is fully populated.

### What statuses are affected

Only runs whose `status` is in `_ACTIVE_STATUSES` are candidates:

```
pending_launch, implementing, blocked, reviewing, stalled, recovering
```

Terminal statuses (`completed`, `failed`, `cancelled`, `stopped`) are never
touched — the query filter excludes them before the sweep loop runs.

### The `build_complete_run` gate

The sweep uses the presence of a `build_complete_run` event in `agent_events`
as the authoritative completion signal — **not** `pr_number`.

Rationale: an agent can open a PR and then crash before calling
`build_complete_run`.  In that case `pr_number` is set but the agent did not
finish cleanly.  The explicit event is the only reliable signal.

Decision logic for each orphan (run not in `live_ids`, `issue_number` is not
`None`, `role != 'reviewer'`):

```
has_complete_event = COUNT(agent_events WHERE agent_run_id = orphan.id
                           AND event_type = 'build_complete_run') > 0

if has_complete_event:
    pass  # already completed — do not mutate
else:
    orphan.status = "failed"
    INSERT agent_events(event_type='orphan_failed', ...)
```

### Guards — runs the sweep never touches

| Guard | Reason |
|-------|--------|
| `role == 'reviewer'` | Reviewer lifecycle is driven by `build_complete_run`, never by poller inference.  Reviewer runs have `pr_number` set at dispatch time, so the old `pr_number → completed` heuristic would kill them immediately. |
| `issue_number is None` | Ad-hoc runs are managed by their asyncio task lifecycle, not by the GitHub polling loop. |
| Status already terminal | The `_ACTIVE_STATUSES` filter on the query excludes `completed`, `failed`, `cancelled`, and `stopped` before the loop runs. |

---

## Alert detection

`detect_alerts()` in `agentception/poller.py` produces three alert classes per
tick:

### 1. Stale claims

An `agent/wip` label is on a GitHub issue but no live worktree exists for that
issue.  The poller auto-heals by calling `clear_wip_label()` so the issue
becomes available for re-spawn.

### 2. Out-of-order PRs

An open PR carries an `agentception/<phase>` label that no longer matches the
currently active phase.  Surfaced as an alert string; no auto-heal.

### 3. Stalled agents — two-signal detection

**Primary (DB heartbeat):** `last_activity_at` is older than
`stall_threshold_seconds` (default: 1800 s / 30 min).  The run is promoted to
`AgentStatus.STALLED` and a `StalledAgentEvent` is emitted.

**Secondary (git commit):** `worktree_last_commit_time()` is older than the
threshold while `last_activity_at` is still fresh.  Advisory warning only — no
`STALLED` promotion.

The DB heartbeat is the authoritative signal.  Agents call
`persist_run_heartbeat()` (via the `build_heartbeat` MCP tool) to keep
`last_activity_at` fresh.

---

## `agent_events` event types emitted by the poller

| `event_type` | Emitted by | Meaning |
|---|---|---|
| `orphan_failed` | `_upsert_agent_runs()` orphan sweep | A run whose worktree disappeared without a `build_complete_run` event was marked `failed`.  Payload: `{"reason": "worktree_gone_no_build_complete"}`. |

### Event types emitted by agents (for reference)

| `event_type` | Emitted by | Meaning |
|---|---|---|
| `done` | `persist_agent_event()` via `build_complete_run` MCP tool | Agent finished work and opened a PR. |
| `build_complete_run` | `complete_agent_run()` in `persist.py` | Authoritative completion gate written atomically with the `completed` status transition. |
| `step_start` | Agent tool loop | Agent started a named execution step. |
| `blocker` | Agent tool loop | Agent encountered a blocker and called `build_block_run`. |
