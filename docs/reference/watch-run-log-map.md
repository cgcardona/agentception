# watch_run.py — Log Pattern Map

This document maps every log pattern consumed by `scripts/watch_run.py` to the
exact emission site in the application source. It is the authoritative reference
for Phase 1 structured event emission work.

**Invariant:** every row in the table below corresponds to a `re.compile` pattern
or substring match in `scripts/watch_run.py` (lines 42–110) and a `logger.*`
call in the application source. The "Proposed subtype" column names the
`ACAgentEvent.subtype` value that will replace the log line in Phase 1.

---

## Pattern table

| Pattern (regex/substring) | Rendered label in watch_run | Emission file:function:line | Proposed subtype |
|---|---|---|---|
| `adhoc run dispatched — run_id=\S+ role=\S+ arch=\S+ context_files=\d+` | 🚀 LAUNCHED `<run_id>` | `agentception/services/agent_loop.py::run_agent_loop:~line 80` | `run.dispatched` |
| `agent_loop start — run_id=\S+ issue=\S+ tools=\d+` | loop ready — N tools available | `agentception/services/agent_loop.py::run_agent_loop:~line 120` | `run.loop_start` |
| `worktree indexed — run_id=\S+ collection=\S+ files=\d+ chunks=\d+` | 🗂 worktree index ready | `agentception/services/code_indexer.py::index_codebase:~line 80` | `run.worktree_indexed` |
| `log_run_step: issue=\S+ step='Step \d+'` | (suppressed — updates ITER counter) | `agentception/mcp/build_commands.py::log_run_step:~line 50` | `run.iteration` |
| `log_run_step: issue=\S+ step='(?!Step).+'` | 📋 `<step text>` | `agentception/mcp/build_commands.py::log_run_step:~line 50` | `run.step` |
| `dispatch_tool — run_id=\S+ tool=\S+.*` | tool icon + tool name + args | `agentception/services/agent_loop.py::dispatch_tool:line 2198` | `tool.dispatch` |
| `✅ read_file_lines — \S+ lines \d+-\d+/\d+` | 📄 `<path>` lines N–M/T | `agentception/tools/file_tools.py::read_file_lines:line 233` | `tool.read_file_lines` |
| `✅ replace_in_file — \S+ \(\d+ replacement` | ✏️ `<path>` (N replacements) | `agentception/tools/file_tools.py::replace_in_file:line 178` | `tool.replace_in_file` |
| `✅ insert_after_in_file — \S+ \(inserted at byte` | ➕ `<path>` (inserted at byte N) | `agentception/tools/file_tools.py::insert_after_in_file:line 329` | `tool.insert_after_in_file` |
| `write_file — \S+( \(\d+ bytes\))?` | 💾 `<path>` (N bytes) | `agentception/tools/file_tools.py::write_file:~line 380` | `tool.write_file` |
| `run_command — '.+' \(cwd=[^)]+\)` | 🐚 `<cmd>` (cwd=`<dir>`) | `agentception/tools/shell_tools.py::run_command:~line 80` | `tool.run_command_start` |
| `run_command done — exit=\d+ stdout=\d+ stderr=\d+` | exit N stdout=N stderr=N | `agentception/tools/shell_tools.py::run_command:~line 110` | `tool.run_command_done` |
| `git_commit_and_push — branch=\S+` | 🔀 git push `<branch>` | `agentception/tools/shell_tools.py::git_commit_and_push:~line 150` | `tool.git_commit_and_push` |
| `github_mcp — tool=\S+` | 🐙 `<tool>` | `agentception/services/agent_loop.py::dispatch_tool:~line 2210` | `tool.github_mcp` |
| `LLM tool-use call — model=\S+ turns=\d+ tools=\d+` | ╔══ ITER N/100 [model…] | `agentception/services/llm.py::call_anthropic_with_tools:~line 750` | `llm.call_start` |
| `LLM usage — input=\d+ cache_written=\d+ cache_read=\d+` | in=N cache_write=N cache_read=N | `agentception/services/llm.py::call_anthropic_with_tools:~line 780` | `llm.usage` |
| `LLM reply — chars=\d+ text=.+` | 💬 (Nch) `<text>` | `agentception/services/llm.py::call_anthropic_with_tools:line 807` | `llm.reply` |
| `LLM tool-use done — stop_reason=\S+ content_chars=\d+ tool_calls=\d+` | ╚══ → N tool calls / end_turn | `agentception/services/llm.py::call_anthropic_with_tools:~line 800` | `llm.call_done` |
| `LLM retry \d+/\d+ after [\d.]+s` | (heartbeat: waiting for Anthropic) | `agentception/services/llm.py::call_anthropic_with_tools:~line 720` | `llm.retry` |
| `inter-turn delay — sleeping [\d.]+s` | ⏳ ▓▓▓░░░░ N.Ns | `agentception/services/agent_loop.py::run_agent_loop:line 397` | `run.inter_turn_delay` |
| `teardown\[[^\]]+\]: .+` | 🧹 teardown[`<run_id>`]: `<msg>` | `agentception/services/teardown.py::teardown_agent_worktree:~line 60` | `run.teardown` |
| `log_run_error` or `agent_loop LLM error` (substring) | (heartbeat: stops LLM-wait timer) | `agentception/services/agent_loop.py::run_agent_loop:~line 450` | `run.error` |
| log level `ERROR` (any message) | ❌ `<msg>` | any module via `logging.getLogger(__name__).error(...)` | `run.error` |
| `⚠️` + keyword (`429`, `stale`, `reaper`, `SSL`, `retry`, `circuit`) | ⚠️ `<msg>` | various — rate-limit / SSL / circuit-breaker paths | `run.warning` |

---

## Notes

### Log line format

`watch_run.py` parses lines emitted by the Docker log driver in this shape:

```
agentception-app | <LEVEL> <module> <message>
```

The outer `re.match` in `process_line` extracts `level`, `module`, and `msg`.
All patterns in the table above match against `msg` only.

### Emission files

| File | Role |
|------|------|
| `agentception/services/agent_loop.py` | Main agent loop — dispatches tools, paces turns, logs LLM lifecycle |
| `agentception/services/llm.py` | Anthropic API wrapper — logs call/usage/reply/done/retry |
| `agentception/tools/file_tools.py` | File I/O tools — logs read/write/replace/insert results |
| `agentception/tools/shell_tools.py` | Shell tool — logs run_command start/done and git_commit_and_push |
| `agentception/mcp/build_commands.py` | MCP callbacks — logs log_run_step (iteration + step text) |
| `agentception/services/teardown.py` | Worktree cleanup — logs teardown progress |
| `agentception/services/code_indexer.py` | Qdrant indexer — logs worktree indexed |

### Phase 1 replacement strategy

Each `Proposed subtype` value maps to a new `ACAgentEvent` row written by the
application at the same call site. The `watch_run.py` script can then consume
`ACAgentEvent` rows via SSE instead of parsing raw log lines. The log calls
remain in place for human debugging; the structured events are additive.

Scale assumption: this table is complete for the current `watch_run.py` at
commit time. If new patterns are added to `watch_run.py`, this document must
be updated in the same PR.
