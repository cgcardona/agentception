# AgentCeption — Architecture

AgentCeption is a multi-agent orchestration system that translates high-level goals into phase-gated GitHub issue graphs and dispatches autonomous agents to execute each phase.

## Package Layout

```
agentception/
  app.py           → FastAPI application factory and lifespan manager
  config.py        → Pydantic Settings (all env vars prefixed AC_)
  models.py        → PlanSpec, PlanIssue, PlanPhase, and domain models
  poller.py        → Background polling loop (pipeline state refresh)
  telemetry.py     → Structured logging setup

  routes/
    api/           → JSON/SSE API endpoints
    ui/            → Jinja2/HTMX/Alpine.js page handlers

  readers/         → LLM planner, GitHub client, worktree manager, transcript reader
  services/        → LLM calls (OpenRouter), external integrations
  mcp/             → MCP server for Cursor/Claude tool integration
  db/              → SQLAlchemy async models, Alembic migrations, engine
  intelligence/    → Cognitive architecture engine
  static/          → Compiled JS/CSS bundles (build with npm run build)
  templates/       → Jinja2 HTML templates
  docs/            → Internal design documents (plan-spec, agent-tree-protocol, etc.)
  tests/           → Unit, integration, regression, and E2E tests
```

## Service Dependencies

| Service    | Purpose                                 | Port  |
|------------|-----------------------------------------|-------|
| agentception | FastAPI application                   | 10003 |
| postgres   | Persistent store (runs, phases, issues) | 5433  |
| qdrant     | Vector store (semantic search)          | 6335  |

## Data Flow

```
Browser / Cursor MCP
      ↓
FastAPI routes (thin HTTP handlers)
      ↓
readers/ (LLM planner, GitHub, worktree)
      ↓
services/ (OpenRouter LLM calls)
      ↓
GitHub API → Issues, PRs, Worktrees
      ↓
Agents (dispatched via Cursor CLI)
      ↓
PRs → merged → next phase unlocks
```

## Agent Hierarchy

- **CTO-tier agent** — surveys unlocked phases, decides dispatch order
- **VP-tier agent** — receives label scope, breaks into tickets, spawns engineers
- **Engineer-tier agents** — each owns one issue, implements in isolated git worktree, opens PR

Each agent receives a **cognitive architecture** composed from figures (historical thinkers), archetypes (thinking styles), skill domains, and behavioral atoms via `scripts/gen_prompts/resolve_arch.py`.

## Further Reading

- [Plan Spec format](../agentception/docs/plan-spec.md)
- [Agent tree protocol](../agentception/docs/agent-tree-protocol.md)
- [Cursor agent spawning](../agentception/docs/cursor-agent-spawning.md)
- [Maestro architecture](https://github.com/cgcardona/maestro/blob/main/docs/reference/architecture.md) — upstream repo where AgentCeption was co-located before extraction
