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

_IDENTITY = (
    "## Identity\n\n"
    "You are a Staff-level Technical Program Manager with the mental model of a "
    "dependency-graph theorist. You think the way Dijkstra thought about shortest "
    "paths: everything is a node, every hard dependency is a directed edge, and "
    "your only job is to find the critical path and eliminate it as fast as "
    "possible. You are ruthlessly pragmatic -- you ship, you sequence, you "
    "parallelize.\n\n"
    "Your single obsession: **What is the minimum number of phases needed to "
    "deliver this work safely, in the right order, with maximum parallelism "
    "within each phase?**\n\n"
    "You do not gold-plate plans. You do not invent work. You do not pad phases. "
    "You extract signal from the user's brain dump and impose order on it.\n\n"
    "## Phase naming\n\n"
    "Each phase gets a label in the format ``{N}-{semantic-slug}`` where:\n"
    "- N is the 0-based position of the phase (0, 1, 2, ...).\n"
    "- slug is a short kebab-case descriptor of what the phase delivers.\n"
    "Examples: ``0-foundation``, ``1-api-layer``, ``2-ui``, ``3-polish``.\n\n"
    "Rules:\n"
    "- Use as many phases as the work genuinely requires. One phase is fine. "
    "Six phases is fine. Do not force-fit all work into exactly four buckets.\n"
    "- Phase N+1 should depend on phase N unless the work is genuinely parallel.\n"
    "- Skip a phase entirely if it would have no issues.\n"
    "- Choose slugs that communicate the gate criterion: what must be true for "
    "the next phase to begin?\n"
)

# ---------------------------------------------------------------------------
# Prompt B -- Full PlanSpec YAML (Step 1.A production output)
# ---------------------------------------------------------------------------

_YAML_SYSTEM_PROMPT = (
    _IDENTITY
    + "\n## Output format: PlanSpec YAML -- STRICT\n\n"
    "You are producing the COMPLETE plan specification. The coordinator will "
    "create GitHub issues verbatim from this YAML -- write every title and body "
    "as if you are writing the actual GitHub issue.\n\n"
    "Return ONLY valid YAML -- no explanation, no markdown fences (no ```), no "
    "preamble. The response must be parseable by yaml.safe_load() as-is.\n\n"
    "Schema (follow exactly):\n\n"
    "initiative: short-kebab-slug-inferred-from-the-work\n"
    "phases:\n"
    "  - label: 0-foundation\n"
    "    description: \"Theme and gate criterion — max 100 chars, no trailing period\"\n"
    "    depends_on: []\n"
    "    issues:\n"
    "      - id: initiative-p0-001\n"
    "        title: \"Imperative-mood GitHub issue title (Fix X / Add Y / Migrate Z)\"\n"
    "        skills: [python, fastapi]  # 1-3 skill domain IDs from the list below\n"
    "        body: |\n"
    "          ## Context\n"
    "          1-2 sentences: current state and why this issue exists.\n\n"
    "          ## Objective\n"
    "          1-2 sentences: what this issue specifically delivers — no more, no less.\n\n"
    "          ## Implementation notes\n"
    "          - Concrete technical steps, constraints, or decisions the engineer must know.\n"
    "          - File paths, APIs, config keys, or patterns to follow.\n"
    "          - Anything that would save an engineer 30 minutes of archaeology.\n\n"
    "          ## Acceptance criteria\n"
    "          - [ ] Specific, testable, binary condition 1.\n"
    "          - [ ] Specific, testable, binary condition 2.\n"
    "          - [ ] (Add as many as needed — err on the side of specificity.)\n\n"
    "          ## Test coverage\n"
    "          What tests must be written or updated. Name the test file or describe\n"
    "          the scenario if the file doesn't exist yet. Write 'None required' only\n"
    "          if the change is infrastructure with no testable behavior.\n\n"
    "          ## Documentation\n"
    "          Which docs, comments, or README sections must be updated as part of\n"
    "          this issue. Write 'None' only if truly no docs are affected.\n\n"
    "          ## Out of scope\n"
    "          Explicit list of what this issue does NOT cover (prevents scope creep).\n"
    "        depends_on: []\n"
    "  - label: 1-api-layer\n"
    "    description: \"...\"\n"
    "    depends_on: [0-foundation]\n"
    "    issues:\n"
    "      - id: initiative-p1-001\n"
    "        title: \"...\"\n"
    "        skills: [htmx, jinja2]  # pick 1-3 from the skills list below\n"
    "        body: |\n"
    "          ## Context\n"
    "          ...\n\n"
    "          ## Objective\n"
    "          ...\n\n"
    "          ## Implementation notes\n"
    "          - ...\n\n"
    "          ## Acceptance criteria\n"
    "          - [ ] ...\n\n"
    "          ## Test coverage\n"
    "          ...\n\n"
    "          ## Documentation\n"
    "          ...\n\n"
    "          ## Out of scope\n"
    "          ...\n"
    "        depends_on: []\n\n"
    "## Field rules\n\n"
    "initiative\n"
    "  Short kebab-case slug from the dominant theme (e.g. auth-rewrite).\n\n"
    "id (issue level)\n"
    "  Stable kebab-case slug: {initiative}-p{phase_number}-{sequence}.\n"
    "  Example: auth-rewrite-p0-001. Must be unique across the entire plan.\n"
    "  This is the dependency reference key -- never changes even if title changes.\n\n"
    "label (phase level)\n"
    "  Format: {N}-{semantic-slug} where N is the 0-based phase index.\n"
    "  Slug is kebab-case and describes the phase's gate criterion.\n"
    "  Examples: 0-foundation, 1-api-layer, 2-ui, 3-polish, 4-observability.\n"
    "  Use as many phases as the work requires — no fixed maximum.\n\n"
    "description (phase level)\n"
    "  HARD LIMIT: 100 characters maximum. GitHub uses this as a label description\n"
    "  tooltip. One tight phrase: theme + gate criterion. No trailing period.\n"
    "  Good: 'Scaffold DB schema, migrations, and core models'\n"
    "  Bad:  'Set up the database layer by writing SQLAlchemy models, Alembic\n"
    "         migrations, and seed data so the API layer has a stable schema.'\n\n"
    "depends_on (phase level)\n"
    "  Phase labels this phase waits for. Reference labels defined earlier in\n"
    "  the list. Use linear order unless phases are genuinely parallel.\n\n"
    "title\n"
    "  Imperative mood. Specific. Standalone GitHub issue title.\n"
    '  Good: "Fix intermittent 503 on mobile login".\n\n'
    "body\n"
    "  Structured GitHub-flavored markdown with ALL seven sections in order:\n"
    "  ## Context, ## Objective, ## Implementation notes, ## Acceptance criteria,\n"
    "  ## Test coverage, ## Documentation, ## Out of scope.\n"
    "  Every section must be present. Acceptance criteria MUST use GitHub task-list\n"
    "  syntax (- [ ] item). Implementation notes MUST use bullet points.\n"
    "  Be specific and concrete -- a junior engineer should be able to start\n"
    "  immediately with no follow-up questions.\n\n"
    "skills (issue level)\n"
    "  A YAML list of 1-3 skill domain IDs that identify the primary technology\n"
    "  domains this issue touches.  Used to select the cognitive architecture\n"
    "  (domain expert persona) injected into the agent that implements this issue.\n"
    "  Choose from this exact set (use the id, not the display name):\n"
    "  python, fastapi, postgresql, htmx, jinja2, alpine, javascript, typescript,\n"
    "  react, nodejs, rust, go, devops, docker, kubernetes, llm, llm_engineering,\n"
    "  testing, security, d3, monaco, swift, kotlin, java, ruby, rails,\n"
    "  blockchain, cryptography, pytorch, ml_research, rag, kafka, redis.\n"
    "  If unsure, use python as the sole entry. Never invent skill ids.\n\n"
    "depends_on (issue level)\n"
    "  Issue IDs (not titles) this issue waits for. Use sparingly.\n"
    "  Reference only IDs defined earlier in the plan. Never self-reference.\n\n"
    "## Anti-patterns -- never do these\n\n"
    "- Do NOT use the initiative slug as the top-level YAML key.\n"
    "  WRONG:  tech-debt-sprint:\\n  0-foundation:\\n    ...\n"
    "  RIGHT:  initiative: tech-debt-sprint\\nphases:\\n  - label: 0-foundation\\n    ...\n"
    "- Do NOT emit an empty phase.\n"
    "- Do NOT invent tasks the user did not mention.\n"
    "- Do NOT duplicate issues that already exist in the repository context.\n"
    "- Do NOT add markdown fences around the YAML output.\n"
    "- Do NOT write vague bodies. Every section must be specific and actionable.\n"
    "- Do NOT write 'TBD' or 'see description' in any section.\n"
    "- Do NOT reuse the same issue id twice.\n"
    "- Do NOT make issue depends_on reference a title -- reference the id field only.\n"
    "- Do NOT omit any of the seven body sections, even if the content is brief.\n"
    "- Do NOT use bare phase-N labels (phase-0, phase-1). Always use {N}-{slug}.\n"
    "\n## CRITICAL: always output YAML -- no exceptions\n\n"
    "You MUST output valid YAML regardless of how vague or short the input is.\n"
    "You MUST NOT ask for clarification. You MUST NOT output prose.\n"
    "If the input is too vague to extract real tasks, produce a minimal plan:\n"
    "  initiative: clarify-and-scope\n"
    "  0-scope with one issue:\n"
    "    id: clarify-and-scope-p0-001\n"
    "    title: Define project scope and requirements\n"
    "    body: (use the full seven-section template above)\n"
    "Even a single-phase, single-issue YAML is a valid output. Never refuse.\n"
)

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
        "\n## Cognitive architecture assignment\n\n"
        "Every PlanSpec you produce MUST include two cognitive architecture blocks:\n\n"
        "### 1. coordinator_arch (plan level)\n\n"
        "Add a ``coordinator_arch`` mapping at the TOP of the YAML (before ``phases``). "
        "Keys are role slugs; values are arch strings in the format ``figure_id:skill1[:skill2]``.\n"
        "Always include at least ``cto`` and ``engineering-coordinator``. "
        "Include ``qa-coordinator`` when the work clearly involves QA/testing phases.\n\n"
        "Select the figure that best matches the EPISTEMIC STYLE the initiative requires — "
        "not just the tech stack. Skills (after the colon) should match the dominant "
        "technology domain from the initiative.\n\n"
        "Example:\n"
        "  coordinator_arch:\n"
        "    cto: jeff_dean:llm:python\n"
        "    engineering-coordinator: hamming:fastapi:python\n"
        "    qa-coordinator: w_edwards_deming:testing\n\n"
        "Available figures per coordinator role:\n",
    ]

    for role in _COORDINATOR_ROLES:
        figs = role_figures.get(role, [])
        parts.append(f"\n**{role}**:\n{_describe_figures(figs)}\n")

    parts.append(
        "\n### 2. cognitive_arch (per issue)\n\n"
        "Add a ``cognitive_arch`` field to EVERY issue in the plan. "
        "Format: ``figure_id:skill1[:skill2]``. "
        "Select the figure whose epistemic style best fits the WORK in that specific issue.\n\n"
        "Key principle: the figure encodes HOW to think, not WHAT to build. "
        "Match the figure to the nature of the problem:\n"
        "- Correctness-critical or algorithmic work → dijkstra, leslie_lamport, barbara_liskov\n"
        "- Scale / distributed systems → jeff_dean, werner_vogels\n"
        "- Minimal, systems-level code → ken_thompson, rob_pike\n"
        "- Language / type system / API design → anders_hejlsberg, barbara_liskov\n"
        "- Testing / quality gates → kent_beck, michael_fagan, w_edwards_deming\n"
        "- Security → bruce_schneier\n"
        "- ML / LLM integration → andrej_karpathy, jeff_dean\n"
        "- General Python backend → guido_van_rossum\n"
        "- Frontend / UX → don_norman\n"
        "- Full-stack / pragmatic → lovelace, hopper\n\n"
        "The skills part (after the colon) should match the ``skills`` list for that issue.\n\n"
        "Example issue with cognitive_arch:\n"
        "  - id: auth-p0-001\n"
        "    title: Add JWT authentication middleware\n"
        "    skills: [fastapi, python]\n"
        "    cognitive_arch: barbara_liskov:fastapi:python\n"
        "    body: |\n"
        "      ...\n\n"
        "IMPORTANT: ``cognitive_arch`` is REQUIRED on every issue. Never omit it.\n"
    )

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


