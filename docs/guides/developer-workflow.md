# Developer Workflow

This guide covers day-to-day development mechanics inside the containerised AgentCeption environment: how the bind-mount loop works, the mandatory verification sequence before pushing, the exact commands to run locally, and the branch protection model.

---

## Bind-mount system

`docker-compose.override.yml` bind-mounts key host directories into the running container:

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `agentception/` | `/app/agentception/` | Application source |
| `tests/` | `/app/tests/` | Test suite |
| `scripts/` | `/app/scripts/` | Helper scripts |
| `pyproject.toml` | `/app/pyproject.toml` | Project metadata |

**The critical implication:** saving a `.py` file on your host is immediately visible to the running process inside the container. The FastAPI dev server (Uvicorn with `--reload`) picks up the change and restarts automatically — **no image rebuild required**.

### When you do and do not need to rebuild

| Change type | Action required |
|-------------|----------------|
| Edit any `.py` file | None — bind mount + auto-reload handles it |
| Edit any Jinja2 template (`.html`) | None — bind mount handles it |
| Edit any `.scss` source file | Run `npm run build:css` then reload the browser |
| Edit any `.js` source file under `static/js/` | Run `npm run build:js` then reload the browser |
| Add or remove a Python dependency (`requirements.txt`) | `docker compose build agentception && docker compose up -d agentception` |
| Change `Dockerfile` or `entrypoint.sh` | `docker compose build agentception && docker compose up -d agentception` |
| Change `pyproject.toml` metadata only | None — live-mounted |

**Rule of thumb:** if it touches the filesystem layer baked into the image (`Dockerfile`, `entrypoint.sh`, installed packages), rebuild. If it touches source code or templates, just save.

---

## Verification order

Before pushing any branch, run checks in this exact order:

```
mypy → pytest → coverage
```

### 1. mypy (type checking)

```bash
docker compose exec agentception mypy agentception/ tests/
```

Run mypy **first**. Type errors are the cheapest to fix at the source — catching them before running tests prevents a class of test failures where the test itself is testing the wrong type contract. A test failure caused by a type error will disappear once the type is fixed, so running tests before mypy means you may run the suite twice.

The mypy configuration in `pyproject.toml` runs with `strict = true`. Zero errors is the only acceptable result.

### 2. pytest (unit and integration tests)

```bash
docker compose exec agentception pytest tests/ -v
```

Run the full test suite only after mypy is clean. If you are working on a specific area, you can run a single file first, but the full suite must pass before you push.

### 3. Coverage check

```bash
docker compose exec agentception sh -c "export COVERAGE_FILE=/tmp/.coverage && python -m coverage run -m pytest tests/ -v && python -m coverage report --fail-under=80 --show-missing"
```

Coverage must not drop below 80%. The `--fail-under=80` flag makes this an enforced ceiling — the command exits non-zero if coverage falls below the threshold.

### Why this order matters

Type errors can mask test failures. If a function returns the wrong type, the test exercising it may fail in a confusing way. Fixing the type error first makes the test failure (if any) immediately legible. Running mypy → tests → coverage in sequence means you rarely need to run any step more than once.

---

## Local commands

All commands run inside the container via `docker compose exec`. Never run Python directly on the host.

### Type checking

```bash
docker compose exec agentception mypy agentception/ tests/
```

### Run all tests

```bash
docker compose exec agentception pytest tests/ -v
```

### Run tests with coverage

```bash
docker compose exec agentception sh -c "export COVERAGE_FILE=/tmp/.coverage && python -m coverage run -m pytest tests/ -v && python -m coverage report --fail-under=80 --show-missing"
```

### Rebuild the image (dependency or Dockerfile changes only)

```bash
docker compose build agentception
```

### Restart the app container (pick up config changes)

```bash
docker compose restart agentception
```

### Tail application logs

```bash
docker compose logs -f agentception
```

### Run database migrations

```bash
docker compose exec agentception alembic -c agentception/alembic.ini upgrade head
```

### Open an interactive shell inside the container

```bash
docker compose exec agentception bash
```

### Typing audit (zero `Any` ceiling)

```bash
python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0
```

---

## Branch protection

This repository uses a three-tier branching model:

```
feature/* → dev → main
```

| Branch | Purpose | Direct push allowed |
|--------|---------|-------------------|
| `feature/*` (or `feat/*`) | Day-to-day development work | Yes — this is your working branch |
| `dev` | Integration target; all features merge here first | **No** — PR required |
| `main` | Production; only merged from `dev` | **No** — PR required |

### Rules

1. **Create a `feature/` branch** from `dev` for every change, no matter how small.
2. **Open a PR targeting `dev`** when your work is ready. Direct pushes to `dev` are blocked.
3. **`dev` is promoted to `main`** via a separate PR when the integration branch is stable. Direct pushes to `main` are blocked.
4. **All checks must pass** (mypy, tests, coverage) before a PR can merge. The verification sequence above mirrors what CI enforces.

For PR conventions (title format, description template, review expectations), see [./contributing.md](./contributing.md).
