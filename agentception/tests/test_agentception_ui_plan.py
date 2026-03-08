from __future__ import annotations

"""Tests for the Plan UI routes.

Covers:
- GET /plan page renders correctly
- GET /plan/recent-runs HTMX partial
- GET /api/plan/{run_id}/plan-text endpoint
- _parse_task_fields helper
- _count_plan_items helper
- _normalize_plan_dict helper (all shape variations)
- Done step shows batch_id, worktree, Track Agents →, View Issues → (issue #42)
- Review section has inline error display for 422 from POST /api/plan/launch

Run targeted:
    pytest agentception/tests/test_agentception_ui_plan.py -v
"""

import textwrap
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.config import AgentCeptionSettings
from agentception.routes.ui.plan_ui import (
    _YamlNode,
    _count_plan_items,
    _normalize_plan_dict,
    _parse_task_fields,
)


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


def test_parse_task_fields_extracts_toml_fields() -> None:
    """_parse_task_fields must parse TOML .agent-task content correctly."""
    content = textwrap.dedent("""\
        [task]
        version = "0.1.1"
        workflow = "bugs-to-issues"

        [pipeline]
        batch_id = "plan-20260303-164033"

        [plan_draft]
        label_prefix = "q2-rewrite"
        dump = "- Some item"
    """)
    fields = _parse_task_fields(content)
    assert fields["WORKFLOW"] == "bugs-to-issues"
    assert fields["BATCH_ID"] == "plan-20260303-164033"
    assert fields["LABEL_PREFIX"] == "q2-rewrite"


def test_parse_task_fields_returns_empty_on_invalid_toml() -> None:
    """_parse_task_fields returns an empty dict when the content is not valid TOML."""
    fields = _parse_task_fields("not valid toml !!!")
    assert fields == {}
    assert "FAKE_KEY" not in fields


def test_parse_task_fields_empty_content() -> None:
    """_parse_task_fields must return an empty dict for empty or invalid content."""
    assert _parse_task_fields("") == {}
    assert _parse_task_fields("A=1\nB=2\n") == {}


def test_count_plan_items_counts_non_empty_lines() -> None:
    """_count_plan_items must count only lines that have non-whitespace content."""
    text = "- Fix login\n- Add dark mode\n\n- Rate limiter\n"
    assert _count_plan_items(text) == 3


def test_count_plan_items_empty_returns_zero() -> None:
    """_count_plan_items must return 0 for blank/empty input."""
    assert _count_plan_items("") == 0
    assert _count_plan_items("   \n\n  ") == 0


# ---------------------------------------------------------------------------
# Unit tests — _normalize_plan_dict
# ---------------------------------------------------------------------------


def test_normalize_plan_dict_passthrough_when_initiative_key_present() -> None:
    """Canonical form (has 'initiative') is returned unchanged."""
    data: _YamlNode = {"initiative": "auth", "phases": []}
    assert _normalize_plan_dict(data) == data


def test_normalize_plan_dict_passthrough_when_phases_key_present() -> None:
    """Dict with 'phases' but no 'initiative' is returned unchanged (Pydantic validates)."""
    data: _YamlNode = {"phases": []}
    assert _normalize_plan_dict(data) == data


def test_normalize_plan_dict_passthrough_non_dict() -> None:
    """Non-dict inputs are returned unchanged."""
    assert _normalize_plan_dict("hello") == "hello"
    assert _normalize_plan_dict(None) is None
    assert _normalize_plan_dict(42) == 42
    assert _normalize_plan_dict([1, 2]) == [1, 2]


def test_normalize_plan_dict_passthrough_multiple_top_level_keys() -> None:
    """Multiple unknown top-level keys → returned unchanged (let Pydantic report the error)."""
    data: _YamlNode = {"auth": {}, "billing": {}}
    assert _normalize_plan_dict(data) == data


def test_normalize_plan_dict_passthrough_no_phase_subkeys() -> None:
    """Single unknown top-level key whose value has no 'phase-*' sub-keys → unchanged."""
    data: _YamlNode = {"auth-rewrite": {"something": "else"}}
    assert _normalize_plan_dict(data) == data


def test_normalize_plan_dict_passthrough_body_not_dict() -> None:
    """Single unknown top-level key whose value is not a dict → unchanged."""
    data: _YamlNode = {"auth-rewrite": "just a string"}
    assert _normalize_plan_dict(data) == data


def test_normalize_plan_dict_converts_initiative_as_key_format() -> None:
    """Converts {initiative: {phase-0: {...}}} to canonical {initiative, phases} form."""
    data: _YamlNode = {
        "auth-rewrite": {
            "phase-0": {
                "description": "Foundation",
                "depends_on": [],
                "issues": [{"title": "Add user model"}],
            },
        },
    }
    result = _normalize_plan_dict(data)
    assert isinstance(result, dict)
    assert result["initiative"] == "auth-rewrite"
    phases = result["phases"]
    assert isinstance(phases, list)
    assert len(phases) == 1
    phase = phases[0]
    assert isinstance(phase, dict)
    assert phase["label"] == "phase-0"
    assert phase["description"] == "Foundation"


def test_normalize_plan_dict_multiple_phases_sorted_by_label() -> None:
    """Multiple phase-* sub-keys are emitted sorted by label name."""
    data: _YamlNode = {
        "my-init": {
            "phase-1": {"description": "Phase one", "depends_on": [], "issues": []},
            "phase-0": {"description": "Phase zero", "depends_on": [], "issues": []},
        },
    }
    result = _normalize_plan_dict(data)
    assert isinstance(result, dict)
    phases = result["phases"]
    assert isinstance(phases, list)
    assert len(phases) == 2
    assert isinstance(phases[0], dict) and phases[0]["label"] == "phase-0"
    assert isinstance(phases[1], dict) and phases[1]["label"] == "phase-1"


def test_normalize_plan_dict_preserves_all_phase_fields() -> None:
    """All fields from the phase body are preserved in the converted phase dict."""
    data: _YamlNode = {
        "my-init": {
            "phase-0": {
                "description": "Do some work",
                "depends_on": ["other-phase"],
                "issues": [{"id": "x", "title": "T"}],
                "extra_field": "should survive",
            },
        },
    }
    result = _normalize_plan_dict(data)
    assert isinstance(result, dict)
    phases = result["phases"]
    assert isinstance(phases, list)
    phase = phases[0]
    assert isinstance(phase, dict)
    assert phase["label"] == "phase-0"
    assert phase["description"] == "Do some work"
    assert phase["depends_on"] == ["other-phase"]
    assert phase["extra_field"] == "should survive"


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
        '[task]\nversion = "0.1.1"\nworkflow = "bugs-to-issues"\n\n'
        '[pipeline]\nbatch_id = "plan-20260303-164033"\n\n'
        '[plan_draft]\ndump = "- Fix login\\n- Add dark mode"\n',
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


def test_plan_page_done_state_has_batch_pill_and_track_agents(client: TestClient) -> None:
    """GET /plan must render the batch_id pill and done-state action buttons.

    The done screen was redesigned in PR #162 — CTAs are now 'Build Board'
    (links to /) and 'View on GitHub' (links to the initiative's GitHub issues
    page).  The batch pill elements are unchanged.
    """
    resp = client.get("/plan")
    assert resp.status_code == 200
    # Batch pill elements — present in the done state section.
    assert "plan-done-batch" in resp.text
    assert "plan-done-batch-id" in resp.text
    assert "copyBatchId" in resp.text
    # Done step CTAs — "Dispatch agents" is the primary action, "View on GitHub" is secondary.
    assert "Dispatch agents" in resp.text
    assert "View on GitHub" in resp.text


def test_plan_page_wires_step_1a_draft_and_sse(client: TestClient) -> None:
    """Plan page (issue #41) wires planForm to POST /api/plan/draft and SSE plan_draft_ready.

    Step 1.A flow: user clicks Generate plan → plan.js submit() calls
    POST /api/plan/draft, then _waitForDraftReady() subscribes to GET /events
    and matches state.plan_draft_events by draft_id for plan_draft_ready.
    This test asserts the page structure and script loading required for that flow.
    """
    resp = client.get("/plan")
    assert resp.status_code == 200
    # planForm() from plan.js is the Alpine component for the Plan page.
    # The template passes ghRepo as an argument: planForm({ ghRepo: "..." })
    assert 'planForm(' in resp.text
    # Write step: submit button triggers submit() which calls POST /api/plan/draft.
    assert "Generate plan" in resp.text
    assert "submit()" in resp.text
    # Generating step exists so the UI can show waiting state while listening for SSE.
    assert "generating" in resp.text
    # app.js bundle includes plan.js which uses EventSource('/events') and
    # plan_draft_events; the plan page must load the bundle.
    assert "/static/app.js" in resp.text
    # Done-state and review step exist so the flow can complete after plan_draft_ready.
    assert "plan-review" in resp.text
    assert "plan-done" in resp.text


def test_plan_page_review_section_has_inline_error_for_422(client: TestClient) -> None:
    """Plan review section must include plan-error so 422 from POST /api/plan/launch can be shown inline."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    # Inline error is shown in the review block (x-show="errorMsg") so user can edit and retry.
    assert "plan-error" in resp.text
    assert "errorMsg" in resp.text