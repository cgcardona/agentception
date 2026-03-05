from __future__ import annotations

"""Shared helpers and constants for all JSON API routes.

Contains:
- ``_SENTINEL``: path to the pipeline-pause sentinel file.
- ``ROLE_DEFAULT_FIGURE``: canonical figure per role slug (all 45 roles).
- ``_derive_skills_from_body``: keyword-based skill extraction from issue body.
- ``_resolve_cognitive_arch``: derives COGNITIVE_ARCH string from role + issue body.
- ``_build_agent_task``: constructs ``.agent-task`` file content for engineer agents.
- ``_build_coordinator_task``: constructs ``.agent-task`` for brain-dump coordinators.
- ``_build_conductor_task``: constructs ``.agent-task`` for conductor/CTO agents.
- ``_issue_is_claimed_api``: checks ``agent:wip`` label presence.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from agentception.config import settings

_AC_SKILLS_RE = re.compile(r"<!--\s*ac:skills:\s*([^-]+?)\s*-->")

# Path to the sentinel file that pauses the agent pipeline.
# Writing this file tells CTO and Eng VP loops to wait rather than spawn agents.
_SENTINEL: Path = settings.ac_dir / ".pipeline-pause"

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
    # Coordinator / manager roles — real figures, not fake placeholder strings
    "engineering-coordinator":   "von_neumann",
    "qa-coordinator":            "w_edwards_deming",
    "coordinator":               "satya_nadella",
    "conductor":                 "jeff_dean",       # wave-level orchestrator
    # PR reviewer — the_guardian: correctness above all
    "pr-reviewer":               "michael_fagan",
    # C-suite roles
    "cto":                       "jeff_dean",       # planetary-scale systems
    "csto":                      "avie_tevanian",   # Chief Software Technology Officer
    "ceo":                       "steve_jobs",
    "cpo":                       "lovelace",
    "coo":                       "jeff_bezos",
    "cdo":                       "shannon",
    "cfo":                       "von_neumann",
    "ciso":                      "bruce_schneier",
    "cmo":                       "paul_graham",
    # VP roles
    "vp-platform":               "patrick_collison",
    "vp-infrastructure":         "linus_torvalds",
    "vp-data":                   "shannon",
    "vp-ml":                     "andrej_karpathy",
    "vp-design":                 "don_norman",
    "vp-mobile":                 "wozniak",
    "vp-security":               "bruce_schneier",
    "vp-product":                "steve_jobs",
    # Fallback — generic but capable default
    "data-scientist":            "fei_fei_li",
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
    ensures every tier (C-suite, VP, coordinator, engineer) receives a real,
    loadable figure rather than the old placeholder strings ``"coordinator"``
    and ``"conductor"``.

    Skills come from ``skills_hint`` when present (set by the LLM planner in
    ``PlanIssue.skills``), falling back to keyword extraction from the issue
    body.  This creates a clean pipeline: Phase 1A primes the skill context,
    spawn time consumes it.
    """
    figure = ROLE_DEFAULT_FIGURE.get(role, "hopper")
    if skills_hint:
        # Explicit hint from call site (e.g. PlanIssue.skills passed directly).
        skills = ":".join(skills_hint)
    else:
        # Try to read embedded skills comment first; fall back to keyword extraction.
        embedded = _extract_skills_from_body(issue_body)
        skills = ":".join(embedded) if embedded else _derive_skills_from_body(issue_body)
    return f"{figure}:{skills}"


def _build_agent_task(
    issue_number: int,
    title: str,
    role: str,
    worktree: Path,
    host_worktree: Path,
    branch: str,
    phase_label: str = "",
    depends_on: str = "none",
    cognitive_arch: str = "hopper:python",
    wave_id: str = "manual",
) -> str:
    """Build the raw text content of a ``.agent-task`` file.

    The format mirrors what the ``parallel-issue-to-pr.md`` coordinator
    script generates so that agents spawned via the control plane receive
    the same context as batch-spawned agents.

    ``worktree`` is the container-side path (written to the file for Docker
    commands).  ``host_worktree`` is the host-side path embedded as
    ``HOST_WORKTREE`` so the Cursor Task launcher can use the correct path
    when opening the worktree as a project root.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    # ROLE_FILE is metadata only — the kickoff prompt embeds all role content
    # inline.  The path uses the host repo dir so it is human-readable even
    # though agents are instructed not to read it from disk.
    role_file_display = f"<host-repo>/.agentception/roles/{role}.md"
    return (
        f"WORKFLOW=issue-to-pr\n"
        f"GH_REPO={repo}\n"
        f"ISSUE_NUMBER={issue_number}\n"
        f"ISSUE_TITLE={title}\n"
        f"ISSUE_URL=https://github.com/{repo}/issues/{issue_number}\n"
        f"PHASE_LABEL={phase_label}\n"
        f"DEPENDS_ON={depends_on}\n"
        f"BRANCH={branch}\n"
        f"ROLE={role}\n"
        f"ROLE_FILE={role_file_display}\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"CLOSES_ISSUES={issue_number}\n"
        f"BATCH_ID={wave_id}\n"
        f"WAVE={wave_id}\n"
        f"COGNITIVE_ARCH={cognitive_arch}\n"
        f"CREATED_AT={now}\n"
        f"SPAWN_MODE=chain\n"
        f"LINKED_PR=none\n"
        f"SPAWN_SUB_AGENTS=false\n"
        f"ATTEMPT_N=0\n"
        f"REQUIRED_OUTPUT=pr_url\n"
        f"ON_BLOCK=stop\n"
    )


def _build_coordinator_task(
    slug: str,
    plan_text: str,
    label_prefix: str,
    worktree: Path,
    host_worktree: Path,
    branch: str,
) -> str:
    """Build the ``.agent-task`` content for a plan coordinator worktree.

    The coordinator agent reads ``WORKFLOW=bugs-to-issues`` and follows
    ``parallel-bugs-to-issues.md``: it runs the Phase Planner, creates GitHub
    labels, creates worktrees for each batch, writes sub-agent task files, and
    launches sub-agents.  AgentCeption's only job is to prepare the worktree
    and this file — the Cursor background agent does all LLM work.

    The ``PLAN_DUMP`` section is appended as a freeform block after the
    structured key=value header so the coordinator can read it verbatim.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    prefix_line = f"LABEL_PREFIX={label_prefix}\n" if label_prefix else ""
    return (
        f"WORKFLOW=bugs-to-issues\n"
        f"GH_REPO={repo}\n"
        f"ROLE=coordinator\n"
        f"ROLE_FILE=<host-repo>/.agentception/roles/coordinator.md\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"BATCH_ID={slug}\n"
        f"WAVE={slug}\n"
        f"COGNITIVE_ARCH={ROLE_DEFAULT_FIGURE.get('engineering-coordinator', 'von_neumann')}:python\n"
        f"{prefix_line}"
        f"CREATED_AT={now}\n"
        f"SPAWN_MODE=chain\n"
        f"SPAWN_SUB_AGENTS=true\n"
        f"ATTEMPT_N=0\n"
        f"REQUIRED_OUTPUT=phase_plan\n"
        f"ON_BLOCK=stop\n"
        f"\nPLAN_DUMP:\n{plan_text}\n"
    )


def _build_conductor_task(
    wave_id: str,
    phases: list[str],
    org: str | None,
    worktree: Path,
    host_worktree: Path,
    branch: str,
) -> str:
    """Build the ``.agent-task`` content for a conductor worktree.

    The conductor agent reads ``WORKFLOW=conductor`` and coordinates across the
    listed phases, spawning sub-agents for each unclaimed issue.  AgentCeption
    only prepares the worktree and this file — all LLM work happens inside
    the Cursor background agent that opens the returned ``host_worktree``.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    return (
        f"WORKFLOW=conductor\n"
        f"GH_REPO={repo}\n"
        f"ROLE=conductor\n"
        f"ROLE_FILE=<host-repo>/.agentception/roles/conductor.md\n"
        f"WAVE_ID={wave_id}\n"
        f"PHASES={','.join(phases)}\n"
        f"ORG={org or ''}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"BATCH_ID={wave_id}\n"
        f"WAVE={wave_id}\n"
        f"COGNITIVE_ARCH={ROLE_DEFAULT_FIGURE.get('conductor', 'jeff_dean')}:python\n"
        f"CREATED_AT={now}\n"
        f"SPAWN_MODE=chain\n"
        f"SPAWN_SUB_AGENTS=true\n"
        f"ATTEMPT_N=0\n"
        f"REQUIRED_OUTPUT=wave_complete\n"
        f"ON_BLOCK=stop\n"
    )


def _issue_is_claimed_api(iss: dict[str, object]) -> bool:
    """Return True when an issue carries the ``agent:wip`` label."""
    raw = iss.get("labels")
    if not isinstance(raw, list):
        return False
    for lbl in raw:
        if isinstance(lbl, str) and lbl == "agent:wip":
            return True
        if isinstance(lbl, dict) and lbl.get("name") == "agent:wip":
            return True
    return False
