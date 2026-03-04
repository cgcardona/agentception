# CI — Continuous Integration

Every pull request and push to `main` runs the full CI suite via GitHub Actions
(`.github/workflows/ci.yml`). There are four jobs, executed in order:

| Job | What it does |
|-----|-------------|
| **mypy** | `mypy agentception/ tests/` with `--strict` — zero errors required |
| **typing-ratchet** | `tools/typing_audit.py --max-any 10` — Any ceiling enforced |
| **pytest** | Full test suite against a live ephemeral Postgres |
| **smoke** | `docker compose up -d --wait` → `curl /health` → `curl /` |

---

## Required GitHub Actions secrets

Set these in **Settings → Secrets and variables → Actions** for `cgcardona/agentception`:

| Secret | Required by | Description |
|--------|-------------|-------------|
| `DB_PASSWORD` | All jobs | Postgres password. Any random string works for CI (the database is ephemeral). Example: `openssl rand -hex 16` |
| `GH_REPO` | smoke, test | The GitHub repo the instance manages. Defaults to `cgcardona/agentception`. |
| `OPENROUTER_API_KEY` | smoke | OpenRouter key for Phase 1A planning. Can be empty for the smoke test (health endpoint doesn't require it). |
| `GITHUB_TOKEN` | smoke | GitHub PAT with `repo` + `issues` scope. Can be empty for smoke-only runs. |

> **Note:** `DB_PASSWORD` is the only secret required for CI to pass today. The others are needed
> for E2E flows that involve real GitHub API calls.

---

## Running CI checks locally

These commands exactly mirror what CI runs, using the same compose override file:

```bash
# Build the image
docker compose -f docker-compose.yml -f docker-compose.ci.yml build agentception

# Type-check
docker compose -f docker-compose.yml -f docker-compose.ci.yml \
  run --rm agentception mypy agentception/ tests/

# Typing ratchet
docker compose -f docker-compose.yml -f docker-compose.ci.yml \
  run --rm agentception python tools/typing_audit.py --dirs agentception/ tests/ --max-any 10

# Full test suite (start postgres first)
docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.ci.yml \
  run --rm agentception alembic -c agentception/alembic.ini upgrade head
docker compose -f docker-compose.yml -f docker-compose.ci.yml \
  run --rm agentception pytest tests/ -v --tb=short

# Smoke test
docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d --wait
curl -f http://localhost:10003/health
docker compose -f docker-compose.yml -f docker-compose.ci.yml down -v
```

---

## What `docker-compose.ci.yml` does

The CI override strips host-specific bind mounts that don't exist on GitHub Actions runners:

- Removes the `~/.cursor` mount (Cursor is not installed on runners)
- Removes the `~/.config/gh` mount (gh CLI auth is handled via `GITHUB_TOKEN`)
- Remaps paths to runner-safe locations (`/root`, `/tmp`)
- Sets `REPO_DIR=/app` (the built image already has the code at `/app`)
