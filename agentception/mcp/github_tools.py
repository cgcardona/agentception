from __future__ import annotations

"""AgentCeption MCP tools for GitHub operations.

Exposes key ``readers.github`` functions as MCP tools so agents can
atomically claim/label issues, post comments, approve PRs, and merge PRs
through the same typed, cached, logged interface used by the UI.

Read operations (list_issues, issue_read, list_pull_requests, pull_request_read)
are delegated to the ``user-github`` MCP server — use those tools directly.

Tool catalogue:
  github_add_label     — add a label to an issue (invalidates cache)
  github_remove_label  — remove a label from an issue (invalidates cache, idempotent)
  github_claim_issue   — add the agent/wip claim label to an issue
  github_unclaim_issue — remove the agent/wip claim label from an issue
  github_add_comment   — post a Markdown comment on an issue
  github_approve_pr    — submit an approving review on a pull request
  github_merge_pr      — squash-merge a pull request (optionally delete branch)
"""

import logging

from agentception.readers.github import (
    add_comment_to_issue,
    add_label_to_issue,
    add_wip_label,
    approve_pr,
    clear_wip_label,
    merge_pr,
    remove_label_from_issue,
)

logger = logging.getLogger(__name__)


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
    """Claim an issue for this agent by adding the ``agent/wip`` label.

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
    """Release an issue claim by removing the ``agent/wip`` label.

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


async def github_add_comment(issue_number: int, body: str) -> dict[str, object]:
    """Post a Markdown comment on a GitHub issue.

    Use this instead of shelling out to ``gh issue comment`` so that every
    comment is routed through the same typed interface, remains observable
    in logs, and benefits from consistent error handling.

    Args:
        issue_number: GitHub issue number to comment on.
        body: Markdown text for the comment body.  Supports GitHub-flavoured
              Markdown including checklists, tables, and code fences.

    Returns:
        ``{"ok": True, "issue_number": N, "comment_url": "..."}`` or
        ``{"ok": False, "error": "..."}``
    """
    logger.info("💬 github_add_comment: issue #%d (%d chars)", issue_number, len(body))
    try:
        comment_url = await add_comment_to_issue(issue_number, body)
    except RuntimeError as exc:
        logger.error("❌ github_add_comment #%d: %s", issue_number, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "issue_number": issue_number, "comment_url": comment_url}


async def github_approve_pr(pr_number: int) -> dict[str, object]:
    """Submit an approving review on a GitHub pull request.

    Use this instead of shelling out to ``gh pr review --approve`` directly.
    All approvals are routed through the same typed, logged interface and
    invalidate the PR cache so subsequent reads reflect the updated review state.

    Args:
        pr_number: GitHub PR number to approve.

    Returns:
        ``{"ok": True, "pr_number": N}`` or ``{"ok": False, "error": "..."}``
    """
    logger.info("✅ github_approve_pr: approving PR #%d", pr_number)
    try:
        await approve_pr(pr_number)
    except RuntimeError as exc:
        logger.error("❌ github_approve_pr #%d: %s", pr_number, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pr_number": pr_number}


async def github_merge_pr(
    pr_number: int,
    delete_branch: bool = True,
) -> dict[str, object]:
    """Squash-merge a GitHub pull request and optionally delete the head branch.

    Use this instead of shelling out to ``gh pr merge`` directly. Routing
    merges through this tool keeps them observable, logged, and auditable.
    The cache is invalidated on success.

    Only call this after the PR has been approved (grade A or B) and all
    required checks pass. For grade C, fix in place and re-grade first.

    Args:
        pr_number:     GitHub PR number to merge.
        delete_branch: When ``True`` (default), delete the head branch after merge.

    Returns:
        ``{"ok": True, "pr_number": N, "delete_branch": bool}`` or
        ``{"ok": False, "error": "..."}``
    """
    logger.info(
        "🚀 github_merge_pr: merging PR #%d (delete_branch=%s)", pr_number, delete_branch
    )
    try:
        await merge_pr(pr_number, delete_branch=delete_branch)
    except RuntimeError as exc:
        logger.error("❌ github_merge_pr #%d: %s", pr_number, exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pr_number": pr_number, "delete_branch": delete_branch}
