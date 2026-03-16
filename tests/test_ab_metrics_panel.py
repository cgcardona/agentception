"""Tests for the A/B metrics HTMX panel on the build page (issue #893)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
async def client() -> AsyncGenerator[AsyncClient, None]:
    """ASGI test client for the metrics/ab route only."""
    from fastapi import FastAPI

    from agentception.routes.api.ab_metrics import router as ab_router

    app = FastAPI()
    app.include_router(ab_router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def test_ab_panel_polling_div_in_build_html() -> None:
    """build.html contains the A/B metrics panel div with HTMX polling attributes."""
    root = Path(__file__).resolve().parent.parent
    build_html = root / "agentception" / "templates" / "build.html"
    content = build_html.read_text()

    class FindAbPanel(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.found: dict[str, str] = {}

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag != "div":
                return
            attrs_d = dict((k, v or "") for k, v in attrs)
            if attrs_d.get("id") == "ab-metrics-panel":
                self.found = attrs_d

    parser = FindAbPanel()
    parser.feed(content)
    assert parser.found.get("id") == "ab-metrics-panel"
    assert parser.found.get("hx-get") == "/api/metrics/ab"
    hx_trigger = parser.found.get("hx-trigger", "")
    assert "every 30s" in hx_trigger


@pytest.mark.anyio
async def test_ab_metrics_htmx_returns_html(client: AsyncClient) -> None:
    """GET /api/metrics/ab with HX-Request: true returns HTML with table.ab-metrics."""
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [
        {
            "variant": "control",
            "role": "developer",
            "runs": 5,
            "avg_iterations": 4.0,
            "avg_input_tokens": 1000.0,
            "avg_output_tokens": 500.0,
            "avg_cache_read_tokens": 200.0,
            "avg_cache_write_tokens": 100.0,
            "total_tokens": 5000,
            "avg_duration_secs": 120.0,
            "retry_count": 0,
            "pass_rate": 0.8,
            "passed": 4,
            "failed": 1,
            "grade_a": 3,
            "grade_b": 1,
            "grade_c": 1,
            "grade_d": 0,
            "grade_f": 0,
        },
    ]

    async def fake_execute(*args: str | int | bool | float | None, **kwargs: str | int | bool | float | None) -> MagicMock:
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = fake_execute
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "agentception.routes.api.ab_metrics.get_session",
        return_value=mock_cm,
    ):
        response = await client.get(
            "/metrics/ab",
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert '<div class="drh">' in response.text


@pytest.mark.anyio
async def test_ab_metrics_json_unaffected(client: AsyncClient) -> None:
    """GET /api/metrics/ab without HX-Request returns JSON ABMetricsResponse."""
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    async def fake_execute(*args: str | int | bool | float | None, **kwargs: str | int | bool | float | None) -> MagicMock:
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = fake_execute
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "agentception.routes.api.ab_metrics.get_session",
        return_value=mock_cm,
    ):
        response = await client.get("/metrics/ab")

    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")
    data = response.json()
    assert "variants" in data
    assert isinstance(data["variants"], list)
