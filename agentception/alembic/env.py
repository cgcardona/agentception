"""Alembic environment for AgentCeption — fully self-contained, no external imports."""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from agentception.db.base import Base
from agentception.db import models  # noqa: F401 — registers all ac_* tables on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Resolve the database URL from the environment.
# DATABASE_URL is the canonical name; fall back to DATABASE_URL if unset
# AgentCeption container Postgres credentials
# without duplicating the secret.
_db_url: str = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
if not _db_url:
    raise RuntimeError(
        "Set DATABASE_URL (or DATABASE_URL) before running AgentCeption migrations."
    )

# Alembic's sync engine needs a sync driver — strip async dialect markers.
_sync_url: str = _db_url.replace("+asyncpg", "").replace("+aiosqlite", "")
config.set_main_option("sqlalchemy.url", _sync_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        version_table="alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Separate version table so AgentCeption migrations don't collide with
        # Standard alembic_version table.
        version_table="alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _db_url  # async driver for the live connection

    connectable = async_engine_from_config(
        cfg, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
