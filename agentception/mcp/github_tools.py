from __future__ import annotations

"""AgentCeption MCP tools for GitHub operations.

Exposes key ``readers.github`` functions as MCP tools so agents can query
and mutate GitHub state through the same typed, cached, logged interface
used by the UI — no raw ``gh`` subprocess calls in prompts needed.

All reads flow through the TTL cache in ``readers/github.py`` (default 10 s),
so rapid successive calls from an agent cost nothing extra.  All writes
automatically invalidate the cache so the next read reflects current state.

Tool catalogue:
  github_list_issues   — list open or closed issues, optionally filtered by label
  github_get_issue     — fetch full metadata + body for a single issue
  github_list_prs      — list open, merged, or all PRs targeting dev
  github_get_pr        — fetch full PR metadata including comments, reviews, checks
  github_add_label     — add a label to an issue (invalidates cache)
  github_remove_label  — remove a label from an issue (invalidates cache, idempotent)
  github_claim_issue   — add the agent:wip claim label to an issue
  github_unclaim_issue — remove the agent:wip claim label from an issue
"""

import logging

from agentception.readers.github import (
    add_label_to_issue,
    add_wip_label,
    clear_wip_label,
    get_closed_issues,
    get_issue,
    get_issue_body,
    get_issue_comments,
    get_merged_prs_full,
    get_open_issues,
    get_open_prs,
    get_pr_checks,
    get_pr_comments,
    get_pr_reviews,
    remove_label_from_issue,
)

logger = logging.getLogger(__name__)


async def github_list_issues(
    label: str | None = None,
    state: str = "open",
    limit: int = 100,
) -> dict[str, object]:
    """List GitHub issues, optionally filtered by label and state.

    Args:
        label: GitHub label to filter by (e.g. ``"ac-plan/phase-0"``).
               Omit to return all issues in the given state.
        state: ``"open"`` (default) or ``"closed"``.
        limit: Maximum number of results (only applied to closed issues).

    Returns:
        ``{"issues": [...], "count": N, "label": label, "state": state}``
    """
    logger.info(
        "🔍 github_list_issues: state=%r label=%r limit=%d", state, label, limit
    )
    try:
        if state == "closed":
            raw = await get_closed_issues(limit=limit)
            if label:
                def _has_label(issue: dict[str, object], target: str) -> bool:
                    labels_raw = issue.get("labels")
                    if not isinstance(labels_raw, list):
                        return False
                    for lbl in labels_raw:
                        if isinstance(lbl, dict) and lbl.get("name") == target:
                            return True
                        if isinstance(lbl, str) and lbl == target:
                            return True
                    return False

                raw = [issue for issue in raw if _has_label(issue, label)]
        else:
            raw = await get_open_issues(label=label)
    except RuntimeError as exc:
        logger.error("❌ github_list_issues: %s", exc)
        return {"ok": False, "error": str(exc)}

    logger.info("✅ github_list_issues: returned %d issues", len(raw))
    return {"ok": True, "issues": raw, "count": len(raw), "label": label, "state": state}


async def github_get_issue(number: int) -> dict[str, object]:
    """Fetch full metadata and body for a single GitHub issue.

    Args:
        number: GitHub issue number.

    Returns:
        ``{"ok": True, "issue": {number, state, title, labels, body}}``
        or ``{"ok": False, "error": "..."}`` when the issue is not found.
    """
    logger.info("🔍 github_get_issue: #%d", number)
    try:
        meta = await get_issue(number)
        body = await get_issue_body(number)
        comments = await get_issue_comments(number)
    except RuntimeError as exc:
        logger.error("❌ github_get_issue #%d: %s", number, exc)
        return {"ok": False, "error": str(exc)}

    issue: dict[str, object] = {**meta, "body": body, "comments": comments}
    logger.info("✅ github_get_issue: #%d fetched", number)
    return {"ok": True, "issue": issue}


async def github_list_prs(state: str = "open") -> dict[str, object]:
    """List pull requests targeting the dev branch.

    Args:
        state: ``"open"`` (default), ``"merged"``, or ``"all"``.
               ``"all"`` returns both open and recently merged PRs.

    Returns:
        ``{"ok": True, "prs": [...], "count": N, "state": state}``
    """
    logger.info("🔍 github_list_prs: state=%r", state)
    try:
        if state == "open":
            raw = await get_open_prs()
        elif state == "merged":
            raw = await get_merged_prs_full()
        else:
            open_prs = await get_open_prs()
            merged_prs = await get_merged_prs_full()
            raw = open_prs + merged_prs
    except RuntimeError as exc:
        logger.error("❌ github_list_prs: %s", exc)
        return {"ok": False, "error": str(exc)}

    logger.info("✅ github_list_prs: returned %d PRs", len(raw))
    return {"ok": True, "prs": raw, "count": len(raw), "state": state}


async def github_get_pr(number: int) -> dict[str, object]:
    """Fetch full PR metadata including comments, reviews, and CI checks.

    Args:
        number: GitHub PR number.

    Returns:
        ``{"ok": True, "pr": {number, comments, reviews, checks}}``
        or ``{"ok": False, "error": "..."}`` on failure.
    """
    logger.info("🔍 github_get_pr: #%d", number)
    try:
        comments = await get_pr_comments(number)
        reviews = await get_pr_reviews(number)
        checks = await get_pr_checks(number)
    except RuntimeError as exc:
        logger.error("❌ github_get_pr #%d: %s", number, exc)
        return {"ok": False, "error": str(exc)}

    pr: dict[str, object] = {
        "number": number,
        "comments": comments,
        "reviews": reviews,
        "checks": checks,
    }
    logger.info("✅ github_get_pr: #%d fetched", number)
    return {"ok": True, "pr": pr}


async def github_add_label(issue_number: int, label: str) -> dict[str, object]:
    """Add a label to a GitHub issue and invalidate the cache.

    Args:
        issue_number: GitHub issue number.
        label: Label name to add (e.g. ``"ac-plan/phase-1"``).

    Returns:
        ``{"ok": True}`` or ``{"ok": False, "error": "..."}``
    """
    logger.info("🏷️  github_add_label: issue #%d ← %r", issue_number, label)
    try:
        await add_label_to_issue(issue_number, label)
    except RuntimeError as exc:
        logger.error("❌ github_add_label #%d %r: %s", issue_number, label, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "issue_number": issue_number, "added": label}


async def github_remove_label(issue_number: int, label: str) -> dict[str, object]:
    """Remove a label from a GitHub issue (idempotent — no error if absent).

    Args:
        issue_number: GitHub issue number.
        label: Label name to remove.

    Returns:
        ``{"ok": True}`` or ``{"ok": False, "error": "..."}``
    """
    logger.info("🏷️  github_remove_label: issue #%d → remove %r", issue_number, label)
    try:
        await remove_label_from_issue(issue_number, label)
    except RuntimeError as exc:
        logger.error("❌ github_remove_label #%d %r: %s", issue_number, label, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "issue_number": issue_number, "removed": label}


async def github_claim_issue(issue_number: int) -> dict[str, object]:
    """Claim an issue for this agent by adding the ``agent:wip`` label.

    Idiomatic pipeline action — call this before starting work on an issue
    so no other agent double-claims it.  Invalidates the cache.

    Args:
        issue_number: GitHub issue number to claim.

    Returns:
        ``{"ok": True}`` or ``{"ok": False, "error": "..."}``
    """
    logger.info("🤖 github_claim_issue: claiming #%d", issue_number)
    try:
        await add_wip_label(issue_number)
    except RuntimeError as exc:
        logger.error("❌ github_claim_issue #%d: %s", issue_number, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "issue_number": issue_number, "claimed": True}


async def github_unclaim_issue(issue_number: int) -> dict[str, object]:
    """Release an issue claim by removing the ``agent:wip`` label.

    Call this when finishing work or when aborting so the issue becomes
    available for another agent.  Invalidates the cache.

    Args:
        issue_number: GitHub issue number to unclaim.

    Returns:
        ``{"ok": True}`` or ``{"ok": False, "error": "..."}``
    """
    logger.info("🤖 github_unclaim_issue: releasing #%d", issue_number)
    try:
        await clear_wip_label(issue_number)
    except RuntimeError as exc:
        logger.error("❌ github_unclaim_issue #%d: %s", issue_number, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "issue_number": issue_number, "claimed": False}
