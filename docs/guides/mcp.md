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
AgentCeption HTTP MCP endpoint and auto-approves all tool calls:

```json
{
  "mcpServers": {
    "agentception": {
      "url": "http://localhost:10003/mcp",
      "autoApprove": ["*"]
    }
  }
}
```

**What this means for you:**

- After cloning the repo and opening it in Cursor, no manual permission clicks are required.
- The `autoApprove: ["*"]` setting causes Cursor to approve all AgentCeption MCP tool calls
  automatically, eliminating the per-session permission dialog.
- The AgentCeption server must be running at `http://localhost:10003` (start it with
  `docker compose up -d`). Without the server running, Cursor will show the server as
  unavailable, but no permission dialog will appear.

This file is committed to the repository so the configuration is version-controlled,
reviewable in PRs, and reproducible across machines without any manual setup.

## Available MCP tools

See `agentception/mcp/server.py` and `agentception/mcp/build_tools.py` for the full list of registered tools. Cursor's MCP panel will enumerate them automatically once the server entry is in `mcp.json`.
