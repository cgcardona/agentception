"""API key authentication middleware for the ``/api/*`` route prefix.

Design
------
Authentication is **opt-in**: when ``settings.ac_api_key`` is empty (the
default) every request passes unchallenged.  This keeps local development
frictionless while making it trivial to harden the service for shared or
public deployments.

When ``AC_API_KEY`` is set, every request to any path under ``/api/`` must
include the key in one of two supported header formats:

    Authorization: Bearer <key>
    X-API-Key: <key>

Requests that fail authentication receive ``401 Unauthorized`` with a JSON
body explaining which headers are accepted.  Unauthenticated paths (the UI,
health endpoint, MCP endpoint prefix) are not affected.

Why middleware instead of a FastAPI dependency?
-----------------------------------------------
A ``Starlette``-level middleware runs before FastAPI's routing, so it can
reject bad requests cheaply — before Pydantic validation or any database
work.  A FastAPI ``Depends`` would also work but would require adding it to
every route or router, which is brittle.  Centralising it here ensures that
new routes added to ``/api/*`` are protected automatically.
"""

from __future__ import annotations

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agentception.config import settings

logger = logging.getLogger(__name__)

_API_PREFIX = "/api/"


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Validate ``AC_API_KEY`` on every ``/api/*`` request.

    Auth is skipped entirely when ``settings.ac_api_key`` is empty so that
    the default local-dev configuration requires no setup.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Check auth header for ``/api/*`` paths; forward everything else."""
        if not settings.ac_api_key:
            # Auth disabled — pass through unconditionally.
            return await call_next(request)

        if not request.url.path.startswith(_API_PREFIX):
            # Non-API path (UI, /health, /mcp, etc.) — no auth required.
            return await call_next(request)

        provided = _extract_key(request)
        if not hmac.compare_digest(provided, settings.ac_api_key):
            logger.warning(
                "⚠️ auth — rejected %s %s from %s (invalid or missing key)",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Authentication required. "
                        "Provide the API key via 'Authorization: Bearer <key>' "
                        "or 'X-API-Key: <key>' header."
                    )
                },
            )

        return await call_next(request)


def _extract_key(request: Request) -> str:
    """Extract the API key from standard auth headers.

    Supports both ``Authorization: Bearer <key>`` and ``X-API-Key: <key>``.
    Returns an empty string when neither header is present.
    """
    # Prefer the Authorization header (Bearer token format).
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]

    # Fall back to the simpler X-API-Key header.
    return request.headers.get("X-API-Key", "")
