# AgentCeption — Architecture

AgentCeption is a multi-agent orchestration system that translates high-level goals into phase-gated GitHub issue graphs and dispatches autonomous agents to execute each phase.

## Package Layout

```
agentception/
  app.py           → FastAPI application factory and lifespan manager
  config.py        → Pydantic Settings (unprefixed env vars)
  models.py        → PlanSpec, PlanIssue, PlanPhase, and domain models
  poller.py        → Background polling loop (pipeline state refresh)
  telemetry.py     → Structured logging setup

  routes/
    api/           → JSON/SSE API endpoints (including /api/runs/*/execute, /api/system/*)
    ui/            → Jinja2/HTMX/Alpine.js page handlers

  middleware/      → Starlette middleware (auth.py — API key validation)
  readers/         → LLM planner, GitHub client, worktree manager, transcript reader
  services/        → LLM calls (provider-agnostic API), agent loop, code indexer
    llm.py         → completion(), completion_stream(), completion_with_tools(); provider selection via config (Anthropic or local)
    agent_loop.py  → Server-side agent execution loop
    code_indexer.py → Qdrant codebase indexing + semantic search
  tools/           → Local agent tools (file I/O, shell, semantic search definitions)
    file_tools.py  → read_file, write_file, list_directory, search_text
    shell_tools.py → run_command (with denylist)
    definitions.py → OpenAI-format JSON schemas for all local tools
  mcp/             → MCP server (tools, resources, prompts, session management)
  db/              → SQLAlchemy async models, Alembic migrations, engine
  intelligence/    → Cognitive architecture engine
  static/          → Compiled JS/CSS bundles (build with npm run build)
  templates/       → Jinja2 HTML templates
  tests/           → Unit, integration, regression, and E2E tests (single directory)
```

## Services

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| agentception | agentception | 1337 | FastAPI application |
| postgres | agentception-postgres | 5433 | Persistent store (runs, phases, issues) |
| qdrant | agentception-qdrant | 6335/6336 | Vector store (semantic search) |

## Data Flow

### Planning pipeline (Phase 1A → GitHub issues → dispatch)

```
Browser / MCP client
      ↓
FastAPI routes (thin HTTP handlers)
      ↓
readers/ (LLM planner, GitHub, worktree)
      ↓
services/llm.py (LLM provider: Anthropic or local via config)
      ↓
GitHub API → Issues, PRs, Worktrees
      ↓
POST /api/runs/{run_id}/execute
      ↓
services/agent_loop.py
      ↓
PRs → merged → next phase unlocks
```

### Agent execution (per-run)

```
POST /api/runs/{run_id}/execute
      ↓
agent_loop.py
  ├─ load DB context
  ├─ load role markdown + resolve_arch.py (cognitive arch)
  ├─ build tool catalogue (file + shell + search_codebase + MCP)
  └─ conversation loop ─→ Anthropic Claude
         ↓ tool calls
   ┌─────────────────────────────────────────┐
   │  read_file / write_file / list_dir      │ ← tools/file_tools.py
   │  run_command (denylist enforced)        │ ← tools/shell_tools.py
   │  search_codebase (Qdrant cosine search) │ ← services/code_indexer.py
   │  GitHub / pipeline MCP tools           │ ← mcp/server.py
   └─────────────────────────────────────────┘
      ↓ on completion
  build_complete_run() / build_cancel_run()
```

### Codebase indexing (one-time, then incremental)

```
POST /api/system/index-codebase
      ↓ (background task)
code_indexer.py
  ├─ walk source files (.py .ts .md .yml …)
  ├─ chunk (~1500 chars, 200-char overlap)
  ├─ embed → fastembed BAAI/bge-small-en-v1.5 (ONNX, CPU, no API key)
  └─ upsert 384-dim vectors → Qdrant
```

## Agent Hierarchy

- **CTO-tier agent** — surveys unlocked phases, decides dispatch order
- **Coordinator-tier agent** — receives label scope, breaks into tickets, spawns engineers
- **Engineer-tier agents** — each owns one issue, implements in isolated git worktree, opens PR

Each agent receives a **cognitive architecture** composed from figures (historical thinkers), archetypes (thinking styles), skill domains, and behavioral atoms via `scripts/gen_prompts/resolve_arch.py`.

## Cognitive Architecture API

The Cognitive Architecture API assembles a per-agent personality and expertise profile at spawn time. It is composed of two parallel tracks that work together.

### Track A — Org Roles (`.agentception/roles/*.md`)

Markdown files that define the organizational identity and operational constraints for each role in the pipeline. These are the "what is this role responsible for" documents.

**Role taxonomy** (`scripts/gen_prompts/role-taxonomy.yaml`): The canonical source for the full three-tier org hierarchy.

| Tier | Count | Examples |
|------|-------|---------|
| C-Suite | 8 | CEO, CTO, CPO, CFO, CISO, CDO, CMO, COO |
| Coordinator Level | 10 | Engineering, QA, Product, Design, Data, Security, Infrastructure, Mobile, Platform, ML |
| Workers | 15 | python-developer, database-architect, reviewer, frontend-developer, full-stack-developer, mobile-developer, systems-programmer, ml-engineer, data-engineer, devops-engineer, security-engineer, test-engineer, architect, api-developer, technical-writer |

Managed via the Role Studio UI (`GET /roles`) and the roles API (`/api/roles/*`).

Only roles in `VALID_ROLES` (defined in `agentception/models.py`) can be spawned as leaf agents via `POST /api/control/spawn`. Orchestration roles (CTO, VPs) are spawnable only through the CTO pipeline.

### Track B — Cognitive Architecture (YAML composition)

A composable YAML system in `scripts/gen_prompts/cognitive_archetypes/` that defines how an agent thinks, not what it is responsible for.

**Hierarchy (bottom-up):**

```
atoms/          — 10 cognitive dimensions (epistemic_style, quality_bar, creativity_level, …)
archetypes/     — 8 named profiles composed from atoms (the_architect, the_guardian, the_hacker, …)
figures/        — 25 named personas extending archetypes (historical + industry leaders)
skill_domains/  — 12 technology-specific skill checklists
```

**25 figures (12 original + 13 new):**

Original historical figures: `dijkstra`, `einstein`, `feynman`, `hamming`, `hopper`, `knuth`, `lovelace`, `mccarthy`, `ritchie`, `shannon`, `turing`, `von_neumann`

New industry personas: `steve_jobs`, `satya_nadella`, `jeff_bezos`, `werner_vogels`, `margaret_hamilton`, `linus_torvalds`, `bjarne_stroustrup`, `martin_fowler`, `kent_beck`, `yann_lecun`, `andrej_karpathy`, `bruce_schneier`, `guido_van_rossum`

**COGNITIVE_ARCH string format:**

```
COGNITIVE_ARCH=figure1,figure2:skill1:skill2:skill3
```

Example: `COGNITIVE_ARCH=andrej_karpathy:llm:python` — Karpathy's build-from-scratch empiricism, with LLM and Python skill checklists injected into the agent's context.

**Resolution:** `scripts/gen_prompts/resolve_arch.py` assembles the final Markdown block injected into each agent's running context at spawn time.

### Cognitive Architecture API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/roles/taxonomy` | Full 3-tier org hierarchy from `role-taxonomy.yaml`. Returns levels → roles with `file_exists` flag. |
| `GET /api/roles/personas` | All 25 figure YAMLs as structured JSON. Used by the GUI persona cards. |
| `GET /api/roles/atoms` | All 10 atom dimensions with named values. Used by the primitive composer dropdowns. |
| `GET /api/roles` | List all managed role files with metadata. |
| `GET /api/roles/{slug}` | Get full content + metadata for one role. |
| `PUT /api/roles/{slug}` | Write new content (no commit). |
| `POST /api/roles/{slug}/diff` | Preview diff vs HEAD. |
| `POST /api/roles/{slug}/commit` | Write + git commit + record version. |
| `GET /api/roles/{slug}/versions` | Structured version history. |

### Role Studio GUI (`/roles`)

Three-panel layout:

- **Left — Org Hierarchy:** Collapsible tree by tier (C-Suite / Coordinator / Worker). Color-coded dots: green = file authored, purple = spawnable leaf agent, grey = draft. Click to select.
- **Center — Personas + Composer:** Two tabs:
  - *Personas*: Card grid of compatible historical/industry personas for the selected role. Click to apply to the composer.
  - *Primitive Composer*: Dropdowns for figure, per-atom overrides, and skill domain checkboxes. Displays the assembled `COGNITIVE_ARCH` string.
- **Right — Role Studio:** Monaco editor for editing the selected role's `.md` file. Diff preview and git commit via the existing roles API.

---

## PlanSpec — YAML Schema Contract

`PlanSpec` is the root Pydantic v2 model that forms the **typed YAML contract** between the Step 1.A (brain-dump → spec) and Step 1.B (spec → enriched GitHub issue manifest) stages of the plan-step-v2 pipeline.

Defined in `agentception/models.py`. Serialized to/from YAML via `to_yaml()` / `from_yaml()`.

### Model Hierarchy

```
PlanSpec
├── initiative: str               # short slug for the batch (e.g. "auth-rewrite")
└── phases: list[PlanPhase]       # ordered; index 0 = foundation (no deps)
    ├── label: str                # phase slug used as GitHub label (e.g. "0-foundation")
    ├── description: str          # one-sentence human summary
    ├── depends_on: list[str]     # labels of phases that must complete first
    └── issues: list[PlanIssue]   # ordered list of issues to create
        ├── title: str            # issue title
        ├── body: str             # issue body (Markdown)
        └── depends_on: list[str] # titles of prerequisite issues
```

### Invariants

| Invariant | Enforced by |
|-----------|-------------|
| `phases` must be non-empty | `@field_validator("phases")` on `PlanSpec` |
| Each phase's `issues` must be non-empty | `@field_validator("issues")` on `PlanPhase` |
| Phase `depends_on` labels must reference only previously-defined phase labels (no forward refs, no cycles) | `@model_validator(mode="after")` on `PlanSpec` |
| Malformed YAML raises `ValueError` | `from_yaml()` catches `yaml.YAMLError` |
| Non-mapping YAML root raises `ValueError` | `from_yaml()` type-guards the parsed object |

### Annotated YAML Example

```yaml
initiative: auth-rewrite

phases:
  - label: 0-foundation
    description: Core auth types and JWT validation primitives
    depends_on: []
    issues:
      - title: Define AuthToken Pydantic model
        body: |
          ## Summary
          Add `AuthToken(BaseModel)` with fields
          `token: str`, `expires_at: datetime`, `scopes: list[str]`.
        depends_on: []

      - title: Add JWT validation helper
        body: |
          ## Summary
          Implement `validate_jwt(token: str) -> AuthToken`.
          Must raise `ValueError` on expired or malformed tokens.
        depends_on:
          - Define AuthToken Pydantic model

  - label: 1-api
    description: REST endpoints for authentication flow
    depends_on:
      - 0-foundation
    issues:
      - title: "POST /auth/login endpoint"
        body: |
          ## Summary
          Add `POST /api/auth/login` that accepts `{username, password}`,
          validates credentials, and returns a signed `AuthToken`.
        depends_on: []
```

### Serialization Contract

| Method | Signature | Behaviour |
|--------|-----------|-----------|
| `to_yaml()` | `(self) -> str` | Serializes to clean PyYAML `safe_dump` output. No Pydantic internal fields. Order preserved (`sort_keys=False`). |
| `from_yaml()` | `(cls, text: str) -> PlanSpec` | Parses with `yaml.safe_load`, then calls `model_validate`. Raises `ValueError` on any error (parse, type, invariant). |

---

## TaskRunner Abstraction

The `TaskRunner` protocol (`agentception/services/task_runner.py`) defines a runner-agnostic interface for executing agent tasks. It decouples coordinators from specific execution engines (Cursor, Anthropic, etc.), allowing the system to swap implementations without changing orchestration logic. Concrete implementations live in sibling modules and are selected via the `ac_task_runner` config setting. The protocol uses structural subtyping (`@runtime_checkable`) to verify implementations at runtime without requiring explicit inheritance.

---

## Query module structure

`agentception/db/queries/` is a Python package. All existing call-sites use
`from agentception.db.queries import X` and continue to work through the
re-exporting `__init__.py`. The package is split into focused submodules to
eliminate parallel-agent merge conflicts on the former 3250-line monolith.

| File | Domain ownership |
|------|-----------------|
| `types.py` | All `TypedDict` return-value shapes shared across the package. No query logic. |
| `board.py` | Board issues, initiative phases, label state, wave summaries, workflow states, and grouped-phase views. |
| `runs.py` | Agent run lifecycle — run rows, active runs, run detail, tree traversal, teardown, and execution plan loading. |
| `messages.py` | Agent thought stream queries (`get_agent_thoughts_tail`). |
| `events.py` | Agent event tail queries and file-edit event hydration. |
| `metrics.py` | Daily metrics, run status counts, throughput, and cost calculation. |

`__init__.py` re-exports every public symbol (and a small set of test-required
private helpers) using the `import X as X` pattern so that mypy's strict
re-export checks are satisfied.

All six files carry `merge=union` in `.gitattributes` to prevent spurious
conflicts when parallel agents append to different domain files simultaneously.

---

## SCSS partial structure

`agentception/static/scss/pages/_build.scss` is a barrel file that imports six
focused partials in cascade order. Edit the partial — never the barrel.

| Partial | Rule ownership |
|---------|---------------|
| `_inspector-layout.scss` | Inspector chrome, toolbar, header, sidebar layout, and general OD structural rules. |
| `_thought-block.scss` | `.thought-block` collapsible sections for LLM reasoning display. |
| `_file-edit-card.scss` | `.file-edit-card` diff viewer cards including `.diff-add`, `.diff-remove`, `.diff-context`. |
| `_assistant-bubble.scss` | `.assistant-bubble` prose message bubbles. |
| `_tool-call-card.scss` | `.tool-call-card` expandable tool-call detail cards. |
| `_event-card.scss` | `.event-card` generic SSE event cards in the event log. |

All six partials carry `merge=union` in `.gitattributes` for the same
parallel-agent conflict-reduction reason as the query submodules.

---

## Context window management

Five mechanisms in `agentception/services/agent_loop.py` keep the context window from overflowing:

1. **Per-turn token logging** — `last_input_tokens` is captured from each `completion_with_tools()` response and logged at INFO level with iteration number, input tokens, output tokens, and cache hit count (line ∼705).
2. **Token-aware history pruning** — `_prune_history()` applies a message-count guard (`_MAX_HISTORY_MESSAGES = 20`), then when `last_input_tokens > _MAX_INPUT_TOKEN_ESTIMATE` (140 000), runs a character-heuristic loop that drops messages from index 1 until the estimate falls below the threshold, always keeping `messages[0]` (task briefing) and the last `_HISTORY_TAIL = 14` messages.
3. **Context pressure warning** — when `last_input_tokens > _CONTEXT_PRESSURE_THRESHOLD` (100 000), `_CONTEXT_PRESSURE_WARNING` is injected into `extra_blocks` each turn, advising targeted reads and `replace_in_file` and reporting the remaining context budget.
4. **Context checkpoint summarisation** — when the token-budget loop drops more than 4 messages, `_summarise_history()` (async, max 1 000 tokens) compresses the dropped messages into a `[Context checkpoint]` user message inserted at index 1, preserving a compressed record of prior work.
5. **Stop-reason=length recovery** — when the API returns `stop_reason="length"`, a one-sentence continuation hint is injected so the agent can resume without losing the current task context.

---

## Further Reading

- [Plan Spec format](plan-spec.md)
- [Plan-scoped integration branch](architecture/plan-scoped-integration-branch.md)
- [LLM provider abstraction](architecture/llm-provider-abstraction.md)
- [Agent tree protocol](agent-tree-protocol.md)
- [Cursor-Free Agent Loop](guides/agent-loop.md)
- [MCP integration guide](guides/mcp.md)
- [Security Guide](guides/security.md)
- [Type contracts reference](reference/type-contracts.md)
