from __future__ import annotations

"""GET /api/version — returns the running application version.

A single, stable endpoint that clients and health-check scripts can call to
confirm which version of AgentCeption is deployed.  The version string is
read from the package metadata at import time so it always matches what was
installed, not a hard-coded constant that can drift.
"""

import importlib.metadata
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

# Read the version once at import time.  Falls back to "0.0.0" when the
# package is not installed (e.g. running directly from source in development).
try:
    _APP_VERSION: str = importlib.metadata.version("agentception")
except importlib.metadata.PackageNotFoundError:
    _APP_VERSION = "0.0.0"


class VersionResponse(BaseModel):
    """Response body for ``GET /api/version``.

    ``version`` is the PEP 440 version string from the installed package
    metadata (``importlib.metadata.version("agentception")``).
    """

    version: str


@router.get("/version", response_model=VersionResponse)
async def get_version() -> VersionResponse:
    """Return the running application version.

    Reads the version from installed package metadata so it is always
    consistent with what was deployed, not a separately maintained constant.
    """
    return VersionResponse(version=_APP_VERSION)
