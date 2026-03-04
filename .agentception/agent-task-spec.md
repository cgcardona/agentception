# `.agent-task` — Formal TOML Specification

> **Version:** 2.0  
> **Format:** [TOML 1.0](https://toml.io/en/v1.0.0)  
> **Parsed by:** `agentception/readers/worktrees.py` (Python `tomllib`, stdlib since 3.11)  
> **Read by:** Cursor's LLM (full raw text as context) + AgentCeption dashboard

---

## Overview

Every agent worktree contains exactly one `.agent-task` file. It is the **single source of
truth** for that agent's assignment, identity, pipeline position, and execution constraints.

Two consumers read this file with different goals:

| Consumer | What it reads | How |
|----------|--------------|-----|
| AgentCeption dashboard | Typed scalar fields for monitoring/telemetry | `tomllib.loads()` → `TaskFile` Pydantic model |
| Cursor's LLM | Full raw text as natural language context | File read → LLM context window |

Because the LLM reads the entire file, **any valid TOML you add is immediately available
to the agent** — even fields AgentCeption doesn't formally parse. This makes the file
extensible at zero cost.

---

## File Layout

```toml
# ── Section order is conventional, not required by TOML ──────────────────────

[task]          # Core identity and lifecycle control
[agent]         # Who is running this task
[repo]          # GitHub and git coordinates
[pipeline]      # Batch/wave/VP lineage for traceability
[spawn]         # Orchestration control: chaining, sub-agents
[target]        # What this task acts on (issue, PR, or custom)
[worktree]      # Local filesystem coordinates
[output]        # Async result rendezvous (for Cursor-driven workflows)
[domain]        # Domain context (non-tech orgs: marketing, legal, ops, etc.)

# ── Optional payload sections (workflow-specific) ─────────────────────────────
[plan_draft]    # WORKFLOW=plan-spec: brain dump dispatch to Cursor
[enriched]      # Coordinator manifest (structured issue set)

# ── Sub-task queues (coordinator and conductor workflows only) ────────────────
[[issue_queue]]         # repeated for each sub-task
[[pr_queue]]            # repeated for each PR review sub-task
[[deliverable_queue]]   # repeated for each non-code deliverable
```

---

## Field Reference

### `[task]`

Core identity. Required in every task file.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | string | yes | Spec version — always `"2.0"` for TOML files |
| `workflow` | string | yes | Workflow type — see **Workflow Types** below |
| `id` | string | yes | UUID v4 uniquely identifying this task instance |
| `created_at` | datetime | yes | ISO 8601 datetime when this file was written |
| `attempt_n` | int | yes | Retry counter. Agent hard-stops when `attempt_n > 2` |
| `required_output` | string | yes | Artifact the agent must produce before self-destructing |
| `on_block` | string | yes | `"stop"` \| `"escalate"` — what to do when blocked |

**Workflow types** (extensible — add new rows as workflows are built):

| Value | Consumer | Produces |
|-------|----------|---------|
| `issue-to-pr` | Implementation engineer | GitHub PR |
| `pr-review` | QA reviewer | Merge + grade |
| `coordinator` | Batch coordinator | N worktrees + N child agents |
| `conductor` | Meta-orchestrator | Pipeline state report |
| `bugs-to-issues` | Issue creator | GitHub issues |
| `plan-spec` | Cursor LLM | YAML PlanSpec written to `output.path` |
| `task-to-deliverable` | Any domain agent | Domain-specific artifact |

**`required_output` values:**

| Value | Meaning |
|-------|---------|
| `pr_url` | Open pull request URL |
| `grade,merge_status,pr_url` | Full review result |
| `pipeline_status_report` | Conductor summary |
| `yaml_file` | Cursor writes YAML to `output.path` |
| `deliverable_path` | Non-code output written to `output.path` |

---

### `[agent]`

Who is executing this task.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | yes | Role slug: `"python-developer"`, `"pr-reviewer"`, `"qa-manager"`, etc. |
| `cognitive_arch` | string | yes | `"figure:skill1:skill2"` — resolved by `resolve_arch.py` |
| `session_id` | string | no | Set by the agent at runtime: `"eng-20260303T134821Z-a7f2"` |

**`cognitive_arch` format:**

```
figures,figure2:skill1:skill2:skill3
│                │
│                └─ colon-separated skill domain IDs (from cognitive_archetypes/skill_domains/)
└─ comma-separated figure/archetype IDs (from cognitive_archetypes/figures/)

Examples:
  "turing:python"                     — single figure + one skill
  "dijkstra:jinja2:htmx:alpine"       — single figure + three skills  
  "lovelace,shannon:htmx:d3:python"   — two-figure blend + three skills
  "the_architect:python:fastapi"       — archetype + two skills
```

---

### `[repo]`

Git and GitHub coordinates.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `gh_repo` | string | yes | GitHub slug: `"owner/repo"`. **Never derive from local path.** |
| `base` | string | yes | Base branch to merge into: `"dev"` |
| `gh_token_env` | string | no | Env var name containing GitHub token (default: `"GH_TOKEN"`) |

---

### `[pipeline]`

Lineage for traceability. Every artifact produced (commit, PR, issue comment) embeds these.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `batch_id` | string | yes | VP-level batch fingerprint: `"eng-20260303T134821Z-a7f2"` |
| `wave` | string | no | Named wave: `"5-plan-step-v2"` |
| `vp_fingerprint` | string | no | VP agent's session ID — propagated to leaf agents |
| `cto_wave` | string | no | CTO-level orchestration round for full-pipeline traces |

Commit message trailers written by every agent:
```
AgentCeption-Batch: <pipeline.batch_id>
AgentCeption-Session: <agent.session_id>
```

---

### `[spawn]`

Controls how this agent spawns and hands off to successors.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mode` | string | yes | `"chain"` \| `"single"` \| `"coordinator"` |
| `sub_agents` | bool | yes | `true` → act as sub-coordinator, launch agents from `[[issue_queue]]` |
| `max_concurrent` | int | no | Safety valve for sub-coordinators (default: 4) |

**`spawn.mode` values:**

| Value | Behaviour |
|-------|-----------|
| `"chain"` | After PR is opened, immediately spawn a QA reviewer for that PR |
| `"single"` | Do the work and stop — no chaining |
| `"coordinator"` | Create worktrees from `[[issue_queue]]`, launch one leaf per worktree, report back |

---

### `[target]`

What this task acts on. Fields present depend on workflow type.

| Field | Type | Workflows | Description |
|-------|------|-----------|-------------|
| `issue_number` | int | `issue-to-pr` | GitHub issue number |
| `issue_title` | string | `issue-to-pr` | Display title |
| `issue_url` | string | `issue-to-pr` | Full GitHub URL |
| `phase_label` | string | `issue-to-pr` | Phase label applied on GitHub |
| `batch_label` | string | `issue-to-pr` | Batch label applied on GitHub |
| `all_labels` | [string] | `issue-to-pr` | All labels on this issue |
| `depends_on` | [int] | `issue-to-pr` | Issue numbers that must be merged first |
| `closes` | [int] | `issue-to-pr` | Issue numbers to close when PR merges |
| `file_ownership` | [string] | `issue-to-pr` | Files this agent owns (prevents conflicts) |
| `pr_number` | int | `pr-review` | Pull request number |
| `pr_title` | string | `pr-review` | PR display title |
| `pr_url` | string | `pr-review` | Full GitHub URL |
| `pr_branch` | string | `pr-review` | PR head branch |
| `files_changed` | [string] | `pr-review` | Files modified in the PR |
| `has_migration` | bool | `pr-review` | `true` if PR contains an Alembic migration |
| `closes_issues` | [int] | `pr-review` | Issues this PR closes |
| `grade_threshold` | string | `pr-review` | Minimum grade to merge: `"A"` \| `"B"` \| `"C"` |
| `merge_after` | int | `pr-review` | PR number that must merge before this one |
| `deliverable_type` | string | `task-to-deliverable` | What gets produced (domain-specific) |
| `deliverable_description` | string | `task-to-deliverable` | Full description of the deliverable |

---

### `[worktree]`

Local filesystem coordinates.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Absolute path to this worktree |
| `branch` | string | no | Git branch name (created by the agent for `issue-to-pr`) |
| `linked_pr` | int | no | Written back by agent after PR opens: `0` until then |

---

### `[output]`

Async result rendezvous. Used when Cursor does LLM work and writes a result back to disk.

| Field | Type | Workflows | Description |
|-------|------|-----------|-------------|
| `path` | string | `plan-spec`, `task-to-deliverable` | Absolute path Cursor writes its output to |
| `draft_id` | string | `plan-spec` | UUID correlating to the AgentCeption dashboard request |
| `format` | string | any | Output format: `"yaml"` \| `"json"` \| `"markdown"` \| `"toml"` |
| `schema_tool` | string | `plan-spec` | MCP tool to call to get the output schema: `"plan_get_schema"` |

When `output.path` is present, the AgentCeption poller watches for this file.
When it appears, the poller emits an SSE event:
```json
{"event": "task_output_ready", "data": {"draft_id": "...", "path": "...", "format": "..."}}
```

---

### `[domain]`

Domain context for non-engineering workflows. Lets AgentCeption support any org type.

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Domain name: `"engineering"` \| `"marketing"` \| `"legal"` \| `"ops"` \| `"finance"` |
| `org_preset` | string | Org preset ID from `org-presets.yaml` |
| `language` | string | Human language for deliverables (default: `"en"`) |
| `context` | string | Free-form domain context injected into agent prompt |
| `tools` | [string] | Domain-specific MCP tools available to this agent |

---

### `[plan_draft]`

Present only when `task.workflow = "plan-spec"`. Carries the brain dump AgentCeption
collected from the web UI, dispatched to Cursor for YAML generation.

| Field | Type | Description |
|-------|------|-------------|
| `dump` | string | Raw brain dump text (multi-line, no escaping needed in TOML `"""`) |
| `mcp_tools_hint` | string | Which AgentCeption MCP tools to call first |
| `output_schema` | string | MCP tool name that returns the expected output schema |

```toml
[plan_draft]
mcp_tools_hint = "call plan_get_schema() to get the PlanSpec TOML schema first"
output_schema = "plan_get_schema"
dump = """
We need to build a billing system.
- Users should be able to subscribe to monthly or annual plans
- Payment via Stripe
- Invoices emailed on charge
- Admin dashboard to manage subscriptions
"""
```

---

### `[enriched]`

Present when a coordinator receives a pre-enriched manifest (output of Plan step 1.A).
When present, the coordinator skips interpretation and executes directly.

| Field | Type | Description |
|-------|------|-------------|
| `manifest_json` | string | JSON-encoded `EnrichedManifest` (full manifest as a multi-line string) |
| `total_issues` | int | Quick summary: total issue count |
| `estimated_waves` | int | Quick summary: estimated parallel waves |
| `phases` | [string] | Phase IDs in dependency order |

---

### `[[issue_queue]]`

Repeated table. Each entry is one sub-task for a `spawn.mode = "coordinator"` agent.
The coordinator creates one worktree per entry and launches one leaf agent.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `number` | int | yes | GitHub issue number |
| `title` | string | yes | Display title |
| `role` | string | yes | Role slug for the leaf agent |
| `cognitive_arch` | string | yes | `"figure:skill"` for the leaf agent |
| `depends_on` | [int] | yes | Issue numbers that must complete first (can be `[]`) |
| `file_ownership` | [string] | no | Files this leaf agent owns |
| `branch` | string | no | Override branch name (default: `"feat/issue-<number>"`) |

```toml
[[issue_queue]]
number = 870
title = "MCP layer + schema tools"
role = "python-developer"
cognitive_arch = "turing:python"
depends_on = []
file_ownership = ["agentception/mcp/"]

[[issue_queue]]
number = 871
title = "Plan tools: label context + coordinator spawn"
role = "python-developer"
cognitive_arch = "turing:python"
depends_on = [870]
file_ownership = ["agentception/mcp/plan_tools.py"]
```

---

### `[[pr_queue]]`

Repeated table. Each entry is one PR to review, for `WORKFLOW=coordinator` QA runs.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `number` | int | yes | PR number |
| `title` | string | yes | PR display title |
| `branch` | string | yes | PR head branch |
| `role` | string | yes | Reviewer role slug |
| `cognitive_arch` | string | yes | Reviewer cognitive architecture |
| `grade_threshold` | string | yes | Minimum grade to merge |
| `merge_order` | int | yes | `1` = first, `2` = second, etc. (must be serial for safety) |
| `closes_issues` | [int] | no | Issues closed by this PR |

---

### `[[deliverable_queue]]`

Repeated table. For non-code workflows — any domain agent producing structured output.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique deliverable ID within this batch |
| `title` | string | yes | Human-readable title |
| `type` | string | yes | Deliverable type: `"blog-post"` \| `"legal-brief"` \| `"market-analysis"` \| etc. |
| `role` | string | yes | Role slug |
| `cognitive_arch` | string | yes | Cognitive architecture |
| `output_format` | string | yes | `"markdown"` \| `"json"` \| `"pdf"` |
| `depends_on` | [string] | yes | Deliverable IDs that must complete first |
| `context` | string | no | Domain-specific context injected into agent prompt |

---

## Complete Examples

### Issue-to-PR (engineering)

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
cognitive_arch = "turing:python"

[repo]
gh_repo = "cgcardona/agentception"
base = "dev"

[pipeline]
batch_id = "eng-20260303T134821Z-a7f2"
wave = "5-plan-step-v2"

[spawn]
mode = "chain"
sub_agents = false

[target]
issue_number = 872
issue_title = "POST /api/plan/draft — accept brain dump, dispatch to Cursor via .agent-task"
issue_url = "https://github.com/cgcardona/agentception/issues/872"
phase_label = "plan-step-v2/2-api-endpoints"
all_labels = ["enhancement", "ac-workflow/5-plan-step-v2", "plan-step-v2/2-api-endpoints"]
depends_on = [870, 871]
closes = [872]
file_ownership = ["agentception/routes/api/plan.py", "agentception/tests/test_agentception_plan_api.py"]

[worktree]
path = "/tmp/worktrees/issue-872"
branch = "feat/issue-872"
linked_pr = 0
```

---

### Plan-spec (Cursor-driven brain dump)

```toml
[task]
version = "2.0"
workflow = "plan-spec"
id = "8b2c4d1e-9f3a-4b7e-c5d8-2e1f6a9b0c3d"
created_at = 2026-03-03T14:22:00Z
attempt_n = 0
required_output = "yaml_file"
on_block = "stop"

[agent]
role = "python-developer"
cognitive_arch = "turing:python"

[repo]
gh_repo = "cgcardona/agentception"
base = "dev"

[pipeline]
batch_id = "plan-20260303T142200Z-f7e1"

[spawn]
mode = "single"
sub_agents = false

[worktree]
path = "/tmp/worktrees/plan-draft-8b2c4d1e"

[output]
path = "/tmp/worktrees/plan-draft-8b2c4d1e/.plan-output.yaml"
draft_id = "8b2c4d1e-9f3a-4b7e-c5d8-2e1f6a9b0c3d"
format = "yaml"
schema_tool = "plan_get_schema"

[plan_draft]
mcp_tools_hint = "call plan_get_schema() first to get the PlanSpec YAML format, then produce valid YAML matching that schema"
dump = """
We need to build a billing system for our SaaS product.

Users should be able to:
- Subscribe to monthly or annual plans
- Pay via Stripe (card + ACH)
- Receive invoices by email on each charge
- View billing history in their account dashboard

Admins should be able to:
- View all active subscriptions
- Manually cancel or refund subscriptions
- See monthly revenue charts
"""
```

---

### Non-tech workflow (marketing team)

```toml
[task]
version = "2.0"
workflow = "task-to-deliverable"
id = "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
created_at = 2026-03-03T15:00:00Z
attempt_n = 0
required_output = "deliverable_path"
on_block = "escalate"

[agent]
role = "content-writer"
cognitive_arch = "sagan:writing"

[repo]
gh_repo = "acme-corp/content"
base = "main"

[pipeline]
batch_id = "mktg-20260303T150000Z-b9c2"
wave = "q1-campaign"

[spawn]
mode = "single"
sub_agents = false

[domain]
name = "marketing"
org_preset = "content-team"
language = "en"
context = "B2B SaaS, technical audience, tone: authoritative but approachable"
tools = ["brand_guidelines", "seo_keyword_lookup", "competitor_analysis"]

[target]
deliverable_type = "blog-post"
deliverable_description = "Write a 1200-word blog post about using AI agents to automate team workflows. Target: CTOs at Series A startups. Include 3 real-world examples."

[worktree]
path = "/tmp/worktrees/content-c1d2e3f4"

[output]
path = "/tmp/worktrees/content-c1d2e3f4/blog-post-ai-agents.md"
format = "markdown"
```

---

## Parser Contract (AgentCeption)

AgentCeption's `parse_agent_task()` in `agentception/readers/worktrees.py` reads the file
using `tomllib.loads()`. The following fields are formally tracked in the `TaskFile` model
(everything else is available to the LLM but silently ignored by the dashboard):

```
task.workflow → TaskFile.task
task.id → TaskFile.id
task.attempt_n → TaskFile.attempt_n
task.required_output → TaskFile.required_output
task.on_block → TaskFile.on_block
agent.role → TaskFile.role
agent.cognitive_arch → TaskFile.cognitive_arch
agent.session_id → TaskFile.session_id
repo.gh_repo → TaskFile.gh_repo
repo.base → TaskFile.base
pipeline.batch_id → TaskFile.batch_id
pipeline.wave → TaskFile.wave
spawn.mode → TaskFile.spawn_mode
spawn.sub_agents → TaskFile.spawn_sub_agents
target.issue_number → TaskFile.issue_number
target.pr_number → TaskFile.pr_number
target.depends_on → TaskFile.depends_on
target.closes → TaskFile.closes_issues
target.file_ownership → TaskFile.file_ownership
worktree.path → TaskFile.worktree
worktree.branch → TaskFile.branch
worktree.linked_pr → TaskFile.linked_pr
output.draft_id → TaskFile.draft_id
output.path → TaskFile.output_path
domain.name → TaskFile.domain
```

### Backwards compatibility

Files that still use the old `KEY=value` flat format (v1) are detected by the absence
of a `[task]` table header. The parser falls back to the legacy line-split parser for
these files. All newly written files must use TOML (v2). The legacy parser is removed
once all coordinator prompts are updated to write TOML.

---

## Extension Guide

### Adding a new workflow type

1. Add a row to the **Workflow types** table above.
2. Add a `[[workflow_name_queue]]` section if it needs sub-task coordination.
3. Add any workflow-specific payload section (`[workflow_name_payload]`).
4. Add the new workflow's `required_output` value to the table.
5. Update `agentception/models.py` `TaskFile` with any new fields the dashboard needs.
6. Update the coordinator prompt that writes this workflow's task files.

### Adding a new domain (non-tech org)

1. Add an entry to `org-presets.yaml` with the domain's role topology.
2. Add role files to `.agentception/roles/` for any new roles.
3. Add cognitive architecture figures/skills to `scripts/gen_prompts/cognitive_archetypes/`
   if the domain has domain-specific figures (e.g., `ogilvy.yaml` for marketing).
4. Set `domain.name` and `domain.org_preset` in the task file.
5. No code changes needed — the domain context flows through the LLM's prompt.
