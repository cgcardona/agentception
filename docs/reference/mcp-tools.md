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

---

### `log_run_heartbeat`

Update `ac_agent_runs.last_activity_at` to prove the agent is still alive.

**Signature**

```python
async def log_run_heartbeat(run_id: str) -> dict[str, object]:
```

**Parameters**

| Field    | Type   | Required | Description                              |
|----------|--------|----------|------------------------------------------|
| `run_id` | string | ✅        | The agent run ID (e.g. `"issue-275"`).   |

**Return shapes**

| Condition          | Response                                                    |
|--------------------|-------------------------------------------------------------|
| Run found          | `{"ok": true, "last_activity_at": "<iso8601 UTC>"}`         |
| Unknown `run_id`   | `{"ok": false, "error": "run not found"}`                   |
| DB failure         | `{"ok": false, "error": "<exception message>"}`             |

**Recommended call interval:** every **2–5 minutes** while the agent is active.

**Non-blocking on DB failure:** the tool catches all exceptions, logs at
`WARNING`, and returns `{"ok": false, "error": ...}` — it never raises.
Agents should not abort on a failed heartbeat; the next call will retry.

**Example MCP call**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "log_run_heartbeat",
    "arguments": {"run_id": "issue-275"}
  }
}
```

**Example success response**

```json
{"ok": true, "last_activity_at": "2024-01-15T12:00:00+00:00"}
```

**Implementation notes**

- Uses a single `UPDATE … RETURNING last_activity_at` query — does not load
  the full `ACAgentRun` row.
- Does **not** change `status` or any other column.
- The stale detector in the poller reads `last_activity_at` to distinguish a
  slow-but-alive agent (heartbeat recent) from a crashed one (heartbeat stale).
