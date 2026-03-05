"""Tests for the Ship page backend (issue #83).

Covers:
- GET /ship returns 200 with initiative param (or 302 redirect when auto-selecting)
- GET /ship?initiative=... returns 200 and renders Ship heading
- GET /ship?batch=... returns 200
- GET /ship/board returns 200 HTML partial
- GET /ship/board?initiative=... filters correctly
- GET /ship/board?batch=... filters correctly
- GET /ship/agent/{run_id}/stream delegates to build stream

Run targeted:
    pytest agentception/tests/test_ship.py -v
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.db.queries import ShipPhaseGroupRow, ShipPRRow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture()
def client_follow() -> Generator[TestClient, None, None]:
    """Client that follows redirects — needed for redirect-to-initiative tests."""
    with TestClient(app, follow_redirects=True) as c:
        yield c


def _make_pr(number: int, phase: str = "phase-1", state: str = "open") -> ShipPRRow:
    return ShipPRRow(
        number=number,
        title=f"PR #{number}",
        state=state,
        head_ref=f"feat/issue-{number}",
        url=f"https://github.com/test/test/pull/{number}",
        labels=[phase],
        closes_issue_number=number + 100,
        merged_at=None,
        phase_label=phase,
        reviewer_run=None,
    )


def _make_groups(*phases: str) -> list[ShipPhaseGroupRow]:
    return [
        ShipPhaseGroupRow(label=phase, prs=[_make_pr(i + 1, phase)])
        for i, phase in enumerate(phases)
    ]


# ---------------------------------------------------------------------------
# GET /ship — full page
# ---------------------------------------------------------------------------


def test_ship_page_redirects_to_first_initiative_when_unscoped(
    client: TestClient,
) -> None:
    """GET /ship without params redirects when initiatives exist."""
    with (
        patch(
            "agentception.routes.ui.ship._initiative_patterns",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.ship.get_initiatives",
            new_callable=AsyncMock,
            return_value=["my-initiative"],
        ),
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        resp = client.get("/ship")
    assert resp.status_code == 302
    assert "initiative=my-initiative" in resp.headers["location"]


def test_ship_page_no_redirect_when_no_initiatives(
    client_follow: TestClient,
) -> None:
    """GET /ship without params does not redirect when there are no initiatives."""
    with (
        patch(
            "agentception.routes.ui.ship._initiative_patterns",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.ship.get_initiatives",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        resp = client_follow.get("/ship")
    assert resp.status_code == 200
    assert "Ship" in resp.text


def test_ship_page_with_initiative_returns_200(client_follow: TestClient) -> None:
    """GET /ship?initiative=... returns 200 and renders PR board."""
    groups = _make_groups("phase-1", "phase-2")
    with (
        patch(
            "agentception.routes.ui.ship._initiative_patterns",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.ship.get_initiatives",
            new_callable=AsyncMock,
            return_value=["test-initiative"],
        ),
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=groups,
        ),
    ):
        resp = client_follow.get("/ship?initiative=test-initiative")
    assert resp.status_code == 200
    assert "Ship" in resp.text
    assert "phase-1" in resp.text
    assert "phase-2" in resp.text


def test_ship_page_with_batch_filter_returns_200(client_follow: TestClient) -> None:
    """GET /ship?batch=... returns 200 (no redirect, batch takes precedence)."""
    groups = _make_groups("phase-1")
    with (
        patch(
            "agentception.routes.ui.ship._initiative_patterns",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.ship.get_initiatives",
            new_callable=AsyncMock,
            return_value=["test-initiative"],
        ),
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=groups,
        ) as mock_query,
    ):
        resp = client_follow.get("/ship?batch=eng-batch-abc")
    assert resp.status_code == 200
    mock_query.assert_awaited_once()
    call_kwargs = mock_query.call_args.kwargs
    assert call_kwargs.get("batch_id") == "eng-batch-abc"


def test_ship_page_pr_count_displayed(client_follow: TestClient) -> None:
    """GET /ship shows the total PR count in the page header."""
    groups = _make_groups("phase-1", "phase-2")
    with (
        patch(
            "agentception.routes.ui.ship._initiative_patterns",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.ship.get_initiatives",
            new_callable=AsyncMock,
            return_value=["ini"],
        ),
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=groups,
        ),
    ):
        resp = client_follow.get("/ship?initiative=ini")
    assert resp.status_code == 200
    # 2 groups, 1 PR each = 2 PRs total
    assert "2 PR" in resp.text


# ---------------------------------------------------------------------------
# GET /ship/board — HTMX partial
# ---------------------------------------------------------------------------


def test_ship_board_partial_returns_200() -> None:
    """GET /ship/board returns 200 HTML partial."""
    with (
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as c:
            resp = c.get("/ship/board")
    assert resp.status_code == 200


def test_ship_board_partial_with_initiative_filter() -> None:
    """GET /ship/board?initiative=... passes initiative to the query."""
    with (
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_query,
    ):
        with TestClient(app) as c:
            resp = c.get("/ship/board?initiative=my-ini")
    assert resp.status_code == 200
    mock_query.assert_awaited_once()
    assert mock_query.call_args.kwargs.get("initiative") == "my-ini"


def test_ship_board_partial_with_batch_filter() -> None:
    """GET /ship/board?batch=... passes batch_id to the query."""
    with (
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_query,
    ):
        with TestClient(app) as c:
            resp = c.get("/ship/board?batch=eng-20260304T000000Z-1234")
    assert resp.status_code == 200
    mock_query.assert_awaited_once()
    assert mock_query.call_args.kwargs.get("batch_id") == "eng-20260304T000000Z-1234"


def test_ship_board_partial_renders_phase_groups() -> None:
    """GET /ship/board renders phase group headings and PR titles."""
    groups = _make_groups("phase-1", "phase-2")
    with (
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=groups,
        ),
    ):
        with TestClient(app) as c:
            resp = c.get("/ship/board?initiative=ini")
    assert resp.status_code == 200
    assert "phase-1" in resp.text
    assert "phase-2" in resp.text
    assert "PR #1" in resp.text


def test_ship_board_empty_shows_no_prs_message() -> None:
    """GET /ship/board with no PRs renders the empty state message."""
    with (
        patch(
            "agentception.routes.ui.ship.get_prs_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as c:
            resp = c.get("/ship/board")
    assert resp.status_code == 200
    assert "No PRs found" in resp.text
