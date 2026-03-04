# AgentCeption

> Multi-agent orchestration system for AI-powered development workflows.

AgentCeption is a standalone FastAPI application that coordinates teams of AI agents through a structured planning and dispatch pipeline. It turns a plain-text "brain dump" into a phase-gated GitHub issue plan, then spawns the right agents at the right tier of the org tree to execute that plan — autonomously.

---

## How it works

```
Brain dump → Phase 1A (LLM planning) → Phase 1B (human review) → GitHub issues
                                                                       ↓
                                                     AgentCeption dispatches agents
                                                     by label, phase, and org tier
                                                                       ↓
                                                         Agents open PRs → merge
```

1. **Phase 1A** — Paste a brain dump into the dashboard. Claude converts it into a structured `PlanSpec` YAML with phase-gated issues.
2. **Phase 1B** — Review and edit the YAML in the Monaco editor, then click "Create Issues" to file everything on GitHub.
3. **Dispatch** — Click "Launch" on any unlocked phase. AgentCeption spawns agents at the correct org tier (CTO → VP → Engineer) with full cognitive-architecture context injected.

---

## Status

Active development. See [cgcardona/maestro#961](https://github.com/cgcardona/maestro/issues/961) for the extraction plan.

---

## Quick start

```bash
git clone https://github.com/cgcardona/agentception
cd agentception
cp .env.example .env        # fill in required values
docker compose up -d
docker compose exec agentception alembic upgrade head
open http://localhost:10003
```

See [docs/guides/setup.md](docs/guides/setup.md) for full setup instructions and required environment variables.

---

## Related projects

- **[cgcardona/maestro](https://github.com/cgcardona/maestro)** — AI music composition backend (Stori DAW). AgentCeption was originally co-located here.
