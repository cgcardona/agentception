from __future__ import annotations

"""Cognitive architecture resolution — service-layer module.

Derives the COGNITIVE_ARCH string (``figure:skill1[:skill2]``) that is
written into the DB row at dispatch time.

Resolution priority (highest to lowest):
1. ``<!-- ac:cognitive_arch: figure:skills -->`` HTML comment embedded in the
   issue body by ``issue_creator._embed_cognitive_arch`` at issue-creation time.
   This is set by the LLM planner in Phase 1A and optionally edited in Phase 1B.
   When present it is used verbatim — no heuristics applied.
2. ``skills_hint`` passed explicitly by the caller (set from ``PlanIssue.skills``).
3. ``<!-- ac:skills: ... -->`` HTML comment embedded in the issue body.
4. Keyword scan of the issue body (last resort fallback).

Kept in the service layer (not routes) so that both ``services/spawn_child``
and route handlers can import it without creating circular dependencies.
"""

import re

_AC_SKILLS_RE = re.compile(r"<!--\s*ac:skills:\s*([^-]+?)\s*-->")

# ---------------------------------------------------------------------------
# Figure display-name catalog (used by the Org Designer UI)
# ---------------------------------------------------------------------------
# Keyed by figure slug (matches the filename stem under cognitive_archetypes/figures/).
# Single-name or acronym figures are given their canonical full name here; the
# rest are derived automatically in the UI by replacing underscores with spaces
# and title-casing, so they don't need an entry.
FIGURE_DISPLAY_NAMES: dict[str, str] = {
    "da_vinci":          "Leonardo da Vinci",
    "darwin":            "Charles Darwin",
    "dhh":               "DHH",
    "dijkstra":          "Edsger Dijkstra",
    "einstein":          "Albert Einstein",
    "feynman":           "Richard Feynman",
    "hamming":           "Richard Hamming",
    "hopper":            "Grace Hopper",
    "knuth":             "Donald Knuth",
    "lovelace":          "Ada Lovelace",
    "matz":              "Yukihiro Matsumoto",
    "mccarthy":          "John McCarthy",
    "newton":            "Isaac Newton",
    "ritchie":           "Dennis Ritchie",
    "shannon":           "Claude Shannon",
    "turing":            "Alan Turing",
    "wozniak":           "Steve Wozniak",
}


def figure_display_name(figure_id: str) -> str:
    """Return a human-readable display name for *figure_id*.

    Falls back to title-casing the slug when no explicit mapping exists.
    """
    if figure_id in FIGURE_DISPLAY_NAMES:
        return FIGURE_DISPLAY_NAMES[figure_id]
    return figure_id.replace("_", " ").title()
_AC_COGNITIVE_ARCH_RE = re.compile(r"<!--\s*ac:cognitive_arch:\s*([^-\n]+?)\s*-->")

# ---------------------------------------------------------------------------
# Role → default cognitive figure mapping
# ---------------------------------------------------------------------------
# Each role slug maps to the single figure that best represents the epistemic
# mindset for that role.  Figure selection is role-driven (who is this agent?)
# while skill selection remains issue-body-driven (what domain is this ticket?).
# This covers all role files in .agentception/roles/ plus the fallback.
ROLE_DEFAULT_FIGURE: dict[str, str] = {
    # Engineering leaf roles — language/framework specifics come from skill injection,
    # not from the role slug.  All developers share the same default figure; the
    # cognitive_arch assigned at dispatch time provides the actual figure override.
    "developer":                 "guido_van_rossum",
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
    "technical-writer":          "feynman",
    # Coordinator / manager roles
    "engineering-coordinator":   "von_neumann",
    "qa-coordinator":            "w_edwards_deming",
    "coordinator":               "satya_nadella",
    "conductor":                 "jeff_dean",
    # PR reviewer
    "reviewer":               "michael_fagan",
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
    if any(k in b for k in ("midi", "variation", "beat")):
        return "midi:python"
    if any(k in b for k in ("llm", "embedding", "rag", "anthropic", "claude")):
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


def _extract_cognitive_arch_from_body(body: str) -> str | None:
    """Extract the fully-resolved arch string embedded by the issue creator.

    Returns the ``figure:skill1[:skill2]`` string when the
    ``<!-- ac:cognitive_arch: ... -->`` comment is present (written by
    ``_embed_cognitive_arch`` in ``issue_creator.py`` from ``PlanIssue.cognitive_arch``
    set by the LLM planner in Phase 1A).

    Returns ``None`` when the comment is absent so that the caller can fall
    back to the heuristic resolution path for legacy issues.
    """
    m = _AC_COGNITIVE_ARCH_RE.search(body)
    if not m:
        return None
    arch = m.group(1).strip()
    return arch if arch else None


def _resolve_cognitive_arch(
    issue_body: str,
    role: str,
    skills_hint: list[str] | None = None,
    figure_override: str | None = None,
) -> str:
    """Derive COGNITIVE_ARCH string from role and issue body.

    Format: ``figure:skill1[:skill2]``.

    Resolution priority (highest to lowest):

    0. *figure_override* — an explicit figure slug chosen by the user in the
       Org Designer (e.g. ``"steve_jobs"``).  When present, it is combined with
       the skill string derived from lower priorities and returned immediately.
       Skills are still resolved from the issue body so the agent has the right
       domain context; only the *figure* is pinned by the caller.
    1. ``<!-- ac:cognitive_arch: ... -->`` comment in *issue_body* — set by the
       LLM planner in Phase 1A and baked into the GitHub issue at creation time.
       When present the string is returned verbatim; no further resolution.
    2. ``skills_hint`` — an explicit skill list passed by the caller (sourced
       from ``PlanIssue.skills``).  Combined with the ``ROLE_DEFAULT_FIGURE``
       lookup.
    3. ``<!-- ac:skills: ... -->`` comment in *issue_body* — embedded at
       issue-creation time from the planner's ``skills`` field.
    4. Keyword scan of *issue_body* — last-resort fallback for issues created
       before Phase 1A arch assignment was introduced.
    """
    # Priority 0: caller-supplied figure override (Org Designer).
    if figure_override and figure_override.strip():
        figure = figure_override.strip()
        if skills_hint:
            skills = ":".join(skills_hint)
        else:
            embedded_skills = _extract_skills_from_body(issue_body)
            skills = ":".join(embedded_skills) if embedded_skills else _derive_skills_from_body(issue_body)
        return f"{figure}:{skills}"

    # Priority 1: planner-assigned arch baked into the issue body.
    embedded_arch = _extract_cognitive_arch_from_body(issue_body)
    if embedded_arch:
        return embedded_arch

    # Priority 2-4: figure from role taxonomy + derived skills.
    figure = ROLE_DEFAULT_FIGURE.get(role, "hopper")
    if skills_hint:
        skills = ":".join(skills_hint)
    else:
        embedded_skills = _extract_skills_from_body(issue_body)
        skills = ":".join(embedded_skills) if embedded_skills else _derive_skills_from_body(issue_body)
    return f"{figure}:{skills}"
