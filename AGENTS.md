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
- **Work directly on `dev` or `main`. Ever.** See Branch Discipline below.

---

## No legacy. No deprecated. No exceptions.

This is the single most repeated rule in this codebase. Every agent must internalize it before writing a single line.

- **Delete on sight.** When you touch a file and find dead code, a deprecated API shape, a backward-compatibility shim, or a legacy fallback — delete it in the same commit. Do not defer it.
- **No fallback paths for old API shapes.** The current shape is the only shape. If a naming convention, field, or endpoint changed, every trace of the old one is deleted — not aliased, not conditionally supported.
- **No "legacy" or "deprecated" annotations.** Code marked `# deprecated` or `# legacy — do not use` should be deleted, not annotated. A comment is not a substitute for deletion.
- **No version shims.** No `if old_format: ... else: new_format`. Enforce the current shape everywhere.
- **No dead constants, dead regexes, dead fields.** A regex that can never match is deleted. A model field never read is deleted. A route never called is deleted.

When you remove something, remove it completely: the implementation, the tests for the old shape, the docs that describe it, and any config that references it.

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

## Branch Discipline — Absolute Rule

**`dev` and `main` are read-only for all agents and all Cursor sessions. Every piece of work — one line or a thousand — happens on a branch or in a worktree.**

### AgentCeption pipeline (worktree)

All agent work runs inside a git worktree created from `origin/dev` at dispatch time. The PR is opened from the worktree branch. The main repo's `dev` branch is never modified by an agent. When the agent finishes, it removes its own worktree.

### Cursor / interactive sessions (feature branch)

Every task follows this complete lifecycle — no step is optional:

1. **Start clean.** Before touching any file, run `git status`. If `dev` is not clean, stop — restore or commit the dirty files before doing anything else.
2. **Branch first.** `git checkout -b fix/<description>` or `git checkout -b feat/<description>` is the **first** command of every task, not an afterthought.
3. **Stage everything before switching.** After any file-generating command (`generate.py`, `npm run build`, code generators, etc.), run `git status` and stage every modified file. Never switch branches while files are dirty — unstaged changes follow you and end up on the wrong branch.
4. **Include all generated outputs in the same commit.** Template source changes and their regenerated outputs (`generate.py` → `.agentception/*.md`) belong in one commit on the feature branch. Never split them across branches.
5. **`.agentception/*.md` are derived artifacts — never edit them directly.** The only correct workflow: edit the `.j2` template in `scripts/gen_prompts/templates/`, run `docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py`, then commit template + regenerated output together in one commit.
6. **Verify locally before opening the PR.** CI does not run on feature → dev PRs — local verification is the gate. Run in this exact order:
   ```bash
   docker compose exec agentception mypy agentception/ tests/                              # zero errors
   docker compose exec agentception python3 tools/typing_audit.py --dirs agentception/ tests/ --max-any 0  # passes
   docker compose exec agentception pytest tests/ -v                                       # all green
   docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py --check  # no drift
   npm run build   # only if .js or .scss files changed
   ```
7. **Open a pull request.** Always create a PR against `dev` — never push directly. Use the `create_pull_request` MCP tool (preferred) or `gh pr create`. Every change, no matter how small, goes through a PR.
8. **Merge the PR immediately.** Use `merge_pull_request` MCP tool (squash merge). Do not wait for CI — it does not run on feature → dev PRs. Do not leave PRs open.
9. **Delete the remote branch.** The `merge_pull_request` MCP tool does this automatically with `deleteBranch: true`; if using `gh`, run `git push origin --delete <branch>`.
10. **Delete the local branch.** `git checkout dev && git branch -D <branch>`.
11. **Pull dev.** `git pull origin dev` — confirm `git status` shows `nothing to commit, working tree clean` before starting the next task.

### Complete task teardown sequence

Run these commands in order at the end of every task:

```bash
# 1. Merge the PR (via MCP tool or gh)
# 2. Return to dev and clean up
git checkout dev
git pull origin dev
git branch -D <feature-branch>           # delete local branch
git push origin --delete <feature-branch> # delete remote branch (if not auto-deleted)
git status                               # must be clean
```

### Enforcement protocol

| Checkpoint | Command | Expected result |
|-----------|---------|-----------------|
| Before creating a branch | `git status` | `nothing to commit, working tree clean` |
| After any file-modifying command | `git status` | Stage or restore every modified file immediately |
| After switching to a branch | `git status` | Only files you intentionally changed are modified |
| Before opening PR | `mypy` + `typing_audit` + `pytest` + `generate.py --check` | All pass locally |
| After task complete | PR created, merged immediately, branch deleted locally and remotely | `git status` on `dev` is clean |

Carrying dirty state from `dev` into a feature branch, then committing only some of the dirty files, is the root cause of every "uncommitted changes on dev" incident. The protocol above prevents it.

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

## GitHub interactions — MCP first

The `user-github` MCP server (officially maintained by GitHub) is available in every Cursor session. **Always prefer MCP tools over `gh` CLI for GitHub operations.** MCP calls are typed, structured, and composable; `gh` is a last resort for operations not yet covered by the server.

| Operation | MCP tool |
|-----------|----------|
| Read an issue | `issue_read` |
| Create / edit an issue | `issue_write` |
| Add an issue comment | `add_issue_comment` |
| List issues | `list_issues` |
| Search issues / PRs | `search_issues`, `search_pull_requests` |
| Read a PR | `pull_request_read` |
| Create a PR | `create_pull_request` |
| Update / merge a PR | `update_pull_request`, `merge_pull_request` |
| Create / submit a review | `pull_request_review_write` |
| List / create branches | `list_branches`, `create_branch` |
| Get current user | `get_me` |
| Search code | `search_code` |

Only fall back to `gh` CLI when the MCP server does not cover the required operation (e.g. `gh worktree`, `gh auth`).

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
  config.py        → Pydantic Settings (env vars)
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

- **Every Python file** must have `from __future__ import annotations` as the first import, immediately after the module docstring (if present). A module docstring may precede it — no other code may. No exceptions.
- **Type everything, 100%.** No untyped function parameters, no untyped return values. Use `list[X]`, `dict[K, V]`, `tuple[A, B]`, `X | None` — never `Optional[X]`, never bare `list` or `dict`.
- **Mypy before tests — always, without exception.** Run `docker compose exec agentception mypy agentception/ tests/` on every Python file you create or modify before running the test suite. Fix all type errors first.
- **Every TypeScript file** (`agentception/static/js/**/*.ts`) must pass `npm run type-check` (`tsc --noEmit`) before committing. See the TypeScript typing rules section below.
- **Convert `.js` → `.ts` for every file you touch.** If you open a JS file to edit it, rename it `.ts` and add types in the same commit.
- **Editing existing files:** Only modify necessary sections. Preserve formatting, structure, and surrounding code.
- **Creating new files:** Write complete, self-contained modules. Include imports, type hints, and docstrings.
- **Before finishing any task:** Confirm types pass (mypy + tsc), tests pass (all four levels), imports resolve, no orphaned code.

### Typing — zero-tolerance rules

This codebase is read and modified by humans and agents alike. Strong, explicit types are the shared contract that makes both effective. These rules have no exceptions.

**Banned — no exceptions:**

| What | Why it's banned | What to use instead |
|------|-----------------|---------------------|
| `Any` | Collapses type safety for all downstream callers | `TypedDict`, `BaseModel`, `Protocol`, a specific union |
| `object` | Effectively `Any` — carries no structural information | The actual type or a constrained union |
| `list` (bare) | Tells nothing about contents | `list[X]` with the concrete element type |
| `dict` (bare) | Same | `dict[K, V]` with concrete key and value types |
| `dict[str, X]` with known keys | Structured data masquerading as dynamic | `TypedDict` or `BaseModel` — if you know the keys, name them |
| `cast(T, x)` | Masks a broken return type upstream | Fix the callee to return `T` correctly |
| `# type: ignore` | A lie in the source — silences a real error | Fix the root cause; use a typed stub for third-party issues |

**The known-keys rule:** `list[X]` and `dict[K, V]` are fine when the collection *is* the abstraction — a homogeneous sequence or a genuine dynamic lookup table. The key question: **do you know the keys at write time?** If yes, use a `TypedDict` or `BaseModel` and name them. If no (any string key is valid), `dict[K, V]` is correct. `dict[str, Any]` or `dict[str, object]` with a known key structure is the highest-signal red flag in the codebase — structured data being treated as unstructured.

**The cast rule deserves emphasis:** if you find yourself writing `cast(SomeType, value)` at a call site, it means the function producing `value` is returning the wrong type. Do not paper over it at the call site. Go upstream, fix the return type, and let the correct type flow down. A cast is always a symptom of a type error elsewhere.

**The `# type: ignore` rule:** there is no valid reason to use it in application code. If mypy flags something, it has found a real problem. The solution is always to fix the type, not to suppress the error. If a third-party library produces an unfixable typing gap, wrap it in a thin, fully-typed adapter and document why.

### Mypy enforcement chain (Python)

| Layer | Command | Threshold |
|-------|---------|-----------|
| Local | `docker compose exec agentception mypy agentception/ tests/` | strict, 0 errors |
| Typing ceiling | `python tools/typing_audit.py --dirs agentception/ tests/ --max-any 0` | blocks commit |
| CI | `python -m mypy agentception/` | blocks PR merge |

### TypeScript — zero-tolerance typing rules

Frontend source files under `agentception/static/js/` are TypeScript (`.ts`). The same discipline that applies to Python with mypy applies here with `tsc`. `tsconfig.json` enforces `strict: true` — every error is real and must be fixed, never silenced.

**Type inference is correct and encouraged for local variables.** Do not add redundant annotations where TypeScript can infer the type exactly:

```typescript
const count = 0;              // ✅ inferred as number — no annotation needed
const names = ['a', 'b'];    // ✅ inferred as string[]
```

**Always annotate explicitly:**
- Every function parameter — inference does not apply to parameters
- Exported function return types
- Variables initialised to `null`, `undefined`, or `[]` / `{}` (inference gives `null`, `never[]`, `{}`)
- Any network or storage boundary (`fetch` response, `localStorage`, `JSON.parse`)
- The `this` context of an Alpine component

**Banned — no exceptions:**

| What | Why banned | Use instead |
|------|------------|-------------|
| `any` | Collapses type safety for all callers | A specific type, `unknown`, or a discriminated union |
| `object` | Structureless — equivalent to `any` | The actual interface or a constrained type |
| `{}` (as a type) | Accepts almost everything | A named `interface` or `type` |
| `Function` | No parameter or return info | An explicit signature `(x: T) => U` |
| Untyped parameters | Cannot be inferred | Always annotate: `fn(x: string): void` |
| `as any` | Silences all downstream errors | Fix the root cause; narrow from `unknown` |
| `// @ts-ignore` | A lie in the source | Fix the error; use `// @ts-expect-error` only with a justification comment |
| `Record<string, unknown>` for known keys | Structured data treated as dynamic | A named `interface` with the known fields |
| Unexplained `!` (non-null assertion) | Masks a potential crash | Add a runtime guard, or document why null is impossible |

**The known-keys rule (mirrors Python):** `Map<K, V>` and `Record<K, V>` are correct when any key is valid at runtime. If you know the keys at write time, define a named `interface`. A `Record<string, unknown>` whose keys are always `"total"`, `"issues"`, and `"batch_id"` is an `interface` waiting to be written.

**Network and storage boundaries:** `JSON.parse` returns `unknown`. `Response.json()` returns `any` in the DOM lib. At every network or storage boundary, assert to a named interface and explain why:

```typescript
const data = await resp.json() as ValidateResponse;   // server contract
```

For SSE streams, use a typed parser (e.g. `parseSseEvent<T>`) that validates the discriminator field before asserting the union type. `as T` inside business logic is always a red flag — fix the upstream function instead.

**TypeScript enforcement chain:**

| Layer | Command | Threshold |
|-------|---------|-----------|
| Local | `npm run type-check` | `tsc --noEmit`, 0 errors |
| Build | `npm run build:js` | esbuild bundles (no type checking) |

Note: esbuild does **not** type-check. `npm run type-check` is the only gate — always run it before committing any `.ts` change.

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

**Run locally before opening a PR — in this exact order.** CI does not run on feature → dev PRs; this checklist is the gate.

> **Dev bind mounts are active.** Your host file edits are instantly visible inside the container — do NOT rebuild for code changes. Only rebuild when `requirements.txt`, `Dockerfile`, or `entrypoint.sh` change.

0. [ ] Confirm you are on a feature branch or inside a worktree — **never on `dev` or `main`**
1. [ ] `docker compose exec agentception mypy agentception/ tests/` — clean, zero errors
2. [ ] `docker compose exec agentception python3 tools/typing_audit.py --dirs agentception/ tests/ --max-any 0` — passes
3. [ ] `docker compose exec agentception pytest tests/ -v` — all green (unit + integration + regression)
4. [ ] Regression test added if this is a bug fix (named `test_<what_broke>_<fixed_behavior>`)
5. [ ] `docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py --check` — no drift (run without `--check` first if you edited `.j2` templates, then re-run with `--check`)
6. [ ] Zero broken tests — fix any you find, not just yours
7. [ ] Affected docs updated in the same commit
8. [ ] No secrets, no `print()`, no dead code, no `Any`, no bare collections, no `cast()`, no `# type: ignore` (Python)
8a. [ ] No `any`, `object`, `{}`, untyped parameters, `as any`, or `// @ts-ignore` (TypeScript)
8b. [ ] No legacy, no deprecated, no shims — if you touched a file with dead patterns, they are deleted in this PR
9. [ ] If any `.ts` files changed: `npm run type-check` passes (zero errors), then `npm run build:js`
9a. [ ] If any `.js` source files were touched: rename to `.ts` and add types in this same commit
9b. [ ] If any `.scss` files changed: `npm run build:css`
10. [ ] If API contract changed → handoff prompt produced
11. [ ] **Open PR and merge immediately** — do not wait for CI (it does not run on dev PRs)
