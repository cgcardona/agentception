# MCP Server Reference

AgentCeption exposes a full [Model Context Protocol](https://modelcontextprotocol.io/) server with tools, resources, and prompts. This document is the complete, machine-verifiable catalogue of every endpoint.

For the full Pydantic/TypedDict field tables behind these types, see [type-contracts.md — MCP Protocol Types](type-contracts.md#11-mcp-protocol-types-actooldefs-acresourcedefs-prompts).

---

## Table of Contents

- [Architecture](#architecture)
- [Transports](#transports)
  - [stdio](#stdio)
  - [HTTP](#http)
- [Tools](#tools)
  - [Tool dispatch](#tool-dispatch)
  - [Plan tools](#plan-tools)
  - [GitHub tools](#github-tools)
  - [Build tools](#build-tools)
  - [Log tools](#log-tools)
  - [Agent role tool surfaces](#agent-role-tool-surfaces)
- [Resources](#resources)
  - [Static resources](#static-resources)
  - [Resource templates](#resource-templates)
  - [Resource resolution](#resource-resolution)
- [Prompts](#prompts)
  - [Parameterised prompts](#parameterised-prompts)
  - [Static agent prompts](#static-agent-prompts)
  - [Dynamic role prompts](#dynamic-role-prompts)
- [Error codes](#error-codes)

---

## Architecture

A JSON-RPC 2.0 request enters the server through one of two transports and is routed to the appropriate handler based on the `method` field.

```mermaid
flowchart LR
    Client["MCP Client"] -->|"JSON-RPC 2.0"| Transport
    subgraph transport [Transports]
        StdioTransport["stdio_server.py"]
        HttpTransport["routes/api/mcp.py"]
    end
    Transport --> ServerPy["server.py\nhandle_request"]
    ServerPy -->|"tools/call"| ToolDispatch["call_tool /\ncall_tool_async"]
    ServerPy -->|"resources/read"| ResourceDispatch["resources.py\nread_resource"]
    ServerPy -->|"prompts/get"| PromptDispatch["prompts.py\nget_prompt"]
    ToolDispatch --> BuildCmds["build_commands.py"]
    ToolDispatch --> GitHubTools["github_tools.py"]
    ToolDispatch --> LogTools["log_tools.py"]
    ToolDispatch --> PlanTools["plan_tools.py"]
```

**Key modules:**

| Module | Responsibility |
|--------|---------------|
| `agentception/mcp/server.py` | Tool catalogue (`TOOLS`), `list_prompts()`, all JSON-RPC method handlers |
| `agentception/mcp/resources.py` | Resource + template catalogue, `read_resource()` URI dispatcher |
| `agentception/mcp/prompts.py` | Prompt catalogue, `get_prompt()` dispatcher |
| `agentception/mcp/build_commands.py` | Build lifecycle tool implementations |
| `agentception/mcp/github_tools.py` | GitHub API tool implementations |
| `agentception/mcp/log_tools.py` | Observability tool implementations |
| `agentception/mcp/plan_tools.py` | Plan validation and phase-gate tool implementations |

---

## Transports

Two transports are available — both speak the same JSON-RPC 2.0 protocol.

### stdio

An MCP client discovers and spawns the server via a configuration file. The entry looks like:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose",
        "-f", "/path/to/agentception/docker-compose.yml",
        "exec", "-T",
        "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ]
    }
  }
}
```

Replace `/path/to/agentception` with the absolute path to your local clone.

**Protocol version:** `2025-03-26` (value of `_MCP_PROTOCOL_VERSION` in `agentception/mcp/server.py`).

**Auto-approved methods** (safe to call without human confirmation):

| Method | Rationale |
|--------|-----------|
| `ping` | No-op health check |
| `resources/list` | Pure read |
| `resources/templates/list` | Pure read |
| `resources/read` | Pure read — all `ac://` URIs are side-effect-free |
| `prompts/list` | Pure read |
| `prompts/get` | Pure read |
| `log_run_step` | Append-only DB write, no external effects |
| `log_run_error` | Append-only DB write, no external effects |

All other tools require explicit human confirmation before execution.

### HTTP

The HTTP transport is available at `POST /api/mcp` once the containers are running. It follows the [MCP 2025-03-26 Streamable HTTP spec](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports/).

**Request shape (JSON-RPC 2.0):**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "log_run_step",
    "arguments": {
      "issue_number": 42,
      "step_name": "Running mypy"
    }
  }
}
```

**Batch support:** Send a JSON array of request objects to process multiple calls in one HTTP round-trip. Notifications (requests without `id`) return `202 Accepted`.

**Authentication:** When `AC_API_KEY` is set, the HTTP endpoint is protected by `ApiKeyMiddleware`. Include the key as a Bearer token:

```json
{
  "mcpServers": {
    "agentception": {
      "url": "http://localhost:1337/api/mcp",
      "headers": {
        "Authorization": "Bearer your-generated-key-here"
      }
    }
  }
}
```

---

## Tools

The server exposes **12 tools** — actions with side effects. Read-only state inspection is exposed as [Resources](#resources), not tools.

### Tool dispatch

Each tool is routed to its implementation module by `call_tool_async` in `server.py`.

```mermaid
flowchart TD
    callTool["call_tool_async"] --> buildCmds["build_commands.py"]
    callTool --> githubTools["github_tools.py"]
    callTool --> logTools["log_tools.py"]
    callTool --> planTools["plan_tools.py"]
    buildCmds --> claimRun["build_claim_run"]
    buildCmds --> completeRun["build_complete_run"]
    buildCmds --> spawnChild["build_spawn_adhoc_child"]
    buildCmds --> cancelRun["build_cancel_run"]
    githubTools --> addLabel["github_add_label"]
    githubTools --> removeLabel["github_remove_label"]
    githubTools --> addComment["github_add_comment"]
    logTools --> logStep["log_run_step"]
    logTools --> logError["log_run_error"]
    planTools --> validateSpec["plan_validate_spec"]
    planTools --> validateManifest["plan_validate_manifest"]
    planTools --> advancePhase["plan_advance_phase"]
```

---

### Plan tools

#### `plan_validate_spec`

Validate a JSON string against the PlanSpec schema. In-memory only — no DB writes.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spec_json` | `string` | yes | A JSON-encoded PlanSpec object to validate |

**Output (success):**

```json
{ "valid": true, "spec": { "...PlanSpec fields..." } }
```

**Output (failure):**

```json
{ "valid": false, "errors": ["field 'title' is required", "..."] }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 1,
  "method": "tools/call",
  "params": {
    "name": "plan_validate_spec",
    "arguments": { "spec_json": "{\"title\": \"My Plan\", \"phases\": []}" }
  }
}
```

---

#### `plan_validate_manifest`

Validate a JSON string against the EnrichedManifest schema. In-memory only — no DB writes.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `json_text` | `string` | yes | A JSON-encoded EnrichedManifest object to validate |

**Output (success):**

```json
{ "valid": true, "manifest": { "..." }, "total_issues": 12, "estimated_waves": 3 }
```

**Output (failure):**

```json
{ "valid": false, "errors": ["..."] }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 2,
  "method": "tools/call",
  "params": {
    "name": "plan_validate_manifest",
    "arguments": { "json_text": "{\"issues\": []}" }
  }
}
```

---

#### `plan_advance_phase`

Atomically advance a phase gate: verify all `from_phase` issues for the given initiative are closed, then unlock all `to_phase` issues by removing the blocked label and adding the active label. **Irreversible** — always requires human confirmation.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `initiative` | `string` | yes | Initiative label shared by all phase issues (e.g. `agentception-ux-phase1b-to-phase3`) |
| `from_phase` | `string` | yes | Phase label that must be fully closed before advancing (e.g. `phase-1`) |
| `to_phase` | `string` | yes | Phase label whose issues become active on success (e.g. `phase-2`) |

**Output (success):**

```json
{ "advanced": true, "unlocked_count": 5 }
```

**Output (failure — open issues remain):**

```json
{ "advanced": false, "error": "2 issues still open in phase-1", "open_issues": [101, 102] }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 3,
  "method": "tools/call",
  "params": {
    "name": "plan_advance_phase",
    "arguments": {
      "initiative": "agentception-ux-phase1b-to-phase3",
      "from_phase": "phase-1",
      "to_phase": "phase-2"
    }
  }
}
```

---

### GitHub tools

#### `github_add_label`

Add a label to a GitHub issue. Invalidates the read cache.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_number` | `integer` | yes | GitHub issue number |
| `label` | `string` | yes | Label name to add |

**Output:**

```json
{ "ok": true, "issue_number": 42, "added": "bug" }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 4,
  "method": "tools/call",
  "params": {
    "name": "github_add_label",
    "arguments": { "issue_number": 42, "label": "bug" }
  }
}
```

---

#### `github_remove_label`

Remove a label from a GitHub issue. Idempotent — no error if the label is not present. Invalidates the read cache.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_number` | `integer` | yes | GitHub issue number |
| `label` | `string` | yes | Label name to remove |

**Output:**

```json
{ "ok": true, "issue_number": 42, "removed": "bug" }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 5,
  "method": "tools/call",
  "params": {
    "name": "github_remove_label",
    "arguments": { "issue_number": 42, "label": "bug" }
  }
}
```

---

#### `github_add_comment`

Post a Markdown comment on a GitHub issue. Routes comments through the typed, logged interface — do not shell out to `gh issue comment`. Returns the comment URL for cross-referencing.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_number` | `integer` | yes | GitHub issue number to comment on |
| `body` | `string` | yes | Markdown body for the comment (GitHub-flavoured Markdown) |

**Output:**

```json
{ "ok": true, "issue_number": 42, "comment_url": "https://github.com/org/repo/issues/42#issuecomment-123" }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 6,
  "method": "tools/call",
  "params": {
    "name": "github_add_comment",
    "arguments": { "issue_number": 42, "body": "## Status\nImplementation complete. PR incoming." }
  }
}
```

---

### Build tools

#### `build_claim_run`

Atomically claim a pending run before spawning its agent. Transitions the run from `pending_launch` to `implementing`. Call this with the `run_id` from `ac://runs/pending` immediately before firing the agent so the run cannot be double-claimed by a concurrent dispatcher.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `run_id` | `string` | yes | Run ID returned by `ac://runs/pending` |

**Output (success):**

```json
{ "ok": true, "run_id": "issue-42", "previous_state": "pending_launch" }
```

**Output (already claimed):**

```json
{ "ok": false, "reason": "Run issue-42 is already in state 'implementing'" }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 7,
  "method": "tools/call",
  "params": {
    "name": "build_claim_run",
    "arguments": { "run_id": "issue-42" }
  }
}
```

---

#### `build_spawn_adhoc_child`

Spawn a child agent run from within a coordinator's tool loop. Creates a git worktree, a DB row with `parent_run_id` linking it to the coordinator, and fires the agent loop immediately as an asyncio task. **Irreversible** — always requires human confirmation.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `parent_run_id` | `string` | yes | Run ID of the coordinator — links the child in the DB hierarchy |
| `role` | `string` | yes | Role slug for the child agent (e.g. `developer`) |
| `task_description` | `string` | yes | Plain-language description of the child's task. Be specific: files to touch, expected output, constraints |
| `figure` | `string` | no | Cognitive figure slug override (e.g. `guido_van_rossum`). Omit to use the role default |
| `base_branch` | `string` | no | Git ref to branch the child worktree from. Defaults to `origin/dev` |

**Output:**

```json
{
  "ok": true,
  "child_run_id": "issue-43",
  "worktree_path": "/tmp/worktrees/issue-43",
  "cognitive_arch": "guido_van_rossum:python:fastapi"
}
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 8,
  "method": "tools/call",
  "params": {
    "name": "build_spawn_adhoc_child",
    "arguments": {
      "parent_run_id": "issue-40",
      "role": "developer",
      "task_description": "Implement the user registration endpoint in agentception/routes/api/auth.py"
    }
  }
}
```

---

#### `build_complete_run`

Record that the agent has finished work and transition the run to `completed`. Persists the done event, linking the PR and updating workflow state. Call this as the final action after pushing the branch and opening the PR.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_number` | `integer` | yes | GitHub issue number |
| `pr_url` | `string` | yes | Full URL of the pull request opened |
| `agent_run_id` | `string` | yes | The agent's own run ID exactly as it appears in the task briefing (e.g. `issue-858` or `review-900`) |
| `summary` | `string` | no | One-sentence summary of the work done |
| `grade` | `string` | no | Reviewer grade (`A`/`B`/`C`/`D`/`F`). Required when called by a reviewer — `A`/`B` merges the PR, `C`/`D`/`F` rejects it with feedback |
| `reviewer_feedback` | `string` | no | Detailed feedback posted as an issue comment when the grade is `C`, `D`, or `F` (rejection). Omit for `A`/`B` grades and non-reviewer roles |

**Output:**

```json
{ "ok": true, "run_id": "issue-42", "status": "completed" }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 9,
  "method": "tools/call",
  "params": {
    "name": "build_complete_run",
    "arguments": {
      "issue_number": 42,
      "pr_url": "https://github.com/org/repo/pull/100",
      "agent_run_id": "issue-42",
      "summary": "Implemented user registration with input validation"
    }
  }
}
```

---

#### `build_cancel_run`

Permanently cancel a run. Terminal state — cannot resume. Valid from any non-terminal state.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `run_id` | `string` | yes | The run ID to cancel |

**Output:**

```json
{ "ok": true, "run_id": "issue-42", "status": "cancelled" }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 10,
  "method": "tools/call",
  "params": {
    "name": "build_cancel_run",
    "arguments": { "run_id": "issue-42" }
  }
}
```

---

### Log tools

#### `log_run_step`

Signal that the agent is starting a new execution step. Call this whenever beginning a distinct phase of work so the mission-control dashboard can track progress in real time. This tool never changes run state.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_number` | `integer` | yes | GitHub issue number being worked on |
| `step_name` | `string` | yes | Short label for the step (e.g. `Reading codebase`) |
| `agent_run_id` | `string` | no | The agent's worktree ID (e.g. `issue-938`) |

**Output:**

```json
{ "ok": true }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 11,
  "method": "tools/call",
  "params": {
    "name": "log_run_step",
    "arguments": { "issue_number": 42, "step_name": "Running mypy" }
  }
}
```

---

#### `log_run_error`

Record an unrecoverable error or crash with semantic distinction from a step message. Use this when the agent is aborting due to an exception, API failure, or any condition it cannot recover from. The dashboard surfaces error events differently for operator triage. After calling this, also call `build_cancel_run`. Never changes run state on its own.

**Input schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_number` | `integer` | yes | GitHub issue number |
| `error` | `string` | yes | Human-readable description of the failure. Include exception type and message |
| `agent_run_id` | `string` | no | The agent's worktree ID |

**Output:**

```json
{ "ok": true }
```

**Example request:**

```json
{
  "jsonrpc": "2.0", "id": 12,
  "method": "tools/call",
  "params": {
    "name": "log_run_error",
    "arguments": { "issue_number": 42, "error": "mypy failed with 3 errors in agentception/routes/api/auth.py" }
  }
}
```

---

### Agent role tool surfaces

Different agent roles are restricted to different subsets of MCP tools. The allowlists below show which tools each role can invoke.

```mermaid
classDiagram
    class DeveloperAllowlist {
        build_complete_run
        build_cancel_run
        github_add_comment
        log_run_step
        log_run_error
    }
    class ReviewerAllowlist {
        build_complete_run
        build_cancel_run
        github_add_comment
        log_run_step
        log_run_error
    }
    class CoordinatorAllowlist {
        build_claim_run
        build_spawn_adhoc_child
        build_complete_run
        build_cancel_run
        github_add_label
        github_remove_label
        github_add_comment
        log_run_step
        log_run_error
        plan_advance_phase
    }
```

Developer and Reviewer agents have the same MCP tool surface — the distinction is in their non-MCP IDE tools (file editing, code search, etc.) and their task prompt. Coordinators have the full MCP tool surface because they manage the lifecycle of other agents.

---

## Resources

Resources are pure reads — stateless, cacheable, and side-effect-free. Call them via `resources/read` with the URI.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "resources/read",
  "params": { "uri": "ac://runs/active" }
}
```

### Static resources

These URIs are fixed — no path parameters.

| URI | Name | What it returns |
|-----|------|----------------|
| `ac://runs/active` | Active runs | All runs in a live or blocked state (`pending_launch`, `implementing`, `reviewing`, `blocked`). Returns `{ok, count, runs: [...]}`. |
| `ac://runs/pending` | Pending runs | Runs queued for Dispatcher launch. Each item has `run_id`, `issue_number`, `role`, `host_worktree_path`, `batch_id`. Returns `{count, pending: [...]}`. |
| `ac://system/dispatcher` | Dispatcher state | Run counts per status, active run total, and the latest active `batch_id`. |
| `ac://system/health` | System health | DB reachability and per-status run counts. Always returns a result — `db_ok: false` signals a degraded database. |
| `ac://system/config` | Pipeline config | Current pipeline label configuration: `claim_label`, `active_label`, `gated_label`, and the configured GitHub repo. Read before writing labels to ensure you use canonical names. |
| `ac://plan/schema` | PlanSpec schema | JSON Schema for `PlanSpec` — the plan-step-v2 YAML contract. Read this before calling `plan_validate_spec`. |
| `ac://plan/labels` | GitHub labels | Full GitHub label list for the configured repository. Returns `{labels: [{name, description}, ...]}`. |
| `ac://roles/list` | Available roles | All role slugs defined in the team taxonomy. Returns `{roles: [str, ...]}` sorted alphabetically. |
| `ac://arch/figures` | Cognitive figure index | Index of all cognitive figures in the corpus. Returns `{figures: [{id, display_name, description}]}` sorted by id. |
| `ac://arch/archetypes` | Cognitive archetype index | Index of all cognitive archetypes. Returns `{archetypes: [{id, display_name, description}]}`. |

### Resource templates

These URIs contain path parameters following [RFC 6570 Level 1](https://www.rfc-editor.org/rfc/rfc6570) template syntax.

| URI template | Name | What it returns |
|--------------|------|----------------|
| `ac://runs/{run_id}` | Run metadata | Lightweight metadata: `status`, `issue_number`, `parent_run_id`, `worktree_path`, `tier`, `role`, `batch_id`. Returns `{ok: false}` when the run does not exist. |
| `ac://runs/{run_id}/status` | Run status | Current `status` and `completed_at` timestamp. Returns `{ok, run_id, status, completed_at}`. |
| `ac://runs/{run_id}/children` | Child runs | All runs spawned by a given `parent_run_id`, ordered by spawn time. Returns `{ok, count, children: [...]}`. |
| `ac://runs/{run_id}/events` | Run event log | Structured MCP events (`log_run_step`, `log_run_blocker`, etc.). Append `?after_id=N` to page through events incrementally. Returns `{ok, count, events: [...]}`. |
| `ac://runs/{run_id}/context` | Run task context | Full task context — the authoritative DB-sourced `RunContextRow`. Includes `run_id`, `status`, `role`, `cognitive_arch`, `task_description`, `issue_number`, `pr_number`, `worktree_path`, `branch`, `tier`, `org_domain`, `batch_id`, `parent_run_id`, `gh_repo`, `spawned_at`, `last_activity_at`, `completed_at`. |
| `ac://runs/{run_id}/task` | Run task description | The `task_description` field for the run as plain text. |
| `ac://batches/{batch_id}/tree` | Batch run tree | All runs in a batch as a flat list with `parent_run_id` references. Assemble into a tree by following `parent_run_id` links. Returns `{ok, count, nodes: [...]}`. |
| `ac://plan/figures/{role}` | Cognitive figures for role | Figures compatible with a given role slug. Returns `{role, figures: [{id, display_name, description}]}`. |
| `ac://roles/{slug}` | Role definition | Full role definition Markdown. Returns `{slug, content}`. Returns `{ok: false}` when the slug is not found. |
| `ac://arch/figures/{figure_id}` | Cognitive figure profile | Full profile: `id`, `display_name`, `description`, `overrides` (atom values), `skill_domains`, `heuristic`, `failure_modes`, `prompt_injection`. |
| `ac://arch/archetypes/{archetype_id}` | Cognitive archetype profile | Full definition: `id`, `display_name`, `description`, default atom values, characteristic traits. |
| `ac://arch/skills/{skill_id}` | Skill domain profile | Full definition: `id`, `display_name`, `description`, characteristic patterns. |
| `ac://arch/atoms/{atom_id}` | Cognitive atom profile | Full definition: `id`, `display_name`, `description`, all possible values with their meanings. |

### Resource resolution

The `read_resource` dispatcher in `resources.py` parses the `ac://` URI and routes to the appropriate query function based on the domain (netloc) and path.

```mermaid
flowchart LR
    URI["ac://domain/path"] --> ReadResource["read_resource"]
    ReadResource -->|"parse URI"| DomainRouter{Domain?}
    DomainRouter -->|runs| RunsQueries["query_tools.py\nquery_active_runs\nquery_pending_runs\nquery_run\nquery_children\nquery_run_events\nquery_run_context\nquery_run_status"]
    DomainRouter -->|batches| BatchQueries["query_tools.py\nquery_run_tree"]
    DomainRouter -->|system| SystemQueries["query_tools.py\nquery_system_health\nquery_dispatcher_state\n+ _get_system_config"]
    DomainRouter -->|plan| PlanTools["plan_tools.py\nplan_get_schema\nplan_get_labels\nplan_get_cognitive_figures"]
    DomainRouter -->|roles| RoleFiles[".agentception/roles/*.md"]
    DomainRouter -->|arch| ArchFiles["cognitive_archetypes/\nfigures · archetypes\nskills · atoms"]
```

---

## Prompts

Prompts are fetched via `prompts/get`. Use `prompts/list` to enumerate all available prompts.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "prompts/get",
  "params": { "name": "task/briefing", "arguments": { "run_id": "issue-42" } }
}
```

### Parameterised prompts

| Prompt name | Arguments | What it returns |
|-------------|-----------|----------------|
| `task/briefing` | `run_id: str` (required) | Full task briefing for an agent run, assembled live from the DB. Inlines: role definition Markdown, cognitive figure profile, skill domain profiles, assignment text (`task_description`), and resource links (`ac://runs/{run_id}/context`, `ac://runs/{run_id}/events`). This is the first message delivered to every agent loop. |

### Static agent prompts

These prompts are compiled from `.agentception/*.md` files at server startup. They have no arguments.

| Prompt name | Description |
|-------------|-------------|
| `agent/engineer` | Engineering worker — implement a single GitHub issue end-to-end |
| `agent/reviewer` | Code review worker — review and merge a single pull request |
| `agent/conductor` | Agent conductor — coordinate multi-step agent workflows |
| `agent/command-policy` | Agent command policy — rules for safe shell and git usage |
| `agent/pipeline-howto` | Pipeline how-to — phase-gate, dependency, and label conventions |
| `agent/task-spec` | Agent task context specification — DB-backed `RunContextRow` field reference |
| `agent/cognitive-arch-enrichment-spec` | Cognitive architecture enrichment specification |
| `agent/conflict-rules` | Conflict resolution rules for concurrent agent operations |

### Dynamic role prompts

Role prompts are discovered at startup from `.agentception/roles/*.md` files. Each file produces one prompt entry following the pattern `role/<slug>`.

For example, if `.agentception/roles/developer.md` exists, the prompt `role/developer` is registered with description `"Role definition for the 'developer' agent role"`.

Use `prompts/list` to see the full set of role slugs available in your deployment. Use `ac://roles/list` to get just the slug names.

---

## Error codes

The server uses standard JSON-RPC 2.0 error codes for protocol-level errors. These appear in the top-level `error` key of the response (distinct from tool-level `isError` results).

| Code | Name | When it occurs |
|------|------|----------------|
| `-32700` | Parse error | The request body is not valid JSON |
| `-32600` | Invalid request | The JSON is valid but not a valid JSON-RPC 2.0 request object |
| `-32601` | Method not found | The `method` field names an unknown JSON-RPC method |
| `-32602` | Invalid params | The `params` field is missing a required key (e.g. `resources/read` called without `uri`) |
| `-32603` | Internal error | An unexpected server-side exception occurred |

**Example: invalid params error (missing `uri` in `resources/read`)**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32602,
    "message": "resources/read requires params.uri",
    "data": null
  }
}
```

---

## Related guides

- [docs/guides/mcp.md](../guides/mcp.md) — MCP client setup, approval tiers, and usage patterns
- [docs/guides/dispatch.md](../guides/dispatch.md) — Dispatching agents via `POST /api/dispatch/issue`
- [docs/reference/type-contracts.md](type-contracts.md) — Full type contract reference including MCP protocol types (sections 11–13)
