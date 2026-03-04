"""AgentCeption database package.

Self-contained: own Base, engine, session factory, and Alembic migration tree.
Designed for clean extraction to a standalone service — no imports from maestro.db.
"""
from __future__ import annotations
