from __future__ import annotations

"""Shared helpers and constants for all JSON API routes.

Contains:
- ``_SENTINEL``: path to the pipeline-pause sentinel file.
- ``ROLE_DEFAULT_FIGURE``: re-exported from ``services.cognitive_arch``.
- ``_derive_skills_from_body``: re-exported from ``services.cognitive_arch``.
- ``_extract_skills_from_body``: re-exported from ``services.cognitive_arch``.
- ``_resolve_cognitive_arch``: re-exported from ``services.cognitive_arch``.
- ``_build_agent_task``: re-exported from ``services.task_builders``.
- ``_build_coordinator_task``: re-exported from ``services.task_builders``.
- ``_build_conductor_task``: re-exported from ``services.task_builders``.
- ``_issue_is_claimed_api``: checks ``agent/wip`` label presence.
"""

from pathlib import Path

from agentception.config import settings
from agentception.services.cognitive_arch import (
    ROLE_DEFAULT_FIGURE as ROLE_DEFAULT_FIGURE,
    _derive_skills_from_body as _derive_skills_from_body,
    _extract_skills_from_body as _extract_skills_from_body,
    _resolve_cognitive_arch as _resolve_cognitive_arch,
)
from agentception.services.task_builders import (
    _build_agent_task as _build_agent_task,
    _build_conductor_task as _build_conductor_task,
    _build_coordinator_task as _build_coordinator_task,
)

# Path to the sentinel file that pauses the agent pipeline.
# Writing this file tells CTO and coordinator loops to wait rather than spawn agents.
_SENTINEL: Path = settings.ac_dir / ".pipeline-pause"


def _issue_is_claimed_api(iss: dict[str, object]) -> bool:
    """Return True when an issue carries the ``agent/wip`` label."""
    raw = iss.get("labels")
    if not isinstance(raw, list):
        return False
    for lbl in raw:
        if isinstance(lbl, str) and lbl == "agent/wip":
            return True
        if isinstance(lbl, dict) and lbl.get("name") == "agent/wip":
            return True
    return False
