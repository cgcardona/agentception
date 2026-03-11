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
  services/        → LLM calls (Anthropic API), agent loop, code indexer
    llm.py         → call_anthropic(), call_anthropic_with_tools()
    agent_loop.py  → Cursor-free agent execution loop
    code_indexer.py → Qdrant codebase indexing + semantic search
  tools/           → Local agent tools (file I/O, shell, semantic search definitions)
    file_tools.py  → read_file, write_file, list_directory, search_text
    shell_tools.py → run_command (with denylist)
    definitions.py → OpenAI-format JSON schemas for all local tools
  mcp/             → MCP server for Cursor/Claude tool integration
  db/              → SQLAlchemy async models, Alembic migrations, engine
  intelligence/    → Cognitive architecture engine
  static/          → Compiled JS/CSS bundles (build with npm run build)
  templates/       → Jinja2 HTML templates
  tests/           → Unit, integration, regression, and E2E tests (single directory)
```

## Service Dependencies

| Service    | Purpose                                 | Port  |
|------------|-----------------------------------------|-------|
| agentception | FastAPI application                   | 10003 |
| postgres   | Persistent store (runs, phases, issues) | 5433  |
| qdrant     | Vector store (semantic search)          | 6335  |

## Data Flow

### Planning pipeline (Phase 1A → GitHub issues → dispatch)

```
Browser / Cursor MCP
      ↓
FastAPI routes (thin HTTP handlers)
      ↓
readers/ (LLM planner, GitHub, worktree)
      ↓
services/llm.py (Anthropic API, HTTPS)
      ↓
GitHub API → Issues, PRs, Worktrees
      ↓
POST /api/runs/{run_id}/execute  ← Cursor-free dispatch
      ↓
services/agent_loop.py
      ↓
PRs → merged → next phase unlocks
```

### Cursor-free agent execution (per-run)

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

## Further Reading

- [Plan Spec format](plan-spec.md)
- [Agent tree protocol](agent-tree-protocol.md)
- [Cursor-Free Agent Loop](guides/agent-loop.md)
- [MCP integration guide](guides/mcp.md)
- [Security Guide](guides/security.md)
- [Type contracts reference](reference/type-contracts.md)
