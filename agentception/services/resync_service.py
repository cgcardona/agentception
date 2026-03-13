from __future__ import annotations

"""Service layer for forcing a full open+closed issue resync from GitHub.

This module coordinates the fetch (via ``agentception.readers.github``) and
the persist (via ``agentception.db.persist``) so operators can trigger an
immediate, complete issue sync without restarting the server or waiting for
the next poller tick.

Typical usage::

    from agentception.services.resync_service import resync_all_issues

    result = await resync_all_issues()
    # {"open": 42, "closed": 137, "upserted": 179}
"""

import asyncio
import logging

from agentception.config import settings
from agentception.db.persist import upsert_issues
from agentception.readers.github import get_closed_issues, get_open_issues

logger = logging.getLogger(__name__)


async def resync_all_issues() -> dict[str, int]:
    """Fetch all open and up to 1 000 closed issues, then upsert them into the DB.

    Fetches open issues (no label filter) and up to 1 000 recently-closed
    issues from GitHub in parallel, combines them, and passes the full list to
    :func:`~agentception.db.persist.upsert_issues`.

    Always uses ``settings.gh_repo`` for both the GitHub API calls and the DB
    upsert key — there is no repo parameter so there is no risk of fetching
    from one repo while writing under a different key.

    The underlying upsert is hash-diff idempotent: rows are only written when
    content has changed, so concurrent calls with identical data produce no
    extra DB writes and raise no errors.

    Returns
    -------
    dict[str, int]
        ``{"open": <count>, "closed": <count>, "upserted": <count>}``

        - ``open``     — number of open issues fetched from GitHub.
        - ``closed``   — number of closed issues fetched from GitHub.
        - ``upserted`` — total rows passed to the upsert (open + closed).
    """
    repo = settings.gh_repo

    open_issues, closed_issues = await asyncio.gather(
        get_open_issues(),
        get_closed_issues(limit=1000),
    )

    all_issues = list(open_issues) + list(closed_issues)
    upserted = await upsert_issues(issues=all_issues, active_label=None, repo=repo)

    logger.info(
        "✅ resync_all_issues: open=%d closed=%d upserted=%d repo=%s",
        len(open_issues),
        len(closed_issues),
        upserted,
        repo,
    )

    return {
        "open": len(open_issues),
        "closed": len(closed_issues),
        "upserted": upserted,
    }
