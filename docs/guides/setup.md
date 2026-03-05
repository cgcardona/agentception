# Setup Guide — First Run

This is a verified, step-by-step guide for running AgentCeption from a cold clone.
Every step was executed against a freshly cloned copy of the repo with no prior environment.

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Docker Desktop (or Docker Engine + Compose v2) | ≥ 24 | `docker compose version` to check |
| `git` | any | |
| A GitHub Personal Access Token | — | `repo` + `issues` scope — [create one here](https://github.com/settings/tokens) |
| An [OpenRouter](https://openrouter.ai/keys) API key | — | Required for Phase 1A planning |

---

## Step 1 — Clone

```bash
git clone https://github.com/cgcardona/agentception
cd agentception
```

---

## Step 2 — Create `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in the required values:

| Variable | Required | What it is | Where to get it |
|----------|----------|------------|-----------------|
| `DB_PASSWORD` | **Yes** | Postgres password. No default — the compose file requires it explicitly. | Generate with `openssl rand -hex 16` |
| `GH_REPO` | **Yes** | The `owner/repo` this AgentCeption instance orchestrates. | Your GitHub repo |
| `GITHUB_TOKEN` | Optional | GitHub PAT with `repo` + `issues` scope. If you have `~/.config/gh` configured (via `gh auth login`), the container volume-mounts it and you can leave this blank. | [github.com/settings/tokens](https://github.com/settings/tokens) |
| `OPENROUTER_API_KEY` | Optional | OpenRouter API key. Required for Phase 1A LLM planning. Without it the service starts, but the planner falls back to a keyword classifier. | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `HOST_WORKTREES_DIR` | Optional | Host path where agent git worktrees are created. Use an absolute path (no `~` — compose doesn't expand it). | Default: `~/.agentception/worktrees` |
| `WORKTREES_DIR` | Optional | Container-internal path that maps to `HOST_WORKTREES_DIR`. Add a matching volume in `docker-compose.override.yml` if you change this. | Default: `/worktrees` |
| `REPO_DIR` | Optional | Absolute path to the cloned agentception repo on the host. Used for git operations inside the container. | Default: `/app` (the container working directory) |
| `PORT` | Optional | Port the FastAPI app listens on. | Default: `10003` |
| `LOG_LEVEL` | Optional | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. | Default: `INFO` |

Minimal `.env` that works for a basic smoke test (no LLM features):

```bash
DB_PASSWORD=<run: openssl rand -hex 16>
GH_REPO=owner/your-repo
```

---

## Step 3 — Build

```bash
docker compose build
```

First build downloads the Python 3.11 slim image, installs system packages (git, curl, gh CLI, Dart Sass), installs Python dependencies, and compiles SCSS. Expect **2–5 minutes** on a cold build.

A successful build ends with:
```
agentception  Built
```

---

## Step 4 — Start the stack

```bash
docker compose up -d
```

This starts three services:

| Container | Host port | Purpose |
|-----------|-----------|---------|
| `agentception-app` | 10003 | FastAPI dashboard + API |
| `agentception-postgres` | 5433 | Persistent database (to avoid collision with other local Postgres instances) |
| `agentception-qdrant` | 6335 / 6336 | Vector store for semantic search |

Verify they're running:
```bash
docker compose ps
```

All three should show `running` (postgres and agentception will also show `healthy` after a few seconds).

---

## Step 5 — Run database migrations

> **Automatic:** `docker compose up` now runs `alembic upgrade head` before
> starting the server, so migrations are applied automatically on every
> container start.  You only need to run this manually when working outside
> Compose (e.g. in CI or a bare Python environment).

```bash
docker compose exec agentception alembic -c agentception/alembic.ini upgrade head
```

Expected output:
```
INFO  [alembic.runtime.migration] Running upgrade  -> ac0001
INFO  [alembic.runtime.migration] Running upgrade ac0001 -> ac0002
...
```

If postgres is still starting, wait 5 seconds and retry.

---

## Step 6 — Verify the dashboard

Open [http://localhost:10003](http://localhost:10003) in your browser. You should see the AgentCeption dashboard with **Build**, **Org Chart**, and **Cognitive Architecture** pages all rendering.

Quick smoke test from the command line:

```bash
# Basic health ping
curl -f http://localhost:10003/health
# → {"status":"ok"}

# Detailed health snapshot (uptime, memory, worktree count, GitHub API latency)
curl -f http://localhost:10003/api/health/detailed
# → {"uptime_seconds":..., "memory_rss_mb":..., "active_worktree_count":0, "github_api_latency_ms":...}
```

Both should return HTTP 200.

---

## Step 7 — Configure Cursor MCP (optional)

To use AgentCeption tools from Cursor, add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose", "exec", "-T", "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "/absolute/path/to/your/agentception"
    }
  }
}
```

---

## Stopping the stack

```bash
# Stop containers, preserve database volumes
docker compose down

# Stop containers and delete all volumes (fresh start)
docker compose down -v
```

---

## Troubleshooting

**`DB_PASSWORD` error on `docker compose up`**

`.env` must have `DB_PASSWORD=<some-value>`. There is no default — the compose file requires it explicitly.

```bash
grep DB_PASSWORD .env   # should show a non-empty value
```

**Build fails: `No module named 'setuptools.backends'`**

This indicates a mismatch between pip and the build backend. Make sure your local `pyproject.toml` uses `build-backend = "setuptools.build_meta"` (not `setuptools.backends.legacy:build`). Pull the latest `main` — this was fixed in [#970](https://github.com/cgcardona/agentception/pull/970).

**Port 10003 already in use**

Another service is occupying that port. Stop it, or change the host port in `docker-compose.yml`:
```yaml
ports:
  - "127.0.0.1:10004:10003"   # use 10004 on the host instead
```

**Alembic migration fails with "connection refused"**

Postgres is still starting. Wait a few seconds and retry.

**Dashboard returns 502 / connection refused**

The app is still starting. Wait ~15 seconds and refresh. Check logs with `docker compose logs agentception`.

**`gh` CLI commands fail inside the container**

The container volume-mounts `~/.config/gh` read-only. Run `gh auth login` on the host first, then restart the container.
