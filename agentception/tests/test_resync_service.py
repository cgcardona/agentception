from __future__ import annotations

"""Tests for agentception.services.resync_service."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_resync_all_issues_returns_counts() -> None:
    """resync_all_issues returns correct open/closed/upserted counts."""
    open_issues = [{"number": i, "title": f"open {i}", "state": "open", "labels": []} for i in range(1, 6)]
    closed_issues = [{"number": i, "title": f"closed {i}", "state": "closed", "labels": []} for i in range(6, 9)]

    with (
        patch(
            "agentception.services.resync_service.get_open_issues",
            new_callable=AsyncMock,
            return_value=open_issues,
        ),
        patch(
            "agentception.services.resync_service.get_closed_issues",
            new_callable=AsyncMock,
            return_value=closed_issues,
        ),
        patch(
            "agentception.services.resync_service.upsert_issues",
            new_callable=AsyncMock,
            return_value=8,
        ),
    ):
        from agentception.services.resync_service import resync_all_issues

        result = await resync_all_issues("owner/repo")

    assert result == {"open": 5, "closed": 3, "upserted": 8}
