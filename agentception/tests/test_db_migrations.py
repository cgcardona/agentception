"""Structural smoke tests for the AgentCeption Alembic migration chain.

Validates the migration chain without a live database connection:
- Migration files exist.
- Revision IDs are unique.
- down_revision references form a linear chain with exactly one root.
- Exactly one migration has ``down_revision = None`` (the initial migration).
- All expected ac_* tables are referenced in upgrade() calls.

A full integration test (``alembic upgrade head`` against a real Postgres)
runs in CI against the docker-compose postgres service on port 5433.
"""

from __future__ import annotations

import re
from pathlib import Path


def _find_migration_dir() -> Path:
    repo_root = Path(__file__).parent.parent
    candidates = [
        repo_root / "agentception" / "alembic" / "versions",
        repo_root / "alembic" / "versions",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"No Alembic versions directory found under {repo_root}. "
        "Checked: " + ", ".join(str(c) for c in candidates)
    )


def _load_migrations() -> list[tuple[str, str]]:
    """Return list of (filename, content) for all migration files."""
    d = _find_migration_dir()
    files = sorted(d.glob("*.py"))
    return [(f.name, f.read_text()) for f in files if not f.name.startswith("__")]


def _extract_field(content: str, field: str) -> str | None:
    """Extract a string field like ``revision = "abc"`` from migration source."""
    match = re.search(
        rf'^{field}\s*=\s*(?:None|["\']([^"\']*)["\'])',
        content,
        re.MULTILINE,
    )
    if match is None:
        return None
    return match.group(1)  # None string → group(1) is None → return None


def test_migration_files_exist() -> None:
    migrations = _load_migrations()
    assert len(migrations) > 0, "No Alembic migration files found in versions/ directory"


def test_migration_revision_ids_are_unique() -> None:
    migrations = _load_migrations()
    revisions: list[str] = []
    for name, content in migrations:
        rev = _extract_field(content, "revision")
        assert rev is not None, f"Could not extract revision ID from {name}"
        revisions.append(rev)
    assert len(revisions) == len(set(revisions)), (
        f"Duplicate revision IDs found: {revisions}"
    )


def test_migration_chain_has_single_root() -> None:
    """Exactly one migration must have down_revision = None (the initial migration)."""
    migrations = _load_migrations()
    roots: list[str] = []
    for name, content in migrations:
        match = re.search(r"^down_revision\s*=\s*None", content, re.MULTILINE)
        if match:
            roots.append(name)
    assert len(roots) == 1, (
        f"Expected exactly 1 initial migration (down_revision=None), "
        f"found {len(roots)}: {roots}"
    )


def test_migration_down_revisions_reference_existing_revisions() -> None:
    """Every non-root down_revision must point at a revision that exists."""
    migrations = _load_migrations()
    all_revisions: set[str] = set()
    down_map: dict[str, str] = {}  # filename → down_revision (skips roots)

    for name, content in migrations:
        rev = _extract_field(content, "revision")
        if rev:
            all_revisions.add(rev)
        down = _extract_field(content, "down_revision")
        if down is not None:  # None string was matched but group(1) is None → root
            down_map[name] = down

    for name, down in down_map.items():
        assert down in all_revisions, (
            f"{name}: down_revision='{down}' references unknown revision. "
            f"Known revisions: {sorted(all_revisions)}"
        )


def test_migration_chain_is_linear() -> None:
    """No two migrations should share the same down_revision (no branches)."""
    migrations = _load_migrations()
    seen_down: dict[str, str] = {}  # down_revision → first filename that uses it

    for name, content in migrations:
        down = _extract_field(content, "down_revision")
        if down is None:
            continue  # root migration — skip
        assert down not in seen_down, (
            f"Branch detected: both '{seen_down[down]}' and '{name}' "
            f"have down_revision='{down}'. The chain must be linear."
        )
        seen_down[down] = name


def test_initial_migration_creates_core_ac_tables() -> None:
    """The first migration (down_revision=None) must create the core ac_* tables."""
    migrations = _load_migrations()
    initial_content = ""
    for _name, content in migrations:
        if re.search(r"^down_revision\s*=\s*None", content, re.MULTILINE):
            initial_content = content
            break

    assert initial_content, "No initial migration found"

    expected_tables = [
        "waves",
        "agent_runs",
        "issues",
        "pull_requests",
    ]
    for table in expected_tables:
        assert table in initial_content, (
            f"Initial migration does not create expected table '{table}'. "
            "Ensure 0001_initial covers all core tables."
        )


def test_all_migrations_have_downgrade() -> None:
    """Every migration must define a downgrade() function for rollback support."""
    migrations = _load_migrations()
    for name, content in migrations:
        has_downgrade = re.search(r"^def downgrade\(", content, re.MULTILINE)
        assert has_downgrade, (
            f"{name} is missing a downgrade() function. "
            "All migrations must be reversible."
        )
