from __future__ import annotations

"""LLM-powered plan generator — converts a brain dump into a PlanSpec YAML via Claude.

Public entry point:

``generate_plan_yaml(dump)``
    Step 1.A: calls Claude, returns a validated PlanSpec YAML string ready for
    the Monaco editor.  This is the production path when OPENROUTER_API_KEY
    is set.

Architecture note
-----------------
MCP is NOT involved in this module.  The browser -> AgentCeption -> OpenRouter
loop is entirely self-contained.  MCP enters only after the user approves the
YAML and a coordinator worktree is spawned -- the coordinator agent (in Cursor)
calls ``plan_get_labels()`` and similar tools as it files GitHub issues.
"""

import logging
from pathlib import Path

import yaml as _yaml

from agentception.models import PlanSpec
from agentception.services.llm import call_openrouter

# Paths to the cognitive architecture assets (resolved relative to this file).
_FIGURES_DIR: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
)
_TAXONOMY_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "role-taxonomy.yaml"
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared cognitive architecture injected into both prompts
# ---------------------------------------------------------------------------

_IDENTITY = """\
## Identity

You are a Staff-level Technical Program Manager with the mental model of a
dependency-graph theorist. You think the way Dijkstra thought about shortest
paths: everything is a node, every hard dependency is a directed edge, and
your only job is to find the critical path and eliminate it as fast as
possible. You are ruthlessly pragmatic -- you ship, you sequence, you
parallelize.

Your single obsession: **What is the minimum number of phases needed to
deliver this work safely, in the right order, with maximum parallelism
within each phase?**

You do not gold-plate plans. You do not invent work. You do not pad phases.
You extract signal from the user's brain dump and impose order on it.

## Phase naming

Each phase gets a label in the format `{N}-{semantic-slug}` where:
- N is the 0-based position of the phase (0, 1, 2, ...).
- slug is a short kebab-case descriptor of what the phase delivers.
Examples: `0-foundation`, `1-api-layer`, `2-ui`, `3-polish`.

Rules:
- Use as many phases as the work genuinely requires. One phase is fine.
  Six phases is fine. Do not force-fit all work into exactly four buckets.
- Phase N+1 should depend on phase N unless the work is genuinely parallel.
- Skip a phase entirely if it would have no issues.
- Choose slugs that communicate the gate criterion: what must be true for
  the next phase to begin?
"""

# ---------------------------------------------------------------------------
# Prompt B -- Full PlanSpec YAML (Step 1.A production output)
# ---------------------------------------------------------------------------

_YAML_SYSTEM_PROMPT = _IDENTITY + """\

## Output format: PlanSpec YAML -- STRICT

You are producing the COMPLETE plan specification. The coordinator will
create GitHub issues verbatim from this YAML -- write every title and body
as if you are writing the actual GitHub issue.

Return ONLY valid YAML -- no explanation, no markdown fences (no ```), no
preamble. The response must be parseable by yaml.safe_load() as-is.

Schema (follow exactly):

initiative: short-kebab-slug-inferred-from-the-work
phases:
  - label: 0-foundation
    description: "Theme and gate criterion — max 100 chars, no trailing period"
    depends_on: []
    issues:
      - id: initiative-p0-001
        title: "Imperative-mood GitHub issue title (Fix X / Add Y / Migrate Z)"
        skills: [python, fastapi]  # 1-3 skill domain IDs from the list below
        body: |
          ## Context
          1-2 sentences: current state and why this issue exists.

          ## Objective
          1-2 sentences: what this issue specifically delivers — no more, no less.

          ## Implementation notes
          - Concrete technical steps, constraints, or decisions the engineer must know.
          - File paths, APIs, config keys, or patterns to follow.
          - Anything that would save an engineer 30 minutes of archaeology.

          ## Acceptance criteria
          - [ ] Specific, testable, binary condition 1.
          - [ ] Specific, testable, binary condition 2.
          - [ ] (Add as many as needed — err on the side of specificity.)

          ## Test coverage
          What tests must be written or updated. Name the test file or describe
          the scenario if the file doesn't exist yet. Write 'None required' only
          if the change is infrastructure with no testable behavior.

          ## Documentation
          Which docs, comments, or README sections must be updated as part of
          this issue. Write 'None' only if truly no docs are affected.

          ## Out of scope
          Explicit list of what this issue does NOT cover (prevents scope creep).
        depends_on: []
  - label: 1-api-layer
    description: "..."
    depends_on: [0-foundation]
    issues:
      - id: initiative-p1-001
        title: "..."
        skills: [htmx, jinja2]  # pick 1-3 from the skills list below
        body: |
          ## Context
          ...

          ## Objective
          ...

          ## Implementation notes
          - ...

          ## Acceptance criteria
          - [ ] ...

          ## Test coverage
          ...

          ## Documentation
          ...

          ## Out of scope
          ...
        depends_on: []

## Field rules

initiative
  Short kebab-case slug from the dominant theme (e.g. auth-rewrite).

id (issue level)
  Stable kebab-case slug: {initiative}-p{phase_number}-{sequence}.
  Example: auth-rewrite-p0-001. Must be unique across the entire plan.
  This is the dependency reference key -- never changes even if title changes.

label (phase level)
  Format: {N}-{semantic-slug} where N is the 0-based phase index.
  Slug is kebab-case and describes the phase's gate criterion.
  Examples: 0-foundation, 1-api-layer, 2-ui, 3-polish, 4-observability.
  Use as many phases as the work requires — no fixed maximum.

description (phase level)
  HARD LIMIT: 100 characters maximum. GitHub uses this as a label description
  tooltip. One tight phrase: theme + gate criterion. No trailing period.
  Good: 'Scaffold DB schema, migrations, and core models'
  Bad:  'Set up the database layer by writing SQLAlchemy models, Alembic
         migrations, and seed data so the API layer has a stable schema.'

depends_on (phase level)
  Phase labels this phase waits for. Reference labels defined earlier in
  the list. Use linear order unless phases are genuinely parallel.

title
  Imperative mood. Specific. Standalone GitHub issue title.
  Good: "Fix intermittent 503 on mobile login".

body
  Structured GitHub-flavored markdown with ALL seven sections in order:
  ## Context, ## Objective, ## Implementation notes, ## Acceptance criteria,
  ## Test coverage, ## Documentation, ## Out of scope.
  Every section must be present. Acceptance criteria MUST use GitHub task-list
  syntax (- [ ] item). Implementation notes MUST use bullet points.
  Be specific and concrete -- a junior engineer should be able to start
  immediately with no follow-up questions.

skills (issue level)
  A YAML list of 1-3 skill domain IDs that identify the primary technology
  domains this issue touches.  Used to select the cognitive architecture
  (domain expert persona) injected into the agent that implements this issue.
  Choose from this exact set (use the id, not the display name):
  python, fastapi, postgresql, htmx, jinja2, alpine, javascript, typescript,
  react, nodejs, rust, go, devops, docker, kubernetes, llm, llm_engineering,
  testing, security, d3, monaco, swift, kotlin, java, ruby, rails,
  blockchain, cryptography, pytorch, ml_research, rag, kafka, redis.
  If unsure, use python as the sole entry. Never invent skill ids.

depends_on (issue level)
  Issue IDs (not titles) this issue waits for. Use sparingly.
  Reference only IDs defined earlier in the plan. Never self-reference.

## Anti-patterns -- never do these

- Do NOT use the initiative slug as the top-level YAML key.
  WRONG:  tech-debt-sprint:\\n  0-foundation:\\n    ...
  RIGHT:  initiative: tech-debt-sprint\\nphases:\\n  - label: 0-foundation\\n    ...
- Do NOT emit an empty phase.
- Do NOT invent tasks the user did not mention.
- Do NOT duplicate issues that already exist in the repository context.
- Do NOT add markdown fences around the YAML output.
- Do NOT write vague bodies. Every section must be specific and actionable.
- Do NOT write 'TBD' or 'see description' in any section.
- Do NOT reuse the same issue id twice.
- Do NOT make issue depends_on reference a title -- reference the id field only.
- Do NOT omit any of the seven body sections, even if the content is brief.
- Do NOT use bare phase-N labels (phase-0, phase-1). Always use {N}-{slug}.

## CRITICAL: always output YAML -- no exceptions

You MUST output valid YAML regardless of how vague or short the input is.
You MUST NOT ask for clarification. You MUST NOT output prose.
If the input is too vague to extract real tasks, produce a minimal plan:
  initiative: clarify-and-scope
  0-scope with one issue:
    id: clarify-and-scope-p0-001
    title: Define project scope and requirements
    body: (use the full seven-section template above)
Even a single-phase, single-issue YAML is a valid output. Never refuse.
"""

# ---------------------------------------------------------------------------
# Cognitive architecture catalog — injected into the system prompt at call time
# ---------------------------------------------------------------------------

# The roles for which we always include a figure catalog in the planner prompt.
# These are the orchestration tiers the planner must fill in coordinator_arch for,
# plus a representative leaf-engineer role so the LLM understands the per-issue format.
_COORDINATOR_ROLES: list[str] = [
    "cto",
    "engineering-coordinator",
    "qa-coordinator",
]


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
        raw_taxonomy: object = _yaml.safe_load(
            _TAXONOMY_PATH.read_text(encoding="utf-8")
        )
    except Exception:
        return ""

    if not isinstance(raw_taxonomy, dict):
        return ""

    # Build a slug → compatible_figures mapping for coordinator roles.
    role_figures: dict[str, list[str]] = {}
    for level in raw_taxonomy.get("levels", []):
        if not isinstance(level, dict):
            continue
        for role_entry in level.get("roles", []):
            if not isinstance(role_entry, dict):
                continue
            slug = role_entry.get("slug", "")
            if slug in _COORDINATOR_ROLES:
                figs = role_entry.get("compatible_figures", [])
                if isinstance(figs, list):
                    role_figures[slug] = [str(f) for f in figs]

    def _describe_figures(fig_ids: list[str]) -> str:
        lines: list[str] = []
        for fig_id in fig_ids:
            path = _FIGURES_DIR / f"{fig_id}.yaml"
            if not path.exists():
                continue
            try:
                fig_raw: object = _yaml.safe_load(path.read_text(encoding="utf-8"))
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
    cto: jeff_dean:llm:python
    engineering-coordinator: hamming:fastapi:python
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
    """Return the full system prompt including the dynamic cognitive arch section."""
    return _YAML_SYSTEM_PROMPT + _get_cognitive_arch_section()


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


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def generate_plan_yaml(dump: str, label_prefix: str = "") -> str:
    """Step 1.A -- convert a brain dump into a validated PlanSpec YAML string.

    Calls Claude Sonnet via OpenRouter with the full PlanSpec YAML prompt.
    Validates the returned YAML against :class:`~agentception.models.PlanSpec`
    so the Monaco editor always shows a structurally correct document.

    If ``label_prefix`` is provided it overrides the ``initiative`` field
    Claude inferred from the text.

    Args:
        dump: Raw plan text from the user.
        label_prefix: Optional initiative slug override (from the UI options field).

    Returns:
        A YAML string that validates against ``PlanSpec``.

    Raises:
        ValueError: Empty dump, invalid YAML from LLM, or schema mismatch.
        RuntimeError: Missing OPENROUTER_API_KEY.
        httpx.HTTPStatusError: Non-2xx from OpenRouter.
    """
    dump = dump.strip()
    if not dump:
        raise ValueError("Plan text must not be empty.")

    raw = await call_openrouter(
        dump,
        system_prompt=_build_yaml_system_prompt(),
        temperature=0.2,
        max_tokens=8192,
    )
    raw = _strip_fences(raw)

    try:
        data: object = _yaml.safe_load(raw)
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
