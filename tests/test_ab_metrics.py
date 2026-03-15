"""Tests for GET /api/metrics/ab — per-variant A/B metrics."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentception.types import JsonValue


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


def _mock_result_two_rows() -> list[dict[str, JsonValue]]:
    """Two rows: control and streamlined."""
    return [
        {
            "variant": "control",
            "role": "developer",
            "runs": 10,
            "avg_iterations": 5.2,
            "avg_input_tokens": 50000.0,
            "total_tokens": 520000,
            "pass_rate": 0.8,
            "passed": 8,
            "failed": 2,
        },
        {
            "variant": "streamlined",
            "role": "developer",
            "runs": 10,
            "avg_iterations": 4.1,
            "avg_input_tokens": 42000.0,
            "total_tokens": 410000,
            "pass_rate": 0.9,
            "passed": 9,
            "failed": 1,
        },
    ]


@pytest.mark.anyio
async def test_ab_metrics_response_shape(client: AsyncClient) -> None:
    """GET /api/metrics/ab returns 200 with ABMetricsResponse and both variants."""
    mock_rows = _mock_result_two_rows()
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = mock_rows

    async def fake_execute(*args: str | int | bool | float | None, **kwargs: str | int | bool | float | None) -> MagicMock:
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = fake_execute
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "agentception.routes.api.ab_metrics.get_session",
        return_value=mock_cm,
    ):
        response = await client.get("/metrics/ab")

    assert response.status_code == 200
    data = response.json()
    assert "variants" in data
    assert len(data["variants"]) == 2
    variants = {v["variant"]: v for v in data["variants"]}
    assert "control" in variants
    assert "streamlined" in variants
    assert variants["control"]["runs"] == 10
    assert variants["control"]["pass_rate"] == 0.8
    assert variants["streamlined"]["avg_iterations"] == 4.1


@pytest.mark.anyio
async def test_ab_metrics_empty_db(client: AsyncClient) -> None:
    """GET /api/metrics/ab with no data returns 200 and empty variants list."""
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
    assert response.json() == {"variants": []}


@pytest.mark.anyio
async def test_ab_metrics_days_param(client: AsyncClient) -> None:
    """GET /api/metrics/ab?days=30 passes days to the query."""
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    async def fake_execute(statement: JsonValue, params: JsonValue) -> MagicMock:
        # Ensure days param is passed (params may be dict or tuple)
        if isinstance(params, dict):
            assert params.get("days") == 30
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
        response = await client.get("/metrics/ab", params={"days": 30})

    assert response.status_code == 200


@pytest.mark.anyio
async def test_ab_metrics_days_clamped(client: AsyncClient) -> None:
    """days=0 is clamped to 1 (Query(ge=1)); invalid values return 422."""
    response = await client.get("/metrics/ab", params={"days": 0})
    assert response.status_code == 422


def test_ab_metrics_sql_uses_cast_not_double_colon() -> None:
    """Regression: asyncpg rejects :param::type syntax in textual SQL.

    The named parameter :days must be written as CAST(:days AS integer) so
    asyncpg does not see the :: cast operator as part of the parameter name.
    """
    from agentception.routes.api.ab_metrics import _AB_QUERY

    sql = str(_AB_QUERY)
    assert "CAST(:days AS integer)" in sql, (
        "SQL must use CAST(:days AS integer) — asyncpg misparses :days::integer"
    )
    assert ":days::integer" not in sql, (
        "Found :days::integer in SQL — this causes asyncpg PostgresSyntaxError"
    )
