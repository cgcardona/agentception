# Cursor decoupling — taxonomy and action plan

AgentCeption is **LLM-agnostic** and does not launch or depend on Cursor for agent execution. All agent runs use the Cursor-free loop (Anthropic, Ollama, or other backends). This document is a full sweep of every Cursor reference in the codebase and a taxonomy for decoupling.

**Principles:**

- **Keep (acceptable):** Cursor as one possible *client* for MCP (like Claude Desktop). Docs can say "e.g. Cursor" where they explain where users put `mcp.json`. Project files `.cursorrules` / `.cursorignore` are IDE-specific and live in the repo; we don't depend on them at runtime.
- **Remove or reword:** Any code path, config, or doc that implies Cursor is required or that we read/write the `.cursor/` directory. No `TaskRunnerChoice.cursor`, no `cursor_project_id` in our data model unless we generalize it. No naming that suggests ".cursor" is our config location.

---

## 1. Taxonomy

| Tier | Description | Action |
|------|-------------|--------|
| **A. Runtime / config coupling** | Code or config that reads/writes `.cursor/` or branches on "Cursor" as execution backend | **Remove or generalize** |
| **B. Data model / API** | Fields or endpoints named after Cursor (e.g. `cursor_project_id`, "cursor files") | **Rename or drop** |
| **C. Naming / comments** | Function or variable names, docstrings, or comments that say "cursor" but mean ".agentception" or "IDE" | **Rename / reword** |
| **D. Documentation — MCP client** | Docs that tell users to edit `~/.cursor/mcp.json` or "restart Cursor" | **Keep as "e.g. Cursor"** — MCP is client-agnostic; Cursor is one client |
| **D. Documentation — Cursor-specific** | Docs that describe Cursor sandbox, allowlist, or internal DB (agent-command-policy, dispatcher walkthrough) | **Move to optional "Cursor as MCP client"** or generalize to "MCP client" |
| **E. Project files** | `.cursorrules`, `.cursorignore`, repo-owned `.cursor/mcp.json` | **Keep** `.cursorrules`/`.cursorignore`; **do not** rely on `.cursor/` in app code; optional: add `.cursor/` to `.gitignore` if we don't want to track `mcp.json` |
| **F. Third-party / false positives** | `.venv`, pip, rich, DOM "cursor" (CSS/UI) | **Ignore** |

---

## 2. Full inventory by tier

### A. Runtime / config coupling (remove or generalize)

| Location | Current | Action |
|----------|---------|--------|
| `agentception/config.py` | `TaskRunnerChoice.cursor` / `TaskRunnerChoice.anthropic`; docstring says "Cursor IDE with Composer agent" | **Remove** `TaskRunnerChoice.cursor` and all references. Default is `anthropic`; no other runner in code. |
| `agentception/config.py` | `ac_task_runner: TaskRunnerChoice` field and `AC_TASK_RUNNER` env | **Remove** `ac_task_runner` and `AC_TASK_RUNNER` if no code path uses `cursor`; else keep enum as `anthropic` only (or rename to execution backend later). |
| `agentception/tests/test_config.py` | Tests for `AC_TASK_RUNNER=cursor` and invalid value | **Remove** cursor test; keep anthropic default and invalid-value test. |
| `scripts/debug_loop.py` | Comment "TaskRunnerChoice (enum: cursor \| anthropic)" | **Update** to describe actual backends (e.g. anthropic only, or current enum). |
| `docker-compose.ci.yml` | Volume `- /tmp/ci-cursor:/root/.cursor` | **Remove** volume. No app code should read `.cursor`; CI doesn't need a dummy dir. |
| `docs/guides/ci.md` | "Removes the `~/.cursor` mount" | **Reword** to state we don't mount or use `.cursor` in CI. |

**Verification:** Grep for `ac_task_runner`, `TaskRunnerChoice`, `AC_TASK_RUNNER` — no execution path branches on them; only config default and tests reference them.

---

### B. Data model / API (rename or drop)

| Location | Current | Action |
|----------|---------|--------|
| `agentception/models/__init__.py` | `ProjectConfig.cursor_project_id`: "Cursor project slug used to locate transcript files" | **Rename** to `ide_project_id` or `transcript_project_id` (generic), or **deprecate** if transcripts are DB-only and this is unused. |
| `agentception/routes/roles.py` | Summary "List all managed role and **cursor** files" | **Change** to "List all managed role files" (allowlist is only `.agentception/roles/*.md`). |
| `agentception/models/__init__.py` | `RoleMeta` docstring "managed role or **cursor configuration** file" | **Change** to "managed role file". |
| `agentception/templates/config.html` | Shows `project.cursor_project_id` as "cursor: …" | **Rename** to `ide_project_id` / "Project ID" or remove if we drop the field. |
| `agentception/tests/test_agentception_pipeline_config.py` | Test data uses `cursor_project_id` | **Rename** to chosen field name or remove from test payloads if field is deprecated. |
| `docs/reference/type-contracts.md` | Documents `cursor_project_id` and `TaskRunnerChoice` | **Update** to match renames/removals. |

**Verification:** No API or UI says "cursor" as a required concept. Transcript resolution is either DB-only or uses a generic "project id" for any IDE.

---

### C. Naming / comments (rename or reword)

| Location | Current | Action |
|----------|---------|--------|
| `agentception/routes/ui/docs.py` | `_scan_cursor_docs()` — scans `.agentception/*.md` | **Rename** to `_scan_agentception_docs()` or `_scan_docs()`. |
| `agentception/config.py` | `ac_dir` docstring "not in ``.cursor/``, which belongs to the IDE" | **Keep** — clarifies we don't use .cursor; no change. |
| `agentception/config.py` | Docstrings for `host_worktrees_dir` / `host_repo_dir` saying "open in Cursor" / "Cursor agents on the host" | **Reword** to "agents running on the host" or "IDE on the host". |
| `agentception/readers/__init__.py` | "Cursor transcript files" | **Change** to "transcript storage" or "DB transcript records". |
| `agentception/readers/llm_phase_planner.py` | "coordinator agent (in Cursor)" | **Change** to "coordinator agent (e.g. in an MCP client)". |
| `agentception/routes/api/agent_run.py` | "Cursor-free agent loop" | **Keep** — describes the architecture (no Cursor dependency). |
| `agentception/routes/ui/agents.py` | "Filesystem transcript — Cursor JSONL file" in docstring | **Update** to "Postgres ``ac_agent_messages``" as primary; remove Cursor JSONL mention if no longer used. |
| `agentception/db/models.py` | "One row per message in a Cursor transcript" / "Cursor agent transcript" | **Change** to "agent transcript" or "agent message". |
| `agentception/db/queries/messages.py` | "captured from Cursor transcripts by the poller" | **Change** to "captured from agent runs" or "stored by the agent loop". |
| `docker-compose.yml` | Comments "spawned Cursor agents" / "Cursor agents (on the host)" | **Reword** to "agents running on the host" or "IDE on the host". |
| `agentception/tests/test_git_reader.py` | "Cursor fix branches are NOT agent branches" | **Change** to "fix/something branches are NOT agent branches" (remove "Cursor"). |
| `tools/typing_audit.py` | "Checks every rule from .cursorrules and AGENTS.md" | **Keep** — .cursorrules is a file path, not a Cursor dependency. |

---

### D. Documentation — MCP client (keep, optionally generalize)

These refer to Cursor as **one** MCP client. Keep as-is or add "e.g. Cursor".

| Location | Current | Action |
|----------|---------|--------|
| `docs/guides/setup.md` | Step 8 / N: "Configure Cursor MCP", `~/.cursor/mcp.json`, "restart Cursor", Cursor bug workaround | **Keep** section; title can be "Configure MCP client (e.g. Cursor)". |
| `docs/guides/dispatcher-walkthrough.md` | Entire doc: run dispatcher from Cursor, `~/.cursor/mcp.json`, allowlist, Cursor bug | **Keep**; add one line that any MCP client can be used; Cursor is the documented example. |
| `docs/guides/mcp.md` | "e.g. `~/.cursor/mcp.json` for Cursor, or the equivalent for your IDE" | **Keep** — already client-agnostic. |
| `docs/reference/mcp.md` | MCP server reference | **No change** — no Cursor-specific logic. |
| `README.md` | "MCP Integration (Cursor / Claude)" | **Keep** or "MCP Integration (e.g. Cursor, Claude)". |
| `docs/README.md` | "Connect Cursor / Claude", "Cursor-Free Agent Loop" | **Keep**; "Cursor-Free" is accurate. |
| `docs/guides/security.md` | "Configure the key in Cursor's mcp.json" | **Reword** to "Configure the key in your MCP client's config (e.g. Cursor's mcp.json)". |

---

### D. Documentation — Cursor-specific behavior (optional "Cursor as client" appendix)

| Location | Current | Action |
|----------|---------|--------|
| `.agentception/agent-command-policy.md` | Describes Cursor tiers, sandbox, allowlist, `Cmd+Shift+J`, `.cursor/sandbox.json` | **Move** to a doc like `docs/guides/mcp-client-cursor.md` or keep in .agentception with a note: "When using Cursor as the MCP client, …". Do not remove — useful for Cursor users. |
| `docs/cursor-agent-spawning.md` | Historical "Cursor Agent Spawning" | **Keep** as historical; add one-line note at top: "Legacy; agents now run via Cursor-free loop." |
| `docs/plan-spec.md` | "Coordinator agent (Cursor / MCP)" | **Change** to "Coordinator agent (MCP client, e.g. Cursor)". |
| `docs/plans/agent-speedup.md` | Table "Cursor" as owner | **Change** to "MCP / IDE" or "Human" if it's about who runs tasks. |

---

### E. Project files (keep; do not depend on .cursor in app)

| Location | Current | Action |
|----------|---------|--------|
| `.cursorrules` | Agent rules for Cursor IDE | **Keep** — project convention; no app dependency. |
| `.cursorignore` | Ignore patterns for Cursor | **Keep**. |
| `.cursor/mcp.json` | Example/local MCP config in repo | **Optional:** Add `.cursor/` to `.gitignore` so each dev uses their own; or keep as committed example. Do **not** read this path in application code. |

---

### F. False positives (ignore)

| Location | Reason |
|----------|--------|
| `agentception/static/js/thought_block.ts` | DOM "cursor" (blinking cursor in UI). |
| `agentception/static/js/org_chart_tree.ts` | CSS `cursor: pointer`. |
| `.venv-mlx/`, `pip/_vendor/rich/` | Third-party code; "cursor" is terminal/UI. |
| `agentception/static/scss/**` | Comment "transcript" only. |

---

## 3. Summary table

| Category | Count (approx) | Action |
|----------|----------------|--------|
| A. Runtime/config | 6 | Remove `TaskRunnerChoice.cursor`, `AC_TASK_RUNNER` usage; remove CI `.cursor` volume. |
| B. Data/API | 6 | Rename/drop `cursor_project_id`; fix "cursor files" in roles API/docs. |
| C. Naming/comments | 12+ | Rename `_scan_cursor_docs`; reword "Cursor" in docstrings/comments to "IDE" or "MCP client". |
| D. Docs (MCP client) | 7 | Keep; optionally add "e.g. Cursor". |
| D. Docs (Cursor-specific) | 4 | Keep as optional Cursor-as-client material. |
| E. Project files | 3 | Keep `.cursorrules` / `.cursorignore`; don't read `.cursor/` in app. |
| F. False positives | — | Ignore. |

---

## 4. Recommended implementation order

1. **A (runtime):** Remove `TaskRunnerChoice.cursor`, `AC_TASK_RUNNER` parsing for `cursor`, and the CI volume. Grep for any use of `ac_task_runner == TaskRunnerChoice.cursor` and delete.
2. **B (data):** Rename `cursor_project_id` → `ide_project_id` (or similar) across models, API, UI, tests, and type-contracts; or remove if transcripts are DB-only and this is unused. Fix roles API summary and RoleMeta docstring.
3. **C (naming):** Rename `_scan_cursor_docs` → `_scan_agentception_docs`; reword all "Cursor" in config/reader/route/db comments to "IDE" or "MCP client" where appropriate.
4. **Docs:** Pass over D items (add "e.g. Cursor" where needed; move Cursor-specific behavior to optional guide).
5. **E:** Confirm no code reads `.cursor/`; add `.cursor/` to `.gitignore` if desired.

After this, the only remaining "Cursor" mentions are: (1) MCP client docs ("e.g. Cursor"), (2) optional Cursor-as-client guide, (3) project files `.cursorrules` / `.cursorignore`, and (4) historical cursor-agent-spawning doc. No runtime or config coupling remains.

---

## 5. Completed (decoupling applied)

All items above have been implemented:

- **A:** Removed `TaskRunnerChoice.cursor`; kept `anthropic` only. Removed CI volume `/tmp/ci-cursor:/root/.cursor`. Updated `docs/guides/ci.md`.
- **B:** Renamed `cursor_project_id` → `ide_project_id` in models, templates, tests, and type-contracts. Updated roles API summary and `RoleMeta` docstring. Test `test_write_pipeline_config_persists` now uses `.agentception/` for the config path.
- **C:** Renamed `_scan_cursor_docs` → `_scan_agentception_docs`. Reworded config/readers/routes/db/docker-compose comments and docstrings. Updated `templates/transcripts.html` empty state. Removed stale "Cursor projects directory" row from `settings.html`.
- **D:** Setup and dispatcher-walkthrough titles/descriptions generalized to "MCP client (e.g. Cursor)". Security guide reworded. `cursor-agent-spawning.md` legacy note added. `plan-spec.md` and `agent-speedup.md` updated. `.agentception/agent-command-policy.md` prefixed with "When using Cursor as the MCP client" note.
- **E:** Confirmed no application code reads `.cursor/`. Added `.cursor/` to `.gitignore`.
