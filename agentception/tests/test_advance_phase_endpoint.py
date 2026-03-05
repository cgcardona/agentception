"""Tests for POST /api/build/advance-phase endpoint (issue #87).

Coverage:
- advance_phase: success path returns advanced=True and unlocked_count
- advance_phase: success path sets HX-Trigger: refreshBoard response header
- advance_phase: blocked path returns advanced=False with error and open_issues
- advance_phase: blocked path does NOT set HX-Trigger header
- advance_phase: missing required fields returns 422 validation error
- build board template: renders Advance button when prev phase complete and current locked
- build board template: does NOT render button when current phase is not locked
- build board template: does NOT render button when prev phase is not complete
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_result(unlocked_count: int = 2) -> dict[str, object]:
    return {"advanced": True, "unlocked_count": unlocked_count}


def _blocked_result(open_issues: list[int] | None = None) -> dict[str, object]:
    issues: list[int] = open_issues or [11, 12]
    return {
        "advanced": False,
        "error": f"Cannot advance: {len(issues)} open issue(s) remain in phase 'phase-1'.",
        "open_issues": issues,
    }


def _advance_payload(
    initiative: str = "my-initiative",
    from_phase: str = "phase-1",
    to_phase: str = "phase-2",
) -> dict[str, str]:
    return {"initiative": initiative, "from_phase": from_phase, "to_phase": to_phase}


def _mock_group(
    *,
    label: str = "phase-1",
    locked: bool = False,
    complete: bool = False,
    issues: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "label": label,
        "issues": issues or [],
        "locked": locked,
        "complete": complete,
        "depends_on": [],
    }


# ---------------------------------------------------------------------------
# Integration tests — POST /api/build/advance-phase
# ---------------------------------------------------------------------------


def test_advance_phase_success_returns_advanced_true(client: TestClient) -> None:
    """POST /api/build/advance-phase returns advanced=True and unlocked_count on success."""
    with patch(
        "agentception.routes.api.build._plan_advance_phase",
        new=AsyncMock(return_value=_ok_result(unlocked_count=3)),
    ):
        resp = client.post("/api/build/advance-phase", json=_advance_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["advanced"] is True
    assert body["unlocked_count"] == 3


def test_advance_phase_success_sets_hx_trigger_header(client: TestClient) -> None:
    """Successful advance sets HX-Trigger: refreshBoard header for HTMX board refresh."""
    with patch(
        "agentception.routes.api.build._plan_advance_phase",
        new=AsyncMock(return_value=_ok_result()),
    ):
        resp = client.post("/api/build/advance-phase", json=_advance_payload())

    assert resp.status_code == 200
    assert resp.headers.get("hx-trigger") == "refreshBoard"


def test_advance_phase_blocked_returns_advanced_false(client: TestClient) -> None:
    """POST /api/build/advance-phase returns advanced=False and open_issues when blocked."""
    with patch(
        "agentception.routes.api.build._plan_advance_phase",
        new=AsyncMock(return_value=_blocked_result([11, 12])),
    ):
        resp = client.post("/api/build/advance-phase", json=_advance_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["advanced"] is False
    assert body["open_issues"] == [11, 12]
    assert "error" in body
    assert "Cannot advance" in body["error"]


def test_advance_phase_blocked_does_not_set_hx_trigger(client: TestClient) -> None:
    """Blocked advance must NOT set HX-Trigger — the board should not refresh spuriously."""
    with patch(
        "agentception.routes.api.build._plan_advance_phase",
        new=AsyncMock(return_value=_blocked_result()),
    ):
        resp = client.post("/api/build/advance-phase", json=_advance_payload())

    assert resp.status_code == 200
    assert "hx-trigger" not in resp.headers


def test_advance_phase_missing_initiative_returns_422(client: TestClient) -> None:
    """POST /api/build/advance-phase with missing required fields returns 422."""
    resp = client.post(
        "/api/build/advance-phase",
        json={"from_phase": "phase-1", "to_phase": "phase-2"},
    )
    assert resp.status_code == 422


def test_advance_phase_missing_to_phase_returns_422(client: TestClient) -> None:
    """POST /api/build/advance-phase with missing to_phase returns 422."""
    resp = client.post(
        "/api/build/advance-phase",
        json={"initiative": "my-initiative", "from_phase": "phase-1"},
    )
    assert resp.status_code == 422


def test_advance_phase_delegates_correct_args(client: TestClient) -> None:
    """advance_phase passes initiative, from_phase, to_phase to plan_advance_phase correctly."""
    with patch(
        "agentception.routes.api.build._plan_advance_phase",
        new=AsyncMock(return_value=_ok_result()),
    ) as mock_fn:
        client.post(
            "/api/build/advance-phase",
            json=_advance_payload("x-initiative", "phase-2", "phase-3"),
        )

    mock_fn.assert_called_once_with("x-initiative", "phase-2", "phase-3")


# ---------------------------------------------------------------------------
# Integration tests — build board template renders Advance button
# ---------------------------------------------------------------------------


def test_build_board_renders_advance_button_when_prev_complete_and_locked(
    client: TestClient,
) -> None:
    """GET /build/board renders Advance button when prev phase is complete and next is locked."""
    groups = [
        _mock_group(label="phase-1", locked=False, complete=True),
        _mock_group(label="phase-2", locked=True, complete=False),
    ]
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=groups),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new=AsyncMock(return_value={}),
        ),
    ):
        resp = client.get("/build/board?initiative=my-initiative")

    assert resp.status_code == 200
    assert "Advance to phase-2" in resp.text
    assert "hx-post" in resp.text
    assert "/api/build/advance-phase" in resp.text


def test_build_board_no_advance_button_when_not_locked(client: TestClient) -> None:
    """GET /build/board does NOT render Advance button when the next phase is not locked."""
    groups = [
        _mock_group(label="phase-1", locked=False, complete=True),
        _mock_group(label="phase-2", locked=False, complete=False),
    ]
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=groups),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new=AsyncMock(return_value={}),
        ),
    ):
        resp = client.get("/build/board?initiative=my-initiative")

    assert resp.status_code == 200
    assert "Advance to phase-2" not in resp.text


def test_build_board_no_advance_button_when_prev_not_complete(
    client: TestClient,
) -> None:
    """GET /build/board does NOT render Advance button when the previous phase is not complete."""
    groups = [
        _mock_group(label="phase-1", locked=False, complete=False),
        _mock_group(label="phase-2", locked=True, complete=False),
    ]
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=groups),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new=AsyncMock(return_value={}),
        ),
    ):
        resp = client.get("/build/board?initiative=my-initiative")

    assert resp.status_code == 200
    assert "Advance to phase-2" not in resp.text


def test_build_board_no_advance_button_without_initiative(
    client: TestClient,
) -> None:
    """GET /build/board without initiative does NOT render the Advance button."""
    groups = [
        _mock_group(label="phase-1", locked=False, complete=True),
        _mock_group(label="phase-2", locked=True, complete=False),
    ]
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=groups),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new=AsyncMock(return_value={}),
        ),
    ):
        resp = client.get("/build/board")

    assert resp.status_code == 200
    assert "advance-phase" not in resp.text
