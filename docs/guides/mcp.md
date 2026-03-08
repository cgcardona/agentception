# AgentCeption — MCP Integration

AgentCeption exposes a best-in-class MCP (Model Context Protocol) server so Cursor, Claude, and any MCP-aware client can invoke tools, read resources, and fetch prompts directly.

## Transports

Two transports are available — both speak the same JSON-RPC 2.0 protocol:

| Transport | Entry point | Best for |
|-----------|-------------|----------|
| **stdio** | `docker compose exec -T agentception python -m agentception.mcp.stdio_server` | Cursor IDE sessions |
| **HTTP** | `POST http://localhost:10003/api/mcp` | Web agents, CI/CD, curl, external clients |

The HTTP transport follows the MCP 2025-03-26 Streamable HTTP spec: single or batch JSON-RPC request bodies, JSON responses.  Notifications (requests without `id`) return `202 Accepted`.

## stdio configuration (`~/.cursor/mcp.json`)

Add an `agentception` entry to your `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose", "-f", "AGENTCEPTION_REPO_ROOT/docker-compose.yml",
        "exec", "-T", "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "AGENTCEPTION_REPO_ROOT"
    }
  }
}
```

Replace `AGENTCEPTION_REPO_ROOT` with the absolute path to your local clone.

## `~/.cursor/mcp.json` configuration

Add an `agentception` entry to your `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose", "-f", "AGENTCEPTION_REPO_ROOT/docker-compose.yml",
        "exec", "-T", "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "AGENTCEPTION_REPO_ROOT"
    }
  }
}
```

Replace `AGENTCEPTION_REPO_ROOT` with the absolute path to your local clone of this repo (e.g. `/Users/you/dev/agentception`).

## Running alongside other MCP servers

If you also run other MCP servers (e.g. a music composition backend), add them as independent top-level keys — they do not interfere with each other:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose", "-f", "AGENTCEPTION_REPO_ROOT/docker-compose.yml",
        "exec", "-T", "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "AGENTCEPTION_REPO_ROOT"
    },
    "other-service": {
      "command": "docker",
      "args": ["compose", "-f", "OTHER_REPO_ROOT/docker-compose.yml", "exec", "-T", "other-service", "python", "-m", "other.mcp.stdio_server"],
      "cwd": "OTHER_REPO_ROOT"
    }
  }
}
```

## Prerequisites

- AgentCeption containers must be running: `docker compose up -d`
- Verify the MCP server responds: `docker compose exec agentception python -m agentception.mcp.stdio_server`

## Three kinds of MCP endpoints

AgentCeption exposes all three MCP endpoint types:

| Kind | Purpose | How to call |
|------|---------|-------------|
| **Tools** | Actions with side effects (mutate state, post comments, start agents) | `CallMcpTool(server="user-agentception", toolName=..., arguments={...})` |
| **Resources** | Pure reads — stateless, cacheable, side-effect-free | `FetchMcpResource(server="user-agentception", uri="ac://...")` |
| **Prompts** | Agent role files and briefing templates | `prompts/get(name="role/python-developer")` or `prompts/list` |

### Resource URI catalogue

| URI | What it returns |
|-----|----------------|
| `ac://runs/active` | All live runs (pending_launch, implementing, reviewing, blocked) |
| `ac://runs/pending` | Runs queued for Dispatcher launch |
| `ac://runs/{run_id}` | Metadata for one run |
| `ac://runs/{run_id}/children` | Child runs spawned by this run |
| `ac://runs/{run_id}/events` | Structured event log; append `?after_id=N` to paginate |
| `ac://runs/{run_id}/task` | Raw `.agent-task` TOML text |
| `ac://batches/{batch_id}/tree` | All runs in a batch |
| `ac://system/dispatcher` | Dispatcher counters and active batch_id |
| `ac://system/health` | DB reachability and per-status counts |
| `ac://system/config` | Pipeline label config (canonical label names) |
| `ac://plan/schema` | PlanSpec JSON Schema |
| `ac://plan/labels` | GitHub label catalogue |
| `ac://plan/figures/{role}` | Cognitive-arch figures for a role slug |
| `ac://roles/list` | All available role slugs |
| `ac://roles/{slug}` | Full role definition Markdown for a slug |

### Prompt catalogue

`prompts/list` returns every compiled role and agent prompt.  `prompts/get(name=...)` returns the full Markdown content as a `user` message.

Naming conventions:
- `role/<slug>` — role definition (e.g. `role/python-developer`, `role/cto`)
- `agent/<name>` — agent prompt (e.g. `agent/dispatcher`, `agent/engineer`, `agent/reviewer`)

## MCP Auto-Approval

Auto-approval is tiered by risk — resources (all reads) and observability tools are
auto-approved; tools that reach outside the service boundary (filing GitHub issues,
starting agents, advancing phase gates) always require an explicit human confirmation.

```json
{
  "mcpServers": {
    "agentception": {
      "url": "http://localhost:10003/api/mcp",
      "autoApprove": [
        "plan_validate_spec",
        "plan_validate_manifest",
        "log_run_step",
        "log_run_blocker",
        "log_run_decision",
        "log_run_message",
        "log_run_error"
      ]
    }
  }
}
```

### Approval tiers

| Tier | Endpoints | Rationale |
|------|-----------|-----------|
| **Auto — resources** | All `ac://` URIs | Pure reads — no external effects, always safe. |
| **Auto — prompts** | All `role/*` and `agent/*` | Static file reads — no effects. |
| **Auto — tools** | `plan_validate_spec`, `plan_validate_manifest` | In-memory validation only. |
| **Auto — tools** | `log_run_step`, `log_run_blocker`, `log_run_decision`, `log_run_message`, `log_run_error` | Append-only DB writes — no external effects. |
| **Prompt** | `build_claim_run`, `build_complete_run`, `build_cancel_run`, `build_stop_run`, `build_block_run`, `build_resume_run` | Pipeline state transitions in the DB — recoverable but worth confirming. |
| **Prompt** | `github_add_label`, `github_remove_label`, `github_claim_issue`, `github_unclaim_issue`, `github_add_comment` | External GitHub API mutations. |
| **Always prompt** | `plan_spawn_coordinator`, `plan_advance_phase`, `build_spawn_child_run`, `build_teardown_worktree` | Create real GitHub issues, git worktrees, and live agents — irreversible side effects. |

**What this means for you:**

- Resource reads (`FetchMcpResource`), prompt fetches, and observability tool calls happen without interruption.
- `plan_spawn_coordinator` and `plan_advance_phase` always show a Cursor confirmation dialog — a mis-fire creates real GitHub issues and running agent processes that are hard to undo.
- The HTTP endpoint is available at `http://localhost:10003/api/mcp` once containers are running.

## Available tools, resources, and prompts

| Module | What it registers |
|--------|-------------------|
| `agentception/mcp/server.py` | Tool catalogue (`TOOLS`), `list_prompts()`, all JSON-RPC handlers |
| `agentception/mcp/resources.py` | Resource + template catalogue, `read_resource()` dispatcher |
| `agentception/mcp/prompts.py` | Prompt catalogue, `get_prompt()` dispatcher |

Cursor's MCP panel enumerates all three automatically once the server entry is in `mcp.json`.
