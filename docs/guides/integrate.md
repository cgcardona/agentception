# Integration Guide

AgentCeption exposes a full [Model Context Protocol](https://modelcontextprotocol.io/) server that any MCP-compatible client can use to **invoke tools** (build, GitHub, plan, log), **read resources** (runs, batches, system state, cognitive arch), and **fetch prompts** (role definitions, task briefings) directly. The server supports both a **stdio transport** (spawned as a subprocess by any MCP client) and an **HTTP transport** (`POST /api/mcp`, JSON-RPC 2.0).

For the complete catalogue of every tool, resource URI, resource template, and prompt — including JSON-RPC error codes — see the reference document:

**[docs/reference/mcp.md](../reference/mcp.md)**

For instructions on dispatching agents, managing worktrees, and the localStorage batch context bar used by the browser UI, see:

**[docs/guides/dispatch.md](dispatch.md)**
