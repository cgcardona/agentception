# Cognitive Architecture Injection Audit

**Issue:** #174 — Audit all agent creation paths for cognitive architecture injection coverage  
**Auditor:** engineer agent · `hopper:llm:python`  
**Date:** 2026-03-07  
**Worktree:** `issue-174-ac2433`

---

## Summary

Cognitive architecture (`cognitive_arch`) is resolved and written to every `.agent-task` file at spawn time — without exception — across all five spawn paths identified in this audit.  The breakdown occurs one step later: **whether the agent actually reads and announces its architecture** depends entirely on whether its role file contains the `MANDATORY FIRST RESPONSE` self-introduction block.  Only three role files include this block (`cto.md`, `engineering-coordinator.md`, `qa-coordinator.md`).  Every leaf role file — 44 files — is silent.

A second, independent bug exists at the root: **`cto.md` hardcodes `COGNITIVE_ARCH="von_neumann"`** instead of reading the field from its own `.agent-task`.  This means the CTO always loads the `von_neumann` persona regardless of what `_resolve_cognitive_arch()` resolved at spawn time.

---

## Spawn Site Audit Table

| # | spawn_site | file:line | agent_type | arch_received | arch_forwarded | intro_triggered |
|---|---|---|---|---|---|---|
| 1 | `POST /api/dispatch/issue` | `agentception/routes/api/dispatch.py:212` | leaf / engineer | **Y** | N/A (leaf) | **N** |
| 2 | `POST /api/dispatch/label` (scope=full_initiative) | `agentception/routes/api/dispatch.py:499` | executive (CTO) | **Y** | **Y** (via build_spawn_child) | **Y** *(hardcoded arch)* |
| 3 | `POST /api/dispatch/label` (scope=phase) | `agentception/routes/api/dispatch.py:499` | coordinator | **Y** | **Y** (via build_spawn_child) | **Y** |
| 4 | `POST /api/dispatch/label` (scope=issue) | `agentception/routes/api/dispatch.py:499` | leaf / engineer | **Y** | N/A (leaf) | **N** |
| 5 | `build_spawn_child` MCP tool → `spawn_child()` service | `agentception/mcp/build_tools.py:210`, `agentception/services/spawn_child.py:352` | any (coordinator or leaf) | **Y** | **Y** (all children) | **Conditional** *(Y for coordinator roles, N for leaf roles)* |
| 6 | `plan_spawn_coordinator` MCP tool → `_build_coordinator_task()` | `agentception/mcp/plan_tools.py:220`, `agentception/routes/api/_shared.py:116` | coordinator (bugs-to-issues workflow) | **Y** | **Y** (hardcoded from `ROLE_DEFAULT_FIGURE`) | **Y** |
| 7 | `_build_conductor_task()` | `agentception/routes/api/_shared.py:180` | executive / conductor | **Y** | **Y** (hardcoded from `ROLE_DEFAULT_FIGURE`) | **Y** |
| 8 | Engineering Coordinator → `build_spawn_child` (per-issue) | `.agentception/roles/engineering-coordinator.md:158` | leaf / engineer | **Y** | N/A (leaf) | **N** |
| 9 | QA Coordinator → `build_spawn_child` (per-PR) | `.agentception/roles/qa-coordinator.md` | leaf / pr-reviewer | **Y** | N/A (leaf) | **N** |
| 10 | CTO sub-spawn (engineering-coordinator child) | `.agentception/roles/cto.md:~290` | coordinator | **Y** | **Y** | **Y** |
| 11 | CTO sub-spawn (qa-coordinator child) | `.agentception/roles/cto.md:~3888` | coordinator | **Y** | **Y** | **Y** |
| 12 | Dispatcher → Task call for executive/coordinator | `.agentception/dispatcher.md:100` | executive / coordinator | **Y** *(in .agent-task)* | **Y** | **Y** *(role file reads from .agent-task)* |
| 13 | Dispatcher → Task call for leaf engineer/reviewer | `.agentception/dispatcher.md:154` | leaf | **Y** *(in .agent-task)* | N/A (leaf) | **N** |

**Column definitions:**
- `arch_received` — Is `cognitive_arch` written into the agent's `.agent-task` file?
- `arch_forwarded` — When the agent spawns children, is `cognitive_arch` resolved and written into each child's `.agent-task`?  (N/A for leaves that do not spawn children.)
- `intro_triggered` — Does the agent produce the mandatory `🧠 Cognitive architecture correctly injected.` self-introduction as its first visible response?

---

## Detailed Spawn Path Analysis

### Path 1 — `POST /api/dispatch/issue`

**File:** `agentception/routes/api/dispatch.py`  
**Lines:** 159–266

This route creates a worktree and `.agent-task` for a single-issue leaf engineer.

```python
# dispatch.py:212
cognitive_arch = _resolve_cognitive_arch(req.issue_body, req.role)
agent_task = _build_agent_task(
    ...
    cognitive_arch=cognitive_arch,
    ...
)
```

`_resolve_cognitive_arch` is called with `req.issue_body` (may be empty) and `req.role`.  The resolved string is written to `[agent].cognitive_arch` in the TOML.

**Gap:** The leaf role file (e.g., `python-developer.md`) contains no `MANDATORY FIRST RESPONSE` block.  The agent reads `cognitive_arch` from its `.agent-task` only when generating the fingerprint comment — it never announces the architecture as a first-response signal.

---

### Path 2–4 — `POST /api/dispatch/label`

**File:** `agentception/routes/api/dispatch.py`  
**Lines:** 425–586

This route handles three scopes (`full_initiative`, `phase`, `issue`).  Cognitive arch is resolved on line 499:

```python
# dispatch.py:499
label_cognitive_arch = _resolve_cognitive_arch("", role)
```

The empty `issue_body` means the arch is determined by role alone (figure from `ROLE_DEFAULT_FIGURE`, skill defaults to `"python"`).  The resolved string is written to `[agent].cognitive_arch` in the TOML (line 511).

**Gap (scope=issue leaf):** Same as Path 1 — leaf role file has no self-introduction block.

**Note (scope=full_initiative / CTO):** `cto.md` has the `MANDATORY FIRST RESPONSE` block but hardcodes `COGNITIVE_ARCH="von_neumann"` instead of reading from `.agent-task` (see Root Cause #1 below).

---

### Path 5 — `build_spawn_child` MCP tool

**File:** `agentception/mcp/build_tools.py:140`  
**Called by:** coordinator agents (CTO, engineering-coordinator, qa-coordinator) at runtime

This is the primary programmatic spawn path for tree growth at runtime.  The tool delegates to `spawn_child()`:

```python
# services/spawn_child.py:426
cognitive_arch = _resolve_cognitive_arch(
    issue_body,
    role,
    skills_hint=skills_hint,
)
```

`issue_body` and `skills_hint` are forwarded from the parent coordinator's call, enabling issue-body-driven arch selection.  The resolved arch is written to `[agent].cognitive_arch` (line 263 in `_build_child_task`).

**Gap:** For children whose role is a leaf (e.g., `python-developer`, `pr-reviewer`), the role file has no self-introduction block.  For children whose role is a coordinator (e.g., `engineering-coordinator`), the role file DOES have the block — intro IS triggered.

---

### Path 6 — `plan_spawn_coordinator` MCP tool

**File:** `agentception/mcp/plan_tools.py:220`  
**Called by:** Phase 1B planning agent (human-initiated)

Calls `_build_coordinator_task()` which derives arch from `ROLE_DEFAULT_FIGURE`:

```python
# _shared.py:136
coord_arch = (
    f"{ROLE_DEFAULT_FIGURE.get('engineering-coordinator', 'von_neumann')}:python"
)
```

This always produces `"von_neumann:python"` for the `engineering-coordinator` figure, written to `[agent].cognitive_arch`.  The `engineering-coordinator.md` role file has the intro block → intro IS triggered.

---

### Path 7 — `_build_conductor_task`

**File:** `agentception/routes/api/_shared.py:180`  
**Called by:** Ship UI conductor dispatch

```python
# _shared.py:199
conductor_arch = (
    f"{ROLE_DEFAULT_FIGURE.get('conductor', 'jeff_dean')}:python"
)
```

Hardcoded from `ROLE_DEFAULT_FIGURE`.  The `cto.md` role file (which conductors use) has the intro block.

---

### Path 8–9 — Coordinator-spawned leaf agents

**Engineering Coordinator** (`.agentception/roles/engineering-coordinator.md:158`):

```
build_spawn_child(
    parent_run_id = <your RUN_ID>,
    role          = "python-developer",
    tier          = "engineer",
    ...
    issue_body    = <full issue body>,
    issue_title   = <issue title>,
)
```

**QA Coordinator** spawns `pr-reviewer` leaves via the same MCP tool.

In both cases, cognitive_arch IS resolved and written to the child's `.agent-task`.  But neither `python-developer.md` nor `pr-reviewer.md` contain a `MANDATORY FIRST RESPONSE` block.

---

### Dispatcher Prompt Analysis

**File:** `.agentception/dispatcher.md`

The Dispatcher does **not** pass `COGNITIVE_ARCH` in the briefing text sent to spawned agents.  For coordinators the briefing is:

```
WORKTREE:    {host_worktree_path}
ROLE:        {role}
TIER:        {tier}
RUN_ID:      {run_id}
SCOPE_TYPE:  label
SCOPE_VALUE: {scope_value}
GH_REPO:     {gh_repo}
AC_URL:      http://localhost:10003
```

For leaf agents, the briefing is similarly arch-free.  The agent must read `COGNITIVE_ARCH` from its `.agent-task` on its own — which coordinator role files do (as part of STEP 0) but leaf role files do not (for the intro announcement).

---

## Role File Coverage Matrix

| Role file | Has STEP 0 (reads arch from .agent-task) | Has MANDATORY FIRST RESPONSE | Intro triggered |
|---|---|---|---|
| `cto.md` | N — hardcodes `COGNITIVE_ARCH="von_neumann"` | **Y** | **Partial** (hardcoded arch announced, not the resolved one) |
| `engineering-coordinator.md` | **Y** (line 17) | **Y** (line 29) | **Y** |
| `qa-coordinator.md` | **Y** (similar to eng-coord) | **Y** (line 29) | **Y** |
| `python-developer.md` | Partial (reads for fingerprint only, line 88) | **N** | **N** |
| `pr-reviewer.md` | Partial (reads for reviewer context, line 12) | **N** | **N** |
| All other leaf roles (42 files) | **N** | **N** | **N** |

---

## Root Cause Summary

### Root Cause #1 — CTO hardcodes cognitive arch instead of reading from `.agent-task`

**File:** `.agentception/roles/cto.md`  
**Line:** 52

```bash
COGNITIVE_ARCH="von_neumann"
```

The CTO role file does not read `[agent].cognitive_arch` from the `.agent-task` file.  It hardcodes `"von_neumann"` — meaning:

1. The CTO always loads the `von_neumann` persona regardless of what `_resolve_cognitive_arch()` resolved.
2. If the CTO was dispatched with a different role mapping (e.g., via a future config change to `ROLE_DEFAULT_FIGURE["cto"]`), the role file would silently ignore it.

**Contrast with `engineering-coordinator.md` line 17** (which correctly reads):

```bash
COGNITIVE_ARCH=$(python3 -c "import tomllib; d=tomllib.loads(open('.agent-task').read()); print(d['agent']['cognitive_arch'])")
```

**Fix:** Replace line 52 of `cto.md` with a TOML read identical to `engineering-coordinator.md:17`.

---

### Root Cause #2 — All leaf role files (44 files) have no self-introduction block

**Files:** All role files except `cto.md`, `engineering-coordinator.md`, `qa-coordinator.md`  
**Examples:** `python-developer.md`, `pr-reviewer.md`, `frontend-developer.md`, `architect.md`, (41 more)

Leaf role files read `cognitive_arch` from `.agent-task` only to generate a fingerprint comment (`resolve_arch.py --fingerprint`, `python-developer.md:88`).  They do NOT call `resolve_arch.py --mode implementer` to load the full cognitive context block, and they contain no `MANDATORY FIRST RESPONSE` announcement.

This means:
- The `cognitive_arch` field is correctly written to the leaf's `.agent-task` at spawn time.
- The leaf agent reads the field for fingerprinting.
- But the agent never loads the full persona context (traits, reasoning style, epistemic stance).
- And the agent never announces its architecture — the first-response signal is absent.

**Fix:** Add a STEP 0 block identical in structure to `engineering-coordinator.md:13–45` to every leaf role file.  The block should (a) read `cognitive_arch` from `.agent-task`, (b) call `resolve_arch.py --mode implementer`, and (c) emit the `🧠 Cognitive architecture correctly injected.` mandatory first response.

---

### Root Cause #3 — Dispatcher briefing omits `COGNITIVE_ARCH`

**File:** `.agentception/dispatcher.md`  
**Lines:** 100–149 (coordinator briefing), 154–186 (leaf briefing)

Neither briefing template includes `COGNITIVE_ARCH` as an inline field.  Both coordinator and leaf agents must discover it by reading `.agent-task`.  Coordinator role files do this (STEP 0), so coordinators are unaffected.  Leaf role files do not perform a discovery step for announcement purposes, so the omission compounds Root Cause #2.

**Fix (secondary):** Add `COGNITIVE_ARCH: {cognitive_arch}` to both briefing templates so the field is surfaced even before the agent reads `.agent-task`.  This is a defense-in-depth fix — the primary fix is Root Cause #2.

---

## Gap Confirmation (Acceptance Criterion)

At least one `arch_received=Y / intro_triggered=N` row is documented in the table above.

**Confirmed gaps:**

| Spawn site | arch_received | intro_triggered |
|---|---|---|
| `POST /api/dispatch/issue` (leaf) | Y | **N** |
| `POST /api/dispatch/label` scope=issue (leaf) | Y | **N** |
| `build_spawn_child` → leaf engineer | Y | **N** |
| `build_spawn_child` → pr-reviewer | Y | **N** |
| Dispatcher → leaf Task call | Y | **N** |

Five distinct spawn paths write the arch field but the leaf agent never announces it.

---

## Out of Scope

No code changes were made in this issue.  All fixes are documented above as future work.  This document is the gate artifact for phase 1.

---

## Resolved via PR #176

**PR:** [feat: propagate cognitive_arch through all coordinator spawn calls](https://github.com/cgcardona/agentception/pull/188)  
**Closes:** #176

The following spawn sites were updated so a parent coordinator can forward its exact `cognitive_arch` string to every child it spawns.  When provided, `_resolve_cognitive_arch()` is bypassed and the value flows unchanged from root to leaf.  When omitted, the existing keyword-extraction fallback is preserved.

| File | Function / call | Status | Notes |
|------|----------------|--------|-------|
| `agentception/services/spawn_child.py` | `spawn_child()` | ✅ Fixed | Added `cognitive_arch: str \| None = None`; skips `_resolve_cognitive_arch()` when set. |
| `agentception/mcp/build_tools.py` | `build_spawn_child()` | ✅ Fixed | Added `cognitive_arch: str = ""`, forwarded to `spawn_child()`. |
| `agentception/mcp/server.py` | `call_tool_async()` | ✅ Fixed | Extracts `cognitive_arch` from tool arguments; MCP schema updated. |

### Usage

Coordinators must now pass `cognitive_arch` explicitly when spawning children:

```python
result = await build_spawn_child(
    parent_run_id=self.run_id,
    role="python-developer",
    tier="engineer",
    scope_type="issue",
    scope_value=str(issue_number),
    gh_repo=gh_repo,
    cognitive_arch=self.cognitive_arch,  # always forward — never omit
)
```

### Test coverage added

| Test | What it verifies |
|------|-----------------|
| `test_spawn_child_forwards_cognitive_arch_without_resolving` | `_resolve_cognitive_arch` is never called when `cognitive_arch` is provided |
| `test_spawn_child_resolves_arch_when_not_provided` | Fallback resolution still works when `cognitive_arch` is omitted |
| `test_cognitive_arch_propagates_to_leaf` | End-to-end: root arch arrives unchanged on the leaf after two spawn hops |
