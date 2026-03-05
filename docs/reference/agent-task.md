# Agent Task Specification

> **Canonical source:** [`.agentception/agent-task-spec.md`](../../.agentception/agent-task-spec.md) — full field reference, all workflow types, complete examples.
>
> **Format:** [TOML 1.0](https://toml.io/en/v1.0.0)  
> **Parser:** `agentception/readers/worktrees.py` → `TaskFile` Pydantic model  
> **Version:** 2.0  
> **Model:** `TaskFile` in `agentception/models/__init__.py` covers the full TOML v2 spec (task, agent, repo, pipeline, spawn, target, worktree, output, domain, and `issue_queue` / `pr_queue` via `IssueSub` / `PRSub`).

---

## What is an `.agent-task` file?

Every agent worktree contains exactly one `.agent-task` file at its root. It is the **single source of truth** for that agent's identity, assignment, pipeline position, and execution constraints.

Two consumers read it with different goals:

| Consumer | What it reads | How |
|----------|--------------|-----|
| AgentCeption dashboard | Typed scalar fields for monitoring | `tomllib.loads()` → `TaskFile` model |
| Cursor's LLM (the agent) | Full raw text as natural language context | File read → context window |

Because the LLM reads the entire file, **any valid TOML you add is immediately available to the agent** — even fields AgentCeption doesn't formally parse. This makes the format extensible at zero cost.

---

## Section overview

```toml
[task]          # Core identity: workflow type, attempt number, required output
[agent]         # Who runs this task: role, tier, cognitive architecture
[repo]          # GitHub + git coordinates
[pipeline]      # Batch/wave lineage for traceability
[spawn]         # Orchestration: chain, single, or coordinator
[target]        # What this task acts on (issue, PR, or deliverable)
[worktree]      # Local filesystem path + branch
[output]        # Async result rendezvous (for Cursor-driven workflows)
[domain]        # Non-tech domain context (marketing, legal, ops, etc.)

[plan_draft]        # Payload for plan-spec workflow
[enriched]          # Pre-enriched coordinator manifest

[[issue_queue]]     # Sub-task list for coordinator agents
[[pr_queue]]        # PR review list for QA coordinators
[[deliverable_queue]] # Non-code deliverable list
```

---

## Quick field reference

### `[task]`

| Field | Type | Description |
|-------|------|-------------|
| `version` | `"2.0"` | Always `"2.0"` for current TOML format |
| `workflow` | string | One of `issue-to-pr`, `pr-review`, `coordinator`, `conductor`, `bugs-to-issues`, `plan-spec`, `task-to-deliverable` |
| `id` | UUID v4 | Unique identifier for this task instance |
| `created_at` | datetime | When this file was written |
| `attempt_n` | int | Retry counter — agent hard-stops when `> 2` |
| `required_output` | string | Artifact to produce: `pr_url`, `yaml_file`, `deliverable_path`, etc. |
| `on_block` | string | `"stop"` or `"escalate"` |

### `[agent]`

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | Role slug: `python-developer`, `cto`, `engineering-coordinator`, etc. |
| `logical_tier` | string | `executive`, `coordinator`, `engineer`, or `reviewer` |
| `cognitive_arch` | string | `"figure:skill1:skill2"` — composed by `resolve_arch.py` |
| `node_type` | string | `coordinator` or `leaf` — drives dispatch behavior |

### `[repo]`

| Field | Type | Description |
|-------|------|-------------|
| `gh_repo` | string | `"owner/repo"` — never derived from local path |
| `base` | string | Base branch: `"dev"` |

### `[pipeline]`

| Field | Type | Description |
|-------|------|-------------|
| `batch_id` | string | Batch fingerprint: `"label-ac-workflow-20260101T000000Z-abcd"` |
| `parent_run_id` | string | `run_id` of the spawning agent (empty for CTO root) |
| `wave` | string | Named phase wave (e.g. `"0-foundation"`) |

### `[spawn]`

| Field | Type | Description |
|-------|------|-------------|
| `mode` | string | `"chain"` (auto-spawn reviewer), `"single"` (do and stop), `"coordinator"` (spawn leaf agents from queue) |
| `sub_agents` | bool | `true` → act as sub-coordinator |
| `max_concurrent` | int | Safety valve for concurrent leaf agents (default: 4) |

### `[target]`

Fields depend on workflow. Key fields:

| Field | Workflows | Description |
|-------|-----------|-------------|
| `issue_number` | `issue-to-pr` | GitHub issue number |
| `depends_on` | `issue-to-pr` | Issue numbers that must merge first |
| `closes` | `issue-to-pr` | Issues to close when PR merges |
| `file_ownership` | `issue-to-pr` | Files this agent owns (conflict prevention) |
| `pr_number` | `pr-review` | Pull request number |
| `grade_threshold` | `pr-review` | Minimum grade to merge: `"A"`, `"B"`, `"C"` |
| `deliverable_type` | `task-to-deliverable` | Type of non-code artifact |

### `[worktree]`

| Field | Type | Description |
|-------|------|-------------|
| `path` | string | Absolute path to this worktree |
| `branch` | string | Git branch name |
| `linked_pr` | int | Written back by agent after PR opens (0 until then) |

### `[output]`

Used when Cursor does LLM work and writes a result to disk.

| Field | Type | Description |
|-------|------|-------------|
| `path` | string | Path Cursor writes its output to |
| `draft_id` | UUID | Correlates to the dashboard request |
| `format` | string | `"yaml"`, `"json"`, `"markdown"`, `"toml"` |
| `schema_tool` | string | MCP tool to call for the output schema |

---

## Minimal example (issue-to-PR)

```toml
[task]
version = "2.0"
workflow = "issue-to-pr"
id = "3f4a9c2e-1b8d-4e7f-a6c5-9d2e8f0b1a3c"
created_at = 2026-03-03T13:48:21Z
attempt_n = 0
required_output = "pr_url"
on_block = "stop"

[agent]
role = "python-developer"
logical_tier = "engineer"
node_type = "leaf"
cognitive_arch = "turing:python"

[repo]
gh_repo = "cgcardona/agentception"
base = "dev"

[pipeline]
batch_id = "label-ac-workflow-20260303T134821Z-a7f2"
parent_run_id = "label-ac-workflow-20260303T134000Z-x9y1"
wave = "0-foundation"

[spawn]
mode = "chain"
sub_agents = false

[target]
issue_number = 41
issue_title = "UI: wire plan.js to /api/plan/draft + SSE plan_draft_ready"
depends_on = []
closes = [41]
file_ownership = ["agentception/static/js/plan.js"]

[worktree]
path = "/tmp/worktrees/issue-41"
branch = "feat/issue-41"
linked_pr = 0
```

---

## How AgentCeption creates task files

The dispatcher (`agentception/readers/worktrees.py`) calls `_build_child_task()` which:

1. Resolves the `cognitive_arch` string from the role config
2. Creates a git worktree at `$HOST_WORKTREES_DIR/{repo}/{run_id}`
3. Writes the `.agent-task` TOML file
4. Inserts an `ACAgentRun` row in the database with `status = "pending_launch"`
5. The Dispatcher agent reads `/api/runs/pending`, claims runs via `/api/runs/{id}/acknowledge`, then spawns Cursor agents via the Task tool

---

## How agents self-report progress

Every agent reports back using `curl` against the AgentCeption API (or MCP tools):

```bash
# Report a step
curl -s -X POST http://localhost:10003/api/runs/$RUN_ID/step \
  -H "Content-Type: application/json" \
  -d '{"step_name": "reading issue body", "issue_number": 41}'

# Report done with PR
curl -s -X POST http://localhost:10003/api/runs/$RUN_ID/done \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 41, "pr_number": 99, "summary": "Opened PR #99"}'
```

`RUN_ID` is written into the `.agent-task` file as a comment and available to the shell environment.

---

## Extension guide

To add a new workflow type:

1. Add a row to the `task.workflow` enum in `agentception/models/__init__.py`
2. Add any new payload section (`[my_workflow_payload]`) to this spec
3. Update `.agentception/agent-task-spec.md` with the full field table
4. Update the coordinator prompt template that writes this workflow's task files
5. If the workflow produces structured output, add it to `output.format` values

See the [full spec](.agentception/agent-task-spec.md) for exhaustive examples including coordinator manifests, PR queues, and non-tech domain workflows.
