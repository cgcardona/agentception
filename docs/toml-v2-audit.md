# TOML v2 Format Compliance Audit

**Audit date:** 2026-03-06  
**Auditor:** AgentCeption engineer agent (run `issue-159-0e6cbe`)  
**Scope:** Full codebase scan for TOML v2 format regressions in `.agent-task` writers, readers, templates, fixtures, tests, and documentation.  
**Spec reference:** `.agentception/agent-task-spec.md` (version 2.0)  
**Parser:** `agentception/readers/worktrees.py` → `tomllib.loads()` (Python stdlib)

---

## Executive Summary

The TOML v2 migration was partially completed. Three distinct migration commits landed successfully:

| Commit | Description |
|--------|-------------|
| `f002865` | Migrate `parse_agent_task()` to TOML-only — delete legacy K=V parser |
| `7e4b890` | Migrate `_build_agent_task` / `_build_coordinator_task` / `_build_conductor_task` in `_shared.py` to TOML v2 |
| `aaf8a75` | Migrate coordinator prompt templates from K=V heredocs to TOML v2 writer blocks |

However, **three writer paths were not migrated** (`dispatch.py` × 2 and `mcp/plan_tools.py` × 1), **all template reader sections** (8 files: `parallel-issue-to-pr.md`, `parallel-pr-review.md`, `parallel-bugs-to-issues.md`, `parallel-conductor.md`, and the four role files `cto.md`, `engineering-coordinator.md`, `qa-coordinator.md`, `pr-reviewer.md`) still use `grep "^KEY="` shell commands that will fail against TOML v2 files, and **nine test files** contain fixtures or assertions anchored to the old flat K=V format.

**Zero findings is not the result.** 19 violations are recorded below (findings #1–#14 were identified in the initial pass; findings #15–#19 are the omitted role and conductor files added in the review remediation pass).

---

## Canonical Spec File

| Path | Status |
|------|--------|
| `.agentception/agent-task-spec.md` | ✅ **Compliant** — specifies TOML v2 sectioned format throughout; all example blocks use `[section]` headers and typed values. |

---

## Compliant Implementations (reference baseline)

These files are correctly implemented and serve as the template for remediation:

| File | Role |
|------|------|
| `agentception/routes/api/_shared.py` | Correct TOML v2 writers: `_build_agent_task()`, `_build_coordinator_task()`, `_build_conductor_task()` |
| `agentception/readers/worktrees.py` | Correct TOML v2 parser: `parse_agent_task()` via `tomllib.loads()` |
| `agentception/tests/test_agentception_worktrees.py` | Correct TOML v2 fixtures (lines 31–103) |
| `agentception/tests/test_agentception_spawn.py` | Tests for TOML v2 builders in `_shared.py` |

---

## Findings Table

| # | file | line(s) | violation_type | description | commit_that_introduced_it |
|---|------|---------|---------------|-------------|--------------------------|
| 1 | `agentception/routes/api/dispatch.py` | 184–204 | `source` | `/api/dispatch` (issue scope) writes flat `KEY=VALUE\n` strings directly — bypasses `_build_agent_task()` from `_shared.py`. Fields: `RUN_ID`, `ISSUE_NUMBER`, `ISSUE_TITLE`, `ROLE`, `ROLE_FILE`, `GH_REPO`, `BRANCH`, `WORKTREE`, `BATCH_ID`, `SPAWN_MODE`, `COGNITIVE_ARCH`, `AC_URL`. | `6df61b6` |
| 2 | `agentception/routes/api/dispatch.py` | 480–510 | `source` | `/api/dispatch-label` (label scope) writes flat `KEY=VALUE\n` strings. Fields: `RUN_ID`, `ROLE`, `TIER`, `ORG_DOMAIN`, `SCOPE_TYPE`, `SCOPE_VALUE`, `INITIATIVE_LABEL`, `GH_REPO`, `BRANCH`, `WORKTREE`, `BATCH_ID`, `PARENT_RUN_ID`, `AC_URL`, `ROLE_FILE`, `HOST_ROLE_FILE`, `COGNITIVE_ARCH`. This is the primary writer for the agent-dispatch pipeline and is read by all downstream agent templates. | `6df61b6` |
| 3 | `agentception/mcp/plan_tools.py` | 275–280 | `source` | `plan_spawn_coordinator()` writes flat `KEY=VALUE\n` format for the coordinator `.agent-task`. Fields: `WORKFLOW`, `BATCH_ID`, `BRANCH`, `WORKTREE`, plus a non-TOML `ENRICHED_MANIFEST:` block with a fenced JSON payload. This file was never updated during the TOML v2 migration wave. | `a098e4d` (initial import; never migrated) |
| 4 | `agentception/tests/test_agentception_control.py` | 37 | `fixture` | Test fixture writes `"WORKFLOW=issue-to-pr\nISSUE_NUMBER=999\nGH_REPO=cgcardona/agentception\n"` as `.agent-task` content — old flat format. | `26a61fb` |
| 5 | `agentception/tests/test_toast.py` | 50 | `fixture` | Test fixture writes `"WORKFLOW=issue-to-pr\nISSUE_NUMBER=999\nGH_REPO=cgcardona/agentception\n"` — old flat format. | `26a61fb` |
| 6 | `agentception/tests/test_agentception_telemetry.py` | 52 | `fixture` | `_make_task_file()` helper writes `f"BATCH_ID={batch_id}\nISSUE_NUMBER={issue_number}\n"` — old flat format. | `26a61fb` |
| 7 | `agentception/tests/test_agentception_ui_plan.py` | 46, 143 | `fixture` | Line 46 (docstring example) and line 143 (mock return value) both use `"WORKFLOW=bugs-to-issues\nBATCH_ID=...\n"` flat format. | `26a61fb` |
| 8 | `agentception/tests/test_label_context_and_dispatch.py` | 295, 297, 373–376 | `test` | Assertions check for flat format strings: `"SCOPE_VALUE=ac-workflow/5-plan-step-v2"`, `"TIER=coordinator"`, `"SCOPE_TYPE=issue"`, `"SCOPE_VALUE=42"`, `"TIER=engineer"` — these assert the old K=V format in the written task content. These tests are coupled to the non-TOML output of finding #2. | `6df61b6` |
| 9 | `agentception/tests/test_lineage_fields.py` | 282–288 | `test` | Asserts `"TIER=" in source` to verify TIER is written — this assertion passes only because dispatch still writes flat K=V (finding #2). After remediation, the assertion must change to check the TOML `[agent] tier` field. | `f6fe516` |
| 10 | `agentception/tests/e2e/test_agentception_workflow_e2e.py` | 104, 183 | `test` | E2E tests check `.agent-task` contains `WORKFLOW=bugs-to-issues` (line 104) and `WORKFLOW=conductor` (line 183) — flat K=V format. | `a098e4d` (initial import; never updated) |
| 11 | `.agentception/parallel-issue-to-pr.md` | 500–504, 524, 533, 672, 722 | `template` | **fixed** — All `grep "^KEY="` reads replaced with `python3 -c "import tomllib; ..."` TOML-aware reads. Fields: `GH_REPO` → `repo.gh_repo`, `ISSUE_NUMBER` → `target.issue_number`, `ATTEMPT_N` → `task.attempt_n`, `BATCH_ID` → `pipeline.batch_id`, `ROLE` → `agent.role`, `COGNITIVE_ARCH` → `agent.cognitive_arch`, `DEPENDS_ON` → `target.depends_on`, `FILE_OWNERSHIP` → `target.file_ownership`. | `aaf8a75` (writer migrated; reader left behind) |
| 12 | `.agentception/parallel-pr-review.md` | 402–414, 463, 889, 1074, 1091, 1207, 1231, 1603, 1640–1641 | `template` | **fixed** — All `grep "^KEY="` reads replaced with TOML-aware reads. 20 replacements. Fields: `GH_REPO`, `PR_NUMBER`, `PR_BRANCH`, `MERGE_AFTER`, `HAS_MIGRATION`, `ATTEMPT_N`, `BATCH_ID`, `COGNITIVE_ARCH`, `WAVE`, `COORD_FINGERPRINT`, `ROLE`, `CLOSES_ISSUES`, `SPAWN_MODE`, `TASK_BATCH_ID` → their TOML v2 equivalents. | `aaf8a75` (writer migrated; reader left behind) |
| 13 | `.agentception/parallel-bugs-to-issues.md` | 423, 458, 464–468 | `template` | **fixed** — All `grep "^KEY="` reads replaced with TOML-aware reads. Fields: `GH_REPO` → `repo.gh_repo`, `ROLE` → `agent.role`, `PHASE_LABEL` → `target.phase_label`, `LABELS_TO_APPLY` → `target.labels_to_apply`, `PHASE_DEPENDS_ON_ISSUES` → `target.phase_depends_on_issues`, `ATTEMPT_N` → `task.attempt_n`. | `aaf8a75` (writer migrated; reader left behind) |
| 14 | `docs/guides/integrate.md` | 83–86 | `spec-markdown` | **fixed** — Documentation block updated to show TOML v2 format with `[task]`, `[agent]`, `[repo]`, `[pipeline]`, `[spawn]`, `[worktree]`, and `[enriched]` sections replacing the old flat `WORKFLOW=bugs-to-issues` K=V example. Section heading and prose updated to reflect TOML format. | `18ca1ab` |
| 15 | `.agentception/parallel-conductor.md` | 162, 164, 165, 363, 378 | `template` | **fixed** — All `grep "^KEY="` reads replaced with TOML-aware reads. Fields: `GH_REPO` → `repo.gh_repo`, `PHASE_FILTER` → `task.phase_filter`, `ATTEMPT_N` → `task.attempt_n`, `MAX_ISSUES_PER_DISPATCH` → `task.max_issues_per_dispatch`, `MAX_PRS_PER_DISPATCH` → `task.max_prs_per_dispatch`. | `aaf8a75` (writer migrated; reader left behind) |
| 16 | `.agentception/roles/cto.md` | 276–277, 982, 984–986, 1006, 1015, 1172, 1222, 2498, 2500–2505, 2509–2510, 2515, 2559, 2569, 3028, 3213, 3230, 3346, 3370, 3742, 3779–3780 | `template` | **fixed** — All 60 `grep "^KEY="` reads across all embedded coordinator and leaf kickoff sections replaced with TOML-aware reads. Fields migrated: full set — `COGNITIVE_ARCH` → `agent.cognitive_arch`, `ROLE` → `agent.role`, `GH_REPO` → `repo.gh_repo`, `ISSUE_NUMBER` → `target.issue_number`, `ATTEMPT_N` → `task.attempt_n`, `BATCH_ID` → `pipeline.batch_id`, `DEPENDS_ON` → `target.depends_on`, `FILE_OWNERSHIP` → `target.file_ownership`, `PR_NUMBER` → `target.pr_number`, `PR_BRANCH` → `target.pr_branch`, `MERGE_AFTER` → `target.merge_after`, `HAS_MIGRATION` → `target.has_migration`, `WAVE` → `pipeline.wave`, `COORD_FINGERPRINT` → `pipeline.coord_fingerprint`, `CLOSES_ISSUES` → `target.closes`, `SPAWN_MODE` → `spawn.mode`, `TASK_BATCH_ID` → `pipeline.batch_id`. | cut -d= -f2`. Broken fields include the full set: `COGNITIVE_ARCH`, `ROLE`, `GH_REPO`, `ISSUE_NUMBER`, `ATTEMPT_N`, `BATCH_ID`, `DEPENDS_ON`, `FILE_OWNERSHIP`, `PR_NUMBER`, `PR_BRANCH`, `MERGE_AFTER`, `HAS_MIGRATION`, `WAVE`, `COORD_FINGERPRINT`, `CLOSES_ISSUES`, `SPAWN_MODE`, `TASK_BATCH_ID`. This is the most operationally critical omission — `cto.md` is the primary document read by the live pipeline's top-level agent. | `aaf8a75` (writer migrated; reader left behind) |
| 17 | `.agentception/roles/engineering-coordinator.md` | 17–18, 723, 725–727, 747, 756, 913, 963, 2239, 2241–2246, 2250–2251, 2256 | `template` | **fixed** — All 30 `grep "^KEY="` reads replaced with TOML-aware reads. Same field mappings as finding #16 applied to the engineering-coordinator role file. | `aaf8a75` (writer migrated; reader left behind) |
| 18 | `.agentception/roles/qa-coordinator.md` | 17–18, 582, 584–589, 593–594, 599, 643, 653, 1112, 1297, 1314, 1430, 1454, 1826 | `template` | **fixed** — All 30 `grep "^KEY="` reads replaced with TOML-aware reads. Same field mappings as finding #16 applied to the qa-coordinator role file. | `aaf8a75` (writer migrated; reader left behind) |
| 19 | `.agentception/roles/pr-reviewer.md` | 12, 49, 50 | `template` | **fixed** — All 3 `grep "^KEY="` reads replaced with TOML-aware reads. Fields: `COGNITIVE_ARCH` → `agent.cognitive_arch`, `PR_NUMBER` → `target.pr_number`, `GH_REPO` → `repo.gh_repo`. | `aaf8a75` (writer migrated; reader left behind) |

---

## Inspection of 20 Most Recent Commits Touching `.agent-task` Content

The `git log --diff-filter=M` command in the worktree returned no results (the worktree branch has no prior commits). The following is based on the main repo history inspected from the host at `/Users/gabriel/dev/tellurstori/agentception`:

| Commit | Files touching `.agent-task` logic | TOML v2 present? | Notes |
|--------|------------------------------------|------------------|-------|
| `e9cf1aa` | `plan.html`, `plan.js`, `plan.scss` | N/A | UI only |
| `1507387` | `llm_phase_planner.py` | N/A | LLM stream fix |
| `fe8297b` | `.agentception/parallel-*.md` | Writer ✅, Reader ❌ | MCP sweep; reader grep not fixed |
| `aaf8a75` | `.agentception/parallel-*.md`, `scripts/gen_prompts/templates/` | Writer ✅, Reader ❌ | **Primary migration commit** — writer sections converted to TOML v2; reader grep sections not updated |
| `59b0ec7` | Merge | — | — |
| `baf87e4` | `.agentception/parallel-*.md` | Writer ✅, Reader ❌ | gh CLI → MCP; did not address format |
| `f6fe516` | `test_lineage_fields.py`, `.agentception/*` | ❌ | Introduced `assert "TIER=" in source` (finding #9) |
| `9c25f93` | `.agentception/roles/*.md` | N/A | Role files |
| `383e148` | Merge | — | — |
| `e67defc` | State machine | — | — |
| `7e4b890` | `routes/api/_shared.py`, `tests/test_agentception_spawn.py` | ✅ | Correct TOML v2 builder migration |
| `2bb3121` | Merge | — | — |
| `c0e17a4` | `parallel-issue-to-pr.md` comment | — | Minor fix |
| `041c235` | Merge | — | — |
| `ee48e83` | `routes/api/dispatch.py` | ❌ | Maintained flat K=V in dispatch |
| `a6f64eb` | Multiple routes | ❌ | Regression in dispatch |
| `dd205ac` | Merge | — | — |
| `e0c0b48` | State machine | — | — |
| `0c013c9` | Merge | — | — |
| `5bc5a98` | Merge | — | — |

---

## TOML Template / Render Sites Inventory

### Writers (source code that emits `.agent-task` content)

| File | Function | Status |
|------|----------|--------|
| `agentception/routes/api/_shared.py` | `_build_agent_task()` | ✅ TOML v2 |
| `agentception/routes/api/_shared.py` | `_build_coordinator_task()` | ✅ TOML v2 |
| `agentception/routes/api/_shared.py` | `_build_conductor_task()` | ✅ TOML v2 |
| `agentception/routes/api/dispatch.py` | `/api/dispatch` route handler (line ~184) | ❌ Flat K=V |
| `agentception/routes/api/dispatch.py` | `/api/dispatch-label` route handler (line ~480) | ❌ Flat K=V |
| `agentception/mcp/plan_tools.py` | `plan_spawn_coordinator()` (line ~275) | ❌ Flat K=V + non-TOML `ENRICHED_MANIFEST:` block |
| `agentception/services/spawn_child.py` | No direct `.agent-task` writes found | ✅ N/A |

### Readers (agent prompt templates that consume `.agent-task` content)

| File | Fields read via `grep "^KEY="` | Status |
|------|-------------------------------|--------|
| `.agentception/parallel-issue-to-pr.md` | GH_REPO, ISSUE_NUMBER, ATTEMPT_N, BATCH_ID, ROLE, COGNITIVE_ARCH, DEPENDS_ON, FILE_OWNERSHIP | ❌ Flat K=V grep |
| `.agentception/parallel-pr-review.md` | GH_REPO, PR_NUMBER, PR_BRANCH, MERGE_AFTER, HAS_MIGRATION, ATTEMPT_N, BATCH_ID, COGNITIVE_ARCH, WAVE, COORD_FINGERPRINT, ROLE, CLOSES_ISSUES, SPAWN_MODE, TASK_BATCH_ID | ❌ Flat K=V grep |
| `.agentception/parallel-bugs-to-issues.md` | GH_REPO, ROLE, PHASE_LABEL, LABELS_TO_APPLY, PHASE_DEPENDS_ON_ISSUES, ATTEMPT_N | ❌ Flat K=V grep |
| `.agentception/parallel-conductor.md` | GH_REPO, PHASE_FILTER, ATTEMPT_N, MAX_ISSUES_PER_DISPATCH, MAX_PRS_PER_DISPATCH | ❌ Flat K=V grep |
| `.agentception/roles/cto.md` | Full field set (all embedded coordinator + leaf sections inlined) | ❌ Flat K=V grep |
| `.agentception/roles/engineering-coordinator.md` | COGNITIVE_ARCH, ROLE, GH_REPO, ISSUE_NUMBER, ATTEMPT_N, BATCH_ID, DEPENDS_ON, FILE_OWNERSHIP, PR_NUMBER, PR_BRANCH, MERGE_AFTER, HAS_MIGRATION, WAVE, COORD_FINGERPRINT | ❌ Flat K=V grep |
| `.agentception/roles/qa-coordinator.md` | COGNITIVE_ARCH, ROLE, GH_REPO, PR_NUMBER, PR_BRANCH, MERGE_AFTER, HAS_MIGRATION, ATTEMPT_N, BATCH_ID, WAVE, COORD_FINGERPRINT, CLOSES_ISSUES, SPAWN_MODE, TASK_BATCH_ID | ❌ Flat K=V grep |
| `.agentception/roles/pr-reviewer.md` | COGNITIVE_ARCH, PR_NUMBER, GH_REPO | ❌ Flat K=V grep |

---

## Test Fixture Inventory

All test files that embed `.agent-task` content as strings or file writes:

| File | Lines | Format | Status |
|------|-------|--------|--------|
| `agentception/tests/test_agentception_worktrees.py` | 31–103 | TOML v2 | ✅ Compliant |
| `agentception/tests/test_agentception_spawn.py` | Full suite | TOML v2 | ✅ Compliant |
| `agentception/tests/test_agentception_control.py` | 37 | Flat K=V | ❌ Violation |
| `agentception/tests/test_toast.py` | 50 | Flat K=V | ❌ Violation |
| `agentception/tests/test_agentception_telemetry.py` | 52 | Flat K=V | ❌ Violation |
| `agentception/tests/test_agentception_ui_plan.py` | 46, 143 | Flat K=V | ❌ Violation |
| `agentception/tests/test_label_context_and_dispatch.py` | 295, 297, 373–376 | Asserts flat K=V | ❌ Violation (coupled to dispatch.py) |
| `agentception/tests/test_lineage_fields.py` | 282–288 | Asserts flat K=V | ❌ Violation (coupled to dispatch.py) |
| `agentception/tests/e2e/test_agentception_workflow_e2e.py` | 104, 183 | Asserts flat K=V | ❌ Violation |

---

## Worktree `.agent-task` File Status

The worktree `.agent-task` at `/Users/gabriel/.agentception/worktrees/agentception/issue-159-0e6cbe/.agent-task` uses the **old flat K=V format** (comment-prefixed `KEY=VALUE` lines). This file was generated by the `/api/dispatch-label` endpoint (finding #2), confirming that the live writer is still producing non-TOML content.

---

## Remediation Priority

Deferred to phase 1 (out of scope for this audit). Suggested order:

1. **Fix writers first** — findings #1, #2, #3 (`dispatch.py` × 2, `plan_tools.py` × 1). Route handlers should delegate to `_build_agent_task()` / `_build_coordinator_task()` from `_shared.py`.
2. **Update test fixtures** — findings #4–#10. Fixtures must use TOML v2 string content; assertions must check sectioned fields.
3. **Update template readers** — findings #11–#13. Replace `grep "^KEY=" .agent-task | cut -d= -f2` with TOML-aware reads (e.g., `python3 -c "import tomllib, sys; d=tomllib.loads(open('.agent-task').read()); print(d['section']['field'])"`).
4. **Update documentation** — finding #14 (`docs/guides/integrate.md`).
