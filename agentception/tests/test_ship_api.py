"""Tests for agentception/routes/api/ship_api.py.

Covers the single endpoint:

    POST /api/ship/{repo}/{initiative}/advance

Scenarios:
- Success path: advanced=True → 200 AdvancePhaseOk + HX-Trigger header.
- Blocked path: advanced=False → 200 AdvancePhaseBlocked.
- HX-Trigger absent on blocked response.
- open_issues list populated from plan_advance_phase result.
- unlocked_count defaults to 0 when result key is missing or non-int.
- error string defaults to fallback when result key is missing or non-string.
- open_issues defaults to [] when result key is missing or non-list.
- plan_advance_phase receives the correct arguments.

All calls to plan_advance_phase are mocked so no GitHub or DB I/O occurs.

Run targeted:
    pytest agentception/tests/test_ship_api.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


_ADVANCE_URL = "/api/ship/my-repo/auth-rewrite/advance"
_ADVANCE_BODY = {"from_phase": "phase-0", "to_phase": "phase-1"}

# ── Success path ──────────────────────────────────────────────────────────────


def test_advance_returns_200_on_success(client: TestClient) -> None:
    """POST /advance returns HTTP 200 when plan_advance_phase reports advanced=True."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": True, "unlocked_count": 3}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.status_code == 200


def test_advance_returns_ok_body_on_success(client: TestClient) -> None:
    """POST /advance response contains advanced=True and unlocked_count on success."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": True, "unlocked_count": 3}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    body = response.json()
    assert body["advanced"] is True
    assert body["unlocked_count"] == 3


def test_advance_sets_hx_trigger_on_success(client: TestClient) -> None:
    """POST /advance sets the HX-Trigger: refreshBoard header when the gate passes."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": True, "unlocked_count": 1}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.headers.get("hx-trigger") == "refreshBoard"


def test_advance_unlocked_count_defaults_to_zero_when_missing(
    client: TestClient,
) -> None:
    """POST /advance sets unlocked_count=0 when the key is absent from the result."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": True}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.json()["unlocked_count"] == 0


def test_advance_unlocked_count_defaults_to_zero_when_non_int(
    client: TestClient,
) -> None:
    """POST /advance sets unlocked_count=0 when the result value is not an int."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": True, "unlocked_count": "three"}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.json()["unlocked_count"] == 0


# ── Blocked path ──────────────────────────────────────────────────────────────


def test_advance_returns_200_when_blocked(client: TestClient) -> None:
    """POST /advance returns HTTP 200 even when the gate is blocked."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(
            return_value={
                "advanced": False,
                "error": "2 issues still open in phase-0",
                "open_issues": [101, 102],
            }
        ),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.status_code == 200


def test_advance_returns_blocked_body_with_open_issues(client: TestClient) -> None:
    """POST /advance blocked response includes advanced=False, error, and open_issues."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(
            return_value={
                "advanced": False,
                "error": "2 issues still open in phase-0",
                "open_issues": [101, 102],
            }
        ),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    body = response.json()
    assert body["advanced"] is False
    assert "open" in body["error"].lower()
    assert body["open_issues"] == [101, 102]


def test_advance_no_hx_trigger_when_blocked(client: TestClient) -> None:
    """POST /advance must NOT set HX-Trigger when the gate is blocked."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(
            return_value={"advanced": False, "error": "blocked", "open_issues": []}
        ),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert "hx-trigger" not in response.headers


def test_advance_error_defaults_to_fallback_when_missing(client: TestClient) -> None:
    """POST /advance uses the fallback error string when 'error' key is absent."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": False}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.json()["error"] == "Phase advance blocked."


def test_advance_open_issues_defaults_to_empty_list_when_missing(
    client: TestClient,
) -> None:
    """POST /advance returns open_issues=[] when the key is absent from the result."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(return_value={"advanced": False, "error": "blocked"}),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.json()["open_issues"] == []


def test_advance_open_issues_filters_non_ints(client: TestClient) -> None:
    """POST /advance filters non-int values from the open_issues list."""
    with patch(
        "agentception.routes.api.ship_api._plan_advance_phase",
        new=AsyncMock(
            return_value={
                "advanced": False,
                "error": "blocked",
                "open_issues": [101, "bad", None, 102],
            }
        ),
    ):
        response = client.post(_ADVANCE_URL, json=_ADVANCE_BODY)
    assert response.json()["open_issues"] == [101, 102]


# ── Argument forwarding ───────────────────────────────────────────────────────


def test_advance_passes_correct_args_to_plan_advance_phase(
    client: TestClient,
) -> None:
    """POST /advance forwards initiative, from_phase, and to_phase to plan_advance_phase."""
    mock = AsyncMock(return_value={"advanced": True, "unlocked_count": 0})
    with patch("agentception.routes.api.ship_api._plan_advance_phase", new=mock):
        client.post(
            "/api/ship/my-repo/auth-rewrite/advance",
            json={"from_phase": "phase-0", "to_phase": "phase-1"},
        )
    mock.assert_awaited_once_with("auth-rewrite", "phase-0", "phase-1")


# ── Validation ────────────────────────────────────────────────────────────────


def test_advance_returns_422_when_from_phase_missing(client: TestClient) -> None:
    """POST /advance returns 422 when from_phase is absent from the request body."""
    response = client.post(_ADVANCE_URL, json={"to_phase": "phase-1"})
    assert response.status_code == 422


def test_advance_returns_422_when_to_phase_missing(client: TestClient) -> None:
    """POST /advance returns 422 when to_phase is absent from the request body."""
    response = client.post(_ADVANCE_URL, json={"from_phase": "phase-0"})
    assert response.status_code == 422
