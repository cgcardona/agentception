# AgentCeption Documentation

Everything you need to understand, operate, and extend AgentCeption.

---

## Guides

Step-by-step instructions for humans.

| Guide | Summary |
|-------|---------|
| [Setup](guides/setup.md) | First-run walkthrough — Docker, environment variables, database migrations, Qdrant indexing |
| [MCP Integration](guides/mcp.md) | Connect Cursor / Claude to AgentCeption via the MCP server (stdio and HTTP transports) |
| [Cursor-Free Agent Loop](guides/agent-loop.md) | Run agents without Cursor — direct Anthropic API, Qdrant semantic search, local tool execution |
| [Security](guides/security.md) | API key auth, TLS configuration, shell denylist, secrets management, threat model |
| [Developer Workflow](guides/developer-workflow.md) | Bind-mount loop, mypy → tests → docs verification order, JS/CSS build pipeline |
| [Contributing](guides/contributing.md) | Branch naming, commit conventions, PR checklist, code review expectations |
| [CI](guides/ci.md) | GitHub Actions pipeline — what runs, how to reproduce locally |
| [Local LLM with MLX](guides/local-llm-mlx.md) | Run the local LLM provider (Qwen/MLX on Apple Silicon): config, env vars, server, probes |

---

## Reference

Precise specifications for every system component.

| Reference | Summary |
|-----------|---------|
| [API Routes](reference/api.md) | Complete HTTP endpoint inventory — semantic URL taxonomy, request/response shapes |
| [Task Context Spec](../.agentception/agent-task-spec.md) | `RunContextRow` DB schema — every field, access patterns, and examples |
| [Type Contracts](reference/type-contracts.md) | Pydantic models, TypedDicts, and the typed layer contracts between DB → service → route |
| [LLM Contract and Provider Abstraction](reference/llm-contract.md) | Provider-agnostic LLM API, provider selection, and how to add a new backend |
| [Cognitive Architecture](reference/cognitive-arch.md) | Figures, archetypes, skill domains, atoms — how agents get their identities |
| [YAML Configuration](reference/yaml-config.md) | `config.yaml`, `team.yaml`, `role-taxonomy.yaml` — full field reference |

---

## System Overview

### The pipeline in one diagram

```
User input (brain dump)
        │
        ▼
┌─────────────────────────────────────┐
│  Phase 1A — LLM Planning            │
│  POST /api/plan/launch              │
│  llm_phase_planner.py               │
│  → PlanSpec YAML (phases + issues)  │
└─────────────────────────────────────┘
        │
        ▼  (human reviews in editor)
┌─────────────────────────────────────┐
│  Phase 1B — Issue Filing            │
│  POST /api/plan/file-issues         │
│  issue_creator.py                   │
│  → GitHub issues + phase labels     │
│  → initiative_phases rows in DB     │
└─────────────────────────────────────┘
        │
        ▼  (human clicks Launch on Ship board)
┌─────────────────────────────────────┐
│  Dispatch                           │
│  POST /api/dispatch/label           │
│  worktrees.py                       │
│  → git worktree + DB context row     │
│  → ACAgentRun row in DB             │
└─────────────────────────────────────┘
        │
        ▼  (POST /api/runs/{run_id}/execute — Cursor-free)
┌─────────────────────────────────────┐
│  Cursor-Free Agent Loop             │
│  agent_loop.py                      │
│  → Reads DB context + role + arch   │
│  → Calls LLM API (Anthropic or local via config) │
│  → Dispatches file/shell/MCP tools  │
│  → Uses Qdrant for code search      │
│  Reports via POST /api/runs/{id}/*  │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  Phase gate check                   │
│  POST /api/ship/{initiative}/advance│
│  plan_advance_phase MCP tool        │
│  → Next phase unlocks in DB         │
└─────────────────────────────────────┘
```

### Directory structure

```
agentception/
  config.py          → Pydantic Settings (all AC_* env vars)
  models/            → Domain models: PlanSpec, PlanPhase, PlanIssue, TaskFile
  db/
    models.py        → SQLAlchemy ORM (ACIssue, ACAgentRun, ACInitiativePhase, …)
    persist.py       → Write functions (one per resource type)
    queries.py       → Read functions, returning typed TypedDicts
  middleware/
    auth.py          → ApiKeyMiddleware — API key enforcement on /api/* routes
  readers/
    llm_phase_planner.py  → Phase 1A: LLM → PlanSpec YAML
    issue_creator.py      → Phase 1B: PlanSpec → GitHub issues + DB rows
    worktrees.py          → Agent dispatch: git worktree + DB context row
  services/
    llm.py           → completion(), completion_stream(), completion_with_tools(); provider selection (Anthropic or local)
    agent_loop.py    → Cursor-free agent execution loop (multi-turn + tool dispatch)
    code_indexer.py  → Qdrant codebase indexing + semantic search (FastEmbed)
  tools/
    file_tools.py    → read_file, write_file, list_directory, search_text (rg)
    shell_tools.py   → run_command (shell command execution with denylist)
    definitions.py   → OpenAI-format JSON schemas for all local tools
  routes/
    ui/              → Browser-facing pages (Jinja2/HTMX)
      build_ui.py    → /plan, /ship/{initiative}, /ship/{initiative}/board
      cognitive_arch.py → /cognitive-arch, /cognitive-arch/{id}
    api/
      dispatch.py    → /api/dispatch/* (issue, label, context, prompt)
      runs.py        → /api/runs/* (pending, acknowledge, children, step, …)
      agent_run.py   → /api/runs/{run_id}/execute (Cursor-free dispatch)
      system.py      → /api/system/index-codebase, /api/system/search
      ship_api.py    → /api/ship/{initiative}/advance
  mcp/
    server.py        → MCP tool definitions (plan_*, build_*, …)
    stdio_server.py  → stdio transport for Cursor integration
    http_server.py   → HTTP Streamable MCP transport (/api/mcp)
  static/
    app.js           → Compiled JS bundle (never edit directly)
    app.css          → Compiled CSS bundle (never edit directly)
    js/              → JS source files (build with npm run build:js)
    scss/            → SCSS source files (build with npm run build:css)
  templates/         → Jinja2 HTML templates
  alembic/           → Database migration scripts

scripts/
  gen_prompts/       → Cognitive architecture engine
    generate.py      → Renders all .j2 templates into final prompt files
    resolve_arch.py  → Composes figure + archetype + skills into agent prompts
    config.yaml      → Pipeline configuration (phases, codebase routing)
    team.yaml        → Agent team definitions
    role-taxonomy.yaml → Full org chart (C-Suite → Coordinator → Worker)
    cognitive_archetypes/
      figures/       → Historical thinkers (YAML)
      archetypes/    → Abstract thinking styles (YAML)
      skill_domains/ → Technical expertise areas (YAML)
      atoms/         → Behavioral primitives (YAML)
    templates/       → Jinja2 prompt templates (.j2)
      snippets/      → Shared fragments (included, not rendered directly)

.agentception/
  agent-task-spec.md → Formal DB context specification
  roles/             → Generated agent role markdown files
  prompts/           → Generated prompt templates
  pipeline-config.json → Runtime pipeline configuration
```

### Services

| Service | Container | Port |
|---------|-----------|------|
| AgentCeption | `agentception` | 10003 |
| Postgres | `agentception-postgres` | 5433 |
| Qdrant | `agentception-qdrant` | 6335 / 6336 |

---

## Key Principles

**Layers never collapse.** Routes are thin (no business logic). Business logic lives in `readers/`. Data shapes live in `db/queries.py` TypedDicts and `models/` Pydantic models.

**Zero `Any`.** The codebase runs `mypy --strict` with a hard ceiling of 0 `Any` patterns. Every type is named. If you know the keys, use a `TypedDict`. If you know the shape, use a `BaseModel`.

**Docker-first.** Never run Python on the host. All commands run inside the container via `docker compose exec agentception <cmd>`. Dev bind mounts mean host edits are instantly visible inside — no rebuild needed for code changes.

**MCP-first.** Agent-to-app communication uses MCP tools. HTTP endpoints exist as the semantic backing for MCP and browser-driven interactions — they are not a secondary transport layer.

**Verification order: mypy → tests → docs.** Always run mypy first. Fix type errors before running tests. Update docs in the same commit as code changes.
