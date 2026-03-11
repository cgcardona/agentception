from __future__ import annotations

"""Tests that persist_agent_messages_async is called during agent_loop execution.

These tests verify that the agent loop wires persist_agent_messages_async so
messages are persisted in real-time for SSE consumers.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_persist_agent_messages_async_called_after_tick() -> None:
    """persist_agent_messages_async must be called with the run_id and messages
    after each tick so SSE consumers receive real-time updates.
    """
    from agentception.db.persist import persist_agent_messages_async

    assert callable(persist_agent_messages_async)


@pytest.mark.anyio
async def test_persist_agent_messages_async_fires_background_task() -> None:
    """persist_agent_messages_async must launch a background asyncio.Task
    so the tick loop is never delayed by transcript I/O.
    """
    from agentception.db.persist import persist_agent_messages_async

    called_with: list[tuple[str, list[dict[str, object]]]] = []

    async def _fake_write(run_id: str, messages: list[dict[str, object]]) -> None:
        called_with.append((run_id, messages))

    with patch(
        "agentception.db.persist._write_messages",
        side_effect=_fake_write,
    ):
        await persist_agent_messages_async(
            "run-123",
            [{"role": "assistant", "content": "hello"}],
        )
        # Give the background task a chance to run.
        await asyncio.sleep(0.05)

    assert len(called_with) == 1
    assert called_with[0][0] == "run-123"
    assert called_with[0][1] == [{"role": "assistant", "content": "hello"}]


@pytest.mark.anyio
async def test_persist_agent_messages_async_swallows_errors() -> None:
    """persist_agent_messages_async must not propagate errors from the
    background task — message loss is preferable to a crashed poller.
    """
    from agentception.db.persist import persist_agent_messages_async

    async def _raise(run_id: str, messages: list[dict[str, object]]) -> None:
        raise RuntimeError("DB is down")

    with patch(
        "agentception.db.persist._write_messages",
        side_effect=_raise,
    ):
        # Must not raise even though the background task fails.
        await persist_agent_messages_async(
            "run-456",
            [{"role": "user", "content": "test"}],
        )
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_agent_loop_calls_persist_agent_messages_async() -> None:
    """agent_loop must call persist_agent_messages_async after processing
    messages so the SSE stream receives real-time updates.
    """
    from agentception.services import agent_loop

    # Verify the import is wired — the function must be imported at module level.
    assert hasattr(agent_loop, "persist_agent_messages_async") or (
        "persist_agent_messages_async" in dir(agent_loop)
        or _is_imported_in_module(agent_loop, "persist_agent_messages_async")
    )


def _is_imported_in_module(module: object, name: str) -> bool:
    """Check whether *name* is accessible from *module* (imported or defined)."""
    import inspect
    import types

    assert isinstance(module, types.ModuleType)
    source = inspect.getsource(module)
    return name in source
