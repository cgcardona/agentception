from __future__ import annotations

"""AgentCeption FastAPI application factory.

Entry point: ``uvicorn agentception.app:app --port 7777 --reload``

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

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from agentception.db.engine import close_db, init_db
from agentception.poller import polling_loop, subscribe, unsubscribe
from agentception.routes.api import router as api_router
from agentception.routes.control import router as control_router
from agentception.routes.intelligence import router as intelligence_router
from agentception.routes.roles import router as roles_router
from agentception.routes.templates_api import router as templates_api_router
from agentception.routes.ui import router as ui_router

logger = logging.getLogger(__name__)

# Resolve paths relative to this file so the app works regardless of cwd.
_HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start DB, background poller on startup; tear both down on shutdown."""
    await init_db()
    poller = asyncio.create_task(polling_loop(), name="agentception-poller")
    logger.info("✅ AgentCeption poller started")
    try:
        yield
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
        await close_db()


app = FastAPI(
    title="Agentception",
    description="Maestro pipeline agent dashboard",
    version="0.1.0",
    lifespan=lifespan,
    # Disable the built-in Swagger/ReDoc UIs — we serve a native branded
    # version at /api-reference instead.
    docs_url=None,
    redoc_url=None,
)

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
    port via environment variables ``AC_HOST`` (default ``0.0.0.0``) and
    ``AC_PORT`` (default ``7777``), or override ``agentception.app:app`` directly
    when running under a production ASGI server.
    """
    import os

    import uvicorn

    host = os.environ.get("AC_HOST", "0.0.0.0")
    port = int(os.environ.get("AC_PORT", "7777"))
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


