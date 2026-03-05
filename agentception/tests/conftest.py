from __future__ import annotations

"""conftest.py for agentception tests.

Deliberately minimal — no Postgres, no Qdrant, no Redis.
AgentCeption is a standalone service; its tests must run cleanly in the
`agentception` Docker container without any external infrastructure.

Run the full agentception suite:
    docker compose exec agentception pytest agentception/tests/ -v
"""

import asyncio
from collections.abc import Generator
from unittest.mock import patch

import pytest


async def _noop_polling_loop() -> None:
    """No-op replacement for the real polling_loop used across all tests.

    The real polling_loop immediately calls tick(), which makes live GitHub API
    and subprocess calls.  Replacing it with this coroutine prevents network I/O
    during TestClient lifespan startup and eliminates the event-loop cleanup hang
    that occurred after the test run when dangling poller tasks were orphaned.
    """
    try:
        await asyncio.sleep(float("inf"))
    except asyncio.CancelledError:
        return


@pytest.fixture(autouse=True, scope="session")
def _patch_polling_loop() -> Generator[None, None, None]:
    """Patch agentception.app.polling_loop for the entire test session.

    Scoped to session so a single patch covers every TestClient lifespan
    startup, regardless of which test file triggers it.  Tests that exercise
    polling_loop directly import it from agentception.poller and are unaffected.
    """
    with patch("agentception.app.polling_loop", _noop_polling_loop):
        yield
