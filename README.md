# AgentCeption

[![CI](https://github.com/cgcardona/agentception/actions/workflows/ci.yml/badge.svg)](https://github.com/cgcardona/agentception/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> *The infinite machine behind the machines.*

**AgentCeption** is a multi-agent orchestration system that turns any idea into a phase-gated plan, files the GitHub issues, and dispatches an org chart of AI agents to execute it — autonomously, in parallel, while you sleep.

```
Brain dump → Structured plan → GitHub issues → Agent org tree → PRs → Merged
```

One input. Zero boilerplate. The work happens.

---

## How It Works

1. **Plan** — Paste anything. Claude converts it into a `PlanSpec`: phases, issues, dependencies, acceptance criteria.
2. **Review** — The YAML opens in an editor. Adjust anything. Click **Create Issues** to file everything on GitHub.
3. **Ship** — The board shows your phases. Click **Launch** on an unlocked phase. A CTO agent surveys the board and cascades work down to coordinators and engineers, each working in an isolated git worktree. PRs appear. Phases unlock. You watch.

Every agent has a **cognitive architecture** — a composed identity (historical thinkers + archetypes + skill domains + behavioral atoms) injected into its context. You are deploying *reasoners*, not LLM calls.

---

## Quick Start

```bash
git clone https://github.com/cgcardona/agentception
cd agentception
cp .env.example .env        # fill in the required values below
docker compose up -d
docker compose exec agentception alembic upgrade head
open http://localhost:10003
```

### Required environment variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string (see `docker-compose.yml` for defaults) |
| `GITHUB_TOKEN` | GitHub PAT with `repo` + `issues` scope |
| `GH_REPO` | Repo this instance manages — `owner/repo` |
| `OPENROUTER_API_KEY` | OpenRouter API key for Phase 1A planning |
| `HOST_WORKTREES_DIR` | Host path where agent worktrees are created |

See [docs/guides/setup.md](docs/guides/setup.md) for the full first-run walkthrough.

---

## MCP Integration (Cursor / Claude)

AgentCeption exposes an MCP server so Cursor and Claude can invoke tools directly:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": ["compose", "-f", "/path/to/agentception/docker-compose.yml",
               "exec", "-T", "agentception", "python", "-m", "agentception.mcp.stdio_server"]
    }
  }
}
```

See [docs/guides/mcp.md](docs/guides/mcp.md) for the full tool reference.

---

## Documentation

| Guide | What it covers |
|-------|----------------|
| [Setup](docs/guides/setup.md) | First-run, Docker, environment variables |
| [MCP Integration](docs/guides/mcp.md) | Cursor / Claude tool integration |
| [Developer Workflow](docs/guides/developer-workflow.md) | Bind mounts, mypy, tests, build pipeline |
| [Contributing](docs/guides/contributing.md) | Branch conventions, PR process, commit style |

| Reference | What it covers |
|-----------|----------------|
| [API Routes](docs/reference/api.md) | Every HTTP endpoint — semantic URL taxonomy |
| [Agent Task Spec](docs/reference/agent-task.md) | `.agent-task` TOML format — all sections and fields |
| [Type Contracts](docs/reference/type-contracts.md) | Pydantic models, TypedDicts, layer contracts |
| [Cognitive Architecture](docs/reference/cognitive-arch.md) | Figures, archetypes, skill domains, atoms |
| [YAML Configuration](docs/reference/yaml-config.md) | `config.yaml`, `team.yaml`, `role-taxonomy.yaml` |

---

## Stack

Python 3.11 · FastAPI · Jinja2 · HTMX · Alpine.js · SCSS · Pydantic v2 · SQLAlchemy (async) · Alembic · PostgreSQL · Qdrant

Models: `anthropic/claude-sonnet-4.6` and `anthropic/claude-opus-4.6` via OpenRouter.

---

## License

MIT
