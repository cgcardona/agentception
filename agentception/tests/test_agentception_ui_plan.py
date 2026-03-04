from __future__ import annotations

"""Tests for the Plan UI routes.

Covers:
- GET /plan page renders correctly
- GET /plan/recent-runs HTMX partial
- GET /api/plan/{run_id}/plan-text endpoint
- _parse_task_fields helper
- _count_plan_items helper

Run targeted:
    pytest agentception/tests/test_agentception_ui_plan.py -v
"""
from __future__ import annotations

import textwrap
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.config import AgentCeptionSettings
from agentception.routes.ui.plan_ui import _count_plan_items, _parse_task_fields


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


def test_parse_task_fields_extracts_key_value_pairs() -> None:
    """_parse_task_fields must parse structured key=value lines correctly."""
    content = textwrap.dedent("""\
        WORKFLOW=bugs-to-issues
        GH_REPO=cgcardona/maestro
        BATCH_ID=plan-20260303-164033
        LABEL_PREFIX=q2-rewrite

        PLAN_DUMP:
        - Some item
    """)
    fields = _parse_task_fields(content)
    assert fields["WORKFLOW"] == "bugs-to-issues"
    assert fields["BATCH_ID"] == "plan-20260303-164033"
    assert fields["LABEL_PREFIX"] == "q2-rewrite"


def test_parse_task_fields_stops_at_plan_dump_marker() -> None:
    """_parse_task_fields must not parse lines after PLAN_DUMP:."""
    content = "KEY=value\nPLAN_DUMP:\nFAKE_KEY=should_not_appear\n"
    fields = _parse_task_fields(content)
    assert "KEY" in fields
    assert "FAKE_KEY" not in fields


def test_parse_task_fields_stops_at_blank_line() -> None:
    """_parse_task_fields must stop at the first blank line (before PLAN_DUMP section)."""
    content = "A=1\nB=2\n\nC=3\n"
    fields = _parse_task_fields(content)
    assert fields["A"] == "1"
    assert fields["B"] == "2"
    assert "C" not in fields


def test_parse_task_fields_empty_content() -> None:
    """_parse_task_fields must return an empty dict for empty content."""
    assert _parse_task_fields("") == {}


def test_count_plan_items_counts_non_empty_lines() -> None:
    """_count_plan_items must count only lines that have non-whitespace content."""
    text = "- Fix login\n- Add dark mode\n\n- Rate limiter\n"
    assert _count_plan_items(text) == 3


def test_count_plan_items_empty_returns_zero() -> None:
    """_count_plan_items must return 0 for blank/empty input."""
    assert _count_plan_items("") == 0
    assert _count_plan_items("   \n\n  ") == 0


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


def test_plan_page_renders(client: TestClient) -> None:
    """GET /plan must return 200 with the page title."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert "Plan" in resp.text


def test_plan_recent_runs_partial_empty(client: TestClient, tmp_path: Path) -> None:
    """GET /plan/recent-runs returns 200 even when the worktrees dir is empty."""
    with patch("agentception.routes.ui.plan_ui._build_recent_plans", return_value=[]):
        resp = client.get("/plan/recent-runs")
    assert resp.status_code == 200
    assert "bd-recent-runs" in resp.text


def test_plan_recent_runs_shows_cards(client: TestClient) -> None:
    """GET /plan/recent-runs renders a card for each recent plan run."""
    fake_runs = [
        {
            "slug": "plan-20260303-164033",
            "label_prefix": "q2-rewrite",
            "preview": "- Fix login bug",
            "ts": "2026-03-03 16:40",
            "batch_id": "plan-20260303-164033",
            "item_count": "3",
        }
    ]
    with patch("agentception.routes.ui.plan_ui._build_recent_plans", return_value=fake_runs):
        resp = client.get("/plan/recent-runs")
    assert resp.status_code == 200
    assert "2026-03-03 16:40" in resp.text
    assert "q2-rewrite" in resp.text
    assert "Fix login bug" in resp.text
    assert "View DAG" in resp.text
    assert "Re-run" in resp.text


def test_plan_run_text_returns_plan(client: TestClient, tmp_path: Path) -> None:
    """GET /api/plan/{run_id}/plan-text returns the PLAN_DUMP section as JSON."""
    run_id = "plan-20260303-164033"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    task_file = run_dir / ".agent-task"
    task_file.write_text(
        "WORKFLOW=bugs-to-issues\nBATCH_ID=plan-20260303-164033\n\nPLAN_DUMP:\n- Fix login\n- Add dark mode\n",
        encoding="utf-8",
    )

    fake_settings = AgentCeptionSettings.model_construct(worktrees_dir=tmp_path)
    with patch("agentception.config.settings", fake_settings):
        resp = client.get(f"/api/plan/{run_id}/plan-text")

    assert resp.status_code == 200
    data = resp.json()
    assert "plan_text" in data
    assert "Fix login" in data["plan_text"]
    assert "Add dark mode" in data["plan_text"]


def test_plan_run_text_invalid_run_id(client: TestClient) -> None:
    """GET /api/plan/{run_id}/plan-text returns 400 for path traversal."""
    resp = client.get("/api/plan/../../etc-passwd/plan-text")
    assert resp.status_code in (400, 404)


def test_plan_run_text_wrong_prefix(client: TestClient) -> None:
    """GET /api/plan/{run_id}/plan-text returns 400 when run_id doesn't start with plan-."""
    resp = client.get("/api/plan/issue-826/plan-text")
    assert resp.status_code == 400


def test_plan_run_text_not_found(client: TestClient, tmp_path: Path) -> None:
    """GET /api/plan/{run_id}/plan-text returns 404 when the worktree doesn't exist."""
    fake_settings = AgentCeptionSettings.model_construct(worktrees_dir=tmp_path)
    with patch("agentception.config.settings", fake_settings):
        resp = client.get("/api/plan/plan-99991231-999999/plan-text")
    assert resp.status_code == 404
