# Weekend Hackathon — AgentCeption

This repo is the **AgentCeption** project: multi-agent orchestration for AI-powered development. This doc summarizes what it does, how to set it up and test it, and how **Knowtation** fits in (MCP + CLI, phase plan).

---

## Passover: Weekend Hackathon — AgentCeption + Knowtation (context for next session)

**Context:** This comes from a prior discussion in the TellUrStoriDAW/maestro workspace. We are now working in the **AgentCeption** repo only (standalone at `~/agentception`), not in Maestro or the DAW.

### What we're doing
- **Weekend hackathon** on AgentCeption: get it running, test it, and demonstrate that it works well with **Knowtation**.
- **Knowtation** ([github.com/aaronrene/knowtation](https://github.com/aaronrene/knowtation)) is a personal/team knowledge vault (capture, index, search, export). It was originally built as marketing-oriented tooling for OpenClaw-like agent handling — notes, specs, and context that agents can read and write.
- **AgentCeption** ([github.com/cgcardona/agentception](https://github.com/cgcardona/agentception)) is multi-agent orchestration: brain dump → PlanSpec → GitHub issues → agent org (CTO → coordinators → engineers) → PRs. We want to show that AgentCeption's agents can use Knowtation as the "org brain": pull context before/during tasks and write back plans/summaries.

### Decisions already made
1. **Repo setup:** AgentCeption lives as its **own workspace** (e.g. `~/agentception`). It is **not** inside TellUrStoriDAW or Maestro. Open it in a **new Cursor window** (File → New Window → Open Folder → `agentception`).
2. **Branch:** Work on the **`weekend-hackathon`** branch (already created from `main`).
3. **Knowtation integration:** Support **both**:
   - **CLI:** Install Knowtation CLI in the agent environment; agents run `knowtation search` / `get-note` / `write` for context and write-back.
   - **MCP:** Run a Knowtation MCP server alongside AgentCeption so Cursor/Claude and agents can call the vault via tools. One core shared with the CLI.
4. **Phase plan (additive):**
   - **K1:** CLI in agent environment + optional bridge (e.g. write phase summaries into the vault after Create Issues or after each phase).
   - **K2:** Knowtation MCP server; document in AgentCeption's MCP guide.
   - **K3 (optional):** Conventions (paths, frontmatter) and provenance for agent-written notes.

### What to do in this workspace
1. **Run and test AgentCeption** per this doc and `docs/guides/setup.md`: Docker, `.env`, migrations, dashboard at http://localhost:10003, then mypy → pytest → coverage.
2. **Demonstrate integration with Knowtation:** Either (a) document the intended flow (agents query vault before tasks; write-back of plan/phase summaries), or (b) implement a minimal path (e.g. CLI in agent env + one bridge script that writes a phase summary into the vault). Goal: show that the two systems work well together — Knowtation as org brain, AgentCeption as execution org.
3. **Keep hackathon scope realistic:** Prefer a clear, working slice (e.g. one flow + docs) over full K1/K2/K3 in one go.

### References in this repo
- **HACKATHON.md** (this file) — What AgentCeption does, setup, testing, Knowtation integration (MCP + CLI, phase plan).
- **docs/guides/setup.md** — First-run, env vars, migrations, Cursor MCP.
- **docs/guides/developer-workflow.md** — Bind mounts, mypy → pytest → coverage.
- **docs/guides/mcp.md** — MCP tool reference.
- **AGENTS.md** — Agent contract, branch discipline, verification checklist.

**Summary:** We are in the **AgentCeption** repo on branch **`weekend-hackathon`**. The goal is to run and test AgentCeption and show it works well with **Knowtation** (knowledge vault / marketing-style agent tooling). Implement or document a minimal integration (CLI and/or MCP, plus optional write-back), and keep the handoff and phase plan above in mind for follow-up work.

---

## Repo status

- **Location:** Standalone project at `~/agentception` (or `$HOME/agentception`) — **not** inside TellUrStoriDAW or Maestro.
- **Branch:** `weekend-hackathon` (created from `main` for this hackathon).
- **Remote:** `origin` → `https://github.com/cgcardona/agentception.git`.

**To work in this repo:** Open a **new Cursor window** (File → New Window), then File → Open Folder → choose `agentception` (your home directory / `~/agentception`). This is its own workspace with no connection to the DAW or Maestro.

---

## What AgentCeption does

**AgentCeption** is an orchestration system that turns a brain dump into a structured plan, GitHub issues, and an agent org that produces PRs.

```
Brain dump → Structured plan (PlanSpec) → GitHub issues → Agent org tree (CTO → coordinators → engineers) → PRs → Merged
```

- **Plan:** Paste anything; Claude turns it into a `PlanSpec` (phases, issues, dependencies, acceptance criteria).
- **Review:** Edit the YAML, then click **Create Issues** to file everything on GitHub.
- **Ship:** Launch phases from the board. A CTO agent surveys the board and cascades work to coordinators and engineers in isolated git worktrees. PRs appear; phases unlock.

Agents use a **cognitive architecture** (figures, archetypes, skill domains, behavioral atoms). The stack is Python 3.11, FastAPI, Jinja2, HTMX, Alpine.js, Pydantic v2, SQLAlchemy (async), Alembic, PostgreSQL, Qdrant. Models: `claude-sonnet-4-6` and `claude-opus-4-6` via Anthropic.

**MCP:** Cursor and Claude can call AgentCeption tools (planning, dispatch, etc.) via the MCP server. See [docs/guides/mcp.md](docs/guides/mcp.md).

---

## Quick setup (Docker)

```bash
cd ~/agentception   # or your path to this repo
cp .env.example .env
# Edit .env: DB_PASSWORD (openssl rand -hex 16), GH_REPO (owner/repo),
#            GITHUB_TOKEN (PAT with repo + issues), ANTHROPIC_API_KEY,
#            HOST_WORKTREES_DIR (absolute path for agent worktrees).

docker compose build
docker compose up -d
docker compose exec agentception alembic -c agentception/alembic.ini upgrade head
open http://localhost:10003
```

**Required env (minimal):** `DB_PASSWORD`, `GH_REPO`. For planning and agents: `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `HOST_WORKTREES_DIR`.

**Services:** AgentCeption app (10003), Postgres (5433), Qdrant (6335/6336). See [docs/guides/setup.md](docs/guides/setup.md).

---

## How to test

Run everything **inside the container** via `docker compose exec agentception ...`. Order: **mypy → pytest → coverage**.

1. **mypy (run first):**
   ```bash
   docker compose exec agentception mypy agentception/ tests/
   ```

2. **pytest:**
   ```bash
   docker compose exec agentception pytest tests/ -v
   ```

3. **Coverage (80% threshold):** Requires `coverage` in the container (add to requirements if needed). Otherwise run unit tests only:
   ```bash
   docker compose exec agentception sh -c "export COVERAGE_FILE=/tmp/.coverage && python -m coverage run -m pytest tests/ -v && python -m coverage report --fail-under=80 --show-missing"
   ```

**Note:** The root `tests/` (12 tests) run with `pytest tests/ -v`. The full suite under `agentception/tests/` has many more tests; run with `pytest agentception/tests/ tests/ -v`. Some tests in `agentception/tests/` may fail (e.g. SSL retry or GitHub API mocks); mypy and the root tests are the minimal gate for the hackathon.

After editing Python: `docker compose restart agentception` (bind mount is live; restart loads new code). After adding deps or changing Dockerfile: rebuild and up. See [docs/guides/developer-workflow.md](docs/guides/developer-workflow.md).

---

## How to use it

- **Dashboard:** http://localhost:10003 — Build, Org Chart, Cognitive Architecture.
- **Health:** `curl -f http://localhost:10003/health` and `curl -f http://localhost:10003/api/health/detailed`.
- **MCP (Cursor):** Add the AgentCeption MCP server to `~/.cursor/mcp.json` with `cwd` set to the absolute path of this repo; see [docs/guides/setup.md](docs/guides/setup.md) Step 8 and [docs/guides/mcp.md](docs/guides/mcp.md).
- **Codebase indexing (optional):** `curl -X POST http://localhost:10003/api/system/index-codebase` for semantic search in the agent loop.

---

## Knowtation integration (org brain)

**Knowtation** = personal/team knowledge vault (capture, index, search, export). **AgentCeption** = the org that does the work. They fit when AgentCeption's agents **read** and **write** the vault.

### Recommendation: both MCP and CLI

- **CLI first:** Install Knowtation CLI in the agent environment; set `KNOWTATION_VAULT_PATH`. Agents run `knowtation search ...` / `knowtation get-note ...` / `knowtation write ...` (e.g. for context before tasks, write-back of phase summaries). Works in any environment and scripts.
- **MCP second:** Run a Knowtation MCP server in the same environment as AgentCeption's agents. Expose `search`, `get_note`, `list_notes`, `write` so Cursor/Claude and AgentCeption can call the vault natively. One core shared with the CLI keeps behavior consistent.

### Phase plan (additive)

Add a **Knowtation integration** slice; keep existing AgentCeption phases as-is.

1. **K1 — CLI in agent environment:** Install Knowtation CLI in the agent container/host; set vault path and config; document "search → get-note" and optional bridge: after Create Issues or after each phase, run `knowtation write vault/projects/<repo>/plans/phase-N-summary.md --stdin` with frontmatter.
2. **K2 — MCP server:** Run Knowtation MCP alongside AgentCeption; document in AgentCeption's MCP guide so agents can query/write the vault via tools.
3. **K3 (optional):** Conventions (paths, frontmatter) and provenance for agent-written notes.

Flows: (1) Agents pull context from the vault before/during tasks. (2) Plan and phase summaries are written into the vault for later search. (3) Vault content (e.g. spec, project notes) can feed the planner as brain-dump input.

**Hackathon deliverables:** See [docs/guides/knowtation-integration.md](docs/guides/knowtation-integration.md) for the intended flow, K1/K2/K3, and the minimal bridge script `scripts/knowtation_write_phase_summary.sh` to write phase summaries into the vault.

---

## References

- [README.md](README.md) — Quick start, stack, MCP.
- [docs/guides/setup.md](docs/guides/setup.md) — First-run, env, migrations, Cursor MCP.
- [docs/guides/developer-workflow.md](docs/guides/developer-workflow.md) — Bind mounts, mypy → pytest → coverage.
- [docs/guides/mcp.md](docs/guides/mcp.md) — MCP tool reference.
- [docs/guides/knowtation-integration.md](docs/guides/knowtation-integration.md) — Knowtation org-brain flow, K1/K2/K3, bridge script.
- [GitHub: cgcardona/agentception](https://github.com/cgcardona/agentception)
- [GitHub: aaronrene/knowtation](https://github.com/aaronrene/knowtation)
