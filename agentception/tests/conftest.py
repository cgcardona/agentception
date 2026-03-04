from __future__ import annotations

"""conftest.py for agentception tests.

Deliberately minimal — no Postgres, no Qdrant, no Redis.
AgentCeption is a standalone service; its tests must run cleanly in the
`agentception` Docker container without any external infrastructure.

Run the full agentception suite:
    docker compose exec agentception pytest agentception/tests/ -v
"""
