# AgentCeption — Cursor MCP Integration

AgentCeption exposes an MCP (Model Context Protocol) server so Cursor and Claude can invoke AgentCeption tools directly from the editor.

## How it works

The MCP server runs inside the AgentCeption Docker container. Cursor communicates with it over stdio, via `docker compose exec -T agentception python -m agentception.mcp.stdio_server`.

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

## MCP Auto-Approval

The repository ships a `.cursor/mcp.json` at the repo root that connects Cursor to the
AgentCeption HTTP MCP endpoint. Auto-approval is tiered by risk — read-only and
observability tools are auto-approved; tools that reach outside the service boundary
(filing GitHub issues, starting agents, advancing phase gates) always require an explicit
human confirmation click.

```json
{
  "mcpServers": {
    "agentception": {
      "url": "http://localhost:10003/mcp",
      "autoApprove": [
        "plan_get_schema",
        "plan_validate_spec",
        "plan_get_labels",
        "plan_validate_manifest",
        "build_get_pending_launches",
        "build_report_step",
        "build_report_blocker",
        "build_report_decision"
      ]
    }
  }
}
```

### Tool approval tiers

| Tier | Tools | Rationale |
|------|-------|-----------|
| **Green — auto-approved** | `plan_get_schema`, `plan_validate_spec`, `plan_get_labels`, `plan_validate_manifest`, `build_get_pending_launches` | Pure reads or in-memory validation — no external effects. |
| **Green — auto-approved** | `build_report_step`, `build_report_blocker`, `build_report_decision` | Append-only observability writes to the DB — no external effects, trivially recoverable. |
| **Yellow — prompt** | `build_report_done` | Changes pipeline run state in the DB; recoverable but worth a confirmation. |
| **Red — always prompt** | `plan_spawn_coordinator`, `plan_advance_phase` | File real GitHub issues, create git worktrees, and start live agents. These are irreversible side effects that always warrant an explicit human sign-off. |

**What this means for you:**

- Read-only and reporting tool calls happen without interruption.
- `plan_spawn_coordinator` and `plan_advance_phase` will always show a Cursor confirmation
  dialog before executing — this is intentional. A mis-fire on either of these creates
  real GitHub issues and running agent processes that are hard to undo.
- The AgentCeption server must be running at `http://localhost:10003` (start it with
  `docker compose up -d`).

This file is committed to the repository so the configuration is version-controlled,
reviewable in PRs, and reproducible across machines without any manual setup.

## Available MCP tools

See `agentception/mcp/server.py` and `agentception/mcp/build_tools.py` for the full list of registered tools. Cursor's MCP panel will enumerate them automatically once the server entry is in `mcp.json`.
