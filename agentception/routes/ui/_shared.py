from __future__ import annotations

"""Shared helpers, constants, and the Jinja2 template singleton for all UI routes.

This module is the single source of truth for:
- ``_TEMPLATES``: the Jinja2Templates instance (created once, filters registered here).
- Helper functions used across multiple UI route modules.
- ``_find_agent`` / ``_issue_is_claimed``: also re-exported from the package
  ``__init__`` so that api.py imports keep working without circular dependencies.
"""

import datetime
import logging
from pathlib import Path

from fastapi.templating import Jinja2Templates

from agentception.config import settings as _settings
from agentception.models import AgentNode, PipelineState

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent  # agentception/routes/ui/
_TEMPLATES = Jinja2Templates(directory=str(_HERE.parent.parent / "templates"))

# Ordered category map for the spawn Mission Control role picker.
# Each entry: slug → (category_name, sort_position_within_category)
# Roles not listed here fall into the "Other" catch-all category, which
# appears last so newly-added taxonomy roles are always visible in the form.
_ROLE_CATEGORY_MAP: dict[str, tuple[str, int]] = {
    # Backend
    "python-developer":         ("Backend", 0),
    "api-developer":            ("Backend", 1),
    "database-architect":       ("Backend", 2),
    "systems-programmer":       ("Backend", 3),
    "go-developer":             ("Backend", 4),
    "rails-developer":          ("Backend", 5),
    # Frontend
    "frontend-developer":       ("Frontend", 0),
    "mobile-developer":         ("Frontend", 1),
    "full-stack-developer":     ("Frontend", 2),
    "typescript-developer":     ("Frontend", 3),
    "react-developer":          ("Frontend", 4),
    # Mobile
    "ios-developer":            ("Mobile", 0),
    "android-developer":        ("Mobile", 1),
    # Quality
    "test-engineer":            ("Quality", 0),
    "pr-reviewer":              ("Quality", 1),
    "technical-writer":         ("Quality", 2),
    # Infrastructure
    "devops-engineer":          ("Infrastructure", 0),
    "security-engineer":        ("Infrastructure", 1),
    "site-reliability-engineer": ("Infrastructure", 2),
    # Data / AI
    "ml-engineer":              ("Data / AI", 0),
    "data-engineer":            ("Data / AI", 1),
    "ml-researcher":            ("Data / AI", 2),
    "data-scientist":           ("Data / AI", 3),
    # Systems
    "rust-developer":           ("Systems", 0),
    # Architecture
    "architect":                ("Architecture", 0),
}

_CATEGORY_ORDER: list[str] = [
    "Backend", "Frontend", "Mobile", "Quality", "Infrastructure",
    "Data / AI", "Systems", "Architecture", "Other",
]


def _timestamp_to_date(ts: float) -> str:
    try:
        return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "—"


def _md_to_html(text: str) -> str:
    """Convert Markdown text to safe HTML for use in Jinja templates.

    Pre-processes docstring-style text to ensure bullet lists are detected:
    Python docstrings often place ``- item`` immediately after a sentence with
    only a single newline, but the Markdown spec requires a blank line before
    a bullet list.  We insert that blank line automatically.

    Enabled extensions:
    - ``fenced_code``  — triple-backtick code blocks
    - ``tables``       — GFM-style tables
    """
    import re
    import markdown as _md  # type: ignore[import-untyped]
    from markupsafe import Markup

    # Insert a blank line before bullet/numbered list items that follow a
    # non-blank line so the Markdown parser recognises them as a list block.
    text = re.sub(r"([^\n])\n([ \t]*(?:[-*+]|\d+\.) )", r"\1\n\n\2", text)

    result = _md.markdown(
        text,
        extensions=["fenced_code", "tables"],
        output_format="html",
    )
    return Markup(result)


def _parse_iso(s: object) -> datetime.datetime | None:
    """Parse an ISO-8601 datetime string, returning None on failure."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None


def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def _fmt_elapsed(spawned_iso: object) -> str:
    """Return a human-readable elapsed time string from an ISO spawn timestamp to now."""
    dt = _parse_iso(spawned_iso)
    if dt is None:
        return ""
    delta = datetime.datetime.utcnow() - dt
    return _fmt_duration(max(0.0, delta.total_seconds()))


def _format_ts(ts: float) -> str:
    """Format a UNIX timestamp as a short UTC datetime string for the telemetry table."""
    try:
        return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def _format_number(n: int) -> str:
    """Format an integer with thousands separators for readability."""
    return f"{n:,}"


def _dirname(path: str) -> str:
    """Return the parent directory of a path string (equivalent to os.path.dirname)."""
    import os
    return os.path.dirname(path)


def _issue_is_claimed(iss: dict[str, object]) -> bool:
    """Return True when an issue carries the ``agent:wip`` label.

    Handles both list-of-strings and list-of-label-objects shapes so the
    helper works correctly regardless of which GitHub reader format is used.
    """
    raw = iss.get("labels")
    if not isinstance(raw, list):
        return False
    for lbl in raw:
        if isinstance(lbl, str) and lbl == "agent:wip":
            return True
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if name == "agent:wip":
                return True
    return False


def _find_agent(state: PipelineState | None, agent_id: str) -> AgentNode | None:
    """Search the live agent tree for an AgentNode by its ID.

    ``agent_id`` is the worktree basename (e.g. ``issue-732``), which is the
    canonical ID assigned by the poller.  Searches root agents then children.
    Returns ``None`` when the state is empty or no match is found.
    """
    if state is None:
        return None
    for agent in state.agents:
        if agent.id == agent_id:
            return agent
        for child in agent.children:
            if child.id == agent_id:
                return child
    return None


# ---------------------------------------------------------------------------
# Register Jinja2 filters and globals at import time.
# Every UI module imports _TEMPLATES from here, so these registrations apply
# across the entire UI package before the first template is rendered.
# ---------------------------------------------------------------------------
_TEMPLATES.env.filters["timestamp_to_date"] = _timestamp_to_date
_TEMPLATES.env.filters["markdown"] = _md_to_html
_TEMPLATES.env.filters["format_ts"] = _format_ts
_TEMPLATES.env.filters["format_number"] = _format_number
_TEMPLATES.env.filters["dirname"] = _dirname
_TEMPLATES.env.globals["gh_repo"] = _settings.gh_repo
_TEMPLATES.env.globals["gh_base_url"] = f"https://github.com/{_settings.gh_repo}"
