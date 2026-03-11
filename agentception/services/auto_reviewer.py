"""Auto-dispatch a pr-reviewer agent after an implementer calls build_complete_run.

The reviewer is triggered as a fire-and-forget background task.  Failures are
logged but never propagate back to the caller — the implementer's run is already
marked complete regardless of whether the reviewer starts.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

from agentception.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"/pull/(\d+)")

# Fixed port — the agentception service always binds 10003 inside the container.
_SERVICE_URL = "http://localhost:10003/api/dispatch/issue"

# Seconds to wait after build_complete_run before fetching the PR branch.
# GitHub needs a moment to register the push before the branch is fetchable.
_REVIEWER_DELAY_SECS: float = 5.0


async def auto_dispatch_reviewer(
    issue_number: int,
    pr_url: str,
    pr_branch: str | None = None,
) -> None:
    """Fire a pr-reviewer dispatch for the given PR.

    Called as a background task from build_complete_run.  Never raises — all
    errors are logged at ERROR level and swallowed so the implementer's completed
    state is not disturbed.

    Args:
        issue_number: GitHub issue number the implementer worked on.
        pr_url: Full PR URL, e.g. ``https://github.com/owner/repo/pull/537``.
        pr_branch: Branch the implementer pushed.  Defaults to
            ``feat/issue-{issue_number}`` when omitted.
    """
    pr_match = _PR_URL_RE.search(pr_url)
    if not pr_match:
        logger.error(
            "❌ auto_reviewer: cannot parse PR number from %r — reviewer not dispatched",
            pr_url,
        )
        return

    pr_number = int(pr_match.group(1))
    branch = pr_branch or f"feat/issue-{issue_number}"

    # Small delay so GitHub has time to register the pushed branch before the
    # reviewer fetches it.
    await asyncio.sleep(_REVIEWER_DELAY_SECS)

    payload = {
        "issue_number": issue_number,
        "issue_title": f"Review PR #{pr_number}",
        "issue_body": "",
        "role": "pr-reviewer",
        "repo": settings.gh_repo,
        "pr_number": pr_number,
        "pr_branch": branch,
    }
    headers = {
        "X-API-Key": settings.ac_api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _SERVICE_URL,
                json=payload,
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
        logger.info(
            "✅ auto_reviewer: dispatched reviewer for PR #%d (issue #%d, branch=%r)",
            pr_number,
            issue_number,
            branch,
        )
    except httpx.HTTPStatusError as exc:
        logger.error(
            "❌ auto_reviewer: dispatch returned %d for PR #%d — %s",
            exc.response.status_code,
            pr_number,
            exc.response.text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "❌ auto_reviewer: failed to dispatch reviewer for PR #%d — %s",
            pr_number,
            exc,
        )
