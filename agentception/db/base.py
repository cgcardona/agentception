from __future__ import annotations

"""Declarative base for all AgentCeption ORM models.

Intentionally isolated from ``maestro.db.database.Base`` so that this package
can be extracted to its own service without touching the Maestro codebase.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all AgentCeption (ac_*) SQLAlchemy models."""
