# AgentCeption — Agent Tree Protocol

This document is the canonical specification for the agent hierarchy.
It governs how agents are scoped, what they read from GitHub, and what
children they may spawn. Every component that creates, dispatches, or
briefs agents must conform to this spec.

---

## Two agent types

AgentCeption uses a multi-way tree structure. In CS terms the tree has
**nodes** and **leaves**; in our domain those map to **coordinators** and
**workers**:

| Domain term | CS equivalent | Behaviour | May spawn |
|-------------|--------------|-----------|-----------|
| **Coordinator** | node | Surveys a scope (issues, PRs, or sub-label), dispatches children, loops until queue drains, produces no direct artifacts | Other coordinators **or** workers |
| **Worker** | leaf | Claims a single unit of work (one issue or one PR), executes it, may chain-spawn one downstream worker before reporting done | One downstream worker only — never a coordinator |

Coordinator examples: `ceo`, `cto`, `engineering-coordinator`, `qa-coordinator`.  
Worker examples: `python-developer`, `frontend-developer`, `pr-reviewer`.

---

## The tree

```
[ceo]  ← example root; any coordinator can be root — tree is pruned at the entry point
 └── cto  (coordinator)
      ├── engineering-coordinator  (coordinator)
      │    └── engineer            (worker — one issue)
      │         └── pr-reviewer   (worker — chain-spawned after engineer opens PR)
      └── qa-coordinator           (coordinator — cleanup sweep only)
           └── pr-reviewer         (worker — one unreviewed PR)
```

`pr-reviewer` reaches the tree in **two independent paths**:

1. **Engineer chain-spawn** — after an engineer opens its PR it immediately
   spawns a `pr-reviewer` worker before exiting.  The reviewer's `PARENT_RUN_ID`
   is the engineer's `RUN_ID` and its `ORG_DOMAIN` is `qa`.  No physical QA
   coordinator needs to be running for the reviewer to be placed correctly in
   the hierarchy — `ORG_DOMAIN` alone is enough for the dashboard.
2. **QA coordinator sweep** — the CTO spawns a `qa-coordinator` when issues
   are exhausted but stale unreviewed PRs remain.  The QA coordinator spawns
   one `pr-reviewer` worker per unreviewed PR.

**Chain-spawn constraint:** engineers must never spawn a QA coordinator; they
spawn only the single `pr-reviewer` worker that covers their own PR.  A
concurrent QA coordinator launched while issues remain would race against
chain-spawned reviewers and find no additional PRs to cover.

---

## Tiers

Tiers are the runtime execution label written into every `.agent-task` file.
They map exactly onto the two agent types: both carry either `coordinator`
or `worker`. The `scope_type` field and the agent's `role` distinguish what
kind of work a worker performs — the tier does not.

| Tier | Agent type | Role examples | GitHub scope | Can spawn |
|------|-----------|--------------|--------------|-----------|
| `coordinator` | coordinator | `ceo`, `cto`, `engineering-coordinator`, `qa-coordinator` | issues and/or PRs filtered to `scope_value` (exact scope determined by role) | any coordinator or worker |
| `worker` | worker | `python-developer`, `pr-reviewer`, `devops-engineer`, … | **one issue** or **one PR** (`scope_value` = number; `scope_type` tells the worker which) | one downstream worker only (chain-spawn) |

### Root coordinator spawn loop

Applies to any coordinator whose role surveys **both** the issue queue and the
PR queue (e.g. `cto`, `ceo`). This is not a one-shot decision — it is a loop
that repeats after each wave of children completes, until both queues drain.

```
loop:
  issues ← list_issues(label=scope_value, state="open")
            filtered: exclude "agent:wip" (claimed), "blocked" (phase-gated),
                      "ticket-blocked" (unresolved dependencies)
  prs    ← list_pull_requests(state="open", unreviewed=true)

  issues > 0:
    spawn 1 engineering-coordinator          # covers all eligible issues
    # implementing workers chain-spawn their own reviewing workers immediately
    # after opening each PR — no qa-coordinator needed while issues remain
    wait → loop

  issues == 0, prs > 0:
    spawn 1 qa-coordinator                   # cleanup sweep only
    # covers PRs whose implementing worker crashed before chain-spawning a reviewer
    wait → loop

  issues == 0, prs == 0:
    exit                                     # both queues fully drained
```

**Key invariant:** a `qa-coordinator` is **never** spawned while issues remain.
Implementing workers chain-spawn their reviewing workers immediately after
opening a PR. A concurrent `qa-coordinator` would race against those
chain-spawned reviewers and attempt to claim PRs that are already covered,
producing duplicate review runs.

### Worker execution loop

Applies to any worker (`scope_type` = `issue` or `pr`). Workers do not survey
a queue — they are dispatched with exactly one unit of work already assigned
in the `.agent-task` file. The "loop" is a single pass followed by an
optional chain-spawn.

```
enter:
  claim scope_value                            # mark issue/PR as "agent:wip"

  scope_type == "issue":
    read issue, check out branch, implement
    open PR
    chain-spawn 1 reviewing worker             # immediate — before exiting
    exit

  scope_type == "pr":
    read PR diff, write review, approve or request-changes
    # no chain-spawn — reviewing worker is always a leaf
    exit
```

**Key invariant:** an implementing worker (`scope_type=issue`) **always**
chain-spawns a reviewing worker before it exits — even if it believes the
PR is trivial. The reviewing worker is the only agent authorised to call the
PR approved. If the implementing worker crashes after opening the PR but
before chain-spawning, the parent `qa-coordinator` sweep picks up the orphaned
PR on its next loop iteration.

---

## `.agent-task` file format

Every dispatched agent receives an `.agent-task` file in its working
directory. This is the agent's complete briefing — no other file is
strictly required to start.

```toml
# ── Identity ──────────────────────────────────────────────────────────────────
RUN_ID        = "label-AC-UI-0-CRITICAL-BUGS-20260303T200000Z-a1b2"
ROLE          = "cto"
TIER          = "coordinator"      # behavioral tier: coordinator|worker
ORG_DOMAIN    = "c-suite"          # UI hierarchy slot: c-suite|engineering|qa

# ── Scope ─────────────────────────────────────────────────────────────────────
# SCOPE_TYPE  label   → coordinator; scope_value is a GitHub label string
# SCOPE_TYPE  issue   → implementing worker; scope_value is the issue number (string)
# SCOPE_TYPE  pr      → reviewing worker; scope_value is the PR number (string)
SCOPE_TYPE    = "label"
SCOPE_VALUE   = "AC-UI/0-CRITICAL-BUGS"

# ── Provenance ────────────────────────────────────────────────────────────────
GH_REPO       = "cgcardona/agentception"
BRANCH        = ""                  # empty for coordinator tiers
WORKTREE      = "$HOME/.agentception/worktrees/agentception/label-AC-UI-0-..."
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = ""                  # empty for root; set by spawner for all other tiers

# ── Callbacks ─────────────────────────────────────────────────────────────────
AC_URL        = "http://localhost:10003"
# ROLE_FILE is the container-side path; HOST_ROLE_FILE is the host-side path.
# Cursor agents running on the developer's machine should use HOST_ROLE_FILE.
ROLE_FILE     = "<container-repo-root>/.agentception/roles/cto.md"
HOST_ROLE_FILE = "<host-repo-root>/.agentception/roles/cto.md"
```

### Worker engineer example

```toml
RUN_ID        = "issue-42-20260303T200100Z-c3d4"
ROLE          = "python-developer"
TIER          = "worker"
ORG_DOMAIN    = "engineering"
SCOPE_TYPE    = "issue"
SCOPE_VALUE   = "42"
GH_REPO       = "cgcardona/agentception"
BRANCH        = "feat/issue-42"
WORKTREE      = "$HOME/.agentception/worktrees/agentception/issue-42"
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = "label-AC-UI-0-CRITICAL-BUGS-20260303T200000Z-a1b2"
AC_URL        = "http://localhost:10003"
ROLE_FILE     = "<container-repo-root>/.agentception/roles/python-developer.md"
HOST_ROLE_FILE = "<host-repo-root>/.agentception/roles/python-developer.md"
```

### Chain-spawned reviewer example (written by the engineer, not the CTO)

```toml
RUN_ID        = "pr-99-20260303T200200Z-e5f6"
ROLE          = "pr-reviewer"
TIER          = "worker"
ORG_DOMAIN    = "qa"               # dashboard places this node under the QA column
SCOPE_TYPE    = "pr"
SCOPE_VALUE   = "99"
GH_REPO       = "cgcardona/agentception"
BRANCH        = ""
WORKTREE      = "$HOME/.agentception/worktrees/agentception/pr-99"
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = "issue-42-20260303T200100Z-c3d4"  # ← worker that spawned this reviewer
AC_URL        = "http://localhost:10003"
ROLE_FILE     = "<container-repo-root>/.agentception/roles/pr-reviewer.md"
HOST_ROLE_FILE = "<host-repo-root>/.agentception/roles/pr-reviewer.md"
```

> **`TIER` vs `ORG_DOMAIN`:** `TIER` is the behavioral execution tier —
> `coordinator` surveys and spawns; `worker` claims one unit of work and executes it.
> The worker's *kind* of work (issue vs PR) is carried by `SCOPE_TYPE` and `ROLE`, not `TIER`.
> `ORG_DOMAIN` is the organisational slot for the UI hierarchy (c-suite, engineering, qa).
> A chain-spawned PR reviewer has `TIER=worker` and `ORG_DOMAIN=qa` even though its
> `PARENT_RUN_ID` points to an implementing worker, so the dashboard nests it under the QA
> column without requiring a physical QA coordinator to be running.

> `<repo-root>` is the absolute path to the cloned repository on the host
> machine — the value of `REPO_DIR` (defaults to `/app` inside the
> container). AgentCeption writes the real absolute path when it creates each
> `.agent-task` file. Never derive this from `pwd` or `basename`.

---

## What each tier reads from GitHub

All GitHub queries use the `user-agentception` MCP server tools. Never use
the `gh` CLI — it is not available inside Cursor agents.

### `root` (CTO)

```
# Open issues for the scope label
github_list_issues(label="$SCOPE_VALUE", state="open")

# All open PRs against dev
github_list_prs(state="open")
```

Decides based on queue state (see "CTO spawn decision" above).
Loops until both queues are empty.

### `engineering-coordinator`

```
# Open issues for the scope label
github_list_issues(label="$SCOPE_VALUE", state="open")
# → filter: exclude issues labelled "agent:wip" (claimed) or "blocked" (phase-gated)
# Only work on issues that have neither label.
```

Spawns one `engineer` Task per eligible issue, up to 3 concurrently.
Each engineer self-replaces (spawns its successor before exiting).

### `qa-coordinator` (cleanup sweep only)

```
# Open PRs against dev (all are in scope — QA reviews everything)
github_list_prs(state="open")
```

Spawns one `pr-reviewer` worker Task per unreviewed PR, up to 3 concurrently.
Only spawned by the CTO when `ISSUES == 0` and stale unreviewed PRs remain.

### `worker` — implementing (scope_type=issue)

```
# Read the single assigned issue
github_get_issue(number=$SCOPE_VALUE)
```

Implements the issue, opens a PR, then **immediately chain-spawns a
`pr-reviewer` worker Task** before calling `report/done`. The reviewer's
`PARENT_RUN_ID` is set to this worker's `RUN_ID`, `TIER` is `worker`, and
`ORG_DOMAIN` is `qa`. This makes the reviewer a logical child of the
implementing worker in the hierarchy even though no physical QA coordinator is
running.

### `worker` — reviewing (scope_type=pr)

```
# Read the single assigned PR
github_get_pr(number=$SCOPE_VALUE)
```

Reviews, requests changes or approves+merges, calls `report/done`, exits.
Spawned by **either** an implementing worker (chain-spawn) **or** a
`qa-coordinator` (cleanup sweep) — the reviewing worker behaves identically in
both cases. The `role` field (`pr-reviewer`) and `scope_type=pr` fully identify
it; no separate tier value is needed.

---

## Spawning rules

- **Spawn all child Tasks simultaneously in a single message** — there is no concurrency limit.
- **Always `subagent_type="generalPurpose"`** — never `shell`. Only
  `generalPurpose` agents have access to the Task tool.
- **Claim before spawning**: coordinator tiers call
  `POST /api/runs/{run_id}/acknowledge` for each child run_id before
  spawning its Task, preventing double-dispatch.
- **PARENT_RUN_ID propagation**: every child task receives its physical
  spawner's `RUN_ID`. For chain-spawned reviewing workers this is the
  implementing worker's `RUN_ID`, not the coordinator's.
- **ORG_DOMAIN propagation**: every child task should include an
  `ORG_DOMAIN` field (c-suite | engineering | qa). The UI uses this
  field to place workers and coordinators in the hierarchy without requiring a
  physical parent coordinator to be running. Chain-spawned reviewing workers
  must pass `org_domain="qa"`.
- **No concurrent QA coordinator when issues remain**: the CTO must never
  spawn a QA coordinator when `ISSUES > 0`. Implementing workers chain-spawn
  their own reviewing workers; a concurrent QA coordinator would race against
  them and find no PRs to review.

---

## Reporting callbacks

All tiers report progress via MCP tools (never HTTP directly):

```
build_report_step(issue_number, step_name, agent_run_id?)
build_report_blocker(issue_number, description, agent_run_id?)
build_report_decision(issue_number, decision, rationale, agent_run_id?)
build_report_done(issue_number, pr_url, summary?, agent_run_id?)  ← worker tiers only
```

Coordinator tiers call `build_report_step` at each phase of their loop.
They do NOT call `build_report_done` — they exit naturally after their queue drains.

---

## Tier → Role mapping (for the dispatch UI)

| Tier | Agent type | Selectable roles | Spawned by |
|------|-----------|-----------------|------------|
| `coordinator` | coordinator | `ceo` | dispatcher (AgentCeption UI / MCP) |
| `coordinator` | coordinator | `cto` | dispatcher, or CEO coordinator when present |
| `coordinator` | coordinator | `engineering-coordinator` | CTO (always when issues > 0) |
| `coordinator` | coordinator | `qa-coordinator` | CTO (cleanup sweep only — issues == 0, PRs > 0) |
| `worker` | worker | `python-developer`, `frontend-developer`, `typescript-developer`, `react-developer`, `go-developer`, `rust-developer`, `api-developer`, `devops-engineer`, `data-engineer`, `site-reliability-engineer`, `security-engineer`, `mobile-developer`, `ios-developer`, `android-developer`, `full-stack-developer`, `architect`, `technical-writer`, `test-engineer`, `ml-engineer`, `systems-programmer`, `rails-developer`, `database-architect` | engineering-coordinator |
| `reviewer` | worker | `pr-reviewer` | engineer worker (chain-spawn after PR open) **or** qa-coordinator (cleanup sweep) |
