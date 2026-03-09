# Cursor-Free Agent Loop

AgentCeption can run agents entirely on its own infrastructure — no Cursor IDE, no local MCP client, no `@Codebase` integration required. This guide explains how the loop works, how to configure it, and how to trigger an agent run.

---

## What It Replaces

| What Cursor provided | What AgentCeption now provides |
|---------------------|-------------------------------|
| Local MCP client (tool dispatch) | `agent_loop.py` dispatches tools internally |
| `@Codebase` semantic search | Qdrant + FastEmbed (`code_indexer.py`) |
| LLM API connectivity | Anthropic (HTTPS via `llm.py`) |
| Cognitive architecture injection | Role files + `resolve_arch.py` |

The result: a full agent execution loop that runs inside the Docker container, calls Anthropic's Claude via the Anthropic API, uses your local codebase as context, and executes file and shell operations in isolated git worktrees.

---

## Architecture

```
POST /api/runs/{run_id}/execute
        ↓
  agent_loop.py
        ↓ load
  DB context (worktree root)
  .agentception/roles/{role}.md
  resolve_arch.py → cognitive architecture markdown
        ↓ build tool catalogue
  FILE_TOOL_DEFS  (read_file, write_file, list_directory, search_text)
  SHELL_TOOL_DEF  (run_command)
  SEARCH_CODEBASE_TOOL_DEF (semantic vector search)
  MCP tools       (GitHub, pipeline state — forwarded via call_tool_async)
        ↓ conversation loop
  call_anthropic_with_tools()  →  Anthropic API  →  Anthropic Claude
        ↓ tool dispatch
  Local tools   → agentception/tools/
  MCP tools     → agentception/mcp/server.py → GitHub / DB
        ↓ on stop_reason == "end_turn"
  build_complete_run() or build_cancel_run()
```

---

## Components

### `agentception/services/agent_loop.py`

The main coroutine `run_agent_loop(run_id, max_iterations=50)` orchestrates the entire agent lifecycle:

1. **Load task** — reads the `ac://runs/{run_id}/context` TOML from the worktree root
2. **Load role** — reads the role markdown from `.agentception/roles/{role}.md`
3. **Build system prompt** — combines role content, cognitive architecture, and a runtime environment note
4. **Build tool catalogue** — merges local tools, semantic search, and MCP tools
5. **Conversation loop** — calls Anthropic with full message history and tool definitions; dispatches tool calls; appends results; repeats until `end_turn` or max iterations
6. **Completion** — calls `build_complete_run` or `build_cancel_run`

### `agentception/services/llm.py` — `call_anthropic_with_tools()`

Multi-turn conversation API over Anthropic API. Sends a `messages` list and `tools` list; returns a `ToolResponse` with `stop_reason`, `content`, and any `tool_calls`. Uses `temperature=0.0` for determinism and `max_tokens=8192`.

### `agentception/tools/`

Local tool implementations executed inside the container:

| Module | Tool name | What it does |
|--------|-----------|-------------|
| `file_tools.py` | `read_file` | Read a file (truncates at 128 KiB) |
| `file_tools.py` | `write_file` | Write a file, creating parent directories |
| `file_tools.py` | `list_directory` | List directory entries |
| `file_tools.py` | `search_text` | Regex search via `rg` (ripgrep) |
| `shell_tools.py` | `run_command` | Run a shell command with denylist enforcement |
| `definitions.py` | — | OpenAI-format JSON schemas for all tools |

### `agentception/services/code_indexer.py` — Semantic Code Search

Replaces Cursor's `@Codebase` with a self-hosted Qdrant + FastEmbed pipeline:

- **`index_codebase(repo_path)`** — walks source files, splits into ~1,500-character overlapping chunks, embeds with `BAAI/bge-small-en-v1.5` (ONNX, runs on CPU, no API key), upserts 384-dim vectors to Qdrant
- **`search_codebase(query, n_results)`** — embeds the query vector, runs cosine-similarity search in Qdrant, returns `SearchMatch` list with file path, line numbers, chunk text, and score

The `search_codebase` tool is available to every agent as `SEARCH_CODEBASE_TOOL_DEF`. Agents use natural language queries:

> "Where is authentication handled?"  
> "Find the GitHub API client"  
> "Show me error handling for LLM calls"

---

## Configuration

All settings live in `agentception/config.py` and are set via environment variables in `docker-compose.override.yml`.

| Env var | Default | Description |
|---------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(required)* | Key for Anthropic Claude |
| `QDRANT_URL` | `http://agentception-qdrant:6333` | Qdrant REST endpoint (internal Docker URL) |
| `QDRANT_COLLECTION` | `code` | Qdrant collection name for code vectors |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | FastEmbed model (384-dim, ONNX) |
| `EMBED_MODEL_DIM` | `384` | Vector dimension — must match the model |
| `AC_API_KEY` | *(empty)* | API key for `/api/*` routes; see [Security Guide](security.md) |

---

## Indexing the Codebase

Before agents can use semantic search, the codebase must be indexed. Indexing runs as a background task and does not block the response.

### Trigger indexing

```bash
curl -X POST http://localhost:10003/api/system/index-codebase
# → 202 Accepted {"ok": true, "message": "Codebase indexing started in the background."}
```

If `AC_API_KEY` is set:

```bash
curl -X POST http://localhost:10003/api/system/index-codebase \
  -H "Authorization: Bearer your-key"
```

Indexing progress appears in the container logs:

```
✅ code_indexer — start indexing /app → http://agentception-qdrant:6333/code
✅ code_indexer — found 312 indexable files
✅ code_indexer — 704 chunks from 312 files
✅ code_indexer — done: 312 files, 704 chunks
```

The first run downloads the FastEmbed model (`BAAI/bge-small-en-v1.5`, ~130 MB) from HuggingFace. Subsequent runs use the cached model and complete in seconds.

### What gets indexed

Files matching these extensions (under 200 KB):

`.py` `.md` `.j2` `.toml` `.yml` `.yaml` `.js` `.ts` `.scss` `.css` `.html` `.json` `.txt` `.sh`

Directories skipped entirely: `.git` `__pycache__` `node_modules` `.venv` `venv` `.mypy_cache` `.pytest_cache`

### Search the index directly

```bash
curl "http://localhost:10003/api/system/search?q=anthropic+api+key&n=5"
```

Returns:

```json
{
  "ok": true,
  "query": "anthropic api key",
  "n_results": 3,
  "matches": [
    {
      "file": "agentception/config.py",
      "score": 0.733,
      "start_line": 101,
      "end_line": 110,
      "chunk": "    anthropic_api_key: str = \"\"\n    ..."
    }
  ]
}
```

---

## Triggering an Agent Run

The `POST /api/runs/{run_id}/execute` endpoint dispatches an agent run using the Cursor-free loop.

### Prerequisites

1. A run row must exist in the DB with `status = "pending_launch"` or `"implementing"`
2. The run's worktree must exist on disk with a valid `DB context row
3. The codebase should be indexed (optional but recommended — agents without the index fall back to `rg`-based `search_text`)

### HTTP request

```bash
curl -X POST http://localhost:10003/api/runs/{run_id}/execute \
  -H "Authorization: Bearer your-key"
```

**Returns `202 Accepted`** immediately. The agent loop runs as a background task.

### Response

```json
{"ok": true, "message": "Agent loop dispatched for run abc-123."}
```

### Status codes

| Code | Meaning |
|------|---------|
| `202` | Loop dispatched successfully |
| `404` | Run not found |
| `409` | Run is not in a dispatchable state (already running, complete, or failed) |

---

## The `ac://runs/{run_id}/context` File

Every agent run reads its configuration from a TOML file at the worktree root:

```toml
[agent]
role = "python-developer"

[target]
issue_number = 42
label = "0-foundation"
cognitive_arch = "feynman:python"

[repo]
repo = "myorg/myrepo"
branch = "ac/issue-42-abc1"
```

| Field | Description |
|-------|-------------|
| `agent.role` | Role slug; maps to `.agentception/roles/{role}.md` |
| `target.issue_number` | GitHub issue the agent is working on |
| `target.label` | Phase label (e.g. `"0-foundation"`) |
| `target.cognitive_arch` | Cognitive architecture string (e.g. `"feynman:python"`) |
| `repo.repo` | `owner/name` for GitHub API calls |
| `repo.branch` | Worktree branch name |

---

## Monitoring Progress

Agent steps are recorded in the DB as `ACRunEvent` rows. The Build dashboard shows them in real time. You can also fetch them via MCP:

```python
# In Cursor
FetchMcpResource(server="user-agentception", uri="ac://runs/{run_id}/events")
```

Or directly:

```bash
curl http://localhost:10003/api/runs/{run_id}/step
```

---

## End-to-End Smoke Test

The smoke test script at `scripts/smoke_test_agent_loop.py` validates all four stages — service health, Qdrant connectivity, indexing, and semantic search — without requiring a real Anthropic API key or GitHub issue:

```bash
python3 scripts/smoke_test_agent_loop.py
```

Expected output:

```
══ AgentCeption Smoke Test — Cursor-Free Agent Loop ══
  AgentCeption: http://127.0.0.1:10003
  Qdrant:       http://127.0.0.1:6335

─── Step 1: AgentCeption health check ───
  OK — AgentCeption at http://127.0.0.1:10003 is healthy
─── Step 2: Qdrant connectivity ───
  OK — Qdrant at http://127.0.0.1:6335 is reachable
─── Step 3: Trigger codebase indexing ───
  OK — 202 Accepted
─── Step 4: Semantic search verification ───
  OK — 'anthropic api key configuration' → 3 results
       top hit: agentception/config.py (score=0.733)
  ✅ ALL STEPS PASSED
```
