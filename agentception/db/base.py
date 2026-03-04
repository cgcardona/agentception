from __future__ import annotations

"""Declarative base for all AgentCeption ORM models.

Intentionally self-contained — no shared Base class from external services.

"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all AgentCeption (ac_*) SQLAlchemy models."""
