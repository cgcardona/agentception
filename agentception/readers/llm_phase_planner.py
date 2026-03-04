from __future__ import annotations

"""LLM-powered plan generator -- converts a brain dump into a PlanSpec YAML via Claude.

Two public entry points:

``generate_plan_yaml(dump)``
    Step 1.A: calls Claude, returns a validated PlanSpec YAML string ready for
    the Monaco editor.  This is the production path when AC_OPENROUTER_API_KEY
    is set.

``plan_phases_llm(dump)``
    Legacy JSON phase-card path (kept for heuristic fallback in plan_ui.py).
    Returns a PlanResult with PhasePreview objects.

Architecture note
-----------------
MCP is NOT involved in this module.  The browser -> AgentCeption -> OpenRouter
loop is entirely self-contained.  MCP enters only after the user approves the
YAML and a coordinator worktree is spawned -- the coordinator agent (in Cursor)
calls ``plan_get_labels()`` and similar tools as it files GitHub issues.
"""
from __future__ import annotations

import json
import logging

import yaml as _yaml

from agentception.models import PhasePreview, PlanResult, PlanSpec
from agentception.services.llm import call_openrouter

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
    "## The four phases -- strict definitions\n\n"
    "**phase-0 -- Foundations & Critical Fixes**: Work everything else depends on.\n"
    "**phase-1 -- Infrastructure & Core Services**: Internal plumbing features need.\n"
    "**phase-2 -- Features & User-Facing Work**: New capabilities visible to users.\n"
    "**phase-3 -- Polish, Tests & Debt**: Tests, docs, refactors, cleanup.\n\n"
    "Only emit phases that have work. Skip empty phases entirely.\n"
)

# ---------------------------------------------------------------------------
# Prompt A -- JSON phase cards (used by plan_phases_llm / heuristic fallback)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    _IDENTITY
    + "\n## Output format: JSON phase cards\n\n"
    "Return ONLY valid JSON -- no explanation, no markdown fences, no preamble:\n\n"
    '{\n  "phases": [\n    {\n      "label": "phase-0",\n'
    '      "description": "One sentence: theme for the confirmation card.",\n'
    '      "estimated_issue_count": 2,\n      "depends_on": []\n    }\n  ]\n}\n\n'
    "phase labels: ONLY phase-0, phase-1, phase-2, phase-3. No others.\n"
    "depends_on: list of phase labels that must complete before this one.\n"
    "estimated_issue_count: distinct GitHub issues, not bullet points.\n"
    "description: one concise sentence for the human confirmation card.\n"
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
    "  - label: phase-0\n"
    "    description: \"One sentence: theme and gate criterion.\"\n"
    "    depends_on: []\n"
    "    issues:\n"
    "      - id: initiative-p0-001\n"
    "        title: \"Imperative-mood GitHub issue title (Fix X / Add Y / Migrate Z)\"\n"
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
    "  - label: phase-1\n"
    "    description: \"...\"\n"
    "    depends_on: [phase-0]\n"
    "    issues:\n"
    "      - id: initiative-p1-001\n"
    "        title: \"...\"\n"
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
    "label\n"
    "  ONLY: phase-0, phase-1, phase-2, phase-3. No others.\n\n"
    "depends_on (phase level)\n"
    "  Phase labels this phase waits for. Use linear order unless phases are\n"
    "  genuinely independent.\n\n"
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
    "depends_on (issue level)\n"
    "  Issue IDs (not titles) this issue waits for. Use sparingly.\n"
    "  Reference only IDs defined earlier in the plan. Never self-reference.\n\n"
    "## Anti-patterns -- never do these\n\n"
    "- Do NOT use the initiative slug as the top-level YAML key.\n"
    "  WRONG:  tech-debt-sprint:\\n  phase-0:\\n    ...\n"
    "  RIGHT:  initiative: tech-debt-sprint\\nphases:\\n  - label: phase-0\\n    ...\n"
    "- Do NOT emit an empty phase.\n"
    "- Do NOT invent tasks the user did not mention.\n"
    "- Do NOT duplicate issues that already exist in the repository context.\n"
    "- Do NOT use more than 4 phases or create custom phase labels.\n"
    "- Do NOT add markdown fences around the YAML output.\n"
    "- Do NOT write vague bodies. Every section must be specific and actionable.\n"
    "- Do NOT write 'TBD' or 'see description' in any section.\n"
    "- Do NOT reuse the same issue id twice.\n"
    "- Do NOT make issue depends_on reference a title -- reference the id field only.\n"
    "- Do NOT omit any of the seven body sections, even if the content is brief.\n"
    "\n## CRITICAL: always output YAML -- no exceptions\n\n"
    "You MUST output valid YAML regardless of how vague or short the input is.\n"
    "You MUST NOT ask for clarification. You MUST NOT output prose.\n"
    "If the input is too vague to extract real tasks, produce a minimal plan:\n"
    "  initiative: clarify-and-scope\n"
    "  phase-0 with one issue:\n"
    "    id: clarify-and-scope-p0-001\n"
    "    title: Define project scope and requirements\n"
    "    body: (use the full seven-section template above)\n"
    "Even a single-phase, single-issue YAML is a valid output. Never refuse.\n"
)


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
        RuntimeError: Missing AC_OPENROUTER_API_KEY.
        httpx.HTTPStatusError: Non-2xx from OpenRouter.
    """
    dump = dump.strip()
    if not dump:
        raise ValueError("Plan text must not be empty.")

    raw = await call_openrouter(
        dump,
        system_prompt=_YAML_SYSTEM_PROMPT,
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


async def plan_phases_llm(dump: str) -> PlanResult:
    """Call Claude via OpenRouter to convert a brain dump into a PlanResult.

    Args:
        dump: Raw plan text from the user.

    Returns:
        A :class:`~agentception.models.PlanResult` with one or more phases.

    Raises:
        ValueError: When ``dump`` is empty, the LLM returns invalid JSON, or
            the response contains no phases.
        RuntimeError: Propagated from :func:`~agentception.services.llm.call_openrouter`
            when the API key is missing.
        httpx.HTTPStatusError: On non-2xx responses from OpenRouter.
    """
    dump = dump.strip()
    if not dump:
        raise ValueError("Plan text must not be empty.")

    raw = await call_openrouter(dump, system_prompt=_SYSTEM_PROMPT, temperature=0.2)
    raw = _strip_fences(raw)

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON: %s\nRaw: %s", exc, raw[:500])
        raise ValueError(f"LLM returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"LLM returned unexpected top-level type: {type(data).__name__}")

    raw_phases: object = data.get("phases", [])
    if not isinstance(raw_phases, list):
        raise ValueError(f"LLM 'phases' field is not a list: {type(raw_phases).__name__}")

    phases: list[PhasePreview] = []
    for item in raw_phases:
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict phase entry: %r", item)
            continue
        try:
            phases.append(
                PhasePreview(
                    label=str(item["label"]),
                    description=str(item["description"]),
                    estimated_issue_count=int(item["estimated_issue_count"]),
                    depends_on=[str(d) for d in item.get("depends_on", [])],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed phase entry %r: %s", item, exc)

    if not phases:
        raise ValueError("LLM returned no valid phases -- check the prompt or input.")

    logger.info("✅ LLM phase plan: %d phases for %d-char dump", len(phases), len(dump))
    return PlanResult(phases=phases)
