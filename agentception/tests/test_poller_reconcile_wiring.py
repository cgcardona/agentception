from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.poller import polling_loop
from agentception.types import JsonValue


@pytest.fixture()
def mock_tick() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_reconcile() -> AsyncMock:
    return AsyncMock(return_value=[])


@pytest.fixture()
def mock_get_session() -> MagicMock:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory


@pytest.mark.anyio
async def test_reconcile_called_each_cycle(
    mock_tick: AsyncMock,
    mock_reconcile: AsyncMock,
    mock_get_session: MagicMock,
) -> None:
    """reconcile_stale_runs is invoked once per poller tick."""
    call_count = 0

    async def _counting_reconcile(*args: str | int | bool | float | None, **kwargs: str | int | bool | float | None) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError
        return []

    with (
        patch("agentception.poller.tick", mock_tick),
        patch("agentception.poller.reconcile_stale_runs", side_effect=_counting_reconcile),
        patch("agentception.poller.get_session", mock_get_session),
        patch("agentception.poller.asyncio.sleep", new_callable=AsyncMock),
    ):
        # polling_loop() catches CancelledError and returns cleanly — no raise
        await polling_loop()

    assert call_count >= 1


@pytest.mark.anyio
async def test_reconcile_exception_does_not_crash_poller(
    mock_tick: AsyncMock,
    mock_get_session: MagicMock,
) -> None:
    """An exception from reconcile_stale_runs must not stop the poller loop."""
    tick_count = 0

    async def _counted_tick() -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            raise asyncio.CancelledError

    async def _failing_reconcile(*args: str | int | bool | float | None, **kwargs: str | int | bool | float | None) -> list[str]:
        raise RuntimeError("github exploded")

    with (
        patch("agentception.poller.tick", side_effect=_counted_tick),
        patch("agentception.poller.reconcile_stale_runs", side_effect=_failing_reconcile),
        patch("agentception.poller.get_session", mock_get_session),
        patch("agentception.poller.asyncio.sleep", new_callable=AsyncMock),
    ):
        # polling_loop() catches CancelledError and returns cleanly — no raise
        await polling_loop()

    assert tick_count >= 2


@pytest.mark.anyio
async def test_threshold_from_env(
    mock_tick: AsyncMock,
    mock_get_session: MagicMock,
) -> None:
    """STALE_RUN_THRESHOLD_MINUTES env var is forwarded to reconcile_stale_runs."""
    received_threshold: list[int] = []

    async def _capture_reconcile(
        session: JsonValue,
        *,
        stale_threshold_minutes: int = 10,
    ) -> list[str]:
        received_threshold.append(stale_threshold_minutes)
        raise asyncio.CancelledError

    with (
        patch("agentception.poller.tick", mock_tick),
        patch("agentception.poller.reconcile_stale_runs", side_effect=_capture_reconcile),
        patch("agentception.poller.get_session", mock_get_session),
        patch("agentception.poller.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.poller.settings") as mock_settings,
    ):
        mock_settings.poll_interval_seconds = 5
        mock_settings.stale_run_threshold_minutes = 20
        # polling_loop() catches CancelledError and returns cleanly — no raise
        await polling_loop()

    assert received_threshold == [20]
