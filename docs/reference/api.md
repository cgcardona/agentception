# API Reference

All endpoints are served by the AgentCeption container on port 10003. Every browser page and MCP tool call resolves to one of these routes.

## Authentication

When `AC_API_KEY` is set (via environment variable), every request to any path under `/api/` must include the key:

```http
Authorization: Bearer <key>
# or
X-API-Key: <key>
```

Paths outside `/api/` — the UI (`/`), `/health`, `/static/*`, `/events` — are always public. See the [Security Guide](../guides/security.md) for full details.

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

#### `POST /api/plan/draft` (Plan step 1.A)

Accept plan text (brain dump), create a git worktree, and write an `.agent-task` file so a Cursor agent can produce the PlanSpec YAML. Returns immediately; completion is signalled asynchronously via **GET /events** (see below).

**Body:** `application/json`
```json
{ "text": "string" }
```

`text` must be non-empty and not whitespace-only (422 otherwise).

**Response:** `200` JSON
```json
{
  "draft_id": "uuid",
  "task_file": "/path/to/worktree/.agent-task",
  "output_path": "/path/to/worktree/.plan-output.yaml",
  "status": "pending"
}
```

**Completion:** Subscribe to **GET /events** (SSE). Each message is a JSON object: the current `PipelineState`. When the poller detects that the Cursor agent has written the file at `output_path`, it appends a **plan_draft_ready** entry to `state.plan_draft_events` for that tick. The entry has `event: "plan_draft_ready"`, `draft_id`, `yaml_text` (the generated YAML), and `output_path`. Match `draft_id` to the value returned by this POST. If the agent does not write the file within the server timeout, a **plan_draft_timeout** entry is emitted instead (same `draft_id`, empty `yaml_text`).

#### `GET /events` (SSE — Plan step 1.A and dashboard)

Streams the current `PipelineState` as Server-Sent Events (one JSON payload per poller tick, default ~5 s). Used by the Plan page to receive **plan_draft_ready** / **plan_draft_timeout** in `state.plan_draft_events` after calling **POST /api/plan/draft**, and by the overview/ship UIs for live agent and board updates.

**Response:** `text/event-stream`. Each event has a `data` field containing the JSON-serialised `PipelineState` (including `plan_draft_events` for the plan flow).

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

Stop a running agent. Sets `status = DONE`, removes the `agent/wip` label.

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

### Agent Execution — `/api/runs/{run_id}/execute`

Cursor-free agent dispatch. Triggers the `agent_loop.py` pipeline for a run that already exists in the DB. See the [Cursor-Free Agent Loop guide](../guides/agent-loop.md) for full documentation.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/runs/{run_id}/execute` | Dispatch an agent run without Cursor |

**Status codes:**

| Code | Meaning |
|------|---------|
| `202` | Agent loop dispatched as a background task |
| `404` | Run not found |
| `409` | Run is not in a dispatchable state (`pending_launch` or `implementing`) |

**Response (202):**
```json
{"ok": true, "message": "Agent loop dispatched for run {run_id}."}
```

---

### System — `/api/system/*`

Infrastructure operations: codebase indexing and semantic search. See the [Cursor-Free Agent Loop guide](../guides/agent-loop.md) for full documentation.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/system/index-codebase` | Index the codebase into Qdrant (background task, 202 Accepted) |
| `GET` | `/api/system/search` | Semantic code search against the Qdrant index |

#### `POST /api/system/index-codebase`

Starts a background job that walks every source file in the configured repo root, chunks and embeds them with FastEmbed (`BAAI/bge-small-en-v1.5`), and upserts 384-dim vectors into Qdrant. Returns `202 Accepted` immediately.

The first run downloads the FastEmbed model (~130 MB). Subsequent runs use the cached model and complete in seconds. Progress is visible in the container logs.

**Response:**
```json
{"ok": true, "message": "Codebase indexing started in the background."}
```

#### `GET /api/system/search`

Search the indexed codebase with a natural-language query.

| Query param | Type | Required | Description |
|-------------|------|----------|-------------|
| `q` | `string` | yes | Natural-language search query |
| `n` | `integer` | no | Number of results (1–20, default 5) |

**Response:**
```json
{
  "ok": true,
  "query": "openrouter api key",
  "n_results": 3,
  "matches": [
    {
      "file": "agentception/config.py",
      "score": 0.733,
      "start_line": 101,
      "end_line": 110,
      "chunk": "    openrouter_api_key: str = \"\"\n    ..."
    }
  ]
}
```

If the codebase has not been indexed yet, returns `{"ok": true, "n_results": 0, "matches": []}`.

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
| `POST` | `/api/control/reset-build` | Full reset: remove all worktrees, clear all agent/wip, set active runs to unknown |
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
