from __future__ import annotations

"""API route: GET /api/ping — liveness check.

Returns ``{"status": "ok"}`` unconditionally.  Use this to verify the
application is reachable before making heavier requests.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class PingResponse(BaseModel):
    """Response body for GET /api/ping."""

    status: str


@router.get("/ping", response_model=PingResponse, tags=["health"])
async def get_ping() -> PingResponse:
    """Return a liveness confirmation.

    Always returns ``{"status": "ok"}`` with HTTP 200.  No database or
    external service calls are made — this endpoint is intentionally cheap
    so load-balancers and smoke tests can use it without side effects.
    """
    return PingResponse(status="ok")
