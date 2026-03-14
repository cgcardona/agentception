# Planning prompts and tight tickets — standards

This guide describes how the planning pipeline (Phase 1A → 1B) turns human input into GitHub issues, and gives **informal rules for writing plan text (brain dumps) so that the resulting tickets are tight enough for autonomous agents to execute without follow-up tightening**.

---

## Pipeline overview

| Step | Input | Output | Owner |
|------|--------|--------|--------|
| **1A** | Free-text plan (brain dump) from a human | PlanSpec YAML (phases + issues with titles and bodies) | Claude via `llm_phase_planner.py` |
| **1B** | Human review/edit of the YAML in CodeMirror | Same PlanSpec, validated | Human |
| **Launch** | Validated PlanSpec | GitHub issues created verbatim from each `PlanIssue` | `issue_creator.py` |

What you type in the plan text box is the **only** prompt the 1A LLM sees (plus a large system prompt that defines the YAML schema and style). There is no separate “issue tightening” step after 1A — so the quality of the brain dump directly determines whether agents get vague tickets or tight ones.

---

## What 1A receives and produces

### Human prompt (input to 1A)

- **Single free-text field** — often called the “brain dump” or “plan text”.
- Sent as the user message to Claude; the system prompt is fixed (schema, field rules, anti-patterns, cognitive-arch catalog).
- No structured fields: no “issue 1 title”, “issue 1 body” — the LLM infers phases and issues from your prose.

### What 1A produces

- **PlanSpec YAML** with:
  - `initiative`: short slug
  - `coordinator_arch`: CTO / engineering-coordinator (and optionally qa-coordinator) with figure:skills
  - `phases`: list of `{ label, description, depends_on, issues }`
  - Each **issue**: `id`, `title`, `skills`, `cognitive_arch`, `body`, `depends_on`

The **body** of each issue is the full Markdown that will become the GitHub issue body. The 1A system prompt tells the model: *"Every issue title and body you write will be created verbatim as a GitHub issue and executed by an autonomous AI agent with no human review. Write as if you are writing the actual GitHub issue — not a summary of it."*

### What 1B and Launch do

- **1B:** Human edits the YAML in the browser (fix titles, add detail, reorder, etc.), then submits. Validation runs against the PlanSpec schema.
- **Launch:** Backend creates one GitHub issue per `PlanIssue`; the issue `title` and `body` are taken directly from the YAML. Labels (initiative, phase, pipeline/active or pipeline/gated) are applied per `issue_creator.py`.

So the **format** of a ticket is exactly the PlanSpec issue: `id`, `title`, and a **body** that must contain seven sections in this order (enforced by 1A anti-patterns and validation):

1. **Context**
2. **Objective**
3. **Implementation notes**
4. **Acceptance criteria**
5. **Test coverage**
6. **Documentation**
7. **Out of scope**

---

## Why tickets end up loose

- The brain dump is high-level (“audit watch_run and document emission sites”). The LLM invents a reasonable-looking issue body but doesn’t constrain **how** the agent spends its iteration budget.
- Read-heavy or doc-only work (audits, mappings, reference docs) has no natural “done” signal in the wording — so the agent keeps grepping/reading until the loop guard or iteration cap.
- No explicit “workflow” or “cap” in the ticket (e.g. “use at most N steps to gather, then write the doc”) so the agent is never told to **stop** gathering and **ship**.

Tightening after the fact (editing the issue in GitHub or re-running 1A with a better dump) works but doesn’t scale. The goal is to **write brain dumps that cause 1A to emit tight tickets the first time**.

---

## Rules for human prompts (brain dumps) that yield tight tickets

Use these when writing the plan text that you paste into the 1A input. They are informal guidelines; the LLM is not explicitly prompted with this list, but following them makes it much more likely that the generated issue bodies will be agent-ready.

### 1. One clear deliverable per issue

- Say explicitly what the **single artifact** is: e.g. “Produce a Markdown doc at `docs/reference/watch-run-log-map.md`” or “Add endpoint `GET /api/health` that returns 200 and a JSON body.”
- Avoid “explore X and then maybe do Y”. Prefer “Produce X; do not do Y (out of scope).”

### 2. Cap read-heavy and audit work

- For audits, mappings, and reference docs, **state a workflow and a cap** in the brain dump so 1A bakes it into Implementation notes:
  - Example: “Use at most 15–20 grep/read steps to locate emission sites, then write the doc. Do not keep searching once you have enough to fill the table.”
  - This gives the agent a clear “stop gathering, start writing” rule and reduces runaway read-only loops.

### 3. Be specific about locations and constraints

- Name **files, functions, or line ranges** when they matter: e.g. “Pattern definitions in `scripts/watch_run.py` around lines 42–110; dispatch logic 192–510.”
- Call out **do-not** constraints: “Do not change production code”; “Document only — no event emission.”

### 4. Define “done” in acceptance criteria

- In your brain dump, imply or state checkable criteria: “Table has a row for every pattern”; “Every row has non-empty Emission file:function:line and Proposed subtype”; “No production source files modified.”
- 1A will turn these into Acceptance criteria bullets; the agent (and reviewers) then have a clear definition of done.

### 5. Keep phases and issues minimal

- Ask for the **minimum number of phases and issues** that match real dependencies. Extra issues mean more dispatch overhead and more chances for vague wording.
- If one person could do the work in one sitting, one issue is often enough.

### 6. Mention format when it matters

- If the deliverable has a required shape (e.g. a Markdown table with given columns), say so in the brain dump. 1A will copy that into Implementation notes and Acceptance criteria.

### 7. Separate “document only” from “implement”

- For doc-only or audit-only work, say explicitly: “Documentation only. No code changes. No new event emission.” So 1A can set Out of scope and Implementation notes accordingly and the agent doesn’t drift into code.

---

## Where this is enforced

| Rule | Enforced where |
|------|-----------------|
| Seven body sections, order, no TBD | 1A system prompt (anti-patterns); PlanSpec validation |
| Issue id format, depends_on references | PlanSpec validators |
| “Write as if the actual GitHub issue” | 1A system prompt (narrative) |
| Caps, workflow, do-nots, one deliverable | **Not** auto-enforced — follow the informal rules above when writing the brain dump so 1A emits them in the issue body |

---

## References

- **PlanSpec schema and lifecycle:** `docs/plan-spec.md`, `docs/architecture.md` (Planning pipeline).
- **1A implementation:** `agentception/readers/llm_phase_planner.py` (system prompt, schema, cognitive-arch injection).
- **1B / Launch:** `agentception/readers/issue_creator.py`; planning UI in `agentception/routes/ui/plan_ui.py`.
- **Issue body section order:** Context → Objective → Implementation notes → Acceptance criteria → Test coverage → Documentation → Out of scope (see `llm_phase_planner.py` anti-patterns).
