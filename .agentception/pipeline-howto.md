# AgentCeption — Parallel Agent Pipeline: How to Kick It Off

This document captures the exact architecture and launch procedure for the
three-tier parallel agent pipeline. Read this before starting any new wave of work.

---

## What Was Built

A fully saturated, self-managing pipeline where:

- **Every open issue** gets an implementation agent immediately — no queue
- **Every open PR** gets a review agent immediately — no queue
- **Merge conflict prevention** is structural, not procedural — agents don't fight each other
- **All battle-tested logic** lives in canonical prompts — managers only route, never describe

---

## The Architecture — Self-Replacing Pool

```
YOU (human)
    │
    └─► CTO Agent  (reads cto.md — sees the whole board, dispatches VPs)
            │
            ├─► Engineering VP 1  (seeds 4 leaf engineers)
            │        │
            │        ├─► Leaf Agent: issue-423  → opens PR → spawns replacement for issue-427 →
            │        │                             issue-427 opens PR → spawns replacement for issue-431 → ...
            │        ├─► Leaf Agent: issue-424  → opens PR → spawns replacement for issue-428 → ...
            │        ├─► Leaf Agent: issue-425  → opens PR → spawns replacement for issue-429 → ...
            │        └─► Leaf Agent: issue-426  → opens PR → spawns replacement for issue-430 → ...
            │
            ├─► Engineering VP 2  (seeds 4 more leaf engineers — same self-replacing pattern)
            │
            ├─► QA VP 1  (seeds 4 leaf reviewers)
            │        │
            │        ├─► Reviewer: PR #485  → merges → spawns replacement for PR #489 → ...
            │        └─► ... 4 concurrent reviewers, each self-replacing
            │
            └─► QA VP 2  (seeds 4 more leaf reviewers — same pattern)
```

**How it works:** VPs seed the initial pool. Each leaf agent, the moment it finishes
its work (PR opened / PR merged), queries GitHub for the next unclaimed item, claims
it with an `agent:wip` label, creates a new worktree, and spawns a fresh leaf agent
via the Task tool — before it exits. No batch boundaries. No waiting for the slowest
agent. The pool stays at N concurrent workers continuously until the queue drains.

**The golden rule:** VPs seed. Leaf agents replace themselves. Canonical prompts describe work. Never cross the streams.

---

## The Files That Make This Work

### Canonical Prompts (the brain — all battle-tested logic lives here)

| File | Purpose |
|------|---------|
| `.agentception/prompts/parallel-issue-to-pr.md` | Full kickoff for implementation leaf agents |
| `.agentception/prompts/parallel-pr-review.md` | Full kickoff for review leaf agents |
| `.agentception/prompts/parallel-bugs-to-issues.md` | Full kickoff for issue-creation agents |
| `.agentception/prompts/parallel-conductor.md` | Full kickoff for a meta-conductor (single-agent orchestration) |

### Cognitive Architecture (role files — who each agent IS)

| File | Used by |
|------|---------|
| `.agentception/roles/cto.md` | CTO agent |
| `.agentception/roles/engineering-coordinator.md` | Engineering Manager |
| `.agentception/roles/qa-coordinator.md` | QA Manager |
| `.agentception/roles/python-developer.md` | Leaf implementation agents (Python/API work) |
| `.agentception/roles/muse-specialist.md` | Leaf agents on Muse VCS / musical analysis work |
| `.agentception/roles/database-architect.md` | Leaf agents on migrations / seed data |
| `.agentception/roles/pr-reviewer.md` | Leaf review agents |
| `.agentception/roles/coordinator.md` | Mid-tier coordinators |

### Conflict Prevention (structural — set and forget)

| File | What it does |
|------|-------------|
| `.gitattributes` | Union merge driver for additive files (`app.py`, docs) — git auto-resolves |
| `.agentception/conflict-rules.md` | Mechanical lookup table: one-line rule per conflict type |
| `agentception/muse/api/routes/__init__.py` | Auto-discovers all routers — agents never touch this file |

### Agent State (per-task)

| Location | What it is |
|----------|-----------|
| `~/.agentception/worktrees/agentception/issue-{N}/` | Git worktree for each issue |
| `~/.agentception/worktrees/agentception/pr-{N}/` | Git worktree for each PR review |
| `<worktree>/.agent-task` | Plain-text task file — the agent's single source of truth |

---

## The `.agent-task` File Format

Every worktree gets exactly one `.agent-task` file. The canonical prompts parse it.

```
TASK=issue-to-pr          # or pr-review
ISSUE_NUMBER=423          # issue number (for issue-to-pr)
PR=485                    # PR number (for pr-review)
BRANCH=feat/issue-423     # git branch name
WORKTREE=$HOME/.agentception/worktrees/agentception/issue-423
ROLE=python-developer     # which cognitive architecture to load
ROLE_FILE=<repo-root>/.agentception/roles/python-developer.md
BASE=dev
GH_REPO=cgcardona/agentception
CLOSES_ISSUES=423         # comma-separated issue numbers to close on merge
MERGE_AFTER=none          # or: issue number whose PR must merge first
CONFLICT_RISK=none        # none | low | high — informs agent behavior
BATCH_ID=eng-20260301T053412Z-a7f2   # VP-level batch fingerprint (propagated to successors)

PRIMARY_FILE=agentception/routes/api/musehub/ui_blame.py   # main file being created/modified
TEST_FILE=tests/test_musehub_ui_blame.py              # targeted test file

SCOPE:
  Multi-line description of exactly what to implement.
  This is what the leaf agent reads to know what to build.
```

## Agent Fingerprinting

Every piece of pipeline work is permanently signed at two granularities:

### BATCH_ID — VP-level identifier
Generated once per VP seed run: `eng-<YYYYMMDDTHHMMSSZ>-<4hex>` (engineering) or `qa-<…>` (QA).
Propagated to every leaf agent in that VP's pool, and carried forward through every self-replacing successor.
Lets you answer: *"Which VP wave spawned this PR?"*

### AGENT_SESSION — leaf-level identifier
Generated fresh by each individual leaf agent: `eng-<timestamp>-<4hex>` or `qa-<…>`.
One session = one agent run = one PR or review.
Lets you answer: *"Which specific agent opened this PR / merged this PR?"*

### Where fingerprints appear

| Artifact | What's embedded |
|----------|----------------|
| Every git commit | `AgentCeption-Batch:` and `AgentCeption-Session:` trailers in the commit message |
| PR description | `<!-- agentception-fingerprint … -->` metadata block + human-readable footer line |
| Issue comment (claim) | `🔖 Claimed by agent` comment with fingerprint block — posted when agent claims the issue |
| Issue comment (PR opened) | `🤖 Implemented by agent` comment with fingerprint block + PR number — posted regardless of whether the issue was claimed from the pool or opened manually |
| Post-merge PR comment (reviews) | `🤖 AgentCeption Review Fingerprint` table with grade, timestamp, batch, session |

### How to trace back

```bash
# Find all commits from a specific batch:
git log --all --grep="AgentCeption-Batch: eng-20260301T053412Z-a7f2"

# Find the PR opened by a specific agent session:
gh pr list --repo cgcardona/agentception --state all --search "eng-20260301T053412Z-a7f2"

# Find which batch a commit came from:
git show <sha> | grep "AgentCeption-"
```

---

## Step-by-Step: How to Launch a New Wave

### Step 1 — Survey the pipeline state

```bash
# What's open?
gh issue list --state open --label "batch-NN" --repo cgcardona/agentception
gh pr list --base dev --state open --repo cgcardona/agentception

# What's in flight?
git worktree list
```

### Step 2 — Create worktrees

```bash
# For each issue to implement:
git worktree add -b feat/issue-{N} ~/.agentception/worktrees/agentception/issue-{N} origin/dev

# For each PR to review (checkout the PR's branch):
BRANCH=$(gh pr view {N} --json headRefName --jq '.headRefName')
git worktree add ~/.agentception/worktrees/agentception/pr-{N} origin/$BRANCH
```

### Step 3 — Write `.agent-task` files

One file per worktree. Use the format above. Key fields:
- `ROLE=` — pick from: `python-developer`, `muse-specialist`, `database-architect`, `pr-reviewer`
- `MERGE_AFTER=` — set to `none` or the issue number this depends on
- `SCOPE:` — multi-line description of what to build/review

### Step 4 — Launch the two managers (skip the CTO for simple waves)

Open two Cursor composer windows or call the Task tool twice simultaneously:

**QA Manager prompt:**
```
You are the QA Manager. Read <repo-root>/.agentception/roles/qa-coordinator.md.

Launch one leaf agent per PR using the Task tool. Each agent gets:
"Read .agent-task at <WORKTREE>/.agent-task, then follow the Kickoff Prompt in
<repo-root>/.agentception/prompts/parallel-pr-review.md.
Your worktree is <WORKTREE>. GH_REPO=cgcardona/agentception"

Your PRs: [list worktrees]
```

**Engineering Manager prompt:**
```
You are the Engineering Manager. Read <repo-root>/.agentception/roles/engineering-coordinator.md.

Launch one leaf agent per issue using the Task tool. Each agent gets:
"Read .agent-task at <WORKTREE>/.agent-task, then follow the Kickoff Prompt in
<repo-root>/.agentception/prompts/parallel-issue-to-pr.md.
Your worktree is <WORKTREE>. GH_REPO=cgcardona/agentception"

Your issues: [list worktrees]
Serialized (MERGE_AFTER): [note any dependency ordering]
```

### Step 5 — (Optional) Launch the CTO for a full autonomous run

Use this when you want the pipeline to run end-to-end without manual intervention:

```
You are the CTO. Read <repo-root>/.agentception/roles/cto.md.

Survey the pipeline state with gh issue list and gh pr list.
Dispatch the Engineering Manager and QA Manager simultaneously using the Task tool.
Each manager launches leaf agents pointing at the canonical prompts — not inline instructions.
Continue until gh issue list --state open returns 0 results and gh pr list --state open returns 0 results.
GH_REPO=cgcardona/agentception
Repo: <repo-root>
```

---

## Platform Constraint: 4-Wide Branching, Unlimited Depth

**The Task tool supports 4 concurrent sub-agents per spawning agent.**

This is a branching factor, not a ceiling. Use it as a tree:

```
CTO (autonomous, loops until pipeline empty)
 ├── Engineering VP  (autonomous, loops until no open issues)
 │    ├── Batch Tech Lead A  →  4 leaf engineers
 │    ├── Batch Tech Lead B  →  4 leaf engineers
 │    ├── Batch Tech Lead C  →  4 leaf engineers
 │    └── Batch Tech Lead D  →  4 leaf engineers
 └── QA VP  (autonomous, loops until no open PRs)
      ├── Review Tech Lead A  →  4 leaf reviewers
      ├── Review Tech Lead B  →  4 leaf reviewers
      ├── Review Tech Lead C  →  4 leaf reviewers
      └── Review Tech Lead D  →  4 leaf reviewers
```

**2 levels below CTO = 4 × 4 = 16 concurrent leaf workers per wave.**
Each level loops — when its queue empties it re-queries and dispatches the next wave.

**You just launch one CTO.** It runs autonomously until GitHub is empty.
As you push new issues to GitHub, the CTO's next survey picks them up.

---

## The Leaf Agent Prompt (copy-paste template)

This is the ONLY thing you pass to a leaf agent. Do not add anything.

**For implementation:**
```
Read the `.agent-task` file at `<WORKTREE>/.agent-task` to get your full assignment,
then follow the complete Kickoff Prompt section in
`<repo-root>/.agentception/prompts/parallel-issue-to-pr.md`.

Your worktree is `<WORKTREE>`. Work only in that directory.
Repo root: <repo-root>
GH_REPO=cgcardona/agentception
```

**For review:**
```
Read the `.agent-task` file at `<WORKTREE>/.agent-task` to get your full assignment,
then follow the complete Kickoff Prompt section in
`<repo-root>/.agentception/prompts/parallel-pr-review.md`.

Your worktree is `<WORKTREE>`. Work only in that directory.
Repo root: <repo-root>
GH_REPO=cgcardona/agentception
```

That is the complete prompt. The canonical file has everything else.

---

## Conflict Prevention — What's Structural vs. Procedural

### Structural (automatic — no agent intervention needed)

| Mechanism | Effect |
|-----------|--------|
| `__init__.py` auto-discovery | New route modules never require editing `__init__.py` |
| `.gitattributes` union merge | `app.py`, docs files auto-resolved by git |
| New files per feature | Each agent creates a new `ui_{slug}.py` — zero overlap |
| Separate worktrees | Each agent has an isolated working directory |

### Procedural (agents follow these from canonical prompts)

| Rule | Where it lives |
|------|---------------|
| Sync `origin/dev` before implementing AND before pushing | `PARALLEL_ISSUE_TO_PR.md` |
| Open `CONFLICT_RULES.md` first — no sed/hexdump loops | Both canonical prompts |
| `git worktree remove` BEFORE `git branch -D` | `PARALLEL_PR_REVIEW.md` STEP 8 |
| mypy before tests | Both canonical prompts |
| Never run full test suite | Both canonical prompts |
| MERGE_AFTER gate polling | Both canonical prompts |

---

## Monitoring a Running Wave

```bash
# Check open PRs
gh pr list --base dev --state open --repo cgcardona/agentception

# Check open issues remaining in a batch
gh issue list --label "batch-07" --state open --repo cgcardona/agentception

# Check worktrees in flight
git worktree list

# Tail a specific agent's progress (find the terminal file)
ls ~/.cursor/projects/<cursor-project-id>/terminals/
```

---

## Resuming After Interruption

If an agent crashes mid-task:

1. Check if its PR was opened: `gh pr list --state open --repo cgcardona/agentception`
2. Check if its worktree still exists: `git worktree list`
3. If worktree exists + no PR: re-launch the leaf agent with the same prompt
4. If worktree missing + PR open: create a review worktree and assign a reviewer
5. Clean up orphaned worktrees: `git worktree prune`

---

## The Invariant That Must Never Break

> **Canonical prompts are the single source of truth for how agents work.**
> Managers and the CTO only route — they never describe how to do the work.
> Every improvement to agent behavior (conflict rules, test policy, mypy order)
> goes into the canonical prompts and flows to every agent automatically.
