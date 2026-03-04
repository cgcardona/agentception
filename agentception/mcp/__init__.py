from __future__ import annotations

"""AgentCeption MCP package.

Provides Model Context Protocol (JSON-RPC 2.0) tool definitions and
dispatchers for the plan-step-v2 pipeline.  All tools operate within the
``agentception/`` boundary — zero imports from maestro, muse, kly, or storpheus.

Public surface:
  - ``agentception.mcp.types``      — protocol TypedDicts (ACToolDef, ACToolResult, …)
  - ``agentception.mcp.plan_tools`` — plan_get_schema(), plan_validate_spec(),
                                      plan_get_labels(), plan_validate_manifest(),
                                      plan_spawn_coordinator()
  - ``agentception.mcp.server``     — JSON-RPC 2.0 dispatcher (handle_request)

Boundary constraint: zero imports from maestro, muse, kly, or storpheus.
"""
from __future__ import annotations
