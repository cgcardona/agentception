from __future__ import annotations

"""AgentCeption MCP package.

Provides Model Context Protocol (JSON-RPC 2.0) tool definitions and
dispatchers for the AgentCeption pipeline.  All tools operate within the
``agentception/`` boundary — zero imports from external packages.

Public surface:
  - ``agentception.mcp.types``      — protocol TypedDicts (ACToolDef, ACToolResult, …)
  - ``agentception.mcp.plan_tools`` — plan_validate_spec(), plan_validate_manifest()
  - ``agentception.mcp.server``     — JSON-RPC 2.0 dispatcher (handle_request)

Boundary constraint: zero imports from external packages.
"""
