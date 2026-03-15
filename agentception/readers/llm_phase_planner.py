from __future__ import annotations

"""LLM-powered plan generator — converts a brain dump into a PlanSpec YAML.

Public entry point:

``generate_plan_yaml(dump)``
    Step 1.A: calls the configured LLM via :func:`~agentception.services.llm.completion`,
    returns a validated PlanSpec YAML string ready for the Monaco editor.
    Provider is selected in the LLM layer (e.g. Anthropic when ANTHROPIC_API_KEY is set).

Architecture note
-----------------
MCP is NOT involved in this module.  The browser -> AgentCeption -> Anthropic
loop is entirely self-contained.  MCP enters only after the user approves the
YAML and a coordinator worktree is spawned -- the coordinator agent (in Cursor)
calls ``plan_get_labels()`` and similar tools as it files GitHub issues.
"""

import logging
import re
from pathlib import Path

import yaml as _yaml

from agentception.models import PlanSpec
from agentception.services.llm import completion
from agentception.types import JsonValue

# Paths to the cognitive architecture assets (resolved relative to this file).
_FIGURES_DIR: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
)
_SKILL_DOMAINS_DIR: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "cognitive_archetypes" / "skill_domains"
)
_TAXONOMY_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "role-taxonomy.yaml"
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cognitive architecture persona — prepended to the system prompt
# ---------------------------------------------------------------------------

_IDENTITY = """\
## Identity

You are a decisive planning engine. Read the input, produce the plan, stop.

Every issue you output is executed verbatim by an autonomous AI agent — a git
branch, an implementation, a PR, a merge. Be specific, be concrete, be done.

## Rules

- Read the user's input once. Decide the phases and issues. Output the YAML. Do
  not reconsider, do not output alternatives, do not restart.
- Issues within a phase run in parallel. Use `depends_on` only for hard data or
  API dependencies (e.g. a model must exist before a route that queries it).
- Do not invent work the user did not mention.
- Do not pad phases. One phase is fine. Six is fine. Match the work.
"""

# ---------------------------------------------------------------------------
# System prompt — Full PlanSpec YAML (Step 1.A production output)
# ---------------------------------------------------------------------------

_YAML_SYSTEM_PROMPT = _IDENTITY + """\

## Output format: PlanSpec YAML

Your entire response is passed to yaml.safe_load() then PlanSpec.model_validate().
Return ONLY valid YAML. No prose, no explanation, no markdown fences, no preamble.

## Phase naming

Each phase gets a label in the format `{N}-{semantic-slug}` where:
- N is the 0-indexed position of the phase (0, 1, 2, ...).
- slug is a short kebab-case descriptor of what the phase delivers.
Examples: `0-foundation`, `1-api-layer`, `2-ui`, `3-polish`.

Rules:
- Use as many phases as the work genuinely requires. One phase is fine.
  Six phases is fine. Do not force-fit all work into a fixed number.
- Phase N+1 should depend on phase N unless the work is genuinely parallel.
- Never emit a phase with zero issues — every phase must contain at least one.
- Choose slugs that communicate the gate criterion: what must be true for
  the next phase to begin?

Schema (follow exactly):

initiative: user-auth                # short kebab-case slug inferred from the work
coordinator_arch:                    # figure IDs are snake_case (person names); see Cognitive architecture section below
  cto: werner_vogels:python:fastapi  # pick from valid figures listed in that section
  engineering-coordinator: linus_torvalds:python
phases:
  - label: 0-foundation
    description: "Scaffold User model, migration, and DB schema"  # max 100 chars, no trailing period
    depends_on: []
    issues:
      - id: user-auth-p0-001
        title: "Add SQLAlchemy User model and Alembic migration"
        skills: [python, postgresql]  # 1-3 IDs from the list below
        cognitive_arch: barbara_liskov:python:postgresql  # required on every issue; snake_case figure id
        body: |
          ## Context
          The application has no persistent user store. Auth endpoints return 501.

          ## Objective
          Add a SQLAlchemy `User` model with `id`, `email`, and `hashed_password`
          fields and generate an Alembic migration so the table exists in Postgres.

          ## Implementation notes
          - Model lives in `agentception/db/models.py` alongside existing models.
          - Migration: `alembic revision --autogenerate -m "add_user_table"`.
          - `email` must have a unique index; `hashed_password` is a non-nullable string.
          - Do not add auth logic — that is out of scope for this issue.

          ## Acceptance criteria
          - [ ] `User` model exists in `agentception/db/models.py`.
          - [ ] Alembic migration applies cleanly on a fresh DB with `alembic upgrade head`.
          - [ ] `email` column has a unique constraint enforced at the DB level.
          - [ ] mypy passes with zero errors on `agentception/db/models.py`.

          ## Test coverage
          Add `tests/test_models.py::test_user_model_fields` asserting column names,
          types, and the unique constraint via SQLAlchemy introspection.

          ## Documentation
          None — internal model, no public API surface yet.

          ## Out of scope
          Password hashing, login endpoints, sessions, JWT — all handled in later phases.
        depends_on: []
  - label: 1-api-layer
    description: "Expose User CRUD via FastAPI routes"
    depends_on: [0-foundation]
    issues:
      - id: user-auth-p1-001
        title: "Add POST /users and GET /users/{id} endpoints"
        skills: [fastapi, python]
        cognitive_arch: guido_van_rossum:fastapi:python
        body: |
          ## Context
          The `User` model exists (user-auth-p0-001) but no API surface exposes it.

          ## Objective
          Implement `POST /users` (create) and `GET /users/{id}` (fetch) as thin
          FastAPI route handlers delegating to a `UserService`.

          ## Implementation notes
          - Routes in `agentception/routes/api/users.py`; auto-discovered via `__init__.py`.
          - `UserService` in `agentception/services/user_service.py` owns DB logic.
          - Request body: `UserCreate(email: str, password: str)` Pydantic model.
          - Response: `UserRead(id: int, email: str)` — never expose `hashed_password`.
          - Hash passwords with `passlib.hash.bcrypt` before persisting.

          ## Acceptance criteria
          - [ ] `POST /users` returns 201 with `UserRead` on valid input.
          - [ ] `POST /users` returns 409 on duplicate email.
          - [ ] `GET /users/{id}` returns 200 with `UserRead` or 404 if not found.
          - [ ] `hashed_password` never appears in any response body.
          - [ ] mypy passes with zero errors on new files.

          ## Test coverage
          `tests/test_users.py` — integration tests for both endpoints using the
          async test client. Cover happy path, duplicate email, and 404 cases.

          ## Documentation
          Update `docs/reference/api.md` with the two new endpoints and their schemas.

          ## Out of scope
          Authentication, authorization, JWT — not part of this issue.
        depends_on: [user-auth-p0-001]

## Field rules

initiative
  Short kebab-case slug from the dominant theme (e.g. auth-rewrite).

id (issue level)
  Stable kebab-case slug: {initiative}-p{phase_number}-{issue_number}.
  Example: auth-rewrite-p0-001. Must be unique across the entire plan.
  This is the dependency reference key — never changes even if the title changes.

description (phase level)
  HARD LIMIT: 100 characters maximum. GitHub uses this as a label tooltip.
  One tight phrase: theme + gate criterion. No trailing period.
  Good: 'Scaffold DB schema, migrations, and core models'
  Bad:  'Set up the database layer by writing SQLAlchemy models, Alembic
         migrations, and seed data so the API layer has a stable schema.'

title
  Imperative mood. Specific. Standalone GitHub issue title.
  Good: "Fix intermittent 503 on mobile login"
  Bad:  "Authentication work"

skills (issue level)
  A YAML list of 1-3 skill domain IDs identifying the primary technology
  domains this issue touches. Used to select the cognitive architecture
  (domain expert persona) injected into the implementing agent.
  Choose from this exact set (use the id, not the display name):
  __SKILL_IDS__
  If unsure, use python as the sole entry. Never invent skill ids.

depends_on (issue level)
  Issue IDs (not titles) this issue waits for. Use sparingly — only for hard
  data or API dependencies. Reference only IDs defined earlier. Never self-reference.

## Validation constraints (any violation rejects the plan)

- Top-level keys are `initiative`, `coordinator_arch`, `phases` — never nest phases under the initiative slug.
- Every phase has at least one issue. No empty phases.
- Phase labels use `{N}-{slug}` format (e.g. `0-foundation`), not bare `phase-0`.
- Phase descriptions are 100 characters max.
- Issue IDs are unique across the plan. `depends_on` references IDs only (not titles), and only IDs defined earlier (no forward references).
- Every issue includes all seven body sections in order: Context, Objective, Implementation notes, Acceptance criteria, Test coverage, Documentation, Out of scope.
- Every issue has `cognitive_arch`. The plan has `coordinator_arch`.
- No markdown fences around the output.

## Vague input

If the input is too vague, output a minimal valid plan (never refuse, never ask for clarification):

initiative: clarify-and-scope
coordinator_arch:
  cto: margaret_hamilton:python
  engineering-coordinator: linus_torvalds:python
phases:
  - label: 0-scope
    description: "Define project scope and requirements"
    depends_on: []
    issues:
      - id: clarify-and-scope-p0-001
        title: "Define project scope and requirements"
        skills: [python]
        cognitive_arch: guido_van_rossum:python
        body: |
          ## Context
          The project brief was too vague to extract concrete tasks.

          ## Objective
          Work with the team to define concrete scope, deliverables, and constraints.

          ## Implementation notes
          - Schedule a scope definition session.
          - Document decisions in the project wiki.

          ## Acceptance criteria
          - [ ] Scope document approved by stakeholders.
          - [ ] At least three concrete deliverables identified.

          ## Test coverage
          None required — this is a planning issue.

          ## Documentation
          Create initial project scope document.

          ## Out of scope
          Any implementation work until scope is approved.
        depends_on: []

Even a single-phase, single-issue YAML is a valid output. Never refuse.
"""

# ---------------------------------------------------------------------------
# Cognitive architecture catalog — injected into the system prompt at call time
# ---------------------------------------------------------------------------

# The orchestration tiers the planner must fill in coordinator_arch for.
# A figure catalog is injected for each role so the model can make an informed choice.
_COORDINATOR_ROLES: list[str] = [
    "cto",
    "engineering-coordinator",
    "qa-coordinator",
]


def _build_skill_ids() -> str:
    """Return a sorted, comma-separated string of all skill domain IDs.

    Reads the skill_domains directory at call time so the list stays in sync
    with the filesystem automatically — adding a new YAML file is sufficient.
    Falls back to 'python' if the directory is absent (e.g. in CI without assets).
    """
    if not _SKILL_DOMAINS_DIR.exists():
        return "python"
    ids = sorted(p.stem for p in _SKILL_DOMAINS_DIR.glob("*.yaml"))
    if not ids:
        return "python"
    return ", ".join(ids)


def _first_sentence(text: str) -> str:
    """Return the first sentence of *text* (up to the first period or newline)."""
    stripped = text.strip()
    line = stripped.split("\n")[0]
    return line.split(". ")[0].rstrip(".")


def _build_figure_catalog_section() -> str:
    """Build the cognitive architecture section appended to the system prompt.

    Reads the taxonomy and figure YAMLs at call time so the catalog is always
    fresh.  Returns an empty string when the assets are absent (e.g. in tests
    that run without the scripts directory).
    """
    if not _TAXONOMY_PATH.exists() or not _FIGURES_DIR.exists():
        return ""

    try:
        raw_taxonomy: JsonValue = _yaml.safe_load(
            _TAXONOMY_PATH.read_text(encoding="utf-8")
        )
    except Exception:
        return ""

    if not isinstance(raw_taxonomy, dict):
        return ""

    # Build a slug → compatible_figures mapping for coordinator roles.
    role_figures: dict[str, list[str]] = {}
    raw_levels: JsonValue = raw_taxonomy.get("levels", [])
    if not isinstance(raw_levels, list):
        return ""
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        raw_roles: JsonValue = level.get("roles", [])
        if not isinstance(raw_roles, list):
            continue
        for role_entry in raw_roles:
            if not isinstance(role_entry, dict):
                continue
            slug_val: JsonValue = role_entry.get("slug", "")
            slug = str(slug_val) if isinstance(slug_val, str) else ""
            if slug in _COORDINATOR_ROLES:
                raw_figs: JsonValue = role_entry.get("compatible_figures", [])
                if isinstance(raw_figs, list):
                    role_figures[slug] = [str(f) for f in raw_figs]

    def _describe_figures(fig_ids: list[str]) -> str:
        lines: list[str] = []
        for fig_id in fig_ids:
            path = _FIGURES_DIR / f"{fig_id}.yaml"
            if not path.exists():
                continue
            try:
                fig_raw: JsonValue = _yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(fig_raw, dict):
                continue
            display = str(fig_raw.get("display_name", fig_id))
            desc = _first_sentence(str(fig_raw.get("description", "")))
            lines.append(f"  - {fig_id}: {display} — {desc}")
        return "\n".join(lines)

    parts: list[str] = [
        """\

## Cognitive architecture assignment

Every PlanSpec you produce MUST include two cognitive architecture blocks:

### 1. coordinator_arch (plan level)

Add a `coordinator_arch` mapping at the TOP of the YAML (before `phases`).
Keys are role slugs; values are arch strings in the format `figure_id:skill1[:skill2]`.
Always include at least `cto` and `engineering-coordinator`.
Include `qa-coordinator` when the work clearly involves QA/testing phases.

Select the figure that best matches the EPISTEMIC STYLE the initiative requires —
not just the tech stack. Skills (after the colon) should match the dominant
technology domain from the initiative.

Example:
  coordinator_arch:
    cto: werner_vogels:python:fastapi
    engineering-coordinator: linus_torvalds:python
    qa-coordinator: w_edwards_deming:testing

Available figures per coordinator role:
""",
    ]

    for role in _COORDINATOR_ROLES:
        figs = role_figures.get(role, [])
        parts.append(f"\n**{role}**:\n{_describe_figures(figs)}\n")

    parts.append("""\

### 2. cognitive_arch (per issue)

Add a `cognitive_arch` field to EVERY issue in the plan.
Format: `figure_id:skill1[:skill2]`.
Select the figure whose epistemic style best fits the WORK in that specific issue.

Key principle: the figure encodes HOW to think, not WHAT to build.
Match the figure to the nature of the problem:
- Correctness-critical or algorithmic work → dijkstra, leslie_lamport, barbara_liskov
- Scale / distributed systems → jeff_dean, werner_vogels
- Minimal, systems-level code → ken_thompson, rob_pike
- Language / type system / API design → anders_hejlsberg, barbara_liskov
- Testing / quality gates → kent_beck, michael_fagan, w_edwards_deming
- Security → bruce_schneier
- ML / LLM integration → andrej_karpathy, jeff_dean
- General Python backend → guido_van_rossum
- Frontend / UX → don_norman
- Full-stack / pragmatic → lovelace, hopper

The skills part (after the colon) should match the `skills` list for that issue.

Example issue with cognitive_arch:
  - id: auth-p0-001
    title: Add JWT authentication middleware
    skills: [fastapi, python]
    cognitive_arch: barbara_liskov:fastapi:python
    body: |
      ...

IMPORTANT: `cognitive_arch` is REQUIRED on every issue. Never omit it.
""")

    return "".join(parts)


# Cache the catalog section so the filesystem is only read once per process.
_COGNITIVE_ARCH_SECTION: str | None = None


def _get_cognitive_arch_section() -> str:
    """Return the cached cognitive arch section, building it on first call."""
    global _COGNITIVE_ARCH_SECTION
    if _COGNITIVE_ARCH_SECTION is None:
        _COGNITIVE_ARCH_SECTION = _build_figure_catalog_section()
    return _COGNITIVE_ARCH_SECTION


def _build_yaml_system_prompt() -> str:
    """Return the full system prompt with skill IDs and cognitive arch injected."""
    prompt = _YAML_SYSTEM_PROMPT.replace("__SKILL_IDS__", _build_skill_ids())
    return prompt + _get_cognitive_arch_section()


# ---------------------------------------------------------------------------
# Fallback plan when LLM returns prose or no valid YAML (never push back)
# ---------------------------------------------------------------------------

_FALLBACK_CLARIFY_PLAN_YAML = """\
initiative: clarify-and-scope
coordinator_arch:
  cto: margaret_hamilton:python
  engineering-coordinator: linus_torvalds:python
phases:
  - label: 0-scope
    description: "Define project scope and requirements"
    depends_on: []
    issues:
      - id: clarify-and-scope-p0-001
        title: "Define project scope and requirements"
        skills: [python]
        cognitive_arch: guido_van_rossum:python
        body: |
          ## Context
          The project brief was too vague to extract concrete tasks.

          ## Objective
          Work with the team to define concrete scope, deliverables, and constraints.

          ## Implementation notes
          - Schedule a scope definition session.
          - Document decisions in the project wiki.

          ## Acceptance criteria
          - [ ] Scope document approved by stakeholders.
          - [ ] At least three concrete deliverables identified.

          ## Test coverage
          None required — this is a planning issue.

          ## Documentation
          Create initial project scope document.

          ## Out of scope
          Any implementation work until scope is approved.
        depends_on: []
"""


def get_fallback_plan_spec() -> PlanSpec:
    """Return the minimal clarify-and-scope PlanSpec when the LLM returns prose or no valid YAML.

    We never push back with an error: if the model does not produce valid YAML,
    we load this fallback so the user always gets a valid plan they can edit.
    """
    data: JsonValue = _yaml.safe_load(_FALLBACK_CLARIFY_PLAN_YAML)
    if not isinstance(data, dict):
        raise RuntimeError("Fallback plan YAML is invalid")
    return PlanSpec.model_validate(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences if the model wraps its output in them."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = "\n".join(lines[1:])
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3].rstrip()
        return inner.strip()
    return raw


_FENCED_YAML_RE: re.Pattern[str] = re.compile(
    r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL
)


def _extract_yaml_from_mixed(text: str) -> str | None:
    """Find a YAML plan inside text that mixes prose and code fences.

    Local models (e.g. Qwen via mlx-openai-server) send thinking and content
    in the same ``content`` stream.  When the accumulated buffer starts with
    prose, ``_strip_fences`` returns it unchanged and parsing fails.

    This function tries two strategies:
    1. Extract the content of the first fenced code block (```yaml ... ```).
    2. Find the first line starting with ``initiative:`` and take from there.

    Returns the extracted YAML string, or None if nothing looks like a plan.
    """
    m = _FENCED_YAML_RE.search(text)
    if m:
        return m.group(1).strip()
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("initiative:"):
            return "\n".join(text.splitlines()[i:]).strip()
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def generate_plan_yaml(dump: str, label_prefix: str = "") -> str:
    """Step 1.A -- convert a brain dump into a validated PlanSpec YAML string.

    Calls Claude Sonnet via the Anthropic API with the full PlanSpec YAML prompt.
    Validates the returned YAML against :class:`~agentception.models.PlanSpec`
    so the CodeMirror 6 editor always shows a structurally correct document.

    If ``label_prefix`` is provided it overrides the ``initiative`` field
    Claude inferred from the text.

    Args:
        dump: Raw plan text from the user.
        label_prefix: Optional initiative slug override (from the UI options field).

    Returns:
        A YAML string that validates against ``PlanSpec``.

    Raises:
        ValueError: Empty dump, invalid YAML from LLM, or schema mismatch.
        RuntimeError: Missing ANTHROPIC_API_KEY.
        httpx.HTTPStatusError: Non-2xx from Anthropic.
    """
    dump = dump.strip()
    if not dump:
        raise ValueError("Plan text must not be empty.")

    raw = await completion(
        dump,
        system_prompt=_build_yaml_system_prompt(),
        temperature=0.2,
        max_tokens=8192,
    )
    raw = _strip_fences(raw)

    try:
        data: JsonValue = _yaml.safe_load(raw)
    except _yaml.YAMLError as exc:
        logger.error("LLM returned invalid YAML: %s\nRaw (first 500): %s", exc, raw[:500])
        raise ValueError(f"LLM returned invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"LLM YAML top level is {type(data).__name__}, expected mapping.")

    if label_prefix:
        data["initiative"] = label_prefix

    try:
        spec = PlanSpec.model_validate(data)
    except Exception as exc:
        logger.error("LLM YAML failed PlanSpec validation: %s", exc)
        raise ValueError(f"LLM output does not match PlanSpec schema: {exc}") from exc

    issue_count = sum(len(p.issues) for p in spec.phases)
    validated_yaml: str = spec.to_yaml()
    logger.info(
        "✅ PlanSpec YAML generated: initiative=%s phases=%d issues=%d",
        spec.initiative,
        len(spec.phases),
        issue_count,
    )
    return validated_yaml
