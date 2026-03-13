# Activity Events — Payload Contract

This document is the canonical reference for the `activity` event subtype
written to `ac_agent_events` by `persist_activity_event`.

Every row with `event_type = "activity"` has a JSON `payload` column that
**always** contains `"subtype": "<subtype_string>"` plus the subtype-specific
fields listed below.  The SSE stream in `build_ui.py` forwards these rows
verbatim to the inspector panel.

---

## SSE envelope

```json
{
  "t": "event",
  "event_type": "activity",
  "payload": { "subtype": "<subtype>", ...fields },
  "recorded_at": "2026-03-13T19:15:45.123456+00:00"
}
```

---

## Subtypes

### `tool_invoked`

Emitted when the agent loop dispatches a tool call to the tool executor.

| Field | Type | Notes |
|---|---|---|
| `tool_name` | `str` | Name of the tool being called |
| `arg_preview` | `str` | Truncated argument preview (≤120 chars) |

```json
{
  "subtype": "tool_invoked",
  "tool_name": "read_file_lines",
  "arg_preview": "{\"path\": \"agentception/db/models.py\", \"start_line\": 1, \"end_line\": 50}"
}
```

---

### `llm_iter`

Emitted at the start of each LLM iteration (one call to the model).

| Field | Type | Notes |
|---|---|---|
| `iteration` | `int` | 0-indexed iteration counter within the current agent run |
| `model` | `str` | Model identifier (e.g. `claude-sonnet-4-5`) |
| `turns` | `int` | Number of message turns in the current context |

```json
{
  "subtype": "llm_iter",
  "iteration": 3,
  "model": "claude-sonnet-4-5",
  "turns": 12
}
```

---

### `llm_usage`

Emitted after each LLM response with token-level billing data.

| Field | Type | Notes |
|---|---|---|
| `input_tokens` | `int` | Tokens in the prompt (excluding cache hits) |
| `cache_write` | `int` | Tokens written to the prompt cache |
| `cache_read` | `int` | Tokens read from the prompt cache |

```json
{
  "subtype": "llm_usage",
  "input_tokens": 4200,
  "cache_write": 1024,
  "cache_read": 3176
}
```

---

### `llm_reply`

Emitted when the model returns a text reply (non-tool-call content block).

| Field | Type | Notes |
|---|---|---|
| `chars` | `int` | Total character count of the reply |
| `text_preview` | `str` | Truncated reply preview (≤200 chars) |

```json
{
  "subtype": "llm_reply",
  "chars": 842,
  "text_preview": "I'll start by reading the existing models to understand the schema..."
}
```

---

### `llm_done`

Emitted when the model signals it has finished (stop_reason received).

| Field | Type | Notes |
|---|---|---|
| `stop_reason` | `str` | Model stop reason (e.g. `end_turn`, `tool_use`, `max_tokens`) |
| `tool_call_count` | `int` | Number of tool calls in this iteration |

```json
{
  "subtype": "llm_done",
  "stop_reason": "tool_use",
  "tool_call_count": 2
}
```

---

### `shell_start`

Emitted immediately before a shell command is executed.

| Field | Type | Notes |
|---|---|---|
| `cmd_preview` | `str` | Truncated command preview (≤200 chars) |
| `cwd` | `str` | Working directory for the command |

```json
{
  "subtype": "shell_start",
  "cmd_preview": "python -m pytest agentception/tests/test_persist.py -v",
  "cwd": "/worktrees/issue-938"
}
```

---

### `shell_done`

Emitted after a shell command exits (success or failure).

| Field | Type | Notes |
|---|---|---|
| `exit_code` | `int` | Process exit code (0 = success) |
| `stdout_bytes` | `int` | Byte count of captured stdout |
| `stderr_bytes` | `int` | Byte count of captured stderr |

```json
{
  "subtype": "shell_done",
  "exit_code": 0,
  "stdout_bytes": 1024,
  "stderr_bytes": 0
}
```

---

### `file_read`

Emitted when the agent reads a file or a line range from a file.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | File path (relative to worktree root) |
| `start_line` | `int` | First line read (1-indexed) |
| `end_line` | `int` | Last line read (1-indexed, inclusive) |
| `total_lines` | `int` | Total lines in the file |

```json
{
  "subtype": "file_read",
  "path": "agentception/db/models.py",
  "start_line": 343,
  "end_line": 369,
  "total_lines": 596
}
```

---

### `file_replaced`

Emitted after a `replace_in_file` / `str_replace` operation completes.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | File path (relative to worktree root) |
| `replacement_count` | `int` | Number of replacements made |

```json
{
  "subtype": "file_replaced",
  "path": "agentception/db/models.py",
  "replacement_count": 1
}
```

---

### `file_inserted`

Emitted after an `insert_after_in_file` operation completes.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | File path (relative to worktree root) |

```json
{
  "subtype": "file_inserted",
  "path": "agentception/db/models.py"
}
```

---

### `file_written`

Emitted after a full `write_file` operation completes.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | File path (relative to worktree root) |
| `byte_count` | `int` | Bytes written |

```json
{
  "subtype": "file_written",
  "path": "agentception/db/activity_events.py",
  "byte_count": 8493
}
```

---

### `git_push`

Emitted after a successful `git push` to the remote.

| Field | Type | Notes |
|---|---|---|
| `branch` | `str` | Branch name that was pushed |

```json
{
  "subtype": "git_push",
  "branch": "feat/issue-938"
}
```

---

### `github_tool`

Emitted when the agent calls a GitHub MCP tool (e.g. `create_pull_request`).

| Field | Type | Notes |
|---|---|---|
| `tool_name` | `str` | Name of the GitHub tool called |
| `arg_preview` | `str` | Truncated argument preview (≤120 chars) |

```json
{
  "subtype": "github_tool",
  "tool_name": "create_pull_request",
  "arg_preview": "{\"title\": \"feat: activity event schema\", \"head\": \"feat/issue-938\"}"
}
```

---

### `delay`

Emitted when the agent deliberately sleeps (e.g. rate-limit back-off).

| Field | Type | Notes |
|---|---|---|
| `secs` | `float` | Duration of the sleep in seconds |

```json
{
  "subtype": "delay",
  "secs": 2.0
}
```

---

### `error`

Emitted when a recoverable error is caught and logged by the agent loop.

| Field | Type | Notes |
|---|---|---|
| `message` | `str` | Error message string |
| `context` | `str` | Human-readable context describing where the error occurred |

```json
{
  "subtype": "error",
  "message": "Connection refused",
  "context": "shell_done handler — stdout capture"
}
```

---

## Implementation reference

- **Persist helper:** `agentception/db/activity_events.persist_activity_event`
- **TypedDicts:** `agentception/db/activity_events` — one class per subtype
- **Subtype registry:** `agentception.db.ACTIVITY_SUBTYPES` (frozenset)
- **ORM model:** `agentception.db.models.ACAgentEvent` (`event_type = "activity"`)
- **SSE stream:** `agentception/routes/ui/build_ui._inspector_sse`

All 15 subtypes are listed in `ACTIVITY_SUBTYPES` and have a corresponding
`TypedDict` class exported from `agentception.db`.
