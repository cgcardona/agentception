"""Tests for agentception/models.py — VALID_ROLES taxonomy sync (issue #822).

Verifies that ``VALID_ROLES`` is derived from the role taxonomy.

Run targeted:
    pytest agentception/tests/test_agentception_models.py -v
"""
from __future__ import annotations

from pathlib import Path

import yaml

from agentception.models import VALID_ROLES
from agentception.types import JsonValue

# Derive taxonomy path the same way models.py does — two levels up from agentception/.
# This avoids importing the private _TAXONOMY_PATH symbol while still testing
# that the path resolution logic is correct.
_HERE = Path(__file__).parent  # agentception/tests/
_TAXONOMY_PATH = _HERE.parent.parent / "scripts" / "gen_prompts" / "role-taxonomy.yaml"


def _spawnable_slugs_from_taxonomy() -> frozenset[str]:
    """Re-read the taxonomy YAML independently and return spawnable slugs.

    Used as a ground-truth reference in tests so any regression — e.g.
    someone accidentally re-introducing a hardcoded frozenset — is caught.
    """
    raw: JsonValue = yaml.safe_load(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "role-taxonomy.yaml must be a YAML mapping"
    slugs: set[str] = set()
    levels = raw.get("levels", [])
    assert isinstance(levels, list)
    for level in levels:
        if not isinstance(level, dict):
            continue
        roles = level.get("roles", [])
        assert isinstance(roles, list)
        for role in roles:
            if isinstance(role, dict) and role.get("spawnable") is True:
                slug = role.get("slug")
                if isinstance(slug, str):
                    slugs.add(slug)
    return frozenset(slugs)


def test_valid_roles_matches_taxonomy() -> None:
    """VALID_ROLES must equal the set of spawnable slugs in role-taxonomy.yaml.

    This is the regression guard for issue #822: if a new role is added to
    the taxonomy with spawnable: true, VALID_ROLES must automatically include
    it without any manual list update in models.py.
    """
    expected = _spawnable_slugs_from_taxonomy()
    assert VALID_ROLES == expected, (
        f"VALID_ROLES is out of sync with role-taxonomy.yaml.\n"
        f"  Missing from VALID_ROLES: {expected - VALID_ROLES}\n"
        f"  Extra in VALID_ROLES:     {VALID_ROLES - expected}"
    )


def test_valid_roles_taxonomy_file_exists() -> None:
    """role-taxonomy.yaml must exist at the resolved path."""
    assert _TAXONOMY_PATH.exists(), (
        f"Taxonomy file not found: {_TAXONOMY_PATH}. "
        "Did the file move? Update _TAXONOMY_PATH in models.py."
    )


def test_valid_roles_is_nonempty() -> None:
    """VALID_ROLES must contain at least the original leaf agent roles."""
    core_roles = {
        "developer",
        "database-architect",
        "reviewer",
    }
    assert core_roles.issubset(VALID_ROLES), (
        f"Core roles missing from VALID_ROLES: {core_roles - VALID_ROLES}"
    )


def test_valid_roles_excludes_non_spawnable() -> None:
    """Orchestration roles (spawnable: false) must not appear in VALID_ROLES."""
    orchestration_roles = {"cto", "engineering-coordinator", "qa-coordinator", "ceo"}
    overlap = orchestration_roles & VALID_ROLES
    assert not overlap, (
        f"Non-spawnable orchestration roles found in VALID_ROLES: {overlap}"
    )


def test_valid_roles_contains_new_taxonomy_roles() -> None:
    """VALID_ROLES must include roles added in the extended taxonomy (issue #822)."""
    new_roles = {
        "developer",
        "developer",
        "developer",
        "developer",
        "developer",
        "developer",
        "developer",
        "site-reliability-engineer",
        "ml-researcher",
        "data-scientist",
    }
    assert new_roles.issubset(VALID_ROLES), (
        f"New taxonomy roles missing from VALID_ROLES: {new_roles - VALID_ROLES}"
    )
