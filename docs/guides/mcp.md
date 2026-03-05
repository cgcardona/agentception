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

AgentCeption is designed for **zero-touch dispatcher operation** — once you paste the
dispatcher prompt, no further Cursor interaction should be required. All MCP tools are
therefore auto-approved in both the user-level and workspace-level configs.

The MCP server runs on localhost and is the same service you operate directly. There is
no external boundary being crossed — auto-approving every tool call is the correct
operational posture for this system.

### User-level config (`~/.cursor/mcp.json`)

This is the config Cursor actually uses to connect (stdio transport):

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": ["compose", "-f", "/path/to/agentception/docker-compose.yml",
               "exec", "-T", "agentception",
               "python", "-m", "agentception.mcp.stdio_server"],
      "autoApprove": ["*"]
    }
  }
}
```

### Workspace-level config (`.cursor/mcp.json`)

Checked into the repo for completeness (HTTP transport fallback):

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

**Important:** `autoApprove` changes require a **full Cursor restart** (Cmd+Q, not just
MCP server restart). Cursor reads the `autoApprove` list once at startup; a server restart
alone does not reload it.

The AgentCeption server must be running at `http://localhost:10003` (start with
`docker compose up -d`).

## Available MCP tools

See `agentception/mcp/server.py` and `agentception/mcp/build_tools.py` for the full list of registered tools. Cursor's MCP panel will enumerate them automatically once the server entry is in `mcp.json`.
