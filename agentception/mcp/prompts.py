"""MCP Prompts catalogue for AgentCeption.

Exposes every compiled role file and agent prompt as a first-class MCP Prompt
so clients can discover and fetch them via ``prompts/list`` and ``prompts/get``
without any filesystem access.

Prompt naming convention
------------------------
``role/<slug>``
    Role definition file from ``.agentception/roles/<slug>.md`` — one per role
    slug in the team taxonomy (e.g. ``role/python-developer``, ``role/cto``).

``agent/<name>``
    Compiled agent-level prompt from ``.agentception/<name>.md`` — covers the
    dispatcher, engineer, reviewer, conductor, and policy documents that agents
    load at runtime.

All prompts are static (no arguments) and returned as a single ``user`` message
whose ``text`` is the raw Markdown file content.  Agents may prepend the
returned message to their conversation context.
"""

from __future__ import annotations


import logging
from pathlib import Path

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

# ---------------------------------------------------------------------------
# Static prompt catalogue
# ---------------------------------------------------------------------------

#: Agent-level prompts under .agentception/*.md (excluding derived artifacts).
_AGENT_PROMPTS: list[tuple[str, str]] = [
    ("agent/dispatcher", "AgentCeption Dispatcher — drain the pending launch queue and spawn the correct agents"),
    ("agent/engineer", "Engineering worker — implement a single GitHub issue end-to-end"),
    ("agent/reviewer", "Code review worker — review and merge a single pull request"),
    ("agent/conductor", "Agent conductor — coordinate multi-step agent workflows"),
    ("agent/command-policy", "Agent command policy — rules for safe shell and git usage"),
    ("agent/pipeline-howto", "Pipeline how-to — phase-gate, dependency, and label conventions"),
    ("agent/task-spec", "Agent task file specification — formal TOML schema for .agent-task files"),
    ("agent/cognitive-arch-enrichment-spec", "Cognitive architecture enrichment specification"),
    ("agent/conflict-rules", "Conflict resolution rules for concurrent agent operations"),
]

#: Maps agent prompt name → relative .agentception/ filename (no extension).
_AGENT_FILENAME_MAP: dict[str, str] = {
    "agent/dispatcher": "dispatcher",
    "agent/engineer": "agent-engineer",
    "agent/reviewer": "agent-reviewer",
    "agent/conductor": "agent-conductor",
    "agent/command-policy": "agent-command-policy",
    "agent/pipeline-howto": "pipeline-howto",
    "agent/task-spec": "agent-task-spec",
    "agent/cognitive-arch-enrichment-spec": "cognitive-arch-enrichment-spec",
    "agent/conflict-rules": "conflict-rules",
}


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
    """Assemble the full prompt catalogue: agent prompts first, then roles."""
    return _build_agent_prompt_defs() + _discover_role_prompts()


#: Full prompt catalogue, built once at module import time.
PROMPTS: list[ACPromptDef] = _build_catalogue()

# ---------------------------------------------------------------------------
# Prompt getter
# ---------------------------------------------------------------------------


def get_prompt(name: str) -> ACPromptResult | None:
    """Return the content of a named prompt.

    Reads the corresponding ``.agentception/`` Markdown file and wraps it in
    an :class:`ACPromptResult` with a single ``user`` message.

    Args:
        name: Prompt name as returned by ``prompts/list``
              (e.g. ``"role/python-developer"`` or ``"agent/dispatcher"``).

    Returns:
        :class:`ACPromptResult` on success, ``None`` when the prompt name is
        unknown or the backing file does not exist.
    """
    path = _resolve_path(name)
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


def _resolve_path(name: str) -> Path | None:
    """Map a prompt name to its backing file path, or None if unknown."""
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
