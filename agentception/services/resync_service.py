from __future__ import annotations

"""Service layer for forced full issue re-sync.

``resync_all_issues(repo)`` fetches every open and closed issue from GitHub
and upserts them into the local DB.  It is intentionally thin: all GitHub I/O
goes through the existing ``readers.github`` helpers so the retry and auth
logic is centralised in one place.

``GitHubAPIError`` is the single exception type this module raises when the
GitHub REST API is unreachable or returns a non-2xx status.  Route handlers
catch it and return 503.
"""

import logging

from agentception.db.persist import upsert_issues_batch
from agentception.readers.github import get_closed_issues, get_open_issues

logger = logging.getLogger(__name__)


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub REST API returns an error during a resync."""


class ResyncResult:
    """Counts returned by a completed resync operation."""

    __slots__ = ("open", "closed", "upserted")

    def __init__(self, open: int, closed: int, upserted: int) -> None:
        self.open = open
        self.closed = closed
        self.upserted = upserted


async def resync_all_issues(repo: str) -> ResyncResult:
    """Fetch all open and closed issues from GitHub and upsert them locally.

    Parameters
    ----------
    repo:
        Full ``owner/repo`` string, e.g. ``cgcardona/agentception``.
        Used as the DB key for the upsert; the GitHub API call uses
        ``settings.gh_repo`` (configured at startup).

    Returns
    -------
    ResyncResult
        Counts of open issues, closed issues, and total rows upserted.

    Raises
    ------
    GitHubAPIError
        When the GitHub REST API call fails (network error or non-2xx status).
    """
    logger.info("resync_all_issues: starting full sync for repo=%s", repo)

    try:
        open_issues = await get_open_issues()
        closed_issues = await get_closed_issues()
    except RuntimeError as exc:
        raise GitHubAPIError(str(exc)) from exc

    all_issues = open_issues + closed_issues
    await upsert_issues_batch(all_issues, repo=repo)

    result = ResyncResult(
        open=len(open_issues),
        closed=len(closed_issues),
        upserted=len(all_issues),
    )
    logger.info(
        "resync_all_issues: done repo=%s open=%d closed=%d upserted=%d",
        repo,
        result.open,
        result.closed,
        result.upserted,
    )
    return result
