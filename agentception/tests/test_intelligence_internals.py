"""Unit tests for the intelligence layer internals.

Covers every pure function and async service in:
  agentception/intelligence/analyzer.py  — parse_deps_from_body, extract_modified_files,
                                           infer_role, infer_parallelism, infer_conflict_risk,
                                           _analyze_body
  agentception/intelligence/dag.py       — build_dag
  agentception/intelligence/guards.py    — _parse_closes, detect_stale_claims,
                                           detect_out_of_order_prs

All pure functions are called synchronously with no mocks.
Async functions that call external I/O are exercised with mocked dependencies.

Run targeted:
    pytest agentception/tests/test_intelligence_internals.py -v
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.intelligence.analyzer import (
    _analyze_body,
    extract_modified_files,
    infer_conflict_risk,
    infer_parallelism,
    infer_role,
    parse_deps_from_body,
)
from agentception.intelligence.dag import build_dag
from agentception.intelligence.guards import (
    PRViolation,
    _parse_closes,
    detect_out_of_order_prs,
    detect_stale_claims,
)
from agentception.models import StaleClaim


# ── parse_deps_from_body ──────────────────────────────────────────────────────


def test_parse_deps_empty_body_returns_empty_list() -> None:
    """An empty body must yield no dependency numbers."""
    assert parse_deps_from_body("") == []


def test_parse_deps_single_depends_on() -> None:
    """'Depends on #614' must extract 614."""
    assert parse_deps_from_body("Depends on #614") == [614]


def test_parse_deps_multiple_issues_on_one_line() -> None:
    """'Depends on #614, #615' must extract both numbers, sorted."""
    result = parse_deps_from_body("Depends on #614, #615")
    assert result == [614, 615]


def test_parse_deps_blocked_by_pattern() -> None:
    """'Blocked by #99' must be recognised and extracted."""
    assert parse_deps_from_body("Blocked by #99") == [99]


def test_parse_deps_requires_pattern() -> None:
    """'Requires #7' must be recognised and extracted."""
    assert parse_deps_from_body("Requires #7") == [7]


def test_parse_deps_bold_markdown() -> None:
    """'**Depends on #614**' (bold) must be parsed correctly."""
    assert parse_deps_from_body("**Depends on #614**") == [614]


def test_parse_deps_case_insensitive() -> None:
    """'DEPENDS ON #1' must be matched case-insensitively."""
    assert parse_deps_from_body("DEPENDS ON #1") == [1]


def test_parse_deps_deduplicates_repeated_issue() -> None:
    """The same issue number appearing twice must appear once in the result."""
    body = "Depends on #42\nBlocked by #42"
    assert parse_deps_from_body(body) == [42]


def test_parse_deps_multiple_keyword_lines() -> None:
    """Dependencies from multiple lines must all be collected."""
    body = "Depends on #10\nBlocked by #20"
    assert parse_deps_from_body(body) == [10, 20]


def test_parse_deps_no_keyword_returns_empty() -> None:
    """A body with issue references but no recognised keyword must yield []."""
    assert parse_deps_from_body("See also #42 and #43") == []


# ── extract_modified_files ────────────────────────────────────────────────────


def test_extract_files_no_section_returns_empty() -> None:
    """A body with no Files heading must yield an empty list."""
    assert extract_modified_files("No heading here") == []


def test_extract_files_backtick_paths() -> None:
    """Backtick-quoted paths must be extracted without the backticks."""
    body = "### Files to Create / Modify\n- `agentception/foo.py`\n"
    assert extract_modified_files(body) == ["agentception/foo.py"]


def test_extract_files_unquoted_paths() -> None:
    """Unquoted list items must be stripped of bullet and trailing annotations."""
    body = "### Files\n- agentception/bar.py (new)\n"
    result = extract_modified_files(body)
    assert result == ["agentception/bar.py"]


def test_extract_files_multiple_items() -> None:
    """Multiple list items must all be collected in order."""
    body = "### Files\n- `a/b.py`\n- `c/d.py`\n"
    assert extract_modified_files(body) == ["a/b.py", "c/d.py"]


def test_extract_files_stops_at_next_heading() -> None:
    """Items after the next heading must not be included."""
    body = "### Files\n- `a/b.py`\n### Other\n- `c/d.py`\n"
    assert extract_modified_files(body) == ["a/b.py"]


def test_extract_files_ignores_prose_lines() -> None:
    """List items without a slash or dot (pure prose) must be discarded."""
    body = "### Files\n- just some note\n- `real/file.py`\n"
    assert extract_modified_files(body) == ["real/file.py"]


def test_extract_files_asterisk_bullet() -> None:
    """List items starting with '*' instead of '-' must also be parsed."""
    body = "### Files\n* `x/y.py`\n"
    assert extract_modified_files(body) == ["x/y.py"]


# ── infer_role ────────────────────────────────────────────────────────────────


def test_infer_role_alembic_in_file_path_returns_database_architect() -> None:
    """A file path containing 'alembic' must trigger the database-architect role."""
    assert infer_role("", ["alembic/versions/0001_init.py"]) == "database-architect"


def test_infer_role_migration_in_file_path_returns_database_architect() -> None:
    """A file path containing 'migration' must trigger the database-architect role."""
    assert infer_role("", ["db/migration_script.py"]) == "database-architect"


def test_infer_role_migration_keyword_in_body_returns_database_architect() -> None:
    """The word 'migration' in the body must trigger the database-architect role."""
    assert infer_role("Add a migration for new table", []) == "database-architect"


def test_infer_role_sqlalchemy_in_body_returns_database_architect() -> None:
    """'sqlalchemy' in the body must trigger the database-architect role."""
    assert infer_role("Use SQLAlchemy ORM", []) == "database-architect"


def test_infer_role_alembic_in_body_returns_database_architect() -> None:
    """'alembic' in the body must trigger the database-architect role."""
    assert infer_role("Run alembic upgrade head", []) == "database-architect"


def test_infer_role_clean_body_returns_python_developer() -> None:
    """A body with no database signals must produce 'python-developer'."""
    assert infer_role("Add a new API endpoint", ["agentception/routes/api/widget.py"]) == "python-developer"


# ── infer_parallelism ─────────────────────────────────────────────────────────


def test_infer_parallelism_serial_marker_must_run_alone() -> None:
    """'must run alone' in body must return 'serial'."""
    assert infer_parallelism("This task must run alone.", []) == "serial"


def test_infer_parallelism_serial_marker_do_not_parallelize() -> None:
    """'do not parallelize' in body must return 'serial'."""
    assert infer_parallelism("Do not parallelize this.", []) == "serial"


def test_infer_parallelism_high_conflict_file_returns_risky() -> None:
    """A file matching the high-conflict set (agentception/app.py) must return 'risky'."""
    assert infer_parallelism("", ["agentception/app.py"]) == "risky"


def test_infer_parallelism_shared_config_file_returns_risky() -> None:
    """A file matching the shared-config set (agentception/config.py) must return 'risky'."""
    assert infer_parallelism("", ["agentception/config.py"]) == "risky"


def test_infer_parallelism_no_files_returns_safe() -> None:
    """No file list and no serial marker must return 'safe'."""
    assert infer_parallelism("Add a helper function.", []) == "safe"


def test_infer_parallelism_new_only_files_returns_safe() -> None:
    """Files that only appear next to '(new)' in the body must return 'safe'."""
    body = "### Files\n- `agentception/widget.py` (new)\n"
    assert infer_parallelism(body, ["agentception/widget.py"]) == "safe"


def test_infer_parallelism_non_conflict_file_returns_safe() -> None:
    """A file not in any conflict set returns 'safe'."""
    assert infer_parallelism("", ["agentception/utils/helpers.py"]) == "safe"


# ── infer_conflict_risk ───────────────────────────────────────────────────────


def test_infer_conflict_risk_no_files_returns_none() -> None:
    """An empty file list must return 'none'."""
    assert infer_conflict_risk([]) == "none"


def test_infer_conflict_risk_unknown_file_returns_none() -> None:
    """A file not in either conflict set must return 'none'."""
    assert infer_conflict_risk(["agentception/utils/new.py"]) == "none"


def test_infer_conflict_risk_high_conflict_file_returns_high() -> None:
    """agentception/app.py must produce 'high' conflict risk."""
    assert infer_conflict_risk(["agentception/app.py"]) == "high"


def test_infer_conflict_risk_shared_config_returns_low() -> None:
    """agentception/config.py (shared config only) must produce 'low' conflict risk."""
    assert infer_conflict_risk(["agentception/config.py"]) == "low"


def test_infer_conflict_risk_high_wins_over_low() -> None:
    """When both high and low conflict files are present, 'high' must win."""
    assert infer_conflict_risk(["agentception/app.py", "agentception/config.py"]) == "high"


# ── _analyze_body (integration) ───────────────────────────────────────────────


def test_analyze_body_empty_returns_safe_defaults() -> None:
    """An empty body must produce safe defaults for all fields."""
    result = _analyze_body(1, "")
    assert result.number == 1
    assert result.dependencies == []
    assert result.parallelism == "safe"
    assert result.conflict_risk == "none"
    assert result.modifies_files == []
    assert result.recommended_role == "python-developer"
    assert result.recommended_merge_after is None


def test_analyze_body_full_integration() -> None:
    """A fully-populated body must produce correct recommendations end-to-end."""
    body = (
        "Depends on #10\n"
        "Blocked by #20\n\n"
        "### Files to Create / Modify\n"
        "- `agentception/config.py`\n\n"
        "### Details\n"
        "Uses sqlalchemy.\n"
    )
    result = _analyze_body(99, body)
    assert result.number == 99
    assert result.dependencies == [10, 20]
    assert result.recommended_merge_after == 20       # max(10, 20)
    assert result.recommended_role == "database-architect"
    assert "agentception/config.py" in result.modifies_files
    assert result.conflict_risk == "low"              # config.py is shared-config


def test_analyze_body_deps_are_sorted_and_deduplicated() -> None:
    """Dependencies must be sorted ascending and deduplicated."""
    body = "Depends on #30, #10\nBlocked by #10"
    result = _analyze_body(1, body)
    assert result.dependencies == [10, 30]
    assert result.recommended_merge_after == 30


# ── _parse_closes ─────────────────────────────────────────────────────────────


def test_parse_closes_found_uppercase_c() -> None:
    """'Closes #42' must return 42."""
    assert _parse_closes("Closes #42") == 42


def test_parse_closes_found_lowercase() -> None:
    """'closes #7' must return 7."""
    assert _parse_closes("closes #7") == 7


def test_parse_closes_found_close_singular() -> None:
    """'Close #1' (singular) must return 1."""
    assert _parse_closes("Close #1") == 1


def test_parse_closes_first_reference_returned() -> None:
    """When multiple 'Closes' references exist, only the first is returned."""
    assert _parse_closes("Closes #5\nCloses #10") == 5


def test_parse_closes_no_match_returns_none() -> None:
    """A body without 'Closes #N' must return None."""
    assert _parse_closes("Fixes the thing") is None


def test_parse_closes_plain_hash_not_matched() -> None:
    """A bare '#42' without the Closes keyword must return None."""
    assert _parse_closes("See #42 for context") is None


# ── detect_stale_claims ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_detect_stale_claims_empty_wip_list_returns_empty(tmp_path: Path) -> None:
    """An empty wip-issues list must return []."""
    result = await detect_stale_claims([], tmp_path)
    assert result == []


@pytest.mark.anyio
async def test_detect_stale_claims_live_worktree_not_stale(tmp_path: Path) -> None:
    """An issue whose worktree directory exists must NOT appear in the result."""
    (tmp_path / "issue-5").mkdir()
    wip = [{"number": 5, "title": "Active work"}]
    result = await detect_stale_claims(wip, tmp_path)
    assert result == []


@pytest.mark.anyio
async def test_detect_stale_claims_missing_worktree_is_stale(tmp_path: Path) -> None:
    """An issue with no worktree directory must be classified as a stale claim."""
    wip = [{"number": 7, "title": "Abandoned"}]
    result = await detect_stale_claims(wip, tmp_path)
    assert len(result) == 1
    assert isinstance(result[0], StaleClaim)
    assert result[0].issue_number == 7
    assert result[0].issue_title == "Abandoned"


@pytest.mark.anyio
async def test_detect_stale_claims_sorted_by_issue_number(tmp_path: Path) -> None:
    """Results must be sorted ascending by issue number."""
    wip = [{"number": 20, "title": "B"}, {"number": 10, "title": "A"}]
    result = await detect_stale_claims(wip, tmp_path)
    assert [c.issue_number for c in result] == [10, 20]


@pytest.mark.anyio
async def test_detect_stale_claims_non_int_number_is_skipped(tmp_path: Path) -> None:
    """An issue with a non-integer number must be skipped without raising."""
    wip: list[dict[str, object]] = [{"number": "not-an-int", "title": "Bad"}]
    result = await detect_stale_claims(wip, tmp_path)
    assert result == []


@pytest.mark.anyio
async def test_detect_stale_claims_mixed_live_and_stale(tmp_path: Path) -> None:
    """Only the issues without a live worktree must appear in the result."""
    (tmp_path / "issue-1").mkdir()  # live — should NOT be stale
    wip = [
        {"number": 1, "title": "Live"},
        {"number": 2, "title": "Stale"},
    ]
    result = await detect_stale_claims(wip, tmp_path)
    assert len(result) == 1
    assert result[0].issue_number == 2


# ── detect_out_of_order_prs ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_detect_out_of_order_prs_no_active_label_returns_empty() -> None:
    """When there is no active label, the function must return [] immediately."""
    with patch(
        "agentception.intelligence.guards.get_active_label",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await detect_out_of_order_prs()
    assert result == []


@pytest.mark.anyio
async def test_detect_out_of_order_prs_no_violations_returns_empty() -> None:
    """When all PRs link issues in the active phase, no violations are returned."""
    pr = {"number": 1, "title": "PR", "body": "Closes #42"}
    issue = {"labels": ["agentception/5-plan"]}

    with (
        patch(
            "agentception.intelligence.guards.get_active_label",
            new_callable=AsyncMock,
            return_value="agentception/5-plan",
        ),
        patch(
            "agentception.intelligence.guards.get_open_prs_with_body",
            new_callable=AsyncMock,
            return_value=[pr],
        ),
        patch(
            "agentception.intelligence.guards.get_issue",
            new_callable=AsyncMock,
            return_value=issue,
        ),
    ):
        result = await detect_out_of_order_prs()
    assert result == []


@pytest.mark.anyio
async def test_detect_out_of_order_prs_violation_detected() -> None:
    """A PR whose linked issue is in a non-active phase must appear as a violation."""
    pr = {"number": 7, "title": "Old PR", "body": "Closes #99"}
    issue = {"labels": ["agentception/3-implement"]}  # mismatches active label

    with (
        patch(
            "agentception.intelligence.guards.get_active_label",
            new_callable=AsyncMock,
            return_value="agentception/5-plan",
        ),
        patch(
            "agentception.intelligence.guards.get_open_prs_with_body",
            new_callable=AsyncMock,
            return_value=[pr],
        ),
        patch(
            "agentception.intelligence.guards.get_issue",
            new_callable=AsyncMock,
            return_value=issue,
        ),
    ):
        result = await detect_out_of_order_prs()

    assert len(result) == 1
    v = result[0]
    assert isinstance(v, PRViolation)
    assert v.pr_number == 7
    assert v.linked_issue == 99
    assert v.expected_label == "agentception/5-plan"
    assert v.actual_label == "agentception/3-implement"


@pytest.mark.anyio
async def test_detect_out_of_order_prs_skips_pr_without_closes() -> None:
    """A PR body without 'Closes #N' must be silently skipped."""
    pr = {"number": 3, "title": "Unlinked PR", "body": "No closes reference"}

    with (
        patch(
            "agentception.intelligence.guards.get_active_label",
            new_callable=AsyncMock,
            return_value="agentception/5-plan",
        ),
        patch(
            "agentception.intelligence.guards.get_open_prs_with_body",
            new_callable=AsyncMock,
            return_value=[pr],
        ),
    ):
        result = await detect_out_of_order_prs()
    assert result == []


# ── build_dag ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_build_dag_empty_issues_returns_empty_dag() -> None:
    """With no open issues, build_dag must return nodes=[] and edges=[]."""
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[],
    ):
        dag = await build_dag()
    assert dag.nodes == []
    assert dag.edges == []


@pytest.mark.anyio
async def test_build_dag_single_issue_no_deps() -> None:
    """A single issue with no dependencies must produce one node and no edges."""
    issue = {
        "number": 1,
        "title": "First issue",
        "state": "open",
        "body": "",
        "labels": [],
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue],
    ):
        dag = await build_dag()

    assert len(dag.nodes) == 1
    assert dag.nodes[0].number == 1
    assert dag.nodes[0].has_wip is False
    assert dag.edges == []


@pytest.mark.anyio
async def test_build_dag_with_dependency_produces_edge() -> None:
    """An issue with 'Depends on #2' must produce an edge (1, 2)."""
    issue = {
        "number": 1,
        "title": "Dependent",
        "state": "open",
        "body": "Depends on #2",
        "labels": [],
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue],
    ):
        dag = await build_dag()

    assert (1, 2) in dag.edges
    assert dag.nodes[0].deps == [2]


@pytest.mark.anyio
async def test_build_dag_wip_label_sets_has_wip() -> None:
    """An issue with the 'agent/wip' label must produce a node with has_wip=True."""
    issue = {
        "number": 5,
        "title": "In progress",
        "state": "open",
        "body": "",
        "labels": [{"name": "agent/wip"}],
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue],
    ):
        dag = await build_dag()

    assert dag.nodes[0].has_wip is True


@pytest.mark.anyio
async def test_build_dag_string_labels_handled() -> None:
    """Labels supplied as plain strings (not dicts) must be accepted."""
    issue = {
        "number": 3,
        "title": "Simple",
        "state": "open",
        "body": "",
        "labels": ["agent/wip", "ac/5-plan"],
    }
    with patch(
        "agentception.intelligence.dag.get_open_issues",
        new_callable=AsyncMock,
        return_value=[issue],
    ):
        dag = await build_dag()

    node = dag.nodes[0]
    assert "agent/wip" in node.labels
    assert node.has_wip is True
