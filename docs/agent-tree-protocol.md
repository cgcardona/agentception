# AgentCeption — Agent Tree Protocol

This document is the canonical specification for the agent hierarchy.
It governs how agents are scoped, what they read from GitHub, and what
children they may spawn. Every component that creates, dispatches, or
briefs agents must conform to this spec.

---

## The tree

```
root (CTO)
 └── engineering-coordinator
      └── engineer  (leaf — one issue)
           └── pr-reviewer  (leaf — chain-spawned by engineer after opening PR)
```

**Chain-spawn model:** engineers open their PR and then immediately spawn their
own reviewer before exiting. There is no concurrent QA coordinator when issues
remain — that was a race condition (the QA coordinator ran before PRs existed,
found nothing, and exited). The QA coordinator is only spawned by the CTO as a
cleanup sweep when all issues are closed but stale unreviewed PRs remain.

Any node can be the entry point. When you launch at `coordinator`,
there is no CTO above it — you prune the tree at that node.

---

## Tiers

| Tier | Role examples | GitHub scope | Can spawn |
|------|--------------|--------------|-----------|
| `root` | `cto` | issues **and** PRs filtered to `SCOPE_VALUE` | Eng coordinator (always); QA coordinator (cleanup only — see below) |
| `coordinator` | `engineering-coordinator` | **issues only** filtered to `SCOPE_VALUE` | any engineering leaf role |
| `coordinator` | `qa-coordinator` | **PRs only** — cleanup sweep when issues = 0 | `pr-reviewer` |
| `engineer` | `python-developer`, `frontend-developer`, `devops-engineer`, … | **one issue** (`SCOPE_VALUE` = issue number) | `pr-reviewer` (immediately after opening PR) |
| `reviewer` | `pr-reviewer` | **one PR** (`SCOPE_VALUE` = PR number) | nothing |

### CTO spawn decision

```
ISSUES > 0  → spawn 1 engineering-coordinator, 0 QA coordinators
              (engineers chain-spawn their own reviewers)
ISSUES == 0
  PRs > 0   → spawn 0 engineering-coordinators, 1 QA coordinator
              (cleanup sweep — handles PRs whose reviewer crashed)
  PRs == 0  → done, exit
```

---

## `.agent-task` file format

Every dispatched agent receives an `.agent-task` file in its working
directory. This is the agent's complete briefing — no other file is
strictly required to start.

```toml
# ── Identity ──────────────────────────────────────────────────────────────────
RUN_ID        = "label-AC-UI-0-CRITICAL-BUGS-20260303T200000Z-a1b2"
ROLE          = "cto"
TIER          = "executive"        # behavioral tier: executive|coordinator|engineer|reviewer
ORG_DOMAIN    = "c-suite"          # UI hierarchy slot: c-suite|engineering|qa

# ── Scope ─────────────────────────────────────────────────────────────────────
# SCOPE_TYPE  label   → executive/coordinator tiers; SCOPE_VALUE is a GitHub label string
# SCOPE_TYPE  issue   → engineer leaf; SCOPE_VALUE is the issue number (string)
# SCOPE_TYPE  pr      → reviewer leaf; SCOPE_VALUE is the PR number (string)
SCOPE_TYPE    = "label"
SCOPE_VALUE   = "AC-UI/0-CRITICAL-BUGS"

# ── Provenance ────────────────────────────────────────────────────────────────
GH_REPO       = "cgcardona/agentception"
BRANCH        = ""                  # empty for executive/coordinator tiers
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

### Leaf engineer example

```toml
RUN_ID        = "issue-42-20260303T200100Z-c3d4"
ROLE          = "python-developer"
TIER          = "engineer"
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
TIER          = "reviewer"
ORG_DOMAIN    = "qa"               # dashboard places this node under the QA column
SCOPE_TYPE    = "pr"
SCOPE_VALUE   = "99"
GH_REPO       = "cgcardona/agentception"
BRANCH        = ""
WORKTREE      = "$HOME/.agentception/worktrees/agentception/pr-99"
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = "issue-42-20260303T200100Z-c3d4"  # ← engineer that spawned this reviewer
AC_URL        = "http://localhost:10003"
ROLE_FILE     = "<container-repo-root>/.agentception/roles/pr-reviewer.md"
HOST_ROLE_FILE = "<host-repo-root>/.agentception/roles/pr-reviewer.md"
```

> **`TIER` vs `ORG_DOMAIN`:** `TIER` is the behavioral execution tier —
> it tells the agent what kind of work it does (survey+spawn vs implement vs review).
> `ORG_DOMAIN` is the organisational slot for the UI hierarchy (c-suite, engineering, qa).
> A chain-spawned PR reviewer has `TIER=reviewer` and `ORG_DOMAIN=qa` even though its
> `PARENT_RUN_ID` points to an engineer, so the dashboard nests it under the QA column
> without requiring a
> physical QA Coordinator node to be running.

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

Spawns one `reviewer` Task per unreviewed PR, up to 3 concurrently.
Only spawned by the CTO when `ISSUES == 0` and stale unreviewed PRs remain.

### `engineer` (leaf)

```
# Read the single assigned issue
github_get_issue(number=$SCOPE_VALUE)
```

Implements the issue, opens a PR, then **immediately chain-spawns a
`pr-reviewer` Task** (Step 6 in the engineering-coordinator role) before
calling `report/done`. The reviewer's `PARENT_RUN_ID` is set to this
engineer's `RUN_ID`, `TIER` is set to `reviewer`, and `ORG_DOMAIN` to `qa`.

### `reviewer` (leaf)

```
# Read the single assigned PR
github_get_pr(number=$SCOPE_VALUE)
```

Reviews, requests changes or approves+merges, calls `report/done`, exits.

---

## Spawning rules

- **Spawn all child Tasks simultaneously in a single message** — there is no concurrency limit.
- **Always `subagent_type="generalPurpose"`** — never `shell`. Only
  `generalPurpose` agents have access to the Task tool.
- **Claim before spawning**: manager tiers call
  `POST /api/runs/{run_id}/acknowledge` for each child run_id before
  spawning its Task, preventing double-dispatch.
- **PARENT_RUN_ID propagation**: every child task receives its physical
  spawner's `RUN_ID`. For chain-spawned reviewers this is the engineer's
  `RUN_ID`, not the coordinator's.
- **ORG_DOMAIN propagation**: every child task should include an
  `ORG_DOMAIN` field (c-suite | engineering | qa). The UI uses this
  field to place nodes in the hierarchy without requiring a physical parent
  node to be running. Chain-spawned reviewers must pass `org_domain="qa"`.
- **No concurrent QA coordinator when issues remain**: the CTO must never
  spawn a QA coordinator when `ISSUES > 0`. Engineers chain-spawn their own
  reviewers; a concurrent QA coordinator would race against them and find
  no PRs to review.

---

## Reporting callbacks

All tiers report progress via MCP tools (never HTTP directly):

```
build_report_step(issue_number, step_name, agent_run_id?)
build_report_blocker(issue_number, description, agent_run_id?)
build_report_decision(issue_number, decision, rationale, agent_run_id?)
build_report_done(issue_number, pr_url, summary?, agent_run_id?)  ← leaf tiers only
```

Manager tiers call `build_report_step` at each phase of their loop.
They do NOT call `build_report_done` — they exit naturally after their queue drains.

---

## Tier → Role mapping (for the dispatch UI)

| Tier | Selectable roles | Spawned by |
|------|-----------------|------------|
| `root` | `cto` | dispatcher (AgentCeption UI / MCP) |
| `coordinator` | `engineering-coordinator` | CTO (always when issues > 0) |
| `coordinator` | `qa-coordinator` | CTO (cleanup sweep only — issues == 0, PRs > 0) |
| `engineer` | `python-developer`, `frontend-developer`, `typescript-developer`, `react-developer`, `go-developer`, `rust-developer`, `api-developer`, `devops-engineer`, `data-engineer`, `site-reliability-engineer`, `security-engineer`, `mobile-developer`, `ios-developer`, `android-developer`, `full-stack-developer`, `architect`, `technical-writer`, `test-engineer`, `ml-engineer`, `systems-programmer`, `rails-developer`, `database-architect` | engineering-coordinator |
| `reviewer` | `pr-reviewer` | engineer (chain-spawn after PR open) or qa-coordinator (cleanup) |
