# Agent Task Context — DB-Backed Specification

> **Version:** 0.1.1 
> **Source:** `ACAgentRun` DB row  
> **Access:** `ac://runs/{run_id}/context` MCP resource · `task/briefing` MCP prompt  
> **Parsed by:** `agentception/db/queries.py` → `RunContextRow` TypedDict

---

## Overview

Every agent run is fully described by a row in the `agent_runs` table.  Agents read
their task context exclusively from the database — no file is written to the worktree.

Two read paths are available:

| Path | Returns | When to use |
|------|---------|-------------|
| `ac://runs/{run_id}/context` | Full `RunContextRow` as JSON | Machine consumption — tool calls, automated processing |
| `task/briefing` (MCP prompt) | Natural-language briefing assembled from the DB row | Human-readable startup message for the agent loop |

---

## `RunContextRow` Field Reference

All fields are present in the JSON returned by `ac://runs/{run_id}/context`.

### Identity

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | Unique run identifier. Format: `"issue-42-a1b2c3"` \| `"label-ac-workflow-x9y8"` \| `"adhoc-1a2b3c"` |
| `status` | string | Execution state — see **Status values** below |
| `spawned_at` | string | ISO 8601 UTC timestamp when the run was created |
| `last_activity_at` | string \| null | ISO 8601 UTC timestamp of most-recent status update |
| `completed_at` | string \| null | ISO 8601 UTC timestamp of terminal-state transition |

**Status values:**

| Value | Meaning |
|-------|---------|
| `pending_launch` | Created, waiting for the agent loop to start |
| `implementing` | Agent loop is active |
| `reviewing` | PR reviewer is active |
| `blocked` | Agent self-reported a blocker |
| `completed` | Successful finish |
| `failed` | Unrecoverable error |
| `cancelled` | Manually stopped or iteration limit hit |
| `stopped` | Graceful stop (e.g. worktree reaped) |

---

### Agent identity

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | Role slug: `"developer"`, `"pr-reviewer"`, `"engineering-coordinator"`, etc. |
| `tier` | string \| null | Execution tier: `"coordinator"` \| `"worker"` |
| `org_domain` | string \| null | UI hierarchy slot: `"c-suite"` \| `"engineering"` \| `"qa"` |
| `cognitive_arch` | string \| null | `"figure:skill1:skill2"` resolved by `resolve_arch.py` |

---

### Target

| Field | Type | Description |
|-------|------|-------------|
| `issue_number` | int \| null | GitHub issue number this run works on |
| `pr_number` | int \| null | GitHub PR number (set when the run opens a PR) |
| `task_description` | string \| null | Inline task description for ad-hoc runs |

---

### Repository

| Field | Type | Description |
|-------|------|-------------|
| `gh_repo` | string \| null | GitHub slug: `"owner/repo"` |
| `branch` | string \| null | Git branch name for this run's worktree |
| `worktree_path` | string \| null | Absolute container path to the worktree |

---

### Pipeline lineage

| Field | Type | Description |
|-------|------|-------------|
| `batch_id` | string \| null | Coordinator-level batch fingerprint. Format: `"eng-20260303T134821Z-a7f2"` |
| `parent_run_id` | string \| null | `run_id` of the agent that physically spawned this run. Null for root dispatches |
| `coord_fingerprint` | string \| null | Spawning coordinator's human-readable fingerprint for GitHub comments. Format: `"Engineering Coordinator · <batch_id>"` |
| `is_resumed` | bool | `true` when this is a retry of a previously cancelled/stale run |

---

## Reading task context at startup

The agent loop calls `task/briefing` with the `run_id` to get the complete initial
message.  Agents may also call `ac://runs/{run_id}/context` directly for structured
access to any field:

```
# Via MCP prompt (natural language)
get_prompt("task/briefing", arguments={"run_id": "<run_id>"})

# Via MCP resource (structured JSON)
read_resource("ac://runs/{run_id}/context")
```

Both sources are authoritative — they read the same DB row.

---

## Related resources

| URI | Contents |
|-----|---------|
| `ac://runs/{run_id}/context` | This spec — full `RunContextRow` as JSON |
| `ac://runs/{run_id}/events` | Structured event log for this run |
| `ac://runs/{run_id}/children` | Child runs spawned by this run |
| `ac://runs/active` | All currently active runs |
| `ac://runs/pending` | Runs in `pending_launch` state |
| `ac://batches/{batch_id}/tree` | Full batch run tree |

---

## Planning pipeline (coordinator/conductor)

Coordinator and conductor agents receive their full context exclusively via
`task/briefing` and `ac://runs/{run_id}/context` — the same as all other agents.
No external files are written or read during dispatch.
