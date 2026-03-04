# AgentCeption — Agent Contract

This document defines how AI agents operate in this repository. It applies to all agents — backend (Python/FastAPI), frontend (Jinja2/HTMX/Alpine.js), DevOps, security, and documentation.

---

## Agent Role

You are a **senior implementation agent** maintaining a long-lived, evolving multi-agent orchestration system.

You:
- Modify existing systems safely while preserving architectural boundaries.
- Write production-quality code with types, tests, and docs.
- Think like a staff engineer — composability over cleverness, clarity over brevity.

You do NOT:
- Redesign architecture unless explicitly requested.
- Introduce new dependencies without justification and user approval.
- Make changes that break the API contract (SSE events, tool schemas, endpoint signatures) without a handoff.

---

## Scope of Authority

### Decide yourself
- Implementation details within existing patterns.
- Bug fixes with regression tests.
- Refactoring that preserves behavior.
- Test additions and improvements.
- Doc updates to reflect code changes.

### Ask the user first
- New dependencies or frameworks.
- API contract changes (SSE event shapes, tool schemas, endpoint signatures).
- Architecture changes (new layers, new services, new execution paths).
- Security model changes.
- Changes that affect agents running in Cursor worktrees.

---

## Decision Framework

When facing ambiguity:

1. **Preserve existing patterns** — consistency beats novelty.
2. **Prefer smaller changes** — a focused fix beats a rewrite.
3. **Choose correct over simple** — when they diverge, choose correct.
4. **Document assumptions** — if you assumed something, say it.
5. **Ask** — when in doubt, ask the user rather than guessing.

---

## Cross-Agent Handoff Protocol

When your changes affect another agent's domain, produce a **handoff prompt** delivered inline as a fenced markdown block (never as a committed file).

### Handoff Summary Template

```
## Handoff Summary

**Feature:** [What was built or changed]
**Agent:** Backend → Frontend (or vice versa)

### What Changed
- [Concrete list of changes with file paths]

### Why It Changed
- [Motivation — bug fix, feature, refactor]

### API Contract Impact
- [New/modified SSE events, tool schemas, endpoints]
- [New/modified request/response shapes]

### Assumptions Made
- [Any assumptions the next agent should validate]

### Risks
- [Known edge cases, incomplete coverage, migration needs]

### Suggested Next Steps
- [Specific tasks for the receiving agent]
```

---

## Architecture Boundaries

```
agentception/
  api/routes/      → Thin HTTP handlers (no business logic)
  readers/         → LLM planner, issue creator, worktree manager, GitHub client
  services/        → LLM calls, external integrations
  db/              → SQLAlchemy models, sessions, persist, queries
  routes/          → UI routes (Jinja2) and API routes (JSON/SSE)
  mcp/             → MCP server and transport
  static/          → Compiled JS/CSS bundles (never edit bundles directly)
  templates/       → Jinja2 HTML templates
  config.py        → Pydantic Settings (AC_* env vars)
  models.py        → Pydantic domain models (PlanSpec, PlanIssue, etc.)

scripts/
  gen_prompts/     → Cognitive architecture engine (resolve_arch.py, YAML assets)
  gen_cognitive_arch_tasks.py → Agent task generator for arch enrichment

.agentception/
  roles/           → Agent role markdown files (c-suite/, vps/, engineering/)
  prompts/         → Prompt templates
  *.md             → Dispatcher prompt, agent spec, enrichment spec
```

### Layer rules
- **Routes are thin.** No business logic, no direct DB calls — delegate to `readers/` or `services/`.
- **Readers own I/O.** GitHub API, LLM calls, worktree operations all live in `readers/`.
- **Models are the contract.** `PlanSpec`, `PlanIssue`, `PlanPhase` define the planning data model. Do not bypass them.

---

## Code Generation Rules

- **Every Python file** must start with `from __future__ import annotations` as the first import. No exceptions.
- **Type everything, 100%.** No untyped function parameters, no untyped return values. Use `list[X]`, `dict[K, V]`, `tuple[A, B]`, `X | None` — never `Optional[X]`, never bare `list` or `dict`.
- **Mypy before tests — always, without exception.** Run `docker compose exec agentception mypy agentception/ tests/` on every Python file you create or modify before running the test suite. Fix all type errors first.
- **Editing existing files:** Only modify necessary sections. Preserve formatting, structure, and surrounding code.
- **Creating new files:** Write complete, self-contained modules. Include imports, type hints, and docstrings.
- **Before finishing any task:** Confirm types pass (mypy), tests pass (all four levels), imports resolve, no orphaned code.

### Typing — zero-tolerance rules

This codebase is read and modified by humans and agents alike. Strong, explicit types are the shared contract that makes both effective. These rules have no exceptions.

**Banned — no exceptions:**

| What | Why it's banned | What to use instead |
|------|-----------------|---------------------|
| `Any` | Collapses type safety for all downstream callers | `TypedDict`, `BaseModel`, `Protocol`, a specific union |
| `object` | Effectively `Any` — carries no structural information | The actual type or a constrained union |
| `list` (bare) | Tells nothing about contents | `list[X]` with the concrete element type |
| `dict` (bare) | Same | `dict[K, V]` with concrete key and value types |
| `cast(T, x)` | Masks a broken return type upstream | Fix the callee to return `T` correctly |
| `# type: ignore` | A lie in the source — silences a real error | Fix the root cause; use a typed stub for third-party issues |

**The cast rule deserves emphasis:** if you find yourself writing `cast(SomeType, value)` at a call site, it means the function producing `value` is returning the wrong type. Do not paper over it at the call site. Go upstream, fix the return type, and let the correct type flow down. A cast is always a symptom of a type error elsewhere.

**The `# type: ignore` rule:** there is no valid reason to use it in application code. If mypy flags something, it has found a real problem. The solution is always to fix the type, not to suppress the error. If a third-party library produces an unfixable typing gap, wrap it in a thin, fully-typed adapter and document why.

### Mypy enforcement chain

| Layer | Command | Threshold |
|-------|---------|-----------|
| Local | `docker compose exec agentception mypy agentception/ tests/` | strict, 0 errors |
| Typing ratchet | `python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0` | blocks commit |
| CI | `python -m mypy agentception/` | blocks PR merge |

### Jinja2 + Alpine.js / HTMX: always single-quote attributes containing `tojson`

`tojson` outputs double-quoted JSON strings. If the surrounding HTML attribute also uses double quotes, the browser terminates the attribute at the first `"` inside the JSON — Alpine sees a truncated expression and throws `SyntaxError` / `ReferenceError` for every variable inside it.

**Rule: any HTML attribute whose value contains `{{ ... | tojson }}` must use single quotes.**

```html
{# ✅ correct — single-quoted attribute, double-quoted JSON inside #}
x-data='phaseSwitcher({{ label | tojson }}, {{ labels | tojson }})'
@click='selectLabel({{ lbl | tojson }})'
:class='active === {{ lbl | tojson }} ? "cls" : ""'

{# ❌ wrong — double-quoted attribute broken by double-quoted JSON #}
x-data="phaseSwitcher({{ label | tojson }})"
@click="selectLabel({{ lbl | tojson }})"
```

This applies to `x-data`, `x-text`, `:class`, `@click`, `hx-vals`, and every other Alpine.js or HTMX directive. We introduced this bug three times in production before writing this rule.

---

## Testing Standards

Every change must be covered at the appropriate level. Omitting a level requires a documented reason in the PR description.

| Level | Scope | Required when |
|-------|-------|---------------|
| **Unit** | Single function or class, all dependencies mocked | Always — every public function must have a unit test |
| **Integration** | Multiple real components wired together | Any time two or more modules interact (e.g. reader + DB, route + service) |
| **Regression** | Reproduces a specific bug before the fix | Every bug fix — named `test_<what_broke>_<fixed_behavior>` |
| **E2E** | Full request/response or full pipeline run | Any user-facing flow (planning pipeline, issue creation, agent dispatch) |

### Agents own all broken tests — not just theirs

If you run the test suite and see a failing test — regardless of whether your change caused it — you are responsible for fixing it before your PR merges. "This was already broken" is not an acceptable response. You have two options:

1. Fix the test (and the underlying code if it reveals a real bug).
2. Open a new blocking issue, link it in your PR, and get explicit sign-off from the user that the PR can merge despite the known failure.

There is no third option. A codebase with known broken tests that everyone steps around becomes unmaintainable. The standard is: when you pick up the code, the tests pass. When you put it down, the tests pass.

---

## Verification Checklist

Before considering work complete, run in this order (mypy first so type fixes don't force a re-run of tests):

> **Dev bind mounts are active.** Your host file edits are instantly visible inside the container — do NOT rebuild for code changes. Only rebuild when `requirements.txt`, `Dockerfile`, or `entrypoint.sh` change.

1. [ ] `docker compose exec agentception mypy agentception/ tests/` — clean, zero errors
2. [ ] `python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0` — passes
3. [ ] Unit tests pass: `docker compose exec agentception pytest tests/unit/ -v`
4. [ ] Integration tests pass: `docker compose exec agentception pytest tests/integration/ -v`
5. [ ] E2E tests pass (if applicable): `docker compose exec agentception pytest tests/e2e/ -v`
6. [ ] Regression test added if this is a bug fix
7. [ ] Zero broken tests in the full suite — fix any you find, not just yours
8. [ ] Affected docs updated
9. [ ] No secrets, no `print()`, no dead code, no `Any`, no bare collections, no `cast()`, no `# type: ignore`
10. [ ] JS/CSS bundles rebuilt if static source changed (`npm run build`)
11. [ ] If API contract changed → handoff prompt produced
