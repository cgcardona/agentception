from __future__ import annotations

"""System prompt assembly for all agent types.

Provides :func:`build_system_prompt` — the single entry-point for assembling
an agent's system prompt from a ``cognitive_arch`` string plus role-specific
instructions.

Required ordering
-----------------
Every system prompt assembled by this module follows this contract:

1. **Cognitive architecture persona block (always first).**
   Establishes who the agent *is* before it reads its role.  The persona
   block contains the figure's display name and mental-model description
   loaded from the cognitive_archetypes YAML files.

2. **Role-specific instructions.**
   What the agent is asked to do in this execution context — coordinator
   survey, leaf implementation, PR review, etc.

3. **Tool/capability declarations.**
   Any trailing capability or tool-usage context appended by the caller.

This ordering must be preserved.  Swapping (1) and (2) causes the agent to
anchor on role identity before persona, which weakens the cognitive
architecture's influence on reasoning style.
"""

import logging
from pathlib import Path

import yaml

from agentception.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(
    cognitive_arch: str | None,
    role_instructions: str,
    *,
    agent_type: str = "leaf",
    is_resumed: bool = False,
) -> str:
    """Assemble the full system prompt for any agent type.

    Enforces the required ordering:

    1. Cognitive architecture persona block (always first).
    2. Self-introduction instruction (omitted when ``is_resumed=True``).
    3. Role-specific instructions.
    4. Tool/capability declarations (appended by the caller after this call).

    The self-introduction instruction instructs the agent to output its name
    and cognitive architecture as the very first visible response — in the
    response content, not inside a thinking/scratchpad block.  Resumed agents
    skip this step because the user already witnessed the introduction in the
    original run.

    Args:
        cognitive_arch: The cognitive architecture string from the agent's
            ``.agent-task`` file (``[agent].cognitive_arch`` field), e.g.
            ``"guido_van_rossum:postgresql:python"``.  Pass ``None`` when the
            field is absent — a warning will be logged.
        role_instructions: Role-specific instructions text (coordinator survey
            steps, leaf implementation steps, PR review criteria, etc.).
        agent_type: Descriptive tag used in log messages; ``"coordinator"``
            or ``"leaf"``.  Does not affect the assembled content.
        is_resumed: When ``True``, the self-introduction instruction is omitted
            because the agent is resuming a previous session and re-announcing
            would be redundant.  Always ``False`` for fresh cold-start agents.

    Returns:
        The assembled system prompt string, ready to be passed as the
        ``system_prompt`` argument to an LLM call.
    """
    persona_block = _build_persona_block(cognitive_arch, agent_type)
    intro_instruction = _build_intro_instruction(cognitive_arch, is_resumed)
    parts: list[str] = []
    if persona_block:
        parts.append(persona_block)
    if intro_instruction:
        parts.append(intro_instruction)
    parts.append(role_instructions)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_intro_instruction(cognitive_arch: str | None, is_resumed: bool) -> str:
    """Build the mandatory self-introduction instruction block.

    Returns an empty string when ``is_resumed`` is ``True`` (resumed agents do
    not re-announce — the user already witnessed the introduction in the
    original session) or when ``cognitive_arch`` is absent or malformed.

    The returned block instructs the agent to output a single visible sentence
    as its very first response, in this exact format::

        My name is {display_name}. My cognitive architecture is: {description}.

    The instruction is structurally placed before role instructions so the LLM
    anchors on it before reading task details.  The instruction also explicitly
    prohibits hiding the announcement inside a thinking / scratchpad block.

    Args:
        cognitive_arch: Raw cognitive architecture string, e.g.
            ``"guido_van_rossum:postgresql:python"``.
        is_resumed: When ``True``, returns ``""`` immediately — no instruction
            is appended for resumed agents.

    Returns:
        A multi-line instruction string, or ``""`` when skipped.
    """
    if is_resumed or not cognitive_arch:
        return ""

    parts = [p.strip() for p in cognitive_arch.split(":") if p.strip()]
    if not parts:
        return ""

    display_name, description = _load_figure_identity(parts[0])
    arch_summary = description if description else cognitive_arch

    return (
        "⚠️ MANDATORY SELF-INTRODUCTION — your very first action before anything else:\n"
        "Output the following sentence verbatim as visible text in your response (not inside "
        "a thinking block, not as a tool call, not as a shell command — actual visible response text):\n\n"
        f'"My name is {display_name}. My cognitive architecture is: {arch_summary}."\n\n'
        "Do not proceed to any other action until this announcement has been sent. "
        "This applies at every tier: coordinator, sub-coordinator, and leaf alike."
    )


def _build_persona_block(cognitive_arch: str | None, agent_type: str) -> str:
    """Build the cognitive architecture persona block for the top of a system prompt.

    Loads the figure YAML from ``scripts/gen_prompts/cognitive_archetypes/figures/``
    and returns a block of the form::

        You are {display_name}. Your cognitive architecture: {description}

    Returns an empty string — **never raises** — when the arch string is absent,
    malformed, or the figure YAML cannot be found.  All failure paths emit a
    warning so injection failures are visible in logs.

    Args:
        cognitive_arch: Raw ``cognitive_arch`` field value, e.g.
            ``"guido_van_rossum:postgresql:python"``.
        agent_type: Used only in log messages.

    Returns:
        Persona block string, or ``""`` when the arch is missing/invalid.
    """
    if not cognitive_arch:
        logger.warning(
            "⚠️ build_system_prompt: cognitive_arch is absent for %s agent — "
            "persona block will be omitted from system prompt.",
            agent_type,
        )
        return ""

    parts = [p.strip() for p in cognitive_arch.split(":") if p.strip()]
    if not parts:
        logger.warning(
            "⚠️ build_system_prompt: cognitive_arch %r is malformed for %s agent — "
            "persona block will be omitted.",
            cognitive_arch,
            agent_type,
        )
        return ""

    figure_id = parts[0]
    display_name, description = _load_figure_identity(figure_id)
    return _format_persona(display_name, description)


def _load_figure_identity(figure_id: str) -> tuple[str, str]:
    """Load display name and description from a figure YAML file.

    Returns:
        ``(display_name, description)`` — both strings, non-empty only when the
        YAML exists and contains the respective fields.  Falls back to
        ``(figure_id, "")`` on any error.
    """
    figures_dir: Path = (
        settings.repo_dir
        / "scripts"
        / "gen_prompts"
        / "cognitive_archetypes"
        / "figures"
    )
    figure_path = figures_dir / f"{figure_id}.yaml"

    if not figure_path.exists():
        logger.warning(
            "⚠️ build_system_prompt: figure YAML not found for %r at %s — "
            "using bare figure_id as persona name.",
            figure_id,
            figure_path,
        )
        return figure_id, ""

    try:
        raw: object = yaml.safe_load(figure_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ build_system_prompt: failed to parse figure YAML for %r: %s",
            figure_id,
            exc,
        )
        return figure_id, ""

    if not isinstance(raw, dict):
        logger.warning(
            "⚠️ build_system_prompt: figure YAML for %r is not a mapping.",
            figure_id,
        )
        return figure_id, ""

    display_name = str(raw.get("display_name", figure_id))
    description = str(raw.get("description", "")).strip()
    return display_name, description


def _format_persona(display_name: str, description: str) -> str:
    """Format the persona line from display name and description.

    Returns a single-sentence block ``"You are {name}. Your cognitive
    architecture: {description}"`` when a description is available, or
    ``"You are {name}."`` otherwise.
    """
    if description:
        return f"You are {display_name}. Your cognitive architecture: {description}"
    return f"You are {display_name}."
