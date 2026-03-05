# API Reference

All endpoints are served by the AgentCeption container on port 10003. Every browser page and MCP tool call resolves to one of these routes.

---

## URL Taxonomy

URLs are semantic: each path segment narrows the resource.

```
/plan                     ← Planning pages
/ship/{initiative}        ← Ship board for a specific initiative
/api/plan/*               ← Plan pipeline (draft, file, launch)
/api/dispatch/*           ← Agent dispatch (issue, label, context, prompt)
/api/runs/{run_id}/*      ← Agent run lifecycle (step, blocker, done, …)
/api/ship/{initiative}/*  ← Ship-level actions (advance phase)
/api/agents/*             ← Agent pipeline state
/api/health/*             ← System health
/api/config               ← Runtime configuration
```

---

## Browser / HTMX Routes (UI)

### Plan

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Redirects to `/plan` |
| `GET` | `/plan` | Planning page (Phase 1A → 1B flow) |
| `GET` | `/plan/recent-runs` | Recent plan run list partial |

### Ship

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ship` | Redirects to `/ship/{first-initiative}` |
| `GET` | `/ship/{initiative}` | Ship board for the given initiative |
| `GET` | `/ship/{initiative}/board` | HTMX board partial (polled every 5 s) |
| `GET` | `/ship/runs/{run_id}/stream` | SSE stream for an agent run's events |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/agents` | Agent list page |
| `GET` | `/agents/{agent_id}` | Agent detail page (persona, transcript, kill modal) |
| `GET` | `/agents/spawn` | Spawn wizard |
| `GET` | `/partials/agents` | HTMX agent list partial |
| `GET` | `/partials/agents/{agent_id}/transcript` | HTMX transcript partial |

### Cognitive Architecture

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/cognitive-arch` | Catalog of all cognitive architectures |
| `GET` | `/cognitive-arch/{arch_id}` | Detail view for a specific architecture |

### Issues and PRs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/issues` | Issue list |
| `GET` | `/issues/{number}` | Issue detail |
| `GET` | `/prs` | PR list |
| `GET` | `/prs/{number}` | PR detail |

### Other Pages

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/org-chart` | Org chart browser |
| `GET` | `/roles` | Role catalog |
| `GET` | `/roles/{slug}/detail` | Role detail partial |
| `GET` | `/worktrees` | Active worktrees list |
| `GET` | `/worktrees/{slug}/detail` | Worktree detail |
| `GET` | `/telemetry` | Telemetry dashboard |
| `GET` | `/config` | Configuration UI |
| `GET` | `/settings` | Settings page |
| `GET` | `/transcripts` | Agent transcript list |
| `GET` | `/transcripts/{uuid}` | Individual transcript |
| `GET` | `/docs` | In-app documentation index |
| `GET` | `/docs/{slug}` | In-app documentation page |
| `GET` | `/dag` | Dependency DAG visualizer |
| `GET` | `/overview` | Project overview |
| `GET` | `/api` | Auto-generated API reference |

---

## JSON API Routes

All API routes are prefixed `/api`.

### Plan pipeline — `/api/plan/*`

These drive Phase 1A (brain dump → draft) and Phase 1B (review → file issues).

#### `POST /api/plan/preview`

Generate a plan preview from a brain dump. Streams SSE events as Claude reasons.

**Body:** `application/json`
```json
{
  "brain_dump": "string",
  "initiative": "string"
}
```

**Response:** `text/event-stream` — SSE events with `plan_draft_ready` payload on completion.

---

#### `POST /api/plan/validate`

Validate an edited PlanSpec YAML before filing.

**Body:** `application/json`
```json
{ "yaml_text": "string" }
```

**Response:**
```json
{
  "valid": true,
  "errors": []
}
```

---

#### `POST /api/plan/file-issues`

File all issues from a validated PlanSpec YAML. Creates GitHub issues, phase labels, and `initiative_phases` DB rows.

**Body:** `application/json`
```json
{ "yaml_text": "string" }
```

**Response:** `text/event-stream` — SSE progress events, then a `file_issues_done` event.

---

#### `GET /api/plan/{run_id}/plan-text`

Retrieve the raw plan text for a given plan run.

---

### Dispatch — `/api/dispatch/*`

Endpoints that create and queue agent runs.

#### `GET /api/dispatch/context?label=<label>`

Fetch label context: the list of issues for a label, grouped by phase, plus the dispatcher prompt.

**Query params:**

| Param | Type | Description |
|-------|------|-------------|
| `label` | `string` | GitHub label string (e.g. `ac-workflow`) |

**Response:** `LabelContextResponse`
```json
{
  "label": "ac-workflow",
  "phases": [
    {
      "label": "ac-workflow/0-foundation",
      "issues": [{"number": 41, "title": "...", "state": "open", "url": "..."}],
      "locked": false,
      "complete": false,
      "depends_on": []
    }
  ],
  "dispatcher_prompt": "..."
}
```

---

#### `POST /api/dispatch/issue`

Dispatch an agent for a single issue. Creates a git worktree, writes `.agent-task`, and inserts an `ACAgentRun` row.

**Body:** `DispatchRequest`
```json
{
  "issue_number": 41,
  "role": "python-developer",
  "gh_repo": "owner/repo",
  "batch_id": "label-ac-workflow-20260101T000000Z-abcd"
}
```

**Response:** `DispatchResponse`
```json
{
  "run_id": "issue-41-python-developer-abcd",
  "worktree_path": "/worktrees/...",
  "branch": "agent/ac-workflow-abcd"
}
```

---

#### `POST /api/dispatch/label`

Dispatch a top-of-tree agent (CTO / coordinator) for an entire label scope. Creates the worktree and queues the run.

**Body:** `LabelDispatchRequest`
```json
{
  "label": "ac-workflow",
  "role": "cto",
  "gh_repo": "owner/repo"
}
```

**Response:** `LabelDispatchResponse`
```json
{
  "run_id": "label-ac-workflow-20260101T000000Z-abcd",
  "worktree_path": "/worktrees/..."
}
```

---

#### `GET /api/dispatch/prompt`

Return the current dispatcher prompt (used by the Dispatcher agent to understand available tools and context).

**Response:** `text/plain`

---

### Run lifecycle — `/api/runs/*`

All run lifecycle endpoints are called by agents via `curl` in their worktree, or by MCP tools.

#### `GET /api/runs/pending`

Return all `ACAgentRun` rows in `pending_launch` state, ready to be claimed and spawned.

**Response:**
```json
{
  "pending": [
    {
      "run_id": "label-ac-workflow-abc",
      "issue_number": 0,
      "role": "cto",
      "branch": "agent/ac-workflow-abc",
      "host_worktree_path": "/home/.agentception/worktrees/...",
      "batch_id": "label-ac-workflow-20260101T000000Z-abc"
    }
  ],
  "count": 1
}
```

---

#### `POST /api/runs/{run_id}/acknowledge`

Atomically claim a pending run. Sets `status = implementing`. Returns `{"ok": false}` if already claimed — safe for concurrent dispatchers.

---

#### `POST /api/runs/{parent_run_id}/children`

Spawn a child agent run under a parent (coordinator spawning an engineer). Creates worktree, writes `.agent-task`, inserts DB row.

**Body:** `SpawnChildRequest`
```json
{
  "role": "python-developer",
  "node_type": "leaf",
  "scope_type": "issue",
  "scope_value": "41",
  "gh_repo": "owner/repo"
}
```

**Response:** `SpawnChildResponse`
```json
{
  "run_id": "issue-41-python-developer-xyz",
  "worktree_path": "/worktrees/..."
}
```

---

#### `POST /api/runs/{run_id}/step`

Report a step completion. Inserts an `ACAgentStep` event visible in the inspector panel.

**Body:** `StepReport`
```json
{
  "step_name": "reading issue body",
  "issue_number": 41
}
```

---

#### `POST /api/runs/{run_id}/blocker`

Report a blocker. Surfaces in the inspector as a blocking event.

**Body:** `BlockerReport`
```json
{
  "description": "Cannot find the module",
  "issue_number": 41
}
```

---

#### `POST /api/runs/{run_id}/decision`

Report a design decision made during execution.

**Body:** `DecisionReport`
```json
{
  "description": "Using SQLAlchemy Core instead of ORM for this query",
  "issue_number": 41
}
```

---

#### `POST /api/runs/{run_id}/done`

Mark a run complete. Triggers worktree cleanup. Optionally links a PR.

**Body:** `DoneReport`
```json
{
  "issue_number": 41,
  "pr_number": 99,
  "summary": "Implemented the feature, opened PR #99"
}
```

---

#### `POST /api/runs/{run_id}/message`

Send a user message into a running agent's context. The agent's SSE stream picks it up on the next poll cycle.

**Body:**
```json
{ "content": "Please focus on the error handling path" }
```

---

#### `POST /api/runs/{run_id}/stop`

Stop a running agent. Sets `status = DONE`, removes the `agent:wip` label.

---

### Ship — `/api/ship/*`

#### `POST /api/ship/{initiative}/advance`

Advance the phase gate for an initiative. Checks that all issues in `from_phase` are closed, then removes the `locked` label from `to_phase` issues.

**Body:**
```json
{
  "from_phase": "ac-workflow/0-foundation",
  "to_phase": "ac-workflow/1-generation"
}
```

**Response:** `AdvancePhaseOk` on success, `AdvancePhaseBlocked` (422) if open issues remain in `from_phase`.

---

### Agents / Pipeline — `/api/agents/*`, `/api/pipeline`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/pipeline` | Current pipeline state (active runs, phase counts) |
| `GET` | `/api/agents` | All agent runs |
| `GET` | `/api/agents/{agent_id}` | Single agent run details |
| `GET` | `/api/agents/{agent_id}/transcript` | Agent conversation transcript |

---

### Config — `/api/config`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/config` | Current runtime configuration |
| `PUT` | `/api/config` | Update runtime configuration |
| `POST` | `/api/config/switch-project` | Switch active codebase/project |

---

### Control — `/api/control/*`

Legacy orchestration control endpoints. Prefer MCP tools for new integrations.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/control/pause` | Pause the pipeline |
| `POST` | `/api/control/resume` | Resume the pipeline |
| `GET` | `/api/control/status` | Pipeline pause/resume status |
| `GET` | `/api/control/active-label` | Currently active label |
| `PUT` | `/api/control/active-label` | Set active label |
| `DELETE` | `/api/control/active-label` | Clear active label |
| `POST` | `/api/control/spawn` | Spawn a single agent |
| `POST` | `/api/control/spawn-wave` | Spawn a wave of agents |
| `POST` | `/api/control/sweep` | Sweep stale runs |
| `POST` | `/api/control/reset-build` | Full reset: remove all worktrees, clear all agent:wip, set active runs to unknown |
| `POST` | `/api/control/trigger-poll` | Trigger an immediate GitHub poll |

---

### Health — `/api/health/*`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health/detailed` | Full `HealthSnapshot` (uptime, memory, latency, worktree count) |
| `GET` | `/ui/health/widget` | HTMX health widget partial |

---

### Worktrees — `/api/worktrees/*`

| Method | Path | Description |
|--------|------|-------------|
| `DELETE` | `/api/worktrees/{slug}` | Remove a worktree and clean up its DB row |

---

### Issues and PRs — `/api/issues/*`, `/api/prs/*`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/issues/{number}/comments` | GitHub comments for an issue |
| `GET` | `/api/prs/{number}/checks` | CI checks for a PR |
| `GET` | `/api/prs/{number}/reviews` | Review decisions for a PR |
| `GET` | `/api/issues/approval-queue` | Issues awaiting human approval |
| `POST` | `/api/issues/{number}/approve` | Approve an issue for dispatch |

---

### Intelligence — `/api/intelligence/*`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dag` | Dependency DAG as JSON |
| `GET` | `/api/intelligence/pr-violations` | PRs with policy violations |
| `POST` | `/api/intelligence/pr-violations/{pr_number}/close` | Close a violation |
| `POST` | `/api/analyze/issue/{number}` | Trigger issue analysis |

---

### Telemetry — `/api/telemetry/*`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/telemetry/waves` | Wave-level telemetry |
| `GET` | `/api/telemetry/cost` | Cost summary |

---

### Org Chart — `/api/org/*`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/org/tree` | Full org tree as JSON |
| `POST` | `/api/org/select-preset` | Select an org preset |
| `GET` | `/api/org/taxonomy` | Role taxonomy |
| `POST` | `/api/org/roles/add` | Add a role |
| `DELETE` | `/api/org/roles/{slug}` | Remove a role |
| `POST` | `/api/org/roles/{slug}/phases` | Assign phases to a role |
| `POST` | `/api/org/templates` | Create from a template |
