# MCP Server Reference

AgentCeption exposes a full [Model Context Protocol](https://modelcontextprotocol.io/) server with tools, resources, and prompts. This document is the complete, machine-verifiable catalogue of every endpoint.

---

## Table of Contents

- [Transports](#transports)
  - [stdio (Cursor)](#stdio-cursor)
  - [HTTP](#http)
- [Tools](#tools)
  - [Plan tools](#plan-tools)
  - [GitHub tools](#github-tools)
  - [Build tools](#build-tools)
  - [Log tools](#log-tools)
- [Resources](#resources)
  - [Static resources](#static-resources)
  - [Resource templates](#resource-templates)
- [Prompts](#prompts)
  - [Parameterised prompts](#parameterised-prompts)
  - [Static agent prompts](#static-agent-prompts)
  - [Dynamic role prompts](#dynamic-role-prompts)
- [Error codes](#error-codes)

---

## Transports

### stdio (Cursor)

Cursor discovers and spawns the server via `.cursor/mcp.json`. The entry looks like:

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
      ],
      "autoApprove": [
        "ping",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
        "log_run_step",
        "log_run_error"
      ]
    }
  }
}
```

Replace `/path/to/agentception` with the absolute path to your local clone.

**Protocol version:** `2025-03-26` (value of `_MCP_PROTOCOL_VERSION` in `agentception/mcp/server.py`).

**Auto-approved tools** (no Cursor confirmation dialog):

| Tool / method | Rationale |
|---------------|-----------|
| `ping` | No-op health check |
| `resources/list` | Pure read |
| `resources/templates/list` | Pure read |
| `resources/read` | Pure read — all `ac://` URIs are side-effect-free |
| `prompts/list` | Pure read |
| `prompts/get` | Pure read |
| `log_run_step` | Append-only DB write, no external effects |
| `log_run_error` | Append-only DB write, no external effects |

All other tools require a Cursor confirmation dialog before execution.

### HTTP

The HTTP transport is available at `POST /api/mcp` once the containers are running.

**Request shape (JSON-RPC 2.0):**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "log_run_step",
    "arguments": {
      "run_id": "issue-42",
      "step": "Running mypy"
    }
  }
}
```

**Batch support:** Send a JSON array of request objects to process multiple calls in one HTTP round-trip.

**Authentication:** No authentication is required for the HTTP endpoint. It is intended for use within the Docker network. Do not expose it publicly without adding authentication.

---

## Tools

The server exposes **12 tools** — actions with side effects. Read-only state inspection is exposed as [Resources](#resources), not tools.

### Plan tools

| Tool name | Description | Required inputs | Return shape |
|-----------|-------------|-----------------|--------------|
| `plan_validate_spec` | Validate a PlanSpec YAML string against the JSON Schema. In-memory only — no DB writes. | `spec_json: str` | `{valid: bool, errors: [str]}` |
| `plan_validate_manifest` | Validate an EnrichedManifest JSON string (coordinator input contract). In-memory only. | `json_text: str` | `{valid: bool, errors: [str]}` |
| `plan_advance_phase` | Advance an initiative from one phase label to the next, opening issues for the new phase. Irreversible — always prompts in Cursor. | `initiative: str`, `from_phase: str`, `to_phase: str` | `{advanced: bool, opened: int, errors: [str]}` |

### GitHub tools

| Tool name | Description | Required inputs | Return shape |
|-----------|-------------|-----------------|--------------|
| `github_add_label` | Add a label to a GitHub issue. | `issue_number: int`, `label: str` | `{ok: bool, issue_number: int}` |
| `github_remove_label` | Remove a label from a GitHub issue. | `issue_number: int`, `label: str` | `{ok: bool, issue_number: int}` |
| `github_add_comment` | Post a Markdown comment on a GitHub issue. Routes through the typed, logged interface — do not shell out to `gh issue comment`. | `issue_number: int`, `body: str` | `{ok: bool, issue_number: int, comment_url: str}` |

### Build tools

| Tool name | Description | Required inputs | Return shape |
|-----------|-------------|-----------------|--------------|
| `build_claim_run` | Transition a run from `pending_launch` to `implementing`. Called by the agent on startup. | `run_id: str` | `{ok: bool, run_id: str, status: str}` |
| `build_spawn_adhoc_child` | Spawn a new child run for a given issue. Creates a git worktree and DB row. Irreversible. | `run_id: str`, `issue_number: int`, `role: str` | `{ok: bool, child_run_id: str, worktree: str, branch: str}` |
| `build_complete_run` | Mark a run as `completed`, release its worktree, and trigger the auto-reviewer. | `run_id: str`, `issue_number: int`, `pr_url: str` | `{ok: bool, run_id: str, status: str}` |
| `build_cancel_run` | Permanently cancel a run (terminal — cannot resume). | `run_id: str` | `{ok: bool, run_id: str, status: str}` |

### Log tools

| Tool name | Description | Required inputs | Return shape |
|-----------|-------------|-----------------|--------------|
| `log_run_step` | Record a step-start event in the run's event log. | `run_id: str`, `step: str` | `{ok: bool, event_id: int}` |
| `log_run_error` | Record an error event with optional traceback. | `run_id: str`, `error: str` | `{ok: bool, event_id: int}` |

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

| URI | What it returns |
|-----|----------------|
| `ac://runs/active` | All runs currently in a live or blocked state (`pending_launch`, `implementing`, `reviewing`, `blocked`). Returns `{ok: true, count: int, runs: [...]}`. |
| `ac://runs/pending` | Runs queued for Dispatcher launch. Each item has `run_id`, `issue_number`, `role`, `host_worktree_path`, `batch_id`. Returns `{count: int, pending: [...]}`. |
| `ac://system/dispatcher` | Dispatcher state: run counts per status, active run total, and the latest active `batch_id`. |
| `ac://system/health` | System-health snapshot: `db_ok` flag and total runs per status. Always returns a result — `db_ok: false` signals a degraded database. |
| `ac://system/config` | Current pipeline label configuration: `claim_label`, `active_label`, `gated_label`, and the configured GitHub repo. Read before writing labels to ensure you use canonical names. |
| `ac://plan/schema` | JSON Schema for `PlanSpec` — the plan-step-v2 YAML contract. Read this before calling `plan_validate_spec`. |
| `ac://plan/labels` | Full GitHub label list for the configured repository. Returns `{labels: [{name: str, description: str}, ...]}`. |
| `ac://roles/list` | All role slugs defined in the team taxonomy. Returns `{roles: [str, ...]}` sorted alphabetically. |
| `ac://arch/figures` | Index of all cognitive figures in the corpus. Returns `{figures: [{id, display_name, description}]}` sorted by id. |
| `ac://arch/archetypes` | Index of all cognitive archetypes in the corpus. Returns `{archetypes: [{id, display_name, description}]}`. |

### Resource templates

These URIs contain path parameters following [RFC 6570 Level 1](https://www.rfc-editor.org/rfc/rfc6570) template syntax.

| URI template | What it returns |
|--------------|----------------|
| `ac://runs/{run_id}` | Lightweight metadata for a single run: `status`, `issue_number`, `parent_run_id`, `worktree_path`, `tier`, `role`, `batch_id`. Returns `{ok: false}` when the run does not exist. |
| `ac://runs/{run_id}/status` | Current status and `completed_at` timestamp for a single run. Returns `{ok: bool, run_id: str, status: str, completed_at: str\|null}`. |
| `ac://runs/{run_id}/children` | All runs spawned by a given `parent_run_id`, ordered by spawn time. Returns `{ok: true, count: int, children: [...]}`. |
| `ac://runs/{run_id}/events` | Structured MCP events for a run (`log_run_step`, `log_run_blocker`, etc.). Append `?after_id=N` to page through events incrementally (returns only events with DB id > N). Returns `{ok: true, count: int, events: [...]}`. |
| `ac://runs/{run_id}/context` | Full task context — the authoritative DB-sourced `RunContextRow`. Includes `run_id`, `status`, `role`, `cognitive_arch`, `task_description`, `issue_number`, `pr_number`, `worktree_path`, `branch`, `tier`, `org_domain`, `batch_id`, `parent_run_id`, `gh_repo`, `spawned_at`, `last_activity_at`, `completed_at`. |
| `ac://batches/{batch_id}/tree` | All runs in a batch as a flat list with `parent_run_id` references. Returns `{ok: true, count: int, nodes: [...]}`. Assemble into a tree by following `parent_run_id` links. |
| `ac://plan/figures/{role}` | Cognitive architecture figures compatible with a given role slug. Returns `{role: str, figures: [{id, display_name, description}]}`. |
| `ac://roles/{slug}` | Full role definition Markdown for a given role slug. Returns `{slug: str, content: str}`. Returns `{ok: false}` when the slug is not found. |
| `ac://arch/figures/{figure_id}` | Full cognitive profile of a named figure: `id`, `display_name`, `description`, `overrides` (atom values), `skill_domains`, `heuristic`, `failure_modes`, and `prompt_injection` text. |
| `ac://arch/archetypes/{archetype_id}` | Full definition of a cognitive archetype: `id`, `display_name`, `description`, default atom values, and characteristic traits. |
| `ac://arch/skills/{skill_id}` | Full definition of a skill domain: `id`, `display_name`, `description`, and characteristic patterns. |
| `ac://arch/atoms/{atom_id}` | Full definition of a cognitive atom dimension: `id`, `display_name`, `description`, and all possible values with their meanings. |

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

- [docs/guides/mcp.md](../guides/mcp.md) — Cursor setup, approval tiers, and usage patterns
- [docs/guides/dispatch.md](../guides/dispatch.md) — Dispatching agents via `POST /api/dispatch/issue`
- [docs/reference/type-contracts.md](type-contracts.md) — Full type contract reference including MCP protocol types
