"""Shared role-file loading utilities.

Both the agent loop (``services/agent_loop.py``) and the MCP prompts layer
(``mcp/prompts.py``) need to resolve a role slug to Markdown content.  This
module owns that logic in a single place so the fallback behaviour is
consistent across all call paths.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Role-family suffixes that serve as meaningful fallbacks for specialised
#: roles.  ``python-developer`` → ``developer``, ``data-engineer`` → ``engineer``, etc.
_BASE_FAMILIES: frozenset[str] = frozenset({
    "developer", "coordinator", "engineer", "analyst",
    "architect", "researcher", "writer", "programmer",
})


def role_family_fallback(role: str) -> str | None:
    """Return the base role slug to try when *role*'s file is missing.

    Language- and domain-prefixed roles (e.g. ``python-developer``,
    ``react-developer``, ``data-engineer``) share execution contracts with
    their base family.  When the specific file is absent, loading the base
    file is far better than returning an empty system prompt.

    Returns ``None`` when no meaningful fallback exists (e.g. the role has no
    ``-`` separator, or the suffix is not a known base family).
    """
    if "-" not in role:
        return None
    suffix = role.rsplit("-", 1)[-1]
    return suffix if suffix in _BASE_FAMILIES else None


def load_role_file(role: str, roles_dir: Path, variant: str | None = None) -> str:
    """Return the Markdown content of the role file for *role*.

    Resolution order:

    1. ``{roles_dir}/{role}-{variant}.md`` (only when *variant* is non-empty)
    2. ``{roles_dir}/{role}.md``
    3. ``{roles_dir}/{base}.md`` where *base* is the role-family fallback
       (e.g. ``python-developer`` → ``developer``)

    Returns an empty string when no file can be found at all.
    """
    if not role:
        logger.warning("⚠️  load_role_file — no role specified")
        return ""

    # 1. Variant file.
    if variant:
        candidate = roles_dir / f"{role}-{variant}.md"
        if candidate.exists():
            logger.info("load_role_file: loading %s (variant=%s)", candidate, variant)
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("⚠️  load_role_file — OS error reading %s: %s", candidate, exc)

    # 2. Exact role file.
    role_path = roles_dir / f"{role}.md"
    try:
        content = role_path.read_text(encoding="utf-8")
        logger.info("load_role_file: loaded %s", role_path)
        return content
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("⚠️  load_role_file — OS error reading %s: %s", role_path, exc)
        return ""

    # 3. Role-family fallback (e.g. python-developer → developer).
    base = role_family_fallback(role)
    if base:
        base_path = roles_dir / f"{base}.md"
        try:
            content = base_path.read_text(encoding="utf-8")
            logger.info("✅ load_role_file — using family fallback %s → %s", role, base)
            return content
        except OSError:
            pass

    logger.warning("⚠️  load_role_file — role file not found: %s", role_path)
    return ""
