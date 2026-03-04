from __future__ import annotations

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

import datetime
import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

_UTC = datetime.timezone.utc


@pytest.fixture()
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
) -> dict[str, object]:
    """Return a minimal open-issue dict."""
    label_objs: list[object] = [{"name": n} for n in (label_names or [])]
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
    assert "Brain Dump" in resp.text
    assert "Org Chart" in resp.text
    assert "Launch Wave" in resp.text


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
