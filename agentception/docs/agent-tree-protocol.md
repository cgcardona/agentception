# AgentCeption — Agent Tree Protocol

This document is the canonical specification for the agent hierarchy.
It governs how agents are scoped, what they read from GitHub, and what
children they may spawn. Every component that creates, dispatches, or
briefs agents must conform to this spec.

---

## The tree

```
root (CTO)
 ├── coordinator
 │    └── engineer  (leaf — one issue)
 └── coordinator
      └── reviewer  (leaf — one PR)
```

Any node can be the entry point. When you launch at `coordinator`,
there is no CTO above it — you prune the tree at that node.

---

## Tiers

| Tier | Role examples | GitHub scope | Can spawn |
|------|--------------|--------------|-----------|
| `root` | `cto` | issues **and** PRs filtered to `SCOPE_VALUE` | `coordinator`, `coordinator` |
| `coordinator` | `engineering-coordinator` | **issues only** filtered to `SCOPE_VALUE` | any engineering leaf role |
| `coordinator` | `qa-coordinator` | **PRs only** filtered to `SCOPE_VALUE` | `pr-reviewer` |
| `engineer` | `python-developer`, `frontend-developer`, `devops-engineer`, … | **one issue** (`SCOPE_VALUE` = issue number) | nothing |
| `reviewer` | `pr-reviewer` | **one PR** (`SCOPE_VALUE` = PR number) | nothing |

---

## `.agent-task` file format

Every dispatched agent receives an `.agent-task` file in its working
directory. This is the agent's complete briefing — no other file is
strictly required to start.

```toml
# ── Identity ──────────────────────────────────────────────────────────────────
RUN_ID        = "label-AC-UI-0-CRITICAL-BUGS-20260303T200000Z-a1b2"
ROLE          = "cto"
TIER          = "executive"

# ── Scope ─────────────────────────────────────────────────────────────────────
# SCOPE_TYPE  label   → manager tiers; SCOPE_VALUE is a GitHub label string
# SCOPE_TYPE  issue   → engineer leaf; SCOPE_VALUE is the issue number (string)
# SCOPE_TYPE  pr      → reviewer leaf; SCOPE_VALUE is the PR number (string)
SCOPE_TYPE    = "label"
SCOPE_VALUE   = "AC-UI/0-CRITICAL-BUGS"

# ── Provenance ────────────────────────────────────────────────────────────────
GH_REPO       = "cgcardona/maestro"
BRANCH        = ""                  # empty for manager tiers (no dedicated branch)
WORKTREE      = "/Users/gabriel/.agentception/worktrees/maestro/label-AC-UI-0-..."
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = ""                  # empty for root; set by parent for all other tiers

# ── Callbacks ─────────────────────────────────────────────────────────────────
AC_URL        = "http://localhost:7777"
ROLE_FILE     = "/Users/gabriel/dev/tellurstori/maestro/.cursor/roles/cto.md"
```

### Leaf engineer example

```toml
RUN_ID        = "issue-42-20260303T200100Z-c3d4"
ROLE          = "python-developer"
TIER          = "engineer"
SCOPE_TYPE    = "issue"
SCOPE_VALUE   = "42"
GH_REPO       = "cgcardona/maestro"
BRANCH        = "feat/issue-42"
WORKTREE      = "/Users/gabriel/.agentception/worktrees/maestro/issue-42"
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = "label-AC-UI-0-CRITICAL-BUGS-20260303T200000Z-a1b2"
AC_URL        = "http://localhost:7777"
ROLE_FILE     = "/Users/gabriel/dev/tellurstori/maestro/.cursor/roles/python-developer.md"
```

### Leaf reviewer example

```toml
RUN_ID        = "pr-99-20260303T200200Z-e5f6"
ROLE          = "pr-reviewer"
TIER          = "reviewer"
SCOPE_TYPE    = "pr"
SCOPE_VALUE   = "99"
GH_REPO       = "cgcardona/maestro"
BRANCH        = ""
WORKTREE      = "/Users/gabriel/.agentception/worktrees/maestro/pr-99"
BATCH_ID      = "label-AC-UI-0-20260303T200000Z-a1b2"
PARENT_RUN_ID = "label-AC-UI-0-CRITICAL-BUGS-20260303T200000Z-a1b2"
AC_URL        = "http://localhost:7777"
ROLE_FILE     = "/Users/gabriel/dev/tellurstori/maestro/.cursor/roles/pr-reviewer.md"
```

---

## What each tier reads from GitHub

### `root` (CTO)

```bash
# Open issues for the scope label
gh issue list --repo $GH_REPO --label "$SCOPE_VALUE" --state open \
  --json number,title,labels,assignees --limit 200

# All open PRs against dev
gh pr list --repo $GH_REPO --base dev --state open \
  --json number,title,labels,headRefName --limit 200
```

Decides: spawn `coordinator` if issues > 0, spawn `coordinator` if PRs > 0.
Loops until both queues are empty.

### `coordinator`

```bash
# Open issues for the scope label, excluding claimed ones
gh issue list --repo $GH_REPO --label "$SCOPE_VALUE" --state open \
  --json number,title,labels,assignees --limit 200 |
  jq '[.[] | select(.labels[].name != "agent:wip")]'
```

Spawns one `engineer` Task per unclaimed issue, up to 3 concurrently.
Each engineer self-replaces (spawns its successor before exiting).

### `coordinator`

```bash
# Open PRs against dev (all are in scope — QA reviews everything)
gh pr list --repo $GH_REPO --base dev --state open \
  --json number,title,headRefName,reviewDecision --limit 200
```

Spawns one `reviewer` Task per PR, up to 3 concurrently.

### `engineer` (leaf)

```bash
# Read the single assigned issue
gh issue view $SCOPE_VALUE --repo $GH_REPO --json number,title,body,labels
```

Implements the issue, opens a PR, calls `report/done`, exits.

### `reviewer` (leaf)

```bash
# Read the single assigned PR
gh pr view $SCOPE_VALUE --repo $GH_REPO --json number,title,body,files,diff
```

Reviews, requests changes or approves+merges, calls `report/done`, exits.

---

## Spawning rules

- **Max 3 concurrent Task calls** per spawning agent (observed Cursor limit).
- **Always `subagent_type="generalPurpose"`** — never `shell`. Only
  `generalPurpose` agents have access to the Task tool.
- **Claim before spawning**: manager tiers call
  `POST /api/build/acknowledge/{run_id}` for each child run_id before
  spawning its Task, preventing double-dispatch.
- **PARENT_RUN_ID propagation**: every child task receives its parent's
  `RUN_ID` so reporting chains back up the tree and the org chart can
  be reconstructed.

---

## Reporting callbacks

All tiers use the same callback surface:

```
POST /api/build/report/step      { run_id, step_name }
POST /api/build/report/blocker   { run_id, description }
POST /api/build/report/decision  { run_id, decision, rationale }
POST /api/build/report/done      { run_id, pr_url? }   ← leaf tiers only
```

Manager tiers call `report/step` at each phase of their loop.
They do NOT call `report/done` — they exit naturally after their queue drains.

---

## Tier → Role mapping (for the dispatch UI)

| Tier | Selectable roles |
|------|-----------------|
| `root` | `cto` |
| `coordinator` | `engineering-coordinator` |
| `coordinator` | `qa-coordinator` |
| `engineer` | `python-developer`, `frontend-developer`, `typescript-developer`, `react-developer`, `go-developer`, `rust-developer`, `api-developer`, `devops-engineer`, `data-engineer`, `site-reliability-engineer`, `security-engineer`, `mobile-developer`, `ios-developer`, `android-developer`, `full-stack-developer`, `architect`, `technical-writer`, `test-engineer`, `ml-engineer`, `systems-programmer`, `rails-developer`, `database-architect` |
| `reviewer` | `pr-reviewer` |
