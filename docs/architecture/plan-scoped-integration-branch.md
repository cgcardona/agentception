# Plan-scoped integration branch (parent feature branch per plan)

**Idea:** When a multi-phase, multi-issue plan is created and approved, create a **single parent feature branch** for that plan. All work (all phases, all issues) lands on that parent branch; only when the **entire plan is complete** do we merge the parent branch into `dev`. So `dev` is never left in a half-broken state from a partially merged plan.

**Status:** Implemented. Plan branch is created on first dispatch; PR base and rebase target use the plan branch; when the last issue's PR is merged into the plan branch, a plan→dev PR is opened and a reviewer is dispatched.

---

## 1. Why it makes sense

- **Today:** Each dispatched agent gets a worktree branched from `origin/dev`. When the agent completes, its PR is merged into `dev`. So `dev` accumulates one merge per issue. If plan has 10 issues, `dev` gets 10 merges over time. Any other agent that forks from `dev` in between can see a partially updated codebase (e.g. phase 0 done, phase 1 not) or a broken intermediate state (e.g. phase 0 merged but introduced a bug that phase 1 would fix).
- **Proposed:** One **plan-scoped branch** (e.g. `feat/plan-{id}-{slug}`) is created from `dev` when the plan is approved. Every issue in that plan:
  - Uses that plan branch as the **base** for its worktree (not `dev`).
  - Opens a PR **into the plan branch** (not into `dev`).
  - When the PR is merged, it lands on the plan branch. `dev` is untouched.
- **When the plan is complete** (all issues merged into the plan branch), we merge the plan branch into `dev` **once**. So `dev` only ever sees a single merge per plan, and that merge is a coherent unit (all phases/issues of that plan).

Benefits:

- **`dev` stays consistent:** No half-finished plan on `dev`. Other agents (or other plans) that branch from `dev` always see either “plan not started” or “plan fully merged.”
- **Fewer merge conflicts on `dev`:** Conflicting work is resolved on the plan branch; only the final resolution is merged to `dev`.
- **Clear rollback:** If a plan was wrong, you revert one merge on `dev` (the plan merge) instead of many.
- **Scales to many agents:** Many agents can work in parallel on the **same plan** (different issues), all targeting the plan branch; they don’t step on each other’s base.

---

## 2. Model (conceptual)

```
Plan approved
    │
    ├─ Create branch: feat/plan-{plan_id}-{slug} from origin/dev
    │  (e.g. feat/plan-readme-section or feat/plan-42-add-auth)
    │
    ▼
For each issue in the plan:
    ├─ worktree base = origin/feat/plan-{plan_id}-{slug}   (not origin/dev)
    ├─ branch = feat/issue-{N}  (or feat/plan-{plan_id}-issue-{N})
    ├─ Agent works, opens PR: head feat/issue-{N} → base feat/plan-{plan_id}-{slug}
    └─ Merge PR → lands on plan branch (dev unchanged)
    │
    ▼
When plan is “complete” (all issues merged into plan branch):
    └─ One PR: head feat/plan-{plan_id}-{slug} → base dev
       Merge → dev now has the full plan in one commit (or squash).
```

So:

- **Base for worktrees:** Plan branch (when plan-scoped) or `origin/dev` (when not).
- **PR target:** Plan branch for issue PRs; `dev` only for the final “plan complete” PR.
- **Who merges the plan into dev?** Either automation when the plan is marked complete, or a human. Design choice.

---

## 3. Design questions

| Question | Options |
|----------|--------|
| **When is the plan branch created?** | When the plan is approved in the Build UI; or when the first issue in the plan is dispatched. Creating at approval keeps a single place to “create plan branch” before any work starts. |
| **How do we know which issues belong to a plan?** | Plan has a stable ID (e.g. from Phase 1A YAML or from the batch/label that launched the plan). Store plan_id on the run or derive from label (e.g. `readme-rules-section/0-readme` → plan = initiative or phase). Need a consistent way to group “all issues that are part of this plan.” |
| **Naming the plan branch** | `feat/plan-{plan_id}-{slug}` or `integration/{initiative}` or `feat/{initiative}-{short_id}`. Should be unique and recognizable. |
| **Final merge into dev** | (a) Automatic when “plan complete” is detected (e.g. all issues merged into plan branch); (b) Manual (human opens PR plan → dev and merges); (c) Optional “Complete plan” button in UI that opens the PR and/or merges. |
| **What if the plan is cancelled or partially abandoned?** | Plan branch can stay open (and never be merged into dev), or be deleted. Policy: don’t delete if there are merged PRs into it that we might want to cherry-pick; otherwise safe to delete. |
| **Interaction with reviewers** | Reviewer today branches from the **implementer’s branch** and opens PR implementer → dev. With plan branch: implementer’s PR goes to plan branch. Reviewer could still branch from implementer’s branch and open PR into plan branch (so reviewer PR: implementer → plan). Flow stays the same; only the target branch changes. |
| **Rebase / “update from dev”** | While working on the plan branch, `dev` may have moved (other plans merged). Before merging plan → dev we’d rebase or merge dev into the plan branch to resolve conflicts. So “plan complete” might mean: all issue PRs merged into plan branch, then rebase plan branch onto dev, then open PR plan → dev. |

---

## 4. Where it plugs in

- **Dispatch:** When creating the worktree, `worktree_base` is today always `origin/dev` (or the reviewer’s branch). It would become `origin/feat/plan-{id}-{slug}` when the dispatch is for an issue that belongs to a plan that has a plan branch. So we need:
  - A way to know “this dispatch is for issue X which is part of plan P.”
  - A way to get “plan P’s branch name” and ensure it exists (create from dev if first use).
- **PR creation:** Today the agent (or MCP) opens a PR with base `dev`. It would need to use the plan branch as base when the run is plan-scoped.
- **Reconcile / “plan complete”:** When all issues in the plan have their PRs merged into the plan branch, we could mark the plan “complete” and either auto-open a PR plan → dev or surface a “Merge plan into dev” action in the UI.
- **Data model:** We may need to persist “plan_id” and “plan_branch” (e.g. in a new table or on the run/batch) so that dispatch and PR logic can look up the base branch.

---

## 5. Trade-offs

- **Pros:** Dev never half-broken per plan; single merge per plan; clear rollback; parallel agents on same plan don’t pollute dev.
- **Cons:** More branches to manage; need a clear definition of “plan” and “plan complete”; final merge (plan → dev) can have conflicts if dev moved a lot; we must create and maintain the plan branch at the right time.

---

## 6. Recommendation

The idea is sound and aligns with “dev is always stable; feature work is isolated.” Next steps could be:

1. **Define “plan” in the system** — e.g. plan_id = initiative label or a new ID assigned when the user approves the Phase 1A YAML. Ensure every dispatched issue can be associated with a plan (or “no plan” for one-off dispatches).
2. **Add plan branch creation** — When a plan is approved (or first issue dispatched), create `feat/plan-{id}-{slug}` from `origin/dev` and push it. Store plan_branch in config or DB.
3. **Make worktree base and PR base configurable per run** — From dispatch params or from run’s plan_id, set `worktree_base` and PR base to the plan branch when present; otherwise keep current behavior (origin/dev).
4. **Implement “plan complete”** — When all issues in the plan have merged PRs into the plan branch, offer “Merge plan into dev” (open PR or merge), with rebase of plan branch onto dev first if desired.

This keeps current behavior for “single issue” or “no plan” dispatches (branch from dev, PR to dev) and adds the plan-scoped path when a plan is in use.
