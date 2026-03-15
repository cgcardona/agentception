# PlanSpec — YAML Contract

> **Version:** 1.0  
> **Status:** Production  
> **Owner:** AgentCeption — Plan step (Step 1.A / 1.B)

---

## What is PlanSpec?

PlanSpec is the structured YAML document that bridges a user's free-form plan description and the GitHub issues that get created from it. It is the **single source of truth** for a planning run — everything downstream (validation, the CodeMirror editor, the coordinator agent, GitHub issue creation) reads from this contract.

### The lifecycle

```
User brain dump (free text)
        │
        ▼  POST /api/plan/preview
   Claude (via Anthropic API directly)
        │  returns PlanSpec YAML
        ▼
  CodeMirror editor  ←── user reviews and edits
        │
        ▼  POST /api/plan/launch
   Coordinator agent (MCP client, e.g. Cursor)
        │  creates GitHub issues from each PlanIssue
        ▼
  GitHub Issues tagged by phase label
```

MCP is **not** involved in plan generation. Claude is called directly from AgentCeption's backend via the Anthropic API. MCP only enters after the user clicks Launch, when a coordinator agent reads the `ac://plan/labels` resource and uses mutation tools to file issues.

---

## Schema

The canonical Pydantic definition lives in `agentception/models.py`. This document is the human-readable version of that source of truth.

### Top level

```yaml
initiative: <slug>      # required
phases:                 # required, non-empty list
  - ...
```

| Field | Type | Required | Description |
|---|---|---|---|
| `initiative` | `str` | ✅ | Short kebab-case slug identifying the batch of work. Inferred by Claude from the dominant theme of the plan (e.g. `auth-rewrite`, `health-ratelimit-dashboard`). Used as a prefix for issue IDs and as the GitHub initiative label. |
| `phases` | `list[PlanPhase]` | ✅ | Ordered list of phases. Must contain at least one phase. |

---

### PlanPhase

```yaml
phases:
  - label: phase-0          # required
    description: "..."      # required
    depends_on: []          # required (can be empty)
    issues:                 # required, non-empty list
      - ...
```

| Field | Type | Required | Description |
|---|---|---|---|
| `label` | `str` | ✅ | Phase identifier. **Must be one of:** `phase-0`, `phase-1`, `phase-2`, `phase-3`. No custom labels. |
| `description` | `str` | ✅ | One sentence describing the theme and gate criterion for this phase. Written for humans reviewing the plan, not for the coordinator. |
| `depends_on` | `list[str]` | ✅ | Phase labels that must complete before this phase can begin. Must reference labels that appear **earlier** in the list (no forward references, no cycles). Empty list means this phase has no dependencies. |
| `issues` | `list[PlanIssue]` | ✅ | Issues to create in this phase. Must contain at least one. |

#### Phase label semantics

| Label | Intent |
|---|---|
| `phase-0` | Foundations and critical fixes — work everything else depends on |
| `phase-1` | Infrastructure and core services — internal plumbing features need |
| `phase-2` | Features and user-facing work — new capabilities visible to users |
| `phase-3` | Polish, tests, and debt — tests, docs, refactors, cleanup |

Only emit phases that have work. Unused phase labels are omitted entirely.

---

### PlanIssue

```yaml
issues:
  - id: health-ratelimit-dashboard-p1-001   # required
    title: "..."                             # required
    body: |                                  # required
      ...
    depends_on: []                           # required (can be empty)
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | `str` | ✅ | Stable kebab-case slug. See [ID format](#id-format) below. Used as the dependency reference key — survives title edits in the editor. |
| `title` | `str` | ✅ | GitHub issue title. Imperative mood, specific, standalone. Good: `"Fix intermittent 503 on mobile login"`. Bad: `"Mobile improvements"`. |
| `body` | `str` | ✅ | GitHub issue body (Markdown). 2–4 sentences: context, what to implement, done criteria. Written directly to the implementing engineer. |
| `depends_on` | `list[str]` | ✅ | **Issue IDs** (not titles) this issue must wait for. IDs must exist elsewhere in the plan. No self-references. Empty list means no issue-level dependencies. |

#### ID format

```
{initiative}-p{phase_number}-{sequence}
```

Examples:
- `health-ratelimit-dashboard-p0-001`
- `health-ratelimit-dashboard-p1-001`
- `health-ratelimit-dashboard-p1-002`  ← second issue in phase-1

Rules:
- Must be unique across the **entire plan** (not just within a phase)
- Sequence is zero-padded to 3 digits
- Must use the same slug as the `initiative` field
- Immutable once set — `depends_on` references this ID, not the title

#### Body guidelines

A well-formed body has three parts:

1. **Context** (1 sentence) — why this matters / what is broken
2. **Implementation** (1–2 sentences) — what specifically to build or fix
3. **Done criteria** (1 sentence) — verifiable acceptance criteria

Example:
```
The /api/v1/health endpoint crashes with a 500 in production because it reads from
Qdrant before the connection is established at startup. Defer the Qdrant readiness
check until after the connection lifecycle is complete, or guard the health handler
with a lazy-initialized readiness flag. Done when /api/v1/health returns 200 under
normal startup and 503 (never 500) when Qdrant is unavailable.
```

---

## Invariants (enforced by PlanSpec validators)

These are checked server-side by Pydantic before the YAML is accepted. The CodeMirror editor runs the same checks via `POST /api/plan/validate` on every keystroke.

| Rule | Error if violated |
|---|---|
| At least one phase | `PlanSpec must contain at least one phase` |
| Every phase has at least one issue | `phases must each contain at least one issue` |
| All issue IDs are unique across the plan | `Duplicate issue id '...'` |
| Phase `depends_on` only references earlier labels | `Phase 'phase-1' depends_on 'phase-2' which is not a previously defined phase label` |
| Issue `depends_on` only references known IDs | `Issue '...' depends_on '...' which is not a known issue id in this plan` |
| No self-referencing issue dependencies | `Issue '...' cannot depend on itself` |

---

## Complete annotated example

```yaml
initiative: health-ratelimit-dashboard

phases:

  # phase-0: no dependencies — must land before anything else
  - label: phase-0
    description: Fix the crashing health endpoint so the service is stable enough to build on.
    depends_on: []
    issues:
      - id: health-ratelimit-dashboard-p0-001
        title: Fix /api/v1/health 500 crash caused by premature Qdrant read at startup
        body: |
          The /api/v1/health endpoint crashes with a 500 in production because it reads
          from Qdrant before the connection is established during startup. Defer the Qdrant
          readiness check until after the connection lifecycle is complete, or guard the
          health handler with a lazy-initialized readiness flag. Done when /api/v1/health
          returns 200 under normal startup and 503 (never 500) when Qdrant is unavailable.
        depends_on: []

  # phase-1: waits for phase-0; has an internal dependency between its own issues
  - label: phase-1
    description: Build the shared counter store and wire it into per-IP rate-limiting middleware.
    depends_on: [phase-0]
    issues:
      - id: health-ratelimit-dashboard-p1-001
        title: Add shared counter store for sliding-window rate-limit state
        body: |
          The rate limiter and the admin dashboard both need a shared, durable counter store.
          Implement a counter store (Redis-backed or in-process with the same interface) that
          supports increment-and-expire keyed by (ip, endpoint) with a 60-second sliding window
          and atomic reads for dashboard aggregation. Done when the store has a documented
          interface, passes unit tests for expiry and atomic increment, and is injectable as a dependency.
        depends_on: []     # no issue-level dep — this is the foundation for p1-002

      - id: health-ratelimit-dashboard-p1-002
        title: Wire per-IP rate-limiting middleware (100 req/min) onto all public API endpoints
        body: |
          With the counter store in place, add an HTTP middleware layer that reads the
          requesting IP, increments the sliding-window counter, and returns 429 with a
          Retry-After header when the count exceeds 100 requests per minute. Apply to every
          public API route; exempt internal and health endpoints. Done when load tests confirm
          429 is returned on the 101st request within a 60-second window and legitimate traffic passes through.
        depends_on: [health-ratelimit-dashboard-p1-001]   # ← issue-level dep by ID

  # phase-2: waits for phase-1; no internal issue dependencies
  - label: phase-2
    description: Deliver the admin dashboard surfacing per-endpoint request counts over the last 7 days.
    depends_on: [phase-1]
    issues:
      - id: health-ratelimit-dashboard-p2-001
        title: Build admin dashboard showing per-endpoint request counts (7-day)
        body: |
          Operators need visibility into API traffic patterns; the counter store now holds the
          data to power this view. Build an admin-only dashboard page that queries the counter
          store (or a roll-up derived from it) and renders request counts per endpoint for each
          of the last 7 days. Done when the page loads without error, displays accurate per-endpoint
          daily counts, and returns 401/403 for unauthenticated requests.
        depends_on: []
```

---

## What happens after Launch

When the user clicks **Launch**, AgentCeption:

1. Validates the edited YAML one final time against PlanSpec.
2. Writes a coordinator `DB context row containing the full PlanSpec.
3. Creates an isolated git worktree for the coordinator agent.

The **coordinator agent** (running in Cursor via MCP) then:

1. Reads the `ac://plan/labels` resource to fetch the phase label configuration.
2. Iterates phases in order. For each phase:
   - Creates all GitHub issues in that phase from the PlanIssue data.
   - Applies the phase label and sets `depends_on` metadata.
   - Waits for all issues in the phase to be closed/merged before advancing.
3. Within a phase, issues with no `depends_on` can be worked in parallel by separate agents in isolated worktrees.
4. Issue-level `depends_on` constrains the order within a phase — the coordinator will not assign a dependent issue until its dependency is merged.

> **Note:** The current implementation creates issues phase-by-phase (phase-N issues are only created after phase-(N-1) completes). See [#933](https://github.com/cgcardona/agentception/issues/933) for the planned migration to create all issues at launch and gate execution by label instead.

---

## Source of truth locations

| Artifact | Location |
|---|---|
| Pydantic models | `agentception/models.py` — `PlanIssue`, `PlanPhase`, `PlanSpec` |
| LLM system prompt | `agentception/readers/llm_phase_planner.py` — `_YAML_SYSTEM_PROMPT` |
| Stream endpoint | `agentception/routes/ui/plan_ui.py` — `POST /api/plan/preview` |
| Validate endpoint | `agentception/routes/ui/plan_ui.py` — `POST /api/plan/validate` |
| GitHub context pack | `agentception/readers/context_pack.py` |
| Frontend editor | `agentception/static/js/plan.js` — `planForm()` |

---

## Anti-patterns

These are things the LLM system prompt explicitly forbids, and the validator will reject:

- Using the initiative slug as the top-level YAML key instead of `initiative: <slug>`
- Emitting a phase with no issues
- Using phase labels other than `phase-0` through `phase-3`
- Referencing issue titles in `depends_on` instead of issue IDs
- Duplicate issue IDs within the plan
- Forward references in phase `depends_on` (phase-0 cannot depend on phase-1)
- Markdown code fences wrapping the YAML output
- Inventing tasks the user did not describe
- Duplicating issues that already exist in the repository (context pack guards against this)
