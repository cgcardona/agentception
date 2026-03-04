# AgentCeption

[![CI](https://github.com/cgcardona/agentception/actions/workflows/ci.yml/badge.svg)](https://github.com/cgcardona/agentception/actions/workflows/ci.yml)

> *The Singularity is here.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The Idea

For all of recorded history, human progress has been constrained by one thing: the number of hours in a day multiplied by the number of people willing to work. Every civilization, every company, every creative act has bumped against this ceiling. We called it scarcity, and we built entire economic systems around managing it.

Then something changed.

The pace of technological change has been accelerating for decades — we've been saying "the singularity is near" for so long it started to sound like a punchline. Then it became "the singularity is nearer," because anyone paying attention could see the curve bending. But with the arrival of autonomous AI agents that can reason, plan, write code, open pull requests, and report back — the singularity isn't near. **It's here.**

What changes when you can take any piece of drudgery — any repetitive, mechanical, soul-draining work — and hand it to an org chart of agents who will execute it while you sleep? Everything changes. The nature of work changes. The relationship between human creativity and human labor changes. The ceiling lifts.

We are moving from a world of scarcity into a world of **superabundance**. Not just material abundance — abundance of time, of creative capacity, of the ability to pursue what is actually meaningful. Every hour an agent spends filing tickets, writing boilerplate, and opening PRs is an hour a human gets back to think, to create, to be human.

AgentCeption is a bet on that future.

---

## What It Does

AgentCeption is a multi-agent orchestration system built on a simple insight: **any body of work can be expressed as an org chart, and any org chart can be staffed by agents.**

You describe what needs to happen. AgentCeption — powered by Claude — translates that into a phase-gated plan: a dependency graph of GitHub issues, organized into waves, sequenced by what must happen before what. Then it dispatches the right agents at the right level of the org tree to execute each wave, autonomously, in parallel, reporting back as they go.

```
Your idea
    ↓
Phase 1A — Claude converts your brain dump into a structured PlanSpec YAML
    ↓
Phase 1B — You review and approve the plan in the editor
    ↓
GitHub issues created, labeled, and phase-gated automatically
    ↓
AgentCeption dispatches agents by org tier and label scope:
  CTO → VP → Engineer
    ↓
Agents open PRs → PRs reviewed → merged → next phase unlocks
    ↓
Done. While you were thinking about the next big idea.
```

The org tree is not a metaphor. It's a literal hierarchy:

- A **CTO-tier agent** surveys the board, identifies which phase is unlocked, and decides what to spin up.
- A **VP-tier agent** receives a label scope, breaks it into individual tickets, and spawns engineers.
- **Engineer-tier agents** each own a single issue, implement it in an isolated git worktree, and open a PR.

Every agent in this chain has a **cognitive architecture** injected into its context — a composition of figures (historical thinkers and builders), archetypes (thinking styles), skill domains (technical expertise), and atoms (behavioral primitives). You are not deploying generic LLM calls. You are deploying *reasoners*, each shaped for their role.

---

## Why It Matters

Most AI coding tools are power tools. They make individual developers faster. That's good. But they leave the fundamental structure of work untouched — one human, one problem, one context window.

AgentCeption is not a power tool. It's a **force multiplier on the organizational unit itself.**

The question it asks is: what would a brilliant 10-person team look like if the team had no size limit? What would you build if you could staff any initiative with the exactly right agents in the exactly right roles, instantly, at no marginal cost?

That question used to be science fiction. It isn't anymore.

We believe this changes what's possible for individual creators, small teams, and anyone who has ever had an idea bigger than their bandwidth. The creative renaissance that has always been one good team away is now one brain dump away.

---

## Quick Start

```bash
git clone https://github.com/cgcardona/agentception
cd agentception
cp .env.example .env        # fill in required values (see below)
docker compose up -d
docker compose exec agentception alembic upgrade head
open http://localhost:10003
```

### Required environment variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string (see docker-compose.yml for local defaults) |
| `GITHUB_TOKEN` | GitHub PAT with `repo` + `issues` scope |
| `GH_REPO` | The repo this instance manages (`owner/repo`) |
| `OPENROUTER_API_KEY` | OpenRouter API key for Phase 1A planning |
| `HOST_WORKTREES_DIR` | Host path where agent worktrees are created |

See `.env.example` for the full list with descriptions. See [docs/guides/setup.md](docs/guides/setup.md) for detailed setup instructions.

---

## The Planning Workflow

### Phase 1A — From brain dump to plan

Open the dashboard at `http://localhost:10003`. Paste anything — a rough idea, a wall of text, a list of tasks, a complaint about what's broken. Click **Plan**.

Claude reads it, reasons about dependencies, and produces a `PlanSpec` YAML: a structured, phase-gated plan with real GitHub issue titles and full structured bodies (context, objective, implementation notes, acceptance criteria, test coverage, documentation, and scope boundaries).

### Phase 1B — Review and approve

The YAML opens in an editor. Read it. Edit it. Add things the LLM missed. Remove things that aren't right. When you're satisfied, click **Create Issues** — AgentCeption files everything on GitHub, creates the phase labels, and wires the dependency graph.

### Dispatch

The Build board shows your phases. Phases that are ready to execute are unlocked. Click **Launch** on any unlocked phase to dispatch the org tree. The CTO agent surveys the board and cascades work down through VPs to engineers, each working in their own isolated git worktree.

You watch the PRs appear.

---

## Cognitive Architecture

Every agent dispatched by AgentCeption has a **cognitive architecture** — a composed identity injected into its system prompt that shapes how it reasons.

The architecture has four layers:

| Layer | What it does |
|-------|-------------|
| **Figures** | Historical thinkers and builders who model how to think (e.g. Alan Turing for logical rigor, Steve Jobs for taste-driven simplicity) |
| **Archetypes** | Abstract thinking styles (Architect, Craftsperson, Strategist, etc.) |
| **Skill domains** | Technical expertise areas (Python, FastAPI, PostgreSQL, DevOps, LLM, etc.) |
| **Atoms** | Fine-grained behavioral primitives (precision, pragmatism, epistemic humility, etc.) |

You choose a figure, an archetype, and skill domains for each agent role. The `resolve_arch.py` engine composes them into a single, coherent system prompt with governing heuristics, failure modes, and behavioral checkpoints. The result is an agent that doesn't just execute — it reasons in a particular way.

This is the infrastructure for deploying *judgment at scale*.

---

## Architecture

```
agentception/
  api/routes/      → Thin HTTP handlers
  readers/         → LLM planner, issue creator, worktree manager, GitHub client
  services/        → LLM calls, external integrations
  db/              → SQLAlchemy models, Alembic migrations
  routes/          → UI (Jinja2/HTMX/Alpine.js) and API (JSON/SSE) routes
  mcp/             → MCP server (Cursor/Claude tool integration)
  static/          → Compiled JS/CSS bundles
  templates/       → Jinja2 HTML templates
  config.py        → Pydantic Settings (env vars)
  models.py        → PlanSpec, PlanIssue, PlanPhase, and domain models

scripts/
  gen_prompts/     → Cognitive architecture engine
    resolve_arch.py           → Composes figure + archetype + skills into agent prompts
    cognitive_archetypes/     → YAML definitions for figures, archetypes, skill domains, atoms

.agentception/
  roles/           → Agent role markdown files (c-suite/, vps/, engineering/)
  prompts/         → Prompt templates
```

**Stack:** Python 3.11+, FastAPI, Jinja2, HTMX, Alpine.js, SCSS, Pydantic v2, SQLAlchemy (async), Alembic, PostgreSQL.

**Models:** `anthropic/claude-sonnet-4.6` and `anthropic/claude-opus-4.6` via OpenRouter.

---

## Related Projects

- **[cgcardona/maestro](https://github.com/cgcardona/maestro)** — AI music composition backend for the Stori DAW. AgentCeption was originally co-located here and has been extracted into this standalone repository.

---

## License

MIT
