from __future__ import annotations

"""Async SQLAlchemy engine, session factory, and lifecycle helpers for AgentCeption.

Intentionally mirrors the pattern in ``maestro/db/database.py`` so both services
look consistent while remaining fully independent — no cross-imports.

Schema is owned by Alembic (``agentception/alembic/``).  This module only
creates the engine and session factory; it never calls ``CREATE TABLE``.
"""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_url() -> str:
    """Read AC_DATABASE_URL from settings; fall back to SQLite for local dev."""
    from agentception.config import settings  # local import — avoids circular at module load

    url = getattr(settings, "database_url", None)
    if not url:
        url = "sqlite+aiosqlite:///./agentception.db"
        logger.warning("⚠️  AC_DATABASE_URL not set — using SQLite: %s", url)
    return url


async def init_db() -> None:
    """Initialise the async engine and session factory.

    Called once in the FastAPI lifespan before the app starts serving requests.
    Alembic must have already run ``upgrade head`` before this is called.
    """
    global _engine, _session_factory

    url = _get_url()
    display_url = url.split("@")[-1] if "@" in url else url
    logger.info("✅ AgentCeption DB initialising: %s", display_url)

    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(url, echo=False, connect_args=connect_args)
    _session_factory = async_sessionmaker(
        bind=_engine, class_=AsyncSession, expire_on_commit=False
    )

    # Import models to register them on Base.metadata even though DDL is
    # Alembic-owned.  This ensures relationship resolution works at runtime.
    from agentception.db import models  # noqa: F401

    logger.info("✅ AgentCeption DB ready")


async def close_db() -> None:
    """Dispose of the engine and clear module-level singletons."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("✅ AgentCeption DB connection closed")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a committed-on-success / rolled-back-on-error session."""
    if _session_factory is None:
        raise RuntimeError("AgentCeption DB not initialised. Call init_db() first.")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_session() -> AsyncSession:
    """Return a raw session for background tasks (non-FastAPI contexts).

    Usage::

        async with get_session() as session:
            session.add(row)
            await session.commit()
    """
    if _session_factory is None:
        raise RuntimeError("AgentCeption DB not initialised. Call init_db() first.")
    return _session_factory()
