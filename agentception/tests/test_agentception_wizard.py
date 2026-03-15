"""Tests for the wizard stepper endpoint (issue #834).

Covers:
- GET /api/wizard/state returns JSON with the correct shape.
- Step 1 complete when open issues carry ac-workflow/* labels.
- Step 1 incomplete when no issues carry ac-workflow/* labels.
- Step 2 complete when pipeline-config.json has a non-null active_org.
- Step 2 incomplete when active_org is absent / null.
- Step 3 active when an unfinished wave started within the last 24 h exists.
- Step 3 inactive when no such wave exists.
- GET /api/wizard/state returns HTML partial when HX-Request header is sent.

All GitHub calls, filesystem reads, and DB queries are mocked — no live
network, no filesystem side-effects, no DB required.

Run targeted:
    pytest agentception/tests/test_agentception_wizard.py -v
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.routes.api.wizard import _has_workflow_label, _read_active_org
from agentception.types import JsonValue

_UTC = datetime.timezone.utc


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full app lifespan."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(
    number: int,
    label_names: list[str] | None = None,
) -> dict[str, JsonValue]:
    """Return a minimal open-issue dict."""
    label_objs: list[JsonValue] = [{"name": n} for n in (label_names or [])]
    return {"number": number, "title": "Test issue", "labels": label_objs, "body": ""}


def _make_wave(
    wave_id: str = "wave-001",
    started_at: datetime.datetime | None = None,
    completed_at: datetime.datetime | None = None,
) -> MagicMock:
    """Return a MagicMock representing an ACWave row."""
    wave = MagicMock()
    wave.id = wave_id
    wave.started_at = started_at or datetime.datetime.now(_UTC)
    wave.completed_at = completed_at
    return wave


# ---------------------------------------------------------------------------
# Step 1: Brain Dump
# ---------------------------------------------------------------------------


def test_wizard_state_step1_complete_when_workflow_issues_exist(
    client: TestClient,
) -> None:
    """Step 1 is complete when open issues carry an ac-workflow/* label."""
    issues = [
        _make_issue(1, ["ac-workflow/1-scaffold"]),
        _make_issue(2, ["ac-workflow/2-core"]),
    ]
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=issues),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step1"]["complete"] is True
    assert "2 issues" in data["step1"]["summary"]


def test_wizard_state_step1_incomplete_when_no_workflow_issues(
    client: TestClient,
) -> None:
    """Step 1 is incomplete when issues exist but carry no ac-workflow/* label."""
    issues = [_make_issue(1, ["bug", "enhancement"])]
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=issues),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step1"]["complete"] is False


def test_wizard_state_step1_singular_summary(client: TestClient) -> None:
    """Summary uses singular 'issue' when exactly one workflow issue exists."""
    issues = [_make_issue(1, ["ac-workflow/0-triage"])]
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=issues),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert "1 issue" in data["step1"]["summary"]
    assert "issues" not in data["step1"]["summary"]


# ---------------------------------------------------------------------------
# Step 2: Org Chart
# ---------------------------------------------------------------------------


def test_wizard_state_step2_complete_when_active_org_set(
    client: TestClient,
) -> None:
    """Step 2 is complete when active_org is a non-empty string."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value="small-team",
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step2"]["complete"] is True
    assert "small-team" in data["step2"]["summary"]


def test_wizard_state_step2_incomplete_when_no_active_org(
    client: TestClient,
) -> None:
    """Step 2 is incomplete when active_org is absent from config."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step2"]["complete"] is False


# ---------------------------------------------------------------------------
# Step 3: Launch Wave
# ---------------------------------------------------------------------------


def test_wizard_state_step3_active_when_running_wave_exists(
    client: TestClient,
) -> None:
    """Step 3 is active when a wave started in the last 24 h with no completed_at."""
    wave = _make_wave(wave_id="wave-xyz", completed_at=None)
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=wave),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step3"]["active"] is True
    assert "wave-xyz" in data["step3"]["summary"]


def test_wizard_state_step3_inactive_when_no_wave(
    client: TestClient,
) -> None:
    """Step 3 is inactive when no wave started in the last 24 h."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step3"]["active"] is False


# ---------------------------------------------------------------------------
# JSON response shape
# ---------------------------------------------------------------------------


def test_wizard_state_response_shape(client: TestClient) -> None:
    """GET /api/wizard/state response has the mandated JSON shape."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert set(data.keys()) == {"step1", "step2", "step3"}
    assert set(data["step1"].keys()) == {"complete", "summary"}
    assert set(data["step2"].keys()) == {"complete", "summary"}
    assert set(data["step3"].keys()) == {"active", "summary"}
    assert isinstance(data["step1"]["complete"], bool)
    assert isinstance(data["step1"]["summary"], str)
    assert isinstance(data["step3"]["active"], bool)


# ---------------------------------------------------------------------------
# HTMX partial
# ---------------------------------------------------------------------------


def test_wizard_state_returns_html_for_htmx(client: TestClient) -> None:
    """GET /api/wizard/state returns HTML stepper partial when HX-Request header is set."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get(
            "/api/wizard/state",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "wizard-stepper" in resp.text
    assert "Plan" in resp.text
    assert "Build" in resp.text
    assert "Ship" in resp.text


# ---------------------------------------------------------------------------
# GitHub error resilience
# ---------------------------------------------------------------------------


def test_wizard_state_step1_graceful_on_github_error(
    client: TestClient,
) -> None:
    """Step 1 defaults to incomplete when GitHub raises an exception."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(side_effect=RuntimeError("gh CLI not found")),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    # Should not crash — graceful degradation
    assert resp.status_code == 200
    data = resp.json()
    assert data["step1"]["complete"] is False


# ---------------------------------------------------------------------------
# Internal helper: mock DB session
# ---------------------------------------------------------------------------


def _mock_db_session(wave: MagicMock | None) -> MagicMock:
    """Build an async context-manager mock session that returns *wave* on scalar_one_or_none."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = wave

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _mock_db_error() -> MagicMock:
    """Build an async context-manager that raises RuntimeError on execute."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=RuntimeError("db offline"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ---------------------------------------------------------------------------
# Unit tests: _has_workflow_label
# ---------------------------------------------------------------------------


def test_has_workflow_label_string_match() -> None:
    """String label that starts with ac-workflow/ is matched."""
    issue: dict[str, JsonValue] = {"labels": ["ac-workflow/1-scaffold", "bug"]}
    assert _has_workflow_label(issue) is True


def test_has_workflow_label_dict_match() -> None:
    """Dict label with matching name is matched (GitHub API shape)."""
    issue: dict[str, JsonValue] = {"labels": [{"name": "ac-workflow/2-core"}, {"name": "bug"}]}
    assert _has_workflow_label(issue) is True


def test_has_workflow_label_no_match_string() -> None:
    """String labels that do not start with ac-workflow/ are not matched."""
    issue: dict[str, JsonValue] = {"labels": ["bug", "enhancement"]}
    assert _has_workflow_label(issue) is False


def test_has_workflow_label_no_match_dict() -> None:
    """Dict labels whose name does not start with ac-workflow/ are not matched."""
    issue: dict[str, JsonValue] = {"labels": [{"name": "bug"}, {"name": "feature"}]}
    assert _has_workflow_label(issue) is False


def test_has_workflow_label_empty_list() -> None:
    """Empty labels list returns False."""
    issue: dict[str, JsonValue] = {"labels": []}
    assert _has_workflow_label(issue) is False


def test_has_workflow_label_missing_key() -> None:
    """Issue without a 'labels' key returns False."""
    assert _has_workflow_label({}) is False


def test_has_workflow_label_non_list_labels() -> None:
    """labels value that is not a list (e.g. None or a string) returns False."""
    assert _has_workflow_label({"labels": None}) is False
    assert _has_workflow_label({"labels": "ac-workflow/x"}) is False


def test_has_workflow_label_dict_without_name_key() -> None:
    """Dict label missing the 'name' key is skipped, not matched."""
    issue: dict[str, JsonValue] = {"labels": [{"title": "ac-workflow/x"}]}
    assert _has_workflow_label(issue) is False


def test_has_workflow_label_mixed_list() -> None:
    """A mix of non-matching dicts and a matching string is handled correctly."""
    issue: dict[str, JsonValue] = {"labels": [{"name": "bug"}, "ac-workflow/0-triage"]}
    assert _has_workflow_label(issue) is True


# ---------------------------------------------------------------------------
# Unit tests: _read_active_org
# ---------------------------------------------------------------------------


def test_read_active_org_returns_none_when_file_missing(tmp_path: Path) -> None:
    """Returns None when pipeline-config.json does not exist."""
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", tmp_path / "missing.json"):
        result = _read_active_org()
    assert result is None


def test_read_active_org_returns_org_name(tmp_path: Path) -> None:
    """Returns active_org string when the file is valid."""
    cfg = tmp_path / "pipeline-config.json"
    cfg.write_text(json.dumps({"active_org": "small-team"}), encoding="utf-8")
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", cfg):
        result = _read_active_org()
    assert result == "small-team"


def test_read_active_org_returns_none_when_key_absent(tmp_path: Path) -> None:
    """Returns None when the file exists but has no active_org key."""
    cfg = tmp_path / "pipeline-config.json"
    cfg.write_text(json.dumps({"other_key": "value"}), encoding="utf-8")
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", cfg):
        result = _read_active_org()
    assert result is None


def test_read_active_org_returns_none_when_active_org_null(tmp_path: Path) -> None:
    """Returns None when active_org is JSON null."""
    cfg = tmp_path / "pipeline-config.json"
    cfg.write_text(json.dumps({"active_org": None}), encoding="utf-8")
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", cfg):
        result = _read_active_org()
    assert result is None


def test_read_active_org_returns_none_when_active_org_empty_string(tmp_path: Path) -> None:
    """Returns None when active_org is an empty string (treated as unset)."""
    cfg = tmp_path / "pipeline-config.json"
    cfg.write_text(json.dumps({"active_org": ""}), encoding="utf-8")
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", cfg):
        result = _read_active_org()
    assert result is None


def test_read_active_org_returns_none_on_invalid_json(tmp_path: Path) -> None:
    """Returns None (no crash) when the file contains invalid JSON."""
    cfg = tmp_path / "pipeline-config.json"
    cfg.write_text("not-valid-json{{}", encoding="utf-8")
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", cfg):
        result = _read_active_org()
    assert result is None


def test_read_active_org_returns_none_when_root_is_not_dict(tmp_path: Path) -> None:
    """Returns None when the JSON root is not a dict (e.g. a list)."""
    cfg = tmp_path / "pipeline-config.json"
    cfg.write_text(json.dumps([{"active_org": "x"}]), encoding="utf-8")
    with patch("agentception.routes.api.wizard._PIPELINE_CONFIG_PATH", cfg):
        result = _read_active_org()
    assert result is None


# ---------------------------------------------------------------------------
# Error resilience: step 2 and step 3
# ---------------------------------------------------------------------------


def test_wizard_state_step2_graceful_on_read_error(client: TestClient) -> None:
    """Step 2 defaults to incomplete when _read_active_org raises unexpectedly."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            side_effect=OSError("permission denied"),
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step2"]["complete"] is False


def test_wizard_state_step3_graceful_on_db_error(client: TestClient) -> None:
    """Step 3 defaults to inactive when the DB query raises."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_error(),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step3"]["active"] is False


# ---------------------------------------------------------------------------
# Edge cases: step 1 with no issues at all
# ---------------------------------------------------------------------------


def test_wizard_state_step1_incomplete_when_issue_list_empty(client: TestClient) -> None:
    """Step 1 is incomplete when GitHub returns an empty issue list."""
    with (
        patch(
            "agentception.routes.api.wizard.get_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.routes.api.wizard._read_active_org",
            return_value=None,
        ),
        patch(
            "agentception.routes.api.wizard.get_session",
            return_value=_mock_db_session(wave=None),
        ),
    ):
        resp = client.get("/api/wizard/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["step1"]["complete"] is False
    assert data["step1"]["summary"] == "No workflow issues yet"
