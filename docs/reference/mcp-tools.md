# MCP Tool Reference

This document describes the MCP tools exposed by the AgentCeption server.
Tools are actions with side effects (state mutations, telemetry writes).
Pure reads are exposed as MCP Resources — see `ac://` URIs in the server.

---

## Log Tools — append-only telemetry

Log tools write structured events to `ac_agent_events`.  They **never** change
run state.  All log tools are best-effort: a DB failure returns
`{"ok": false, "error": "..."}` and never raises an exception that would abort
the agent.

**Remaining tools:** `log_run_step`, `log_run_error`. See [MCP reference](mcp.md) for full tool catalogue.
