"""Auto-redispatch a developer run after a reviewer rejection.

When the reviewer calls build_complete_run with a failing grade (C/D/F),
this service:

1. Posts a rejection comment on the existing (still-open) PR with the
   full defect list.
2. Fetches the original issue details from GitHub for the enhanced body.
3. Dispatches a developer run configured to *continue from the same branch*
   rather than starting from origin/dev — so the agent only needs to fix
   the specific defects, not re-implement everything from scratch.

The reviewer's worktree must be released (via release_worktree) by the
caller *before* this coroutine is awaited, so that the branch is free for
the new developer worktree to reattach.

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
from agentception.readers.github import add_comment_to_issue, get_issue

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"/pull/(\d+)")
_SERVICE_URL = "http://localhost:10003/api/dispatch/issue"

# Brief pause after worktree release before touching git/GitHub so git prune
# has time to flush its ref locks.
_REDISPATCH_DELAY_SECS: float = 1.0

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
    pr_branch: str | None = None,
) -> None:
    """Post a rejection comment and redispatch the developer for a new attempt.

    Called as a fire-and-forget background task from build_complete_run when
    the reviewer signals a failing grade.  Never raises — all errors are
    logged at ERROR level and swallowed so the reviewer's completed state is
    not disturbed.

    The PR is kept open and the branch is kept alive.  The new developer run
    is dispatched with ``continuation_branch`` set to *pr_branch* so it
    reattaches to the existing branch and only has to fix the specific
    defects identified by the reviewer.

    The caller must have already called ``release_worktree`` on the
    reviewer's worktree before scheduling this task so the branch is free.

    Args:
        issue_number: GitHub issue number the implementer worked on.
        pr_url: Full URL of the (still-open) pull request.
        reviewer_feedback: Full defect list from the reviewer (plain text).
        grade: Single letter grade from the reviewer (e.g. "C", "D", "F").
        pr_branch: Name of the existing PR branch (e.g. "feat/issue-869").
            When provided the re-dispatched developer will reattach to this
            branch instead of branching fresh from origin/dev.
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

    # Post a rejection comment on the open PR so the history is visible on
    # GitHub — the PR itself stays open for the developer to push a fix onto
    # the same branch.
    try:
        await add_comment_to_issue(
            pr_number,
            body=(
                f"**Grade {grade} — Attempt {attempt} needs rework.**\n\n"
                f"The implementation has been sent back for corrections. "
                f"Defects to resolve:\n\n{reviewer_feedback}"
            ),
        )
        logger.info(
            "✅ auto_redispatch: posted rejection comment on PR #%d (attempt %d, grade %s)",
            pr_number,
            issue_number,
            attempt,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "❌ auto_redispatch: failed to comment on PR #%d — %s (continuing)",
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
    # When the PR branch is known, tell the dispatch endpoint to reattach the
    # developer worktree to the existing branch instead of starting fresh from
    # origin/dev.  This means the agent only patches the specific defects —
    # it does not have to re-implement from scratch.
    if pr_branch:
        payload["continuation_branch"] = pr_branch

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
            "✅ auto_redispatch: developer dispatched for issue #%d (attempt %d/%d, branch=%r)",
            issue_number,
            attempt,
            _MAX_ATTEMPTS,
            pr_branch,
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
