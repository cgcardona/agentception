# Agent Speedup Plan: Cutting Iterations from 50 to ~15

**Problem:** Agents burn 30–40 iterations on pure reconnaissance (one tool call per
LLM turn, no pre-loaded context) before writing a single line of code. IDE agents
solve the same task in ~15 turns because they have semantic search, multi-tool batching, and
pre-loaded file context. We have all the infrastructure — it just isn't connected.

**Current state (confirmed):**
- Qdrant `code` collection: **3,456 points** — index is live and populated ✅
- `search_codebase` tool: **already defined and dispatched** in `agent_loop.py` ✅
- `_dispatch_tool_calls`: handles multiple tool calls per response ✅ (but sequentially)
- Model returns `tool_calls×1` every turn because prompt never says to batch ❌
- No file context injected at dispatch time ❌
- `search_codebase` tool description doesn't say "use me first" ❌
- Worktrees not re-indexed on spawn (use main repo index via `/app` path) ❌

---

## Phase 0 — Prompt wiring: teach agents to use what already exists
*Effort: tiny. Impact: highest. No code changes required.*

### 0A — `search_codebase` tool description
**File:** `agentception/tools/definitions.py`

Rewrite the `search_codebase` description from passive to directive:

> **Use this as your FIRST tool call for any code discovery task.** One semantic
> search replaces 5–10 sequential grep/cat/read calls. The index covers all `.py`,
> `.md`, `.j2`, `.yaml`, `.toml` files in the repo. Search before you read; read only
> the specific lines the search points you to.

### 0B — `worker-base.md.j2` batching rule
**File:** `scripts/gen_prompts/templates/snippets/worker-base.md.j2`

Add a "Parallel Tool Calls" section:

> **Batch your tool calls.** When you need information from multiple sources,
> emit ALL reads as tool calls in a single response — not one at a time. The
> loop processes every tool call in your response before asking you again.
> Three reads in one response = one LLM turn. Three reads across three responses
> = three LLM turns and three inter-turn delays.

### 0C — `worker-base.md.j2` search-first rule
Add a "Search Before Reading" section:

> **Call `search_codebase` before any `grep` or file read.** One semantic query
> ("where does AgentStatus get persisted?") returns exact file + line numbers.
> Only `read_file_lines` for the specific range the search returns. Never `cat`
> an entire file when you need one function.

**Deliverable:** Updated `worker-base.md.j2` → `generate.py` → regenerate
`.agentception/*.md` → PR.

---

## Phase 1 — Parallel tool dispatch: asyncio.gather in the loop
*Effort: small. Impact: free latency win when model does batch.*

### 1A — `_dispatch_tool_calls` in `agent_loop.py`

Replace the sequential `for tc in tool_calls` loop with `asyncio.gather`:

```python
async def _dispatch_tool_calls(...) -> list[dict[str, object]]:
    tasks = [
        _dispatch_single_tool(tc, worktree_path, run_id, ...)
        for tc in tool_calls
    ]
    results_raw = await asyncio.gather(*tasks, return_exceptions=True)
    results = []
    for tc, raw in zip(tool_calls, results_raw):
        content = raw if isinstance(raw, str) else f"error: {raw}"
        results.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
    return results
```

When the model batches 3 reads into one response, they now execute in parallel
instead of sequentially. Free 2–3× speedup on multi-read turns.

**Deliverable:** Updated `agent_loop.py` + updated `test_agent_loop.py` → PR.

---

## Phase 2 — Context pre-injection: files in the first message
*Effort: medium. Impact: eliminates the discovery phase entirely for scoped tasks.*

### 2A — `AdhocRunRequest` gains `context_files`

```python
class AdhocRunRequest(BaseModel):
    role: str
    task_description: str
    figure: str | None = None
    base_branch: str = "origin/dev"
    context_files: list[str] | None = None
    """Repo-relative file paths whose contents are injected into the first
    message before the task description. The agent starts with full knowledge
    of these files — zero discovery turns required."""
```

### 2B — Adhoc handler reads and injects

In `routes/api/adhoc.py`, before dispatching:

```python
injected = ""
if req.context_files:
    for path_str in req.context_files:
        abs_path = settings.repo_dir / path_str
        try:
            content = abs_path.read_text(encoding="utf-8")
            injected += f"\n\n### {path_str}\n```\n{content}\n```"
        except OSError:
            pass
task = f"{injected}\n\n---\n\n{req.task_description}" if injected else req.task_description
```

### 2C — `run_factory.py` / `agent_loop.py` pass through

Thread the `context_files` (or pre-built injected string) through
`create_run → agent_loop` so it becomes the first message content.

**Deliverable:** `AdhocRunRequest` update + adhoc handler + `run_factory.py` pass-through
+ `test_adhoc.py` coverage → PR.

---

## Phase 3 — Worktree indexing on spawn
*Effort: medium. Impact: agents can search worktree-specific changes (e.g. other
agent's uncommitted edits).*

### 3A — `run_factory.py` triggers background index

After `_create_worktree` succeeds, fire a background task:

```python
from agentception.services.code_indexer import index_codebase

# In create_run():
background_tasks.add_task(
    index_codebase,
    repo_path=worktree_path,
    collection=f"worktree-{run_id}",
)
```

### 3B — `search_codebase` tool accepts optional `collection` arg

Add an optional `collection` parameter to the agent tool so agents can
search the worktree-specific index (`worktree-{WTNAME}`) when it exists,
falling back to the main `code` collection.

### 3C — Worktree collection cleanup on teardown

In `teardown.py`, delete the `worktree-{run_id}` collection from Qdrant
when the worktree is torn down.

**Note:** Worktrees start from `origin/dev`, which is already indexed under
the main `code` collection. Phase 3 is optional for initial dispatch — agents
can search `/app` (the bind-mounted main repo) via the existing index. Only
needed for tasks where agents make edits another agent needs to find.

**Deliverable:** `run_factory.py` + `teardown.py` + `tools/definitions.py`
`search_codebase` schema update → PR.

---

## Phase 4 — Redispatch #36 with all improvements
*Execute after Phase 0 + 1 are merged. Use Phase 2 context injection manually
in the task_description for the first run, then wire Phase 2 once it's built.*

### Dispatch brief for #36

Uses `context_files` to inject:
- `agentception/workflow/status.py` (full — 140 lines)
- `agentception/db/persist.py` lines 1208–1232 (stop_agent_run as pattern)
- `agentception/poller.py` lines 243–264 (stall detection block)
- `agentception/alembic/versions/0007_agent_run_pipeline_fields.py` (migration pattern)

**Expected iteration count with all improvements:**
- Iteration 1: `search_codebase` × 2 (confirm AgentStatus location, confirm persist pattern) — batched in 1 turn
- Iterations 2–6: write `workflow/status.py` changes, write `persist.py` helper, write migration, write poller wiring — 1 file per turn
- Iterations 7–9: run mypy, run tests, fix any errors
- Iteration 10: commit + open PR
- **Target: ~12 iterations total** (vs. 50 today)

---

## Execution order

| Step | Phase | Owner | Status |
|------|-------|-------|--------|
| Update `search_codebase` tool description | 0A | MCP/IDE | ⬜ |
| Add batching + search-first rules to `worker-base.md.j2` | 0B/0C | MCP/IDE | ⬜ |
| Regenerate `.agentception/*.md` | 0 | MCP/IDE | ⬜ |
| PR: Phase 0 | 0 | MCP/IDE | ⬜ |
| `asyncio.gather` in `_dispatch_tool_calls` | 1A | MCP/IDE | ⬜ |
| PR: Phase 1 | 1 | MCP/IDE | ⬜ |
| `context_files` in `AdhocRunRequest` | 2A/2B/2C | MCP/IDE | ⬜ |
| PR: Phase 2 | 2 | MCP/IDE | ⬜ |
| Redispatch #36 | 4 | MCP/IDE | ⬜ |
| Worktree indexing on spawn | 3A/3B/3C | AgentCeption | ⬜ |
