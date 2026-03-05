from __future__ import annotations

"""Cognitive architecture resolution — service-layer module.

Derives the COGNITIVE_ARCH string (``figure:skill1[:skill2]``) that is
written into every ``.agent-task`` file at spawn time.

Kept in the service layer (not routes) so that both ``services/spawn_child``
and route handlers can import it without creating circular dependencies.
"""

import re

_AC_SKILLS_RE = re.compile(r"<!--\s*ac:skills:\s*([^-]+?)\s*-->")

# ---------------------------------------------------------------------------
# Role → default cognitive figure mapping
# ---------------------------------------------------------------------------
# Each role slug maps to the single figure that best represents the epistemic
# mindset for that role.  Figure selection is role-driven (who is this agent?)
# while skill selection remains issue-body-driven (what domain is this ticket?).
# This covers all 45 role files in .agentception/roles/ plus the fallback.
ROLE_DEFAULT_FIGURE: dict[str, str] = {
    # Engineering leaf roles
    "python-developer":          "guido_van_rossum",
    "frontend-developer":        "don_norman",
    "full-stack-developer":      "lovelace",
    "typescript-developer":      "anders_hejlsberg",
    "api-developer":             "turing",
    "database-architect":        "dijkstra",
    "data-engineer":             "shannon",
    "devops-engineer":           "linus_torvalds",
    "site-reliability-engineer": "margaret_hamilton",
    "security-engineer":         "bruce_schneier",
    "test-engineer":             "kent_beck",
    "architect":                 "avie_tevanian",
    "ml-engineer":               "andrej_karpathy",
    "ml-researcher":             "yann_lecun",
    "data-scientist":            "fei_fei_li",
    "systems-programmer":        "ritchie",
    "rust-developer":            "graydon_hoare",
    "go-developer":              "rob_pike",
    "react-developer":           "ryan_dahl",
    "ios-developer":             "scott_forstall",
    "android-developer":         "james_gosling",
    "mobile-developer":          "scott_forstall",
    "rails-developer":           "dhh",
    "technical-writer":          "feynman",
    "muse-specialist":           "lovelace",
    # Coordinator / manager roles
    "engineering-coordinator":   "von_neumann",
    "qa-coordinator":            "w_edwards_deming",
    "coordinator":               "satya_nadella",
    "conductor":                 "jeff_dean",
    # PR reviewer
    "pr-reviewer":               "michael_fagan",
    # C-suite roles
    "cto":                       "jeff_dean",
    "csto":                      "avie_tevanian",
    "ceo":                       "steve_jobs",
    "cpo":                       "lovelace",
    "coo":                       "jeff_bezos",
    "cdo":                       "shannon",
    "cfo":                       "von_neumann",
    "ciso":                      "bruce_schneier",
    "cmo":                       "paul_graham",
    # VP / coordinator roles
    "platform-coordinator":      "patrick_collison",
    "infrastructure-coordinator": "linus_torvalds",
    "data-coordinator":          "shannon",
    "ml-coordinator":            "andrej_karpathy",
    "design-coordinator":        "don_norman",
    "mobile-coordinator":        "wozniak",
    "security-coordinator":      "bruce_schneier",
    "product-coordinator":       "steve_jobs",
    # Fallback
    "engineer":                  "hopper",
}


def _derive_skills_from_body(body: str) -> str:
    """Extract a colon-separated skill string from issue body keywords.

    Returns the first matching skill string — priority order reflects how
    distinct and actionable each technology signal is in practice.
    """
    b = body.lower()
    if any(k in b for k in ("d3.js", "force-directed", "d3.force", "d3.select")):
        return "d3:javascript"
    if any(k in b for k in ("monaco", "vs/loader", "editor.*cdn")):
        return "monaco"
    if any(k in b for k in ("htmx", "hx-", "sse-connect", "hx-ext")):
        skills = "htmx"
        if any(k in b for k in ("jinja2", ".html", "templateresponse")):
            skills += ":jinja2"
        if any(k in b for k in ("alpine", "x-data", "x-show")):
            skills += ":alpine"
        return skills
    if any(k in b for k in ("jinja2", "templateresponse")):
        return "jinja2"
    if any(k in b for k in ("postgres", "alembic", "migration", "sqlalchemy")):
        return "postgresql:python"
    if any(k in b for k in ("dockerfile", "from python", "compose")):
        return "devops"
    if any(k in b for k in ("midi", "muse", "variation", "beat")):
        return "midi:python"
    if any(k in b for k in ("llm", "embedding", "rag", "openrouter", "claude")):
        return "llm:python"
    if any(k in b for k in ("apirouter", "fastapi", "depends", "response_model")):
        return "fastapi:python"
    if any(k in b for k in ("pytest", "test_", "assert", "coverage", "fixture")):
        return "testing:python"
    if any(k in b for k in ("typescript", ".ts", "tsx")):
        return "typescript:javascript"
    if any(k in b for k in ("rust", "cargo", "tokio")):
        return "rust"
    if any(k in b for k in ("docker", "kubernetes", "k8s", "helm")):
        return "devops"
    return "python"


def _extract_skills_from_body(body: str) -> list[str] | None:
    """Extract skill domain IDs embedded by the issue creator.

    Returns a non-empty list when the ``<!-- ac:skills: ... -->`` comment is
    present (written by ``_embed_skills`` in ``issue_creator.py``).  Returns
    ``None`` when the comment is absent, allowing the caller to fall back to
    keyword extraction.
    """
    m = _AC_SKILLS_RE.search(body)
    if not m:
        return None
    raw = m.group(1)
    skills = [s.strip() for s in raw.split(",") if s.strip()]
    return skills if skills else None


def _resolve_cognitive_arch(
    issue_body: str,
    role: str,
    skills_hint: list[str] | None = None,
) -> str:
    """Derive COGNITIVE_ARCH string from role and issue body.

    Format: ``figure:skill1[:skill2]``.

    Figure is selected from ``ROLE_DEFAULT_FIGURE`` keyed by role slug — this
    ensures every tier (C-suite, coordinator, engineer) receives a real,
    loadable figure rather than placeholder strings.

    Skills come from ``skills_hint`` when present (set by the LLM planner in
    ``PlanIssue.skills``), falling back to keyword extraction from the issue
    body.  This creates a clean pipeline: Phase 1A primes the skill context,
    spawn time consumes it.
    """
    figure = ROLE_DEFAULT_FIGURE.get(role, "hopper")
    if skills_hint:
        skills = ":".join(skills_hint)
    else:
        embedded = _extract_skills_from_body(issue_body)
        skills = ":".join(embedded) if embedded else _derive_skills_from_body(issue_body)
    return f"{figure}:{skills}"
