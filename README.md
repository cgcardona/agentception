# AgentCeption

[![CI](https://github.com/cgcardona/agentception/actions/workflows/ci.yml/badge.svg)](https://github.com/cgcardona/agentception/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> *The Singularity is here. The infinite machine behind the machines.*

For all of recorded history, human progress has been constrained by one thing: the number of hours in a day multiplied by the number of people willing to work. We called it scarcity, and we built entire economic systems around managing it.

Then something changed.

Autonomous AI agents can now reason, plan, write code, open pull requests, and report back. The singularity isn't near. **It's here.** When agents can carry the full cognitive load of an org — planning, dependency modeling, implementation, review — humans get to operate at the level of ideas. We are moving from a world of scarcity into a world of **superabundance**.

AgentCeption is a bet on that future.

```
Brain dump → Structured plan → GitHub issues → Agent org tree → PRs → Merged
```

One input. Zero boilerplate. The work happens.

---

## How It Works

1. **Plan** — Paste anything. The LLM converts it into a `PlanSpec`: phases, issues, dependencies, acceptance criteria.
2. **Review** — The YAML opens in an editor. Adjust anything. Click **Create Issues** to file everything on GitHub.
3. **Ship** — The board shows your phases. Click **Launch** on an unlocked phase. A CTO agent surveys the board and cascades work down to coordinators and engineers, each working in an isolated git worktree. PRs appear. Phases unlock. You watch.

Every agent has a **cognitive architecture** — a composed identity (historical thinkers + archetypes + skill domains + behavioral atoms) injected into its context. You are deploying *reasoners*, not LLM calls. This is the infrastructure for deploying **judgment at scale**.

Most AI coding tools are power tools. They make individual developers faster. AgentCeption is not a power tool. It is a **force multiplier on the organizational unit itself** — what would a brilliant 10-person team look like if the team had no size limit? The creative renaissance that has always been one good team away is now one brain dump away.

---

## Quick Start

### Option A — Cloud (Anthropic)

```bash
git clone https://github.com/cgcardona/agentception
cd agentception
cp .env.example .env
# Set ANTHROPIC_API_KEY, GITHUB_TOKEN, GH_REPO, HOST_WORKTREES_DIR
docker compose up -d
docker compose exec agentception alembic upgrade head
open http://localhost:10003
```

### Option B — Local models on macOS (free, private)

Run agents entirely on your own hardware with [Ollama](https://ollama.com). No API key, no cloud, no usage bill. Runs on Apple Silicon via Metal — GPU-accelerated.

```bash
# 1. Install Ollama and pull a model
brew install ollama
brew services start ollama
ollama pull qwen2.5-coder:7b      # fast, good quality (~4 GB)
# ollama pull qwen2.5-coder:32b   # better quality, needs 16 GB+ RAM

# 2. Clone and configure
git clone https://github.com/cgcardona/agentception
cd agentception
cp .env.example .env
```

Then set in `.env`:

```bash
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://host.docker.internal:11434
LOCAL_LLM_MODEL=qwen2.5-coder:7b
GITHUB_TOKEN=ghp_...
GH_REPO=owner/repo
HOST_WORKTREES_DIR=/path/to/worktrees
```

```bash
# 3. Start
docker compose up -d
docker compose exec agentception alembic upgrade head
open http://localhost:10003
```

> **Performance tip:** Set `WORKTREE_INDEX_ENABLED=false` in `.env` to skip per-agent code indexing (saves ~2 GB RSS and significant CPU) when running on constrained hardware.

See [docs/guides/local-llm-mlx.md](docs/guides/local-llm-mlx.md) for the full Ollama setup guide and model recommendations.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | ✅ | GitHub PAT with `repo` + `issues` scope |
| `GH_REPO` | ✅ | Repo this instance manages — `owner/repo` |
| `HOST_WORKTREES_DIR` | ✅ | Host path where agent worktrees are created |
| `DATABASE_URL` | ✅ | PostgreSQL connection string (default in `docker-compose.yml`) |
| `LLM_PROVIDER` | — | `anthropic` (default) or `local` |
| `ANTHROPIC_API_KEY` | Cloud only | Required when `LLM_PROVIDER=anthropic` |
| `LOCAL_LLM_BASE_URL` | Local only | Ollama base URL, e.g. `http://host.docker.internal:11434` |
| `LOCAL_LLM_MODEL` | Local only | Model tag, e.g. `qwen2.5-coder:7b` |
| `WORKTREE_INDEX_ENABLED` | — | `true`/`false` — enable per-agent code search (default `false`) |

See [docs/guides/setup.md](docs/guides/setup.md) for the full first-run walkthrough.

> **Security note:** By default all `/api/*` endpoints are unauthenticated. If your machine is on a shared network (office LAN, cloud VM, dev box), set `AC_API_KEY` in `.env` before starting. Without it, anyone who can reach port 10003 can dispatch agents and burn your Anthropic credits. Generate a key with `openssl rand -hex 32`.

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

See [docs/guides/integrate.md](docs/guides/integrate.md) for the full tool reference.

---

## Documentation

| Guide | What it covers |
|-------|----------------|
| [Setup](docs/guides/setup.md) | First-run, Docker, environment variables |
| [Local LLM / Ollama](docs/guides/local-llm-mlx.md) | Running agents on local hardware with Ollama |
| [Local LLM Scaling](docs/guides/local-llm-scaling.md) | Multi-agent concurrency and LiteLLM proxy |
| [MCP Integration](docs/guides/integrate.md) | Cursor / Claude tool integration |
| [Dispatching Agents](docs/guides/dispatch.md) | How to launch, monitor, and cancel agent runs |
| [Developer Workflow](docs/guides/developer-workflow.md) | Bind mounts, mypy, tests, build pipeline |
| [Contributing](docs/guides/contributing.md) | Branch conventions, PR process, commit style |

| Reference | What it covers |
|-----------|----------------|
| [API Routes](docs/reference/api.md) | Every HTTP endpoint — semantic URL taxonomy |
| [Cognitive Architecture](docs/reference/cognitive-arch.md) | Figures, archetypes, skill domains, atoms |
| [Type Contracts](docs/reference/type-contracts.md) | Pydantic models, TypedDicts, layer contracts |

---

## Stack

Python 3.12 · FastAPI · Jinja2 · HTMX · Alpine.js · SCSS · Pydantic v2 · SQLAlchemy (async) · Alembic · PostgreSQL · Qdrant

**LLM backends:** Anthropic (`claude-sonnet-4-6`, `claude-opus-4-6`) or any [Ollama](https://ollama.com)-compatible local model. Switch with a single env var — no code changes required.

---

## License

MIT
