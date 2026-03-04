# AgentCeption

> **The infinite agent pipeline dashboard.** Monitor, control, and scale multi-tier AI agent workflows — in real time.

AgentCeption is a self-contained FastAPI + HTMX dashboard that turns any GitHub-based AI agent pipeline into a live, inspectable system. It polls your repository's worktrees, open issues, and pull requests to give you a single-pane view of every agent wave — from spawn to merge.

---

## What AgentCeption Is

```
┌────────────────────────────────────────────────────────────┐
│  AgentCeption                                              │
│                                                            │
│  ┌──────────────────┐   ┌──────────────────────────────┐  │
│  │  Web UI          │   │  MCP Server                  │  │
│  │  (HTMX + SSE)    │   │  agentception/mcp/server.py  │  │
│  │                  │   │                              │  │
│  │  Plan / Build /  │   │  Tools Cursor calls:         │  │
│  │  Ship pages      │   │  - plan_get_schema()         │  │
│  └────────┬─────────┘   │  - plan_validate_spec()      │  │
│           │             │  - plan_get_labels()         │  │
│  ┌────────▼─────────┐   │  - plan_validate_manifest()  │  │
│  │  FastAPI         │   │  - plan_spawn_coordinator()  │  │
│  │  + GitHub Poller │   └──────────────────────────────┘  │
│  └──────────────────┘                                      │
│                                                            │
│  GitHub is the single source of truth.                     │
│  Cursor is the only inference engine.                      │
│  AgentCeption has zero direct LLM calls.                   │
└────────────────────────────────────────────────────────────┘
```

AgentCeption has exactly two external interfaces:

| Interface | Protocol | Consumers |
|-----------|----------|-----------|
| Web dashboard | HTTP / SSE | Human operators (Plan/Build/Ship pages) |
| MCP server | JSON-RPC 2.0 | Cursor — the only inference engine |

**What AgentCeption is NOT:**
- Not an LLM client — it never calls OpenRouter, Anthropic, or any model API directly.
- Not a Cursor fork — it is a standalone Python service that Cursor talks to.
- Not tightly coupled to Maestro — `agentception/` has zero imports from `maestro/`, `muse/`, `kly/`, or `storpheus/`.

---

## The Two Communication Channels

### Channel 1 — Cursor → AgentCeption (MCP tool calls)

Standard MCP JSON-RPC 2.0. Cursor is the client; AgentCeption is the server. Cursor calls a tool, AgentCeption responds with structured data. This is how Cursor gets the `PlanSpec` JSON schema, validates a spec, fetches GitHub labels, or triggers a coordinator spawn.

```
User: "Plan this idea: ..."
  → Cursor LLM decides to call plan_get_schema()
  → AgentCeption returns PlanSpec JSON schema
  → Cursor LLM produces YAML using schema + codebase context
  → Cursor calls plan_validate_spec(yaml_text)
  → AgentCeption returns validation result (or errors)
  → Cursor calls plan_spawn_coordinator(manifest_json)
  → AgentCeption creates worktree + .agent-task → returns path
```

### Channel 2 — AgentCeption → Cursor (`.agent-task` file)

When AgentCeption needs Cursor to perform LLM work on behalf of the web UI, it writes a `.agent-task` file into a git worktree. Cursor's background agent poll loop detects the file, runs its LLM with the task as context, and writes the result back (to a file, GitHub PR, etc.). AgentCeption's poller detects the output and SSE-pushes it to the browser.

**The `.agent-task` file is the AgentCeption-to-Cursor IPC channel.** It is filesystem-based, not HTTP.

```
User fills "Plan" textarea in web UI → clicks "Generate"
  → AgentCeption writes .agent-task (WORKFLOW=plan-spec, DUMP=..., OUTPUT_PATH=...)
  → Cursor agent poll loop detects the file
  → Cursor LLM generates YAML spec, writes to OUTPUT_PATH
  → AgentCeption poller detects OUTPUT_PATH, reads YAML
  → SSE pushes YAML to browser → user reviews and edits
  → User clicks "Launch"
  → AgentCeption calls plan_spawn_coordinator(manifest_json) internally
  → New Cursor agent picks up the coordinator .agent-task
```

This means the brain dump can live entirely in the AgentCeption web UI — the user never has to open Cursor. Cursor does the inference in the background.

---

## Architecture

```mermaid
graph TD
    subgraph "Human operator"
        BROWSER[Browser\nPlan / Build / Ship]
    end

    subgraph "Cursor (inference engine)"
        CURSOR_LLM[Cursor LLM]
        CURSOR_MCP[MCP Client]
        CURSOR_POLL[Agent Poll Loop]
    end

    subgraph "AgentCeption Service"
        APP[FastAPI app\nagentception.app:app]
        POLLER[Background Poller\npoller.py]
        SSE[SSE Stream /events]
        ROUTES[API Routes\nroutes/]
        MCP_SRV[MCP Server\nmcp/server.py]
        INTEL[Intelligence\nintelligence/]
        READERS[Readers\nreaders/]
    end

    subgraph "External"
        GH[GitHub API]
        WT[Git Worktrees\n+ .agent-task files]
        CFG[pipeline-config.json]
    end

    BROWSER -->|HTTP/SSE| APP
    APP --> POLLER
    APP --> SSE
    APP --> ROUTES
    APP --> MCP_SRV
    POLLER --> READERS
    READERS --> GH
    READERS --> WT
    READERS --> CFG
    INTEL --> GH
    SSE --> BROWSER

    CURSOR_MCP -->|JSON-RPC 2.0| MCP_SRV
    CURSOR_LLM --> CURSOR_MCP
    CURSOR_POLL -->|reads| WT
    CURSOR_POLL -->|writes result| WT
    POLLER -->|detects result| WT
    ROUTES -->|writes| WT
```

**Key design choices:**

- **Event-driven UI** — the browser subscribes to `/events` (SSE) and receives a full `PipelineState` snapshot every polling tick (default 5 s). No polling from the browser.
- **Reader layer** — `readers/` modules are pure async functions that fetch data from GitHub, the filesystem, and config files. They never mutate state.
- **Intelligence layer** — `intelligence/` modules analyse the pipeline state to surface A/B test results, DAG ordering, scaling recommendations, and guard-rail violations.
- **Config-first multi-repo** — switch the monitored repository by editing `pipeline-config.json`; no code changes needed.
- **MCP-native planning** — the Plan step never calls an LLM directly. It provides context (schema, labels, validation) to Cursor via MCP tools. Cursor's LLM does the reasoning.

---

## Quick Start

```bash
# 1. Install
pip install -e agentception/   # from repo root, or:
pip install agentception        # once published to PyPI

# 2. Configure (optional — defaults work for local dev)
export REPO_DIR=/path/to/your/repo
export GH_REPO=owner/repo-name

# 3. Launch
agentception
# → http://localhost:7777
```

That's it. The dashboard auto-refreshes every 5 seconds.

---

## Configuration Reference

AgentCeption is configured via environment variables or via the `pipeline-config.json` file in your repository's `.cursor/` directory.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `REPO_DIR` | Current working directory | Absolute path to the repository root |
| `WORKTREES_DIR` | `~/.agentception/worktrees/maestro` | Directory containing agent git worktrees |
| `CURSOR_PROJECTS_DIR` | `~/.cursor/projects` | Cursor projects directory (for transcript reading) |
| `GH_REPO` | `cgcardona/maestro` | GitHub repository slug (`owner/repo`) |
| `POLL_INTERVAL_SECONDS` | `5` | How often the poller refreshes state |
| `GITHUB_CACHE_SECONDS` | `10` | TTL for GitHub API response cache |
| `HOST` | `0.0.0.0` | Bind host for the uvicorn server |
| `PORT` | `7777` | Bind port for the uvicorn server |

### `pipeline-config.json` Schema

Place this file at `<your-repo>/.cursor/pipeline-config.json`:

```jsonc
{
  // Maximum Engineering VP agents active at once
  "max_eng_vps": 1,

  // Maximum QA VP agents active at once
  "max_qa_vps": 1,

  // Developer agents per VP
  "pool_size_per_vp": 4,

  // Ordered list of phase labels (controls wave sequencing)
  "active_labels_order": [
    "phase/0-scaffold",
    "phase/1-core",
    "phase/2-polish"
  ],

  // A/B testing configuration for role variants
  "ab_mode": {
    "enabled": false,
    "target_role": "python-developer",
    "variant_a_file": ".cursor/roles/python-developer.md",
    "variant_b_file": ".cursor/roles/python-developer-v2.md"
  },

  // Multi-repo support: list projects and set the active one
  "active_project": "my-project",
  "projects": [
    {
      "name": "my-project",
      "gh_repo": "owner/my-project",
      "repo_dir": "/path/to/my-project",
      "worktrees_dir": "~/.agentception/worktrees/my-project"
    }
  ]
}
```

When `active_project` is set, its `gh_repo`, `repo_dir`, and `worktrees_dir` override the environment-variable defaults. This is the primary mechanism for pointing AgentCeption at a new repository without touching env vars.

---

## Phase Overview

AgentCeption was built in six phases, each shipped as a labelled GitHub issue wave:

| Phase | Label | Features |
|---|---|---|
| 0 | `agentception/0-scaffold` | FastAPI app skeleton, SSE stream, poller loop, static dashboard shell |
| 1 | `agentception/1-controls` | Start/stop/pause wave controls, spawn next-wave button, agent kill switch |
| 2 | `agentception/2-telemetry` | Per-agent telemetry cards, transcript viewer, worktree diff viewer |
| 3 | `agentception/3-roles` | Role file management, role version history, A/B role assignment UI |
| 4 | `agentception/4-intelligence` | DAG ordering, guard-rail analysis, scaling advisor, A/B result comparison |
| 5 | `agentception/5-scaling` | VP auto-spawn, pool-size dynamic adjustment, wave concurrency controls |
| 6 | `agentception/6-generalization` | Multi-repo support, `pyproject.toml`, README, extraction procedure |

---

## Using AgentCeption with a New Repository

AgentCeption ships with a **pipeline template** that you can import into any GitHub repository using the template export/import feature (AC-602).

### Import a Template

```bash
# From within AgentCeption, navigate to:
# Settings → Templates → Import

# Or via the API:
curl -X POST http://localhost:7777/api/templates/import \
  -H "Content-Type: application/json" \
  -d '{
    "template_name": "standard-6-phase",
    "target_gh_repo": "owner/new-repo",
    "target_repo_dir": "/path/to/new-repo"
  }'
```

### Minimal Manual Setup

1. Copy `.cursor/pipeline-config.json` from this repo to your target repo, updating `gh_repo`, `repo_dir`, and `worktrees_dir`.
2. Create GitHub issue labels matching your `active_labels_order` entries.
3. Create role files at `.cursor/roles/<role-name>.md`.
4. Launch AgentCeption with `REPO_DIR=/path/to/new-repo agentception`.

---

## Contributing

### Development Setup

```bash
git clone https://github.com/cgcardona/maestro
cd maestro/agentception

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest agentception/tests/ -v

# Type check
mypy agentception/
```

### Code Standards

- Every Python file starts with `from __future__ import annotations`.
- Type hints everywhere — no `# type: ignore` without a comment explaining why.
- `logging.getLogger(__name__)` — never `print()`.
- Async for all I/O operations.
- Black formatting.

### Adding a New Reader

1. Create `agentception/readers/my_reader.py` with an async function returning a typed Pydantic model.
2. Import and call it from `agentception/poller.py` in the `_fetch_state()` function.
3. Add the new field to `agentception/models.py` `PipelineState`.
4. Add tests in `agentception/tests/test_agentception_my_reader.py`.

### Running the Full Test Suite

```bash
pytest agentception/tests/ -v
```

### Submitting a PR

1. Branch from `dev`: `git checkout -b feat/my-feature`
2. Run mypy first: `mypy agentception/`
3. Run tests: `pytest agentception/tests/ -v`
4. Push and open a PR targeting `dev`.

---

## License

MIT — see [LICENSE](../LICENSE).
