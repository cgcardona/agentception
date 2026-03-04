"""Tests for agentception.intelligence.analyzer.

All tests call the synchronous _analyze_body helper directly so no event loop,
GitHub CLI, or network access is required.  The async analyze_issue function is
covered by an integration smoke-test that is skipped in CI.
"""
from __future__ import annotations

import pytest

from agentception.intelligence.analyzer import (
    IssueAnalysis,
    _analyze_body,
    extract_modified_files,
    infer_conflict_risk,
    infer_parallelism,
    infer_role,
    parse_deps_from_body,
)

# ---------------------------------------------------------------------------
# parse_deps_from_body
# ---------------------------------------------------------------------------


def test_analyze_detects_deps_single() -> None:
    """Single 'Depends on #N' pattern is parsed correctly."""
    body = "## Overview\n\nDepends on #614\n\nSome more text."
    deps = parse_deps_from_body(body)
    assert deps == [614]


def test_analyze_detects_deps_multiple_inline() -> None:
    """Multiple deps on the same line (comma-separated) are all captured."""
    body = "**Depends on #614, #615**"
    deps = parse_deps_from_body(body)
    assert set(deps) == {614, 615}


def test_analyze_detects_deps_blocked_by() -> None:
    """'Blocked by' is treated identically to 'Depends on'."""
    body = "Blocked by #620"
    deps = parse_deps_from_body(body)
    assert deps == [620]


def test_analyze_detects_deps_requires() -> None:
    """'Requires' keyword is also recognised."""
    body = "Requires #611"
    deps = parse_deps_from_body(body)
    assert deps == [611]


def test_analyze_no_deps_returns_empty() -> None:
    """Issue body without dependency declarations returns empty list."""
    body = "## Overview\n\nJust implement a new endpoint."
    deps = parse_deps_from_body(body)
    assert deps == []


def test_analyze_deps_are_sorted_and_deduped() -> None:
    """Duplicate deps are deduplicated; output is sorted ascending."""
    body = "Depends on #615, #614, #615"
    deps = parse_deps_from_body(body)
    assert deps == [614, 615]


# ---------------------------------------------------------------------------
# extract_modified_files
# ---------------------------------------------------------------------------


def test_extract_files_from_standard_section() -> None:
    """Files listed under a '### Files to Create / Modify' heading are returned."""
    body = (
        "## Overview\n\nSome text.\n\n"
        "### Files to Create / Modify\n\n"
        "- `agentception/intelligence/analyzer.py` (new)\n"
        "- `agentception/routes/api.py`\n"
        "- `agentception/tests/test_agentception_analyzer.py` (new)\n\n"
        "## Tests\n\nMore text."
    )
    files = extract_modified_files(body)
    assert files == [
        "agentception/intelligence/analyzer.py",
        "agentception/routes/api.py",
        "agentception/tests/test_agentception_analyzer.py",
    ]


def test_extract_files_no_section_returns_empty() -> None:
    """Body without a Files section returns an empty list."""
    body = "## Overview\n\nImplement the thing."
    files = extract_modified_files(body)
    assert files == []


def test_extract_files_stops_at_next_heading() -> None:
    """File extraction does not bleed into the next heading's content."""
    body = (
        "### Files\n\n"
        "- `foo/bar.py`\n\n"
        "### Tests\n\n"
        "- Not a file\n"
    )
    files = extract_modified_files(body)
    assert files == ["foo/bar.py"]


# ---------------------------------------------------------------------------
# infer_role
# ---------------------------------------------------------------------------


def test_analyze_migration_recommends_database_role() -> None:
    """Issues whose files include an Alembic migration get database-architect."""
    files = ["alembic/versions/0006_add_table.py"]
    role = infer_role("Some body text", files)
    assert role == "database-architect"


def test_analyze_alembic_keyword_in_body_recommends_database_role() -> None:
    """Mention of 'alembic' in the body without file list → database-architect."""
    body = "Run alembic revision --autogenerate to create the migration."
    role = infer_role(body, [])
    assert role == "database-architect"


def test_analyze_new_module_recommends_python_developer() -> None:
    """Pure Python modules with no migration signals → python-developer."""
    files = ["agentception/intelligence/analyzer.py"]
    role = infer_role("Add a new endpoint to parse tickets.", files)
    assert role == "python-developer"


# ---------------------------------------------------------------------------
# infer_parallelism
# ---------------------------------------------------------------------------


def test_analyze_new_files_is_safe() -> None:
    """Issue that only creates new files is classified as safe."""
    body = (
        "### Files\n\n"
        "- `agentception/intelligence/analyzer.py` (new)\n"
        "- `agentception/tests/test_agentception_analyzer.py` (new)\n"
    )
    files = extract_modified_files(body)
    parallelism = infer_parallelism(body, files)
    assert parallelism == "safe"


def test_analyze_shared_file_is_risky() -> None:
    """Issue that modifies a known high-conflict file is classified as risky."""
    files = ["agentception/app.py"]
    parallelism = infer_parallelism("Modify app.py to register a new router.", files)
    assert parallelism == "risky"


def test_analyze_serial_marker_overrides() -> None:
    """Explicit 'must run alone' text forces serial classification."""
    body = "This migration must run alone before anything else."
    parallelism = infer_parallelism(body, [])
    assert parallelism == "serial"


def test_analyze_shared_config_file_is_risky() -> None:
    """Touching maestro/config.py is classified as risky."""
    files = ["maestro/config.py"]
    parallelism = infer_parallelism("Update config settings.", files)
    assert parallelism == "risky"


# ---------------------------------------------------------------------------
# infer_conflict_risk
# ---------------------------------------------------------------------------


def test_analyze_no_shared_files_no_risk() -> None:
    """Brand-new files produce zero conflict risk."""
    files = ["agentception/intelligence/dag.py"]
    risk = infer_conflict_risk(files)
    assert risk == "none"


def test_analyze_high_conflict_file_is_high_risk() -> None:
    """High-conflict additive files (e.g. app.py) produce high risk."""
    files = ["agentception/app.py"]
    risk = infer_conflict_risk(files)
    assert risk == "high"


def test_analyze_shared_config_is_low_risk() -> None:
    """Shared config files produce low (not high) risk."""
    files = ["maestro/config.py"]
    risk = infer_conflict_risk(files)
    assert risk == "low"


# ---------------------------------------------------------------------------
# _analyze_body (integration over all heuristics)
# ---------------------------------------------------------------------------


def test_analyze_no_body_returns_safe_defaults() -> None:
    """Empty body produces safe defaults with no dependencies."""
    result = _analyze_body(number=999, body="")
    assert isinstance(result, IssueAnalysis)
    assert result.number == 999
    assert result.dependencies == []
    assert result.parallelism == "safe"
    assert result.conflict_risk == "none"
    assert result.modifies_files == []
    assert result.recommended_role == "python-developer"
    assert result.recommended_merge_after is None


def test_analyze_full_issue_body() -> None:
    """Realistic issue body produces correct recommendations end-to-end."""
    body = (
        "## Overview\n\n"
        "Implement the ticket analyzer endpoint.\n\n"
        "**Depends on:** #616\n\n"
        "### Files to Create / Modify\n\n"
        "- `agentception/intelligence/analyzer.py` (new)\n"
        "- `agentception/routes/api.py`\n"
        "- `agentception/tests/test_agentception_analyzer.py` (new)\n\n"
        "## Tests\n\nSee acceptance criteria."
    )
    result = _analyze_body(number=632, body=body)
    assert result.number == 632
    assert result.dependencies == [616]
    assert result.recommended_merge_after == 616
    assert result.recommended_role == "python-developer"
    # api.py is not in the high-conflict or shared-config sets
    assert result.parallelism == "safe"
    assert result.conflict_risk == "none"
    assert "agentception/intelligence/analyzer.py" in result.modifies_files
    assert "agentception/routes/api.py" in result.modifies_files
