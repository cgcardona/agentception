from __future__ import annotations

"""Tests for agentception/routes/api/org_live.py.

Endpoints:
    GET /api/org/batches/{initiative}
    GET /api/org/live/{initiative}          (SSE stream)

All DB queries and settings are mocked so no live DB or GitHub I/O occurs.

SSE streaming tests call ``_live_generator`` directly to avoid ASGI transport
buffering the infinite stream.  HTTP-level tests use a finite stand-in
generator to verify routing and headers.

Run targeted:
    pytest agentception/tests/test_org_live.py -v
"""

import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentception.db.queries import BatchSummaryRow, RunTreeNodeRow


def _batch(batch_id: str, total: int = 3, active: int = 1) -> BatchSummaryRow:
    return BatchSummaryRow(
        batch_id=batch_id,
        spawned_at="2026-01-01T00:00:00",
        total_count=total,
        active_count=active,
    )


def _node(run_id: str, parent_id: str | None = None) -> RunTreeNodeRow:
    return RunTreeNodeRow(
        id=run_id,
        role="developer",
        status="implementing",
        agent_status="implementing",
        tier="worker",
        org_domain="engineering",
        parent_run_id=parent_id,
        issue_number=42,
        pr_number=None,
        batch_id="batch-abc",
        spawned_at="2026-01-01T00:00:00",
        last_activity_at=None,
        current_step="Writing tests",
    )


def _mock_request(disconnect_after: int = 1) -> MagicMock:
    """Return a mock Starlette Request that disconnects after *disconnect_after* polls."""
    req = MagicMock()
    calls = [False] * disconnect_after + [True]
    req.is_disconnected = AsyncMock(side_effect=calls)
    return req


# ── GET /api/org/batches/{initiative} ─────────────────────────────────────────


@pytest.mark.anyio
async def test_get_org_batches_returns_200() -> None:
    """GET /api/org/batches returns 200 with a list of BatchSummaryRow objects."""
    from agentception.app import app

    with (
        patch("agentception.routes.api.org_live.settings") as mock_settings,
        patch(
            "agentception.routes.api.org_live.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[{"issues": [{"number": 42, "state": "open"}]}],
        ),
        patch(
            "agentception.routes.api.org_live.get_batch_summaries_for_initiative",
            new_callable=AsyncMock,
            return_value=[_batch("batch-abc"), _batch("batch-xyz")],
        ),
    ):
        mock_settings.gh_repo = "owner/repo"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/org/batches/my-initiative")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["batch_id"] == "batch-abc"


@pytest.mark.anyio
async def test_get_org_batches_returns_empty_when_no_batches() -> None:
    """GET /api/org/batches returns an empty list when no batches exist."""
    from agentception.app import app

    with (
        patch("agentception.routes.api.org_live.settings") as mock_settings,
        patch(
            "agentception.routes.api.org_live.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.api.org_live.get_batch_summaries_for_initiative",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_settings.gh_repo = "owner/repo"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/org/batches/my-initiative")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_get_org_batches_fields_shape() -> None:
    """Each batch row must expose batch_id, spawned_at, total_count, active_count."""
    from agentception.app import app

    with (
        patch("agentception.routes.api.org_live.settings") as mock_settings,
        patch(
            "agentception.routes.api.org_live.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[{"issues": [{"number": 1, "state": "open"}]}],
        ),
        patch(
            "agentception.routes.api.org_live.get_batch_summaries_for_initiative",
            new_callable=AsyncMock,
            return_value=[_batch("batch-123", total=5, active=2)],
        ),
    ):
        mock_settings.gh_repo = "owner/repo"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/org/batches/my-initiative")

    body = resp.json()
    row = body[0]
    assert row["batch_id"] == "batch-123"
    assert row["total_count"] == 5
    assert row["active_count"] == 2
    assert "spawned_at" in row


# ── _live_generator unit tests (direct invocation, no HTTP buffering) ─────────
# The ASGI transport buffers the full response body before returning chunks,
# so infinite SSE generators cannot be tested via HTTP in unit tests.
# We call _live_generator directly and mock request.is_disconnected to
# terminate the loop after the first poll.


@pytest.mark.anyio
async def test_live_generator_emits_tree_event() -> None:
    """_live_generator yields a 'tree' SSE frame when a batch is active."""
    from agentception.routes.api.org_live import _live_generator

    nodes = [_node("issue-42")]
    req = _mock_request(disconnect_after=1)

    with (
        patch("agentception.routes.api.org_live.settings") as mock_settings,
        patch(
            "agentception.routes.api.org_live.get_run_tree_by_batch_id",
            new_callable=AsyncMock,
            return_value=nodes,
        ),
        patch("agentception.routes.api.org_live.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_settings.gh_repo = "owner/repo"

        chunks: list[str] = []
        async for chunk in _live_generator(req, "my-initiative", "batch-abc"):
            chunks.append(chunk)

    data_lines = [c[len("data: "):].strip() for c in chunks if c.startswith("data: ")]
    assert data_lines, "expected at least one SSE data frame"
    event = json.loads(data_lines[0])
    assert event["t"] == "tree"
    assert event["batch_id"] == "batch-abc"
    assert isinstance(event["nodes"], list)


@pytest.mark.anyio
async def test_live_generator_emits_idle_when_no_batch() -> None:
    """_live_generator yields an 'idle' SSE frame when no active batch is found."""
    from agentception.routes.api.org_live import _live_generator

    req = _mock_request(disconnect_after=1)

    with (
        patch("agentception.routes.api.org_live.settings") as mock_settings,
        patch(
            "agentception.routes.api.org_live.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.api.org_live.get_latest_active_batch_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agentception.routes.api.org_live.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_settings.gh_repo = "owner/repo"

        chunks: list[str] = []
        async for chunk in _live_generator(req, "my-initiative", None):
            chunks.append(chunk)

    data_lines = [c[len("data: "):].strip() for c in chunks if c.startswith("data: ")]
    assert data_lines
    event = json.loads(data_lines[0])
    assert event["t"] == "idle"


# ── HTTP-level route test: headers only (finite stand-in generator) ───────────


@pytest.mark.anyio
async def test_org_live_route_returns_event_stream_header() -> None:
    """GET /api/org/live returns Content-Type: text/event-stream."""
    from agentception.app import app

    async def _finite_gen() -> AsyncGenerator[str, None]:
        yield 'data: {"t": "idle"}\n\n'

    with patch(
        "agentception.routes.api.org_live._live_generator",
        return_value=_finite_gen(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/org/live/my-initiative")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
