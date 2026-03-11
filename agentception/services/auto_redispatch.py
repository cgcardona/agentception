"""Auto-redispatch a developer run after a reviewer rejection.

When the reviewer calls build_complete_run with a failing grade (C/D/F),
this service:

1. Closes the rejected PR.
2. Fetches the original issue details from GitHub.
3. Builds an enhanced issue body injecting the reviewer's defect list at the top.
4. Redispatches a developer run with the enriched briefing so the executor
   sees the full defect list before writing a single line of code.

Maximum 3 attempts. After the third rejection the run is abandoned and an
error is logged — no further redispatch occurs.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from agentception.config import settings
from agentception.db.queries import get_agent_run_task_description
from agentception.readers.github import close_pr, get_issue

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"/pull/(\d+)")
_SERVICE_URL = "http://localhost:10003/api/dispatch/issue"

# Seconds to wait before redispatching — gives GitHub time to process
# the PR close before the new worktree fetch starts.
_REDISPATCH_DELAY_SECS: float = 3.0

# Maximum number of reviewer-rejected attempts before giving up.
_MAX_ATTEMPTS: int = 3

# Marker string embedded in enhanced issue bodies so we can count retries.
_REJECTION_MARKER = "## Reviewer Rejection — Attempt"


def _count_prior_rejections(task_description: str | None) -> int:
    """Count existing rejection sections already injected into the task description."""
    if not task_description:
        return 0
    return task_description.count(_REJECTION_MARKER)


def _build_enhanced_body(
    original_body: str,
    reviewer_feedback: str,
    grade: str,
    attempt: int,
) -> str:
    """Prepend a rejection section to the original issue body.

    The rejection section is always prepended so the executor sees it
    before reading the rest of the spec — highest-priority context first.
    """
    feedback_section = (
        f"{_REJECTION_MARKER} {attempt} (Grade: {grade})\n\n"
        f"The previous implementation was **rejected**. Every defect listed "
        f"below **must** be resolved before calling `build_complete_run`.\n\n"
        f"{reviewer_feedback}\n\n"
        f"---\n\n"
    )
    return feedback_section + original_body


async def auto_redispatch_after_rejection(
    issue_number: int,
    pr_url: str,
    reviewer_feedback: str,
    grade: str,
) -> None:
    """Close a rejected PR and redispatch the developer for a new attempt.

    Called as a fire-and-forget background task from build_complete_run when
    the reviewer signals a failing grade.  Never raises — all errors are
    logged at ERROR level and swallowed so the reviewer's completed state is
    not disturbed.

    Args:
        issue_number: GitHub issue number the implementer worked on.
        pr_url: Full URL of the rejected pull request.
        reviewer_feedback: Full defect list from the reviewer (plain text).
        grade: Single letter grade from the reviewer (e.g. "C", "D", "F").
    """
    run_id = f"issue-{issue_number}"

    # Determine attempt number from the current task_description in the DB.
    # Each prior rejection prepends a _REJECTION_MARKER section; counting them
    # gives us the number of prior attempts without any extra DB columns.
    task_desc = await get_agent_run_task_description(run_id)
    prior_count = _count_prior_rejections(task_desc)
    attempt = prior_count + 1

    if attempt > _MAX_ATTEMPTS:
        logger.error(
            "❌ auto_redispatch: max attempts (%d) reached for issue #%d — "
            "no further redispatch. Last grade: %s",
            _MAX_ATTEMPTS,
            issue_number,
            grade,
        )
        return

    pr_match = _PR_URL_RE.search(pr_url)
    if not pr_match:
        logger.error(
            "❌ auto_redispatch: cannot parse PR number from %r — abandoning issue #%d",
            pr_url,
            issue_number,
        )
        return
    pr_number = int(pr_match.group(1))

    await asyncio.sleep(_REDISPATCH_DELAY_SECS)

    # Close the rejected PR so the branch can be recreated clean.
    try:
        await close_pr(
            pr_number,
            comment=(
                f"**Grade {grade} — Attempt {attempt} rejected.** "
                f"Closing for rework. Defects:\n\n{reviewer_feedback}"
            ),
        )
        logger.info(
            "✅ auto_redispatch: closed PR #%d for issue #%d (attempt %d, grade %s)",
            pr_number,
            issue_number,
            attempt,
            grade,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "❌ auto_redispatch: failed to close PR #%d — %s (continuing)",
            pr_number,
            exc,
        )

    # Fetch the original issue from GitHub for title + body.
    try:
        issue = await get_issue(issue_number)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "❌ auto_redispatch: failed to fetch issue #%d — %s",
            issue_number,
            exc,
        )
        return

    issue_title = str(issue.get("title") or f"Issue #{issue_number}")
    original_body = str(issue.get("body") or "")
    enhanced_body = _build_enhanced_body(original_body, reviewer_feedback, grade, attempt)

    payload: dict[str, object] = {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_body": enhanced_body,
        "role": "developer",
        "repo": settings.gh_repo,
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
            "✅ auto_redispatch: developer dispatched for issue #%d (attempt %d/%d)",
            issue_number,
            attempt,
            _MAX_ATTEMPTS,
        )
    except httpx.HTTPStatusError as exc:
        logger.error(
            "❌ auto_redispatch: dispatch returned %d for issue #%d — %s",
            exc.response.status_code,
            issue_number,
            exc.response.text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "❌ auto_redispatch: failed to dispatch for issue #%d — %s",
            issue_number,
            exc,
        )
