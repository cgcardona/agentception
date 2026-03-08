"""AgentCeption FastAPI application factory.

Entry point: ``uvicorn agentception.app:app --port 10003 --reload``

Architecture:
- ``lifespan`` starts the background ``polling_loop`` task from ``poller.py``
  that periodically refreshes the ``PipelineState`` from the filesystem and
  GitHub API.
- ``GET /events`` streams the live ``PipelineState`` as Server-Sent Events to
  connected dashboard clients.
- Static files are served from ``agentception/static/``.
- HTML pages are rendered via Jinja2 from ``agentception/templates/``.
- JSON API routes live in ``agentception/routes/``.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

# Configure application-level logging before any module imports so that
# every `logging.getLogger(__name__)` in agentception.* emits at INFO+.
# Uvicorn's --log-level flag only controls uvicorn's own access log, not
# the Python root logger, so we set it here explicitly.
logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(levelname)s  %(name)s  %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            # AgentCeption application code — INFO so agent loop steps are visible.
            "agentception": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            # Uvicorn and third-party noise stays at WARNING to keep output readable.
            "uvicorn": {"level": "WARNING"},
            "uvicorn.error": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
            "httpx": {"level": "WARNING"},
            "openai": {"level": "WARNING"},
        },
        "root": {
            "handlers": ["console"],
            "level": "WARNING",
        },
    }
)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from agentception.db.engine import close_db, init_db
from agentception.middleware.auth import ApiKeyMiddleware
from agentception.poller import polling_loop, subscribe, unsubscribe
from agentception.services.worktree_reaper import reap_stale_worktrees
from agentception.routes.api import router as api_router
from agentception.routes.control import router as control_router
from agentception.routes.intelligence import router as intelligence_router
from agentception.routes.roles import router as roles_router
from agentception.routes.templates_api import router as templates_api_router
from agentception.routes.ui import router as ui_router

logger = logging.getLogger(__name__)

# Resolve paths relative to this file so the app works regardless of cwd.
_HERE = Path(__file__).parent


async def _reaper_loop() -> None:
    """Periodic worktree reaper — runs every 15 minutes for the process lifetime."""
    while True:
        await asyncio.sleep(900)
        await reap_stale_worktrees()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start DB, background poller, and worktree reaper on startup; tear all down on shutdown."""
    await init_db()

    # Startup sweep: remove any worktrees left over from crashed agents in the
    # previous session before accepting new traffic.
    await reap_stale_worktrees()

    poller = asyncio.create_task(polling_loop(), name="agentception-poller")
    reaper = asyncio.create_task(_reaper_loop(), name="agentception-reaper")
    logger.info("✅ AgentCeption poller and worktree reaper started")
    try:
        yield
    finally:
        poller.cancel()
        reaper.cancel()
        for task in (poller, reaper):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await close_db()


app = FastAPI(
    title="Agentception",
    description="AgentCeption orchestration dashboard",
    version="0.1.1",
    lifespan=lifespan,
    # Disable the built-in Swagger/ReDoc UIs — we serve a native branded
    # version at /api-reference instead.
    docs_url=None,
    redoc_url=None,
)

# Auth middleware — validates AC_API_KEY on /api/* routes when the key is set.
app.add_middleware(ApiKeyMiddleware)

# Mount static assets — CSS, future JS bundles.
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# Register UI, API, control-plane, roles, intelligence, and templates routers.
app.include_router(ui_router)
app.include_router(api_router)
app.include_router(control_router)
app.include_router(roles_router)
app.include_router(intelligence_router)
app.include_router(templates_api_router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness probe — returns ``{"status": "ok"}`` when the service is up."""
    return {"status": "ok"}


def main() -> None:
    """CLI entrypoint: ``agentception`` (installed via pyproject.toml scripts).

    Launches the AgentCeption dashboard with uvicorn.  Configure the host and
    port via environment variables ``HOST`` (default ``0.0.0.0``) and
    ``PORT`` (default ``10003``), or override ``agentception.app:app`` directly
    when running under a production ASGI server.
    """
    import os

    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "10003"))
    uvicorn.run("agentception.app:app", host=host, port=port, reload=False)


@app.get("/events", tags=["sse"])
async def sse_stream(request: Request) -> EventSourceResponse:
    """Stream live ``PipelineState`` snapshots as Server-Sent Events.

    Each connected dashboard client receives one event per polling tick
    (default every 5 s).  The connection is cleaned up automatically when
    the client disconnects.
    """
    q = subscribe()

    async def generator() -> AsyncIterator[dict[str, str]]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    state = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Keep-alive: yield an empty comment so the connection
                    # stays open through proxies that close idle SSE streams.
                    yield {"comment": "ping"}
                    continue
                yield {"data": state.model_dump_json()}
        finally:
            unsubscribe(q)

    return EventSourceResponse(generator())


