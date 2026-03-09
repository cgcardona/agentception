"""API routes: PR violations and issue analysis.

The canonical DAG endpoint is ``GET /api/intelligence/dag`` served by
``routes/intelligence.py``.  This module provides supplementary intelligence
endpoints that share the same ``/api`` prefix.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agentception.intelligence.analyzer import IssueAnalysis, analyze_issue
from agentception.intelligence.guards import PRViolation, detect_out_of_order_prs
from agentception.readers.github import close_pr

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/intelligence/pr-violations", tags=["intelligence"])
async def pr_violations_api() -> list[PRViolation]:
    """Return open PRs that violate active pipeline phase ordering.

    Checks each open PR's ``Closes #N`` reference against the currently active
    ``agentception/*`` label.  A PR is a violation when the issue it closes
    belongs to an earlier (or later) phase than the one currently being worked.

    Returns an empty list when there are no violations or no active label.
    """
    return await detect_out_of_order_prs()


@router.post("/intelligence/pr-violations/{pr_number}/close", tags=["intelligence"])
async def close_violating_pr(pr_number: int) -> dict[str, int]:
    """Close a PR identified as an out-of-order violation.

    Posts an automated comment explaining the closure before closing the PR so
    the git history and GitHub timeline both retain the reason.

    Raises
    ------
    HTTP 500
        When the ``gh pr close`` subprocess call fails (e.g. PR already closed).
    """
    try:
        await close_pr(
            pr_number,
            "Closed by AgentCeption: out-of-order PR violation.",
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to close PR #{pr_number}: {exc}",
        ) from exc
    logger.info("✅ Closed violating PR #%d", pr_number)
    return {"closed": pr_number}


@router.post("/analyze/issue/{number}", tags=["intelligence"])
async def analyze_issue_api(number: int) -> IssueAnalysis:
    """Parse an issue body and return structured parallelism / role recommendations.

    Fetches the issue body from GitHub via the ``gh`` CLI and applies
    local heuristics to infer dependencies, conflict risk, and the
    recommended engineer role.  No model calls are made — results are
    deterministic for a given issue body.

    This endpoint feeds into the Eng VP ``.agent-task`` generation pipeline:
    the caller can use ``recommended_role``, ``parallelism``, and
    ``recommended_merge_after`` to decide whether and how to schedule an agent.

    Parameters
    ----------
    number:
        GitHub issue number to analyse.

    Raises
    ------
    HTTP 404
        When the GitHub CLI cannot find the issue (non-existent or no access).
    HTTP 500
        When the ``gh`` subprocess exits with a non-zero status for any other
        reason.
    """
    try:
        return await analyze_issue(number)
    except RuntimeError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 500
        raise HTTPException(status_code=status, detail=detail) from exc
