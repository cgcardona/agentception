# Contributing to AgentCeption

This document is the single authoritative reference for contribution standards. It covers the full lifecycle of a change — from branching through merge — and the automated gates that every pull request must pass.

---

## PR flow

### 1. Branch from `dev`

All feature work, bug fixes, and documentation changes start from `dev`:

```bash
git checkout dev
git pull origin dev
git checkout -b feat/short-description   # or fix/, docs/, refactor/, test/, chore/
```

The branch name should mirror the commit type prefix (see [Commit message conventions](#commit-message-conventions)).

### 2. Open a draft PR early

Open a draft pull request as soon as the branch exists and you have at least one commit — even if the work is incomplete. This keeps the team informed and lets CI surface integration problems early.

```bash
gh pr create \
  --repo cgcardona/agentception \
  --base dev \
  --draft \
  --title "feat: short description of change" \
  --body "Closes #<issue number>"
```

### 3. Iterate until CI is green

Push commits to the branch normally. Each push triggers the full CI suite (see [CI gates](#ci-gates)). Do not mark the PR ready-for-review until every CI check is green.

### 4. Mark ready-for-review

When CI is fully green:

```bash
gh pr ready <PR number> --repo cgcardona/agentception
```

At this point, request a reviewer. At least **one approving review** from a maintainer is required before merge.

### 5. Squash-merge to `dev`

All merges into `dev` are squash-merges to keep history linear. The squash commit message must follow the [Commit message conventions](#commit-message-conventions) below.

GitHub enforces squash-merge at the repository level; you do not need to do this manually.

---

## Commit message conventions

AgentCeption follows [Conventional Commits](https://www.conventionalcommits.org/) with imperative mood and a 72-character subject line limit.

### Format

```
<type>: <short imperative summary>

[optional body — explain *why*, not *what*]
```

### Permitted type prefixes

| Type | When to use |
|------|-------------|
| `feat:` | A new user-facing feature or capability |
| `fix:` | A bug fix |
| `docs:` | Documentation-only changes |
| `refactor:` | Code restructuring with no behaviour change |
| `test:` | Adding or updating tests with no production code change |
| `chore:` | Build system, dependency, or tooling changes |

### Examples

**`feat:`**

```
# Good
feat: add SSE progress events to Phase 1B planning endpoint

# Bad
feat: SSE stuff
```

**`fix:`**

```
# Good
fix: prevent double-dispatch when agent worktree is reused across runs

# Bad
fix: fixed the bug
```

### Rules

- Subject line: imperative mood, ≤ 72 characters, no trailing period.
- Body: optional but strongly encouraged for non-trivial changes. Explain *why* the change was made, not *what* changed (the diff shows that). Wrap at 72 characters.
- Reference the closing issue in the body or as a trailer: `Closes #61`.

---

## CI gates

Every pull request must pass all four CI jobs before it can be merged. These checks are enforced automatically; a failing check blocks merge.

| Job | Command | Pass condition |
|-----|---------|----------------|
| **mypy** | `mypy agentception/ tests/` | Zero errors, `strict = true` |
| **typing-ratchet** | `python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0` | Zero `Any` annotations |
| **pytest** | `pytest tests/ -v --tb=short` | All tests pass; coverage ≥ 80 % |
| **smoke** | `docker compose up -d --wait && curl -f /health` | HTTP 200 from the health endpoint |

### Running CI checks locally

These commands mirror what CI runs exactly:

```bash
# 1. Type-check
docker compose exec agentception mypy agentception/ tests/

# 2. Typing ratchet
docker compose exec agentception \
  python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0

# 3. Full test suite with coverage
docker compose exec agentception sh -c \
  "export COVERAGE_FILE=/tmp/.coverage && \
   python -m coverage run -m pytest tests/ -v && \
   python -m coverage report --fail-under=80 --show-missing"

# 4. Smoke test (from the host)
docker compose up -d --wait
curl -f http://localhost:10003/health
```

> **Dev bind mounts are active.** `docker-compose.override.yml` bind-mounts `agentception/`, `tests/`, `scripts/`, and `pyproject.toml` into the container. Host file edits are instantly visible — no rebuild needed for code changes. Only rebuild (`docker compose build agentception && docker compose up -d`) when `requirements.txt`, `Dockerfile`, or `entrypoint.sh` change.

### Verification order

Always run in this order: **mypy → typing-ratchet → tests**. Fix type errors first so you only need one test pass; a type error can mask a test failure, but the reverse is not true.

---

## Zero-Any typing ceiling

AgentCeption enforces a hard ceiling of **zero `Any` annotations** across all application and test code. This is not a style preference — it is a correctness contract that allows both humans and agents to navigate the codebase safely.

### What is banned

| Banned | Why | Use instead |
|--------|-----|-------------|
| `Any` | Collapses type safety for every downstream caller | `TypedDict`, `BaseModel`, `Protocol`, or a specific union |
| `object` | Carries no structural information — effectively `Any` | The actual type or a constrained union |
| `list` (bare) | Contents are unknown to mypy | `list[X]` with the concrete element type |
| `dict` (bare) | Same | `dict[K, V]` with concrete key and value types |
| `dict[str, X]` with known keys | Structured data treated as dynamic | `TypedDict` or `BaseModel` — if you know the keys, name them |
| `cast(T, x)` | Masks a broken return type upstream | Fix the callee to return `T` |
| `# type: ignore` | Silences a real error | Fix the root cause; use a typed stub for third-party issues |

### mypy configuration

The mypy configuration lives in `pyproject.toml`:

```toml
[tool.mypy]
strict = true
```

`strict = true` enables, among other things, `disallow_any_explicit`, `disallow_any_generics`, `disallow_untyped_defs`, and `warn_return_any`. All of these must stay enabled. The CI **mypy** job enforces zero errors against this configuration.

### The typing ratchet

`tools/typing_audit.py` counts explicit `Any` annotations in the source tree and compares the count against a hard ceiling:

```bash
python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0
```

The ceiling is **0**. The CI **typing-ratchet** job runs this command and fails the build if the count exceeds the ceiling. Never raise the ceiling — fix the type instead.

### Exception process

There is no blanket exception. If a third-party library produces an unavoidable typing gap, the correct approach is:

1. Write a thin, fully-typed wrapper around the problematic call.
2. Document in a code comment *why* the wrapper was necessary and which library version is responsible.
3. Describe the workaround in the PR description so a maintainer can evaluate it explicitly.

A `# type: ignore` comment or an `Any` annotation introduced without this process will be rejected in review.

### The known-keys rule

`list[X]` and `dict[K, V]` are correct when the collection *is* the abstraction — a homogeneous sequence or a genuine dynamic lookup table where any key is valid. The key question: **do you know the keys at write time?**

- **Yes** → use a `TypedDict` or `BaseModel`. A `dict[str, str]` whose keys are always `"title"`, `"body"`, and `"label"` is a `TypedDict` waiting to be written. Name it.
- **No** → `dict[K, V]` is correct. A label-to-colour map or a phase-to-count lookup has no fixed key structure.

`dict[str, Any]` with a known key structure is the highest-signal red flag in the codebase — structured data masquerading as unstructured. Always replace it with a named type.

---

## See also

- [CI reference](ci.md) — full CI job definitions and GitHub Actions secrets
- [Setup guide](setup.md) — local development environment
- [Type contracts reference](../reference/type-contracts.md) — public Pydantic models and function signatures
- `pyproject.toml` — mypy, pytest, and coverage configuration
- `AGENTS.md` — agent-specific contribution rules and verification checklist
