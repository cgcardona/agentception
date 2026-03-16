"""conftest.py for agentception tests.

Deliberately minimal — no Postgres, no Qdrant, no Redis.
AgentCeption is a standalone service; its tests must run cleanly in the
`agentception` Docker container without any external infrastructure.

Run the full agentception suite:
    docker compose exec agentception pytest agentception/tests/ -v
"""

from __future__ import annotations

import asyncio
import collections.abc
from collections.abc import AsyncGenerator, Callable, Generator
from unittest.mock import patch

import pytest


def make_create_task_side_effect() -> (
    Callable[[collections.abc.Coroutine[object, object, object]], asyncio.Future[None]]
):
    """Return a side-effect for patching ``asyncio.create_task`` in tests.

    When ``asyncio.create_task`` is mocked the coroutine passed to it is never
    scheduled, so Python emits ``RuntimeWarning: coroutine … was never awaited``
    during garbage collection.  This helper closes the incoming coroutine
    immediately (suppressing the warning) and returns a resolved Future so any
    code that reads the return value of ``create_task`` still gets a valid object.

    Usage::

        patch("some.module.asyncio.create_task",
              side_effect=make_create_task_side_effect())
    """

    def _side_effect(
        coro: collections.abc.Coroutine[object, object, object],
        **_: object,
    ) -> asyncio.Future[None]:
        coro.close()
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    return _side_effect


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


async def _noop_init_db() -> None:
    """No-op replacement for init_db during the test session.

    asyncpg connections are bound to the event loop that created them.
    With pytest-asyncio's function-scoped event loops each async test runs on a
    fresh loop, so any engine created inside one test's lifespan is orphaned
    (its connections reference a now-closed loop) by the time the next test's
    lifespan calls close_db().  This produces the "Loop closed" asyncpg warning
    and causes dispose() to hang, making the full test suite freeze on the
    second or third consecutive run.

    The test suite mocks all service-layer functions; no test reaches the real
    DB.  Making init_db/close_db no-ops is therefore safe and is consistent with
    the "no Postgres" contract stated in this module's docstring.
    """


async def _noop_close_db() -> None:
    """No-op replacement for close_db during the test session."""


async def _noop_tick() -> None:
    """No-op replacement for tick() fired as fire-and-forget background tasks.

    Two route handlers spawn tick() as an unnamed asyncio.create_task:
    - overview.py  fires tick() (aliased as _poller_tick) on every page load
    - control.py   fires tick() on /control/trigger-poll

    These fire-and-forget tasks outlive the request and make real GitHub/DB I/O.
    When pytest-asyncio closes the function-scoped event loop after each async
    test, any still-pending tick() task is destroyed mid-gather, which causes
    the "Task was destroyed but it is pending!" warning and intermittently hangs
    the event-loop teardown for 30+ seconds.

    Patching tick() to a no-op prevents the tasks from being created at all.
    Tests that exercise tick() directly import it from agentception.poller and
    are unaffected.
    """


@pytest.fixture(autouse=True, scope="session")
def _patch_app_lifespan() -> Generator[None, None, None]:
    """Patch all background-task entry points for the entire test session.

    - polling_loop              → sleeps forever, cancels instantly
    - init_db / close_db       → no-ops (no asyncpg engine, no event-loop binding)
    - agentception.poller.tick  → no-op (covers control.py's local import)
    - overview._poller_tick     → no-op (module-level alias of tick in overview.py)

    Session scope means a single patch set covers every AsyncClient lifespan
    startup regardless of which test file triggers it.  Tests that import these
    symbols directly from their source modules are unaffected.
    """
    with (
        patch("agentception.app.polling_loop", _noop_polling_loop),
        patch("agentception.app.init_db", _noop_init_db),
        patch("agentception.app.close_db", _noop_close_db),
        patch("agentception.poller.tick", _noop_tick),
        patch("agentception.routes.ui.overview._poller_tick", _noop_tick),
    ):
        yield
