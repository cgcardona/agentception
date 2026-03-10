from __future__ import annotations

"""API route: GET /api/version — application version."""

import importlib.metadata
import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class VersionResponse(BaseModel):
    version: str


@router.get("/version", response_model=VersionResponse, tags=["system"])
async def get_version() -> VersionResponse:
    """Return the application version from package metadata."""
    version = importlib.metadata.version("agentception")
    return VersionResponse(version=version)
