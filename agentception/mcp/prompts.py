"""MCP Prompts catalogue for AgentCeption.

Two categories of prompts:

Static prompts (no arguments)
    ``role/<slug>``  — role definition Markdown from ``.agentception/roles/<slug>.md``
    ``agent/<name>`` — compiled agent prompts from ``.agentception/<name>.md``
                       (engineer, reviewer, conductor, command-policy, etc.)

    Named after their filesystem source.  Returned as a single ``user`` message
    whose ``text`` is the raw Markdown file content.

Parameterized prompts (require arguments, DB-backed)
    ``task/briefing`` — full task briefing for a run, resolved live from the DB.
        Arguments: ``run_id`` (required)

        Returns a fully self-contained Markdown briefing.  The cognitive
        architecture (figure + skills) is inlined directly — the agent receives
        the figure's reasoning identity, heuristic, failure modes, and checklist,
        plus every assigned skill's technical standards and review checklist,
        all in the initial message.  No follow-up MCP reads are required to
        internalize the full cognitive context.

        The loop calls ``get_prompt("task/briefing", {"run_id": run_id})`` to
        get the initial user message — task context is sourced entirely from
        the ``ACAgentRun`` DB row, no file reads or inline text pasting.

Prompt naming convention
    ``role/<slug>``     role definition files
    ``agent/<name>``    compiled agent-level prompts
    ``task/<name>``     dynamic, argument-driven task prompts (DB-backed)
"""

from __future__ import annotations


import logging
from pathlib import Path
from typing import TypedDict

import yaml

from agentception.db.queries import RunContextRow, get_run_context
from agentception.mcp.types import (
    ACPromptArgument,
    ACPromptContent,
    ACPromptDef,
    ACPromptMessage,
    ACPromptResult,
)

logger = logging.getLogger(__name__)

# Root directory of the compiled prompt files.  Works both inside Docker
# (/app is the repo root) and on the host (two levels up from this file).
_MCP_DIR = Path(__file__).parent
_APP_ROOT = _MCP_DIR.parent.parent
_AGENTCEPTION_DIR = _APP_ROOT / ".agentception"

# Cognitive architecture corpus — same root used by resources.py.
_ARCH_ROOT = _APP_ROOT / "scripts" / "gen_prompts" / "cognitive_archetypes"


# ---------------------------------------------------------------------------
# Typed shapes for the cognitive-arch YAML files we read
# ---------------------------------------------------------------------------


class _PromptInjection(TypedDict, total=False):
    prefix: str
    suffix: str


class _FigureData(TypedDict, total=False):
    display_name: str
    heuristic: str
    failure_modes: list[str]
    prompt_injection: _PromptInjection


class _SkillData(TypedDict, total=False):
    display_name: str
    prompt_fragment: str
    review_checklist: str


# ---------------------------------------------------------------------------
# YAML loaders for cognitive-arch corpus
# ---------------------------------------------------------------------------


def _load_figure(figure_id: str) -> _FigureData:
    """Read and return a figure YAML as a typed dict.

    Returns an empty dict (all optional fields absent) when the file is not
    found or unparseable — callers must handle missing fields gracefully.
    """
    path = _ARCH_ROOT / "figures" / f"{figure_id}.yaml"
    if not path.exists():
        logger.warning("⚠️  briefing: figure '%s' not found at %s", figure_id, path)
        return {}
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        data: _FigureData = {}
        if isinstance(raw.get("display_name"), str):
            data["display_name"] = raw["display_name"]
        if isinstance(raw.get("heuristic"), str):
            data["heuristic"] = raw["heuristic"]
        fm = raw.get("failure_modes")
        if isinstance(fm, list):
            data["failure_modes"] = [str(m) for m in fm]
        pi = raw.get("prompt_injection")
        if isinstance(pi, dict):
            inj: _PromptInjection = {}
            if isinstance(pi.get("prefix"), str):
                inj["prefix"] = pi["prefix"]
            if isinstance(pi.get("suffix"), str):
                inj["suffix"] = pi["suffix"]
            data["prompt_injection"] = inj
        return data
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ briefing: could not load figure '%s': %s", figure_id, exc)
        return {}


def _load_skill(skill_id: str) -> _SkillData:
    """Read and return a skill-domain YAML as a typed dict.

    Returns an empty dict when the file is not found or unparseable.
    """
    path = _ARCH_ROOT / "skill_domains" / f"{skill_id}.yaml"
    if not path.exists():
        logger.warning("⚠️  briefing: skill '%s' not found at %s", skill_id, path)
        return {}
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        data: _SkillData = {}
        if isinstance(raw.get("display_name"), str):
            data["display_name"] = raw["display_name"]
        if isinstance(raw.get("prompt_fragment"), str):
            data["prompt_fragment"] = raw["prompt_fragment"]
        if isinstance(raw.get("review_checklist"), str):
            data["review_checklist"] = raw["review_checklist"]
        return data
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ briefing: could not load skill '%s': %s", skill_id, exc)
        return {}

# ---------------------------------------------------------------------------
# Static prompt catalogue
# ---------------------------------------------------------------------------

#: Agent-level prompts under .agentception/*.md (excluding derived artifacts).
_AGENT_PROMPTS: list[tuple[str, str]] = [
    ("agent/engineer", "Engineering worker — implement a single GitHub issue end-to-end"),
    ("agent/reviewer", "Code review worker — review and merge a single pull request"),
    ("agent/conductor", "Agent conductor — coordinate multi-step agent workflows"),
    ("agent/command-policy", "Agent command policy — rules for safe shell and git usage"),
    ("agent/pipeline-howto", "Pipeline how-to — phase-gate, dependency, and label conventions"),
    ("agent/task-spec", "Agent task context specification — DB-backed RunContextRow field reference"),
    ("agent/cognitive-arch-enrichment-spec", "Cognitive architecture enrichment specification"),
    ("agent/conflict-rules", "Conflict resolution rules for concurrent agent operations"),
]

#: Maps agent prompt name → relative .agentception/ filename (no extension).
_AGENT_FILENAME_MAP: dict[str, str] = {
    "agent/engineer": "agent-engineer",
    "agent/reviewer": "agent-reviewer",
    "agent/conductor": "agent-conductor",
    "agent/command-policy": "agent-command-policy",
    "agent/pipeline-howto": "pipeline-howto",
    "agent/task-spec": "agent-task-spec",
    "agent/cognitive-arch-enrichment-spec": "cognitive-arch-enrichment-spec",
    "agent/conflict-rules": "conflict-rules",
}

# ---------------------------------------------------------------------------
# Parameterized prompt catalogue
# ---------------------------------------------------------------------------

#: Prompts that require runtime arguments and are resolved from the DB.
_PARAMETERIZED_PROMPTS: list[ACPromptDef] = [
    ACPromptDef(
        name="task/briefing",
        description=(
            "Full task briefing for a run — role definition, cognitive architecture, "
            "and task assignment resolved live from the ACAgentRun DB row. "
            "Pass run_id to receive the complete initial message for the agent loop."
        ),
        arguments=[
            ACPromptArgument(
                name="run_id",
                description="The run ID to fetch the briefing for (e.g. 'adhoc-81fd84e7d64d').",
                required=True,
            )
        ],
    ),
]


def _discover_role_prompts() -> list[ACPromptDef]:
    """Build the role-prompt catalogue from .agentception/roles/*.md files."""
    roles_dir = _AGENTCEPTION_DIR / "roles"
    if not roles_dir.is_dir():
        logger.warning("⚠️  prompts: .agentception/roles/ not found at %s", roles_dir)
        return []

    prompts: list[ACPromptDef] = []
    for md_file in sorted(roles_dir.glob("*.md")):
        slug = md_file.stem
        prompts.append(
            ACPromptDef(
                name=f"role/{slug}",
                description=f"Role definition for the '{slug}' agent role",
                arguments=[],
            )
        )
    return prompts


def _build_agent_prompt_defs() -> list[ACPromptDef]:
    """Build ACPromptDef objects for the static agent-level prompts."""
    defs: list[ACPromptDef] = []
    for name, description in _AGENT_PROMPTS:
        filename = _AGENT_FILENAME_MAP[name]
        path = _AGENTCEPTION_DIR / f"{filename}.md"
        if path.exists():
            defs.append(ACPromptDef(name=name, description=description, arguments=[]))
        else:
            logger.debug("prompts: skipping %s — %s not found", name, path)
    return defs


def _build_catalogue() -> list[ACPromptDef]:
    """Assemble the full prompt catalogue: parameterized → agent → roles."""
    return _PARAMETERIZED_PROMPTS + _build_agent_prompt_defs() + _discover_role_prompts()


#: Full prompt catalogue, built once at module import time.
PROMPTS: list[ACPromptDef] = _build_catalogue()

# ---------------------------------------------------------------------------
# Prompt getter — async to support DB-backed parameterized prompts
# ---------------------------------------------------------------------------


async def get_prompt(
    name: str,
    arguments: dict[str, str] | None = None,
) -> ACPromptResult | None:
    """Return the content of a named prompt.

    For static prompts (``role/*``, ``agent/*``) the backing Markdown file is
    read from disk.  For parameterized prompts (``task/*``) the result is
    resolved live from the DB using the supplied *arguments*.

    Args:
        name: Prompt name as returned by ``prompts/list``
              (e.g. ``"role/developer"``, ``"task/briefing"``).
        arguments: Key/value pairs for parameterized prompts.
                   Ignored for static prompts.

    Returns:
        :class:`ACPromptResult` on success, ``None`` when the prompt name is
        unknown, the backing file does not exist, or a required argument is
        missing.
    """
    if name == "task/briefing":
        return await _get_task_briefing(arguments or {})

    path = _resolve_static_path(name)
    if path is None:
        logger.warning("⚠️  get_prompt: unknown prompt name %r", name)
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("❌ get_prompt: could not read %s — %s", path, exc)
        return None

    description = _get_description(name)
    return ACPromptResult(
        description=description,
        messages=[
            ACPromptMessage(
                role="user",
                content=ACPromptContent(type="text", text=text),
            )
        ],
    )


# ---------------------------------------------------------------------------
# task/briefing — DB-backed parameterized prompt
# ---------------------------------------------------------------------------


async def _get_task_briefing(arguments: dict[str, str]) -> ACPromptResult | None:
    """Render the task/briefing prompt for a given run_id.

    Pulls the full task context from the DB, loads the role definition file,
    and composes a structured Markdown briefing that serves as the agent's
    complete initial message.

    Returns ``None`` when ``run_id`` is missing from *arguments* or the run
    does not exist in the DB.
    """
    run_id = arguments.get("run_id", "").strip()
    if not run_id:
        logger.warning("⚠️  task/briefing: missing required argument 'run_id'")
        return None

    ctx = await get_run_context(run_id)
    if ctx is None:
        logger.warning("⚠️  task/briefing: run_id=%r not found in DB", run_id)
        return None

    role = ctx["role"]
    role_content = _load_role_content(role)
    text = _render_task_briefing(ctx, role_content)

    return ACPromptResult(
        description=f"Task briefing for run {run_id} — role: {role}",
        messages=[
            ACPromptMessage(
                role="user",
                content=ACPromptContent(type="text", text=text),
            )
        ],
    )


def _parse_arch_components(cognitive_arch: str) -> tuple[list[str], list[str]]:
    """Parse a ``cognitive_arch`` string into figure IDs and skill IDs.

    Format: ``figure1[,figure2]:skill1:skill2:...``
    Examples:
        ``"guido_van_rossum:python"``         → (["guido_van_rossum"], ["python"])
        ``"linus_torvalds"``                   → (["linus_torvalds"], [])
        ``"lovelace,shannon:htmx:jinja2"``    → (["lovelace", "shannon"], ["htmx", "jinja2"])
    """
    if not cognitive_arch or cognitive_arch == "not set":
        return [], []
    tokens = cognitive_arch.strip().split(":")
    figures = [f.strip() for f in tokens[0].split(",") if f.strip()]
    skills = [s.strip() for s in tokens[1:] if s.strip()]
    return figures, skills


def _render_figure_section(figure_ids: list[str]) -> list[str]:
    """Inline the cognitive identity for all assigned figures.

    Produces the prefix prose, heuristic, and failure modes for each figure.
    The suffix (pre-submit checklist) is rendered separately at the end of the
    briefing so the agent encounters it immediately before submitting work.
    """
    parts: list[str] = []
    for fig_id in figure_ids:
        data = _load_figure(fig_id)
        prefix = (data.get("prompt_injection") or {}).get("prefix", "")
        if prefix:
            parts.append(prefix.strip())
            parts.append("")
        heuristic = data.get("heuristic", "")
        if heuristic:
            parts += [f"> **Heuristic:** {heuristic.strip()}", ""]
        failure_modes = data.get("failure_modes") or []
        if failure_modes:
            parts += ["### Failure Modes (active compensations — read before starting)", ""]
            for fm in failure_modes:
                parts.append(f"- {fm.strip()}")
            parts.append("")
    return parts


def _render_skill_sections(skill_ids: list[str]) -> list[str]:
    """Inline the prompt_fragment for each assigned skill domain."""
    if not skill_ids:
        return []
    parts: list[str] = ["", "---", ""]
    for skill_id in skill_ids:
        data = _load_skill(skill_id)
        fragment = data.get("prompt_fragment", "")
        if fragment:
            parts.append(fragment.strip())
            parts.append("")
    return parts


def _render_submit_checklist(figure_ids: list[str], skill_ids: list[str]) -> list[str]:
    """Render the pre-submit checklist: figure suffix + skill review checklists.

    Placed at the end of the briefing so the agent reads it immediately before
    opening a PR — the moment the checklist is most actionable.
    """
    parts: list[str] = ["", "---", ""]
    has_content = False

    for fig_id in figure_ids:
        data = _load_figure(fig_id)
        suffix = (data.get("prompt_injection") or {}).get("suffix", "")
        if suffix:
            parts.append(suffix.strip())
            parts.append("")
            has_content = True

    for skill_id in skill_ids:
        skill_data = _load_skill(skill_id)
        display = skill_data.get("display_name") or skill_id
        checklist = skill_data.get("review_checklist", "")
        if checklist:
            parts += [f"### {display} checklist", ""]
            for line in str(checklist).strip().splitlines():
                stripped = line.strip()
                if stripped:
                    parts.append(f"- {stripped}" if not stripped.startswith("-") else stripped)
            parts.append("")
            has_content = True

    return parts if has_content else []


def _render_task_briefing(ctx: RunContextRow, role_content: str) -> str:
    """Compose the Markdown briefing from task context and role definition.

    Cognitive architecture is fully inlined — the agent receives the figure's
    reasoning identity, heuristic, failure modes, and pre-submit checklist, plus
    every assigned skill's technical standards and review checklist, all in this
    single message.  No follow-up MCP reads are required for cognitive context.
    """
    run_id: str = ctx["run_id"]
    role: str = ctx["role"]
    cognitive_arch: str = ctx["cognitive_arch"] or "not set"
    worktree_path: str = ctx["worktree_path"] or "not set"
    branch: str = ctx["branch"] or "not set"
    issue_number: int | None = ctx["issue_number"]
    task_description: str | None = ctx["task_description"]
    batch_id: str | None = ctx["batch_id"]
    parent_run_id: str | None = ctx["parent_run_id"]

    figure_ids, skill_ids = _parse_arch_components(cognitive_arch)

    # Build the assignment section.
    if task_description:
        assignment = str(task_description).strip()
    elif issue_number:
        assignment = (
            f"Implement GitHub issue **#{issue_number}**.\n\n"
            f"The full issue body was not injected into this briefing. "
            f"Read `ac://runs/{run_id}/context` for available task context. "
            f"Use `gh issue view {issue_number}` (shell) to fetch the issue body — "
            f"this is more reliable than the `issue_read` MCP tool for initial context loading."
        )
    else:
        assignment = f"Read `ac://runs/{run_id}/context` for your full task context."

    lineage_lines: list[str] = []
    if batch_id:
        lineage_lines.append(f"**Batch:** `{batch_id}`")
    if parent_run_id:
        lineage_lines.append(f"**Spawned by:** `{parent_run_id}`")

    worktree_collection = f"worktree-{run_id}"

    parts: list[str] = [
        f"## Task Briefing — run `{run_id}`",
        "",
        f"**Role:** {role}  ",
        f"**Cognitive Architecture:** `{cognitive_arch}`  ",
        f"**Worktree:** `{worktree_path}`  ",
        f"**Branch:** `{branch}`  ",
        f"**Search index:** `{worktree_collection}` (your worktree) · `code` (full repo)",
        "",
        "> **Before you read any file:** call `search_codebase` first.",
        "> One semantic query ('where is X defined', 'pattern for Y') returns exact",
        "> file paths and line numbers in a single turn.",
        "> Pass `collection: \"{worktree_collection}\"` to scope results to your worktree.",
        "> The `code` collection (default) indexes the full repository.",
    ]

    if lineage_lines:
        parts += ["", *lineage_lines]

    parts += [
        "",
        "---",
        "",
        "## Your Assignment",
        "",
        assignment,
    ]

    # ── Inline cognitive identity (figure) ────────────────────────────────────
    parts += _render_figure_section(figure_ids)

    # ── Inline skill domain technical standards ────────────────────────────────
    parts += _render_skill_sections(skill_ids)

    # ── Role definition ────────────────────────────────────────────────────────
    parts += ["", "---", ""]
    if role_content:
        parts += [
            "## Your Role Definition",
            "",
            role_content.strip(),
        ]

    # ── Pre-submit checklist (figure suffix + skill review checklists) ─────────
    parts += _render_submit_checklist(figure_ids, skill_ids)

    # ── Reference resources (no reads required — all context is above) ─────────
    parts += [
        "",
        "---",
        "",
        "## Run Resources",
        "",
        f"- `ac://runs/{run_id}/context` — full task context from the DB",
        f"- `ac://runs/{run_id}/events` — prior activity log (resume after crash)",
        f"- `ac://runs/{run_id}/children` — child runs you have spawned",
        "- `ac://system/config` — pipeline label names",
        f"- `ac://roles/{role}` — your role definition",
    ]
    if figure_ids:
        for fig in figure_ids:
            parts.append(f"- `ac://arch/figures/{fig}` — extended figure profile")
    if skill_ids:
        for skill in skill_ids:
            parts.append(f"- `ac://arch/skills/{skill}` — extended skill profile")

    return "\n".join(parts)


def _load_role_content(role: str) -> str:
    """Return the Markdown content of the role file for *role*, or empty string."""
    if not role:
        return ""
    path = _AGENTCEPTION_DIR / "roles" / f"{role}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("⚠️  task/briefing: role file not found for %r", role)
        return ""
    except OSError as exc:
        logger.warning("⚠️  task/briefing: could not read role file for %r: %s", role, exc)
        return ""


# ---------------------------------------------------------------------------
# Sync accessor for static prompts (used by the sync MCP handler path)
# ---------------------------------------------------------------------------


def get_static_prompt(name: str) -> ACPromptResult | None:
    """Return a static prompt without hitting the DB.

    Only handles ``role/*`` and ``agent/*`` prompts.  Returns ``None`` for
    parameterized prompts (``task/*``) — the caller should reject those with
    an appropriate error message directing clients to the async path.

    Use :func:`get_prompt` (async) for parameterized prompts.
    """
    path = _resolve_static_path(name)
    if path is None:
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("❌ get_static_prompt: could not read %s — %s", path, exc)
        return None

    return ACPromptResult(
        description=_get_description(name),
        messages=[
            ACPromptMessage(
                role="user",
                content=ACPromptContent(type="text", text=text),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Static prompt helpers
# ---------------------------------------------------------------------------


def _resolve_static_path(name: str) -> Path | None:
    """Map a static prompt name to its backing file path, or None if unknown."""
    if name.startswith("role/"):
        slug = name[5:]
        path = _AGENTCEPTION_DIR / "roles" / f"{slug}.md"
        return path if path.exists() else None

    if name.startswith("agent/"):
        filename = _AGENT_FILENAME_MAP.get(name)
        if filename is None:
            return None
        path = _AGENTCEPTION_DIR / f"{filename}.md"
        return path if path.exists() else None

    return None


def _get_description(name: str) -> str:
    """Return the description for a prompt name from the catalogue."""
    for prompt in PROMPTS:
        if prompt["name"] == name:
            return prompt["description"]
    return name
