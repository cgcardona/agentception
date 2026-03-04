from __future__ import annotations

"""Pipeline guard functions for the AgentCeption intelligence layer.

Contains two families of guards:

**Stale-claim detection** (AC-404): An issue carrying ``agent:wip`` with no
corresponding worktree on the local filesystem indicates an agent that was
killed or crashed before removing its label, leaving the issue permanently
locked out of the scheduling pool.

**Out-of-order PR detection** (AC-403): Detects open PRs whose linked issue
belongs to a pipeline phase label that no longer matches the currently active
phase. Surfaces violations so operators can close stale PRs before they create
merge conflicts or pollute the dev branch with work from the wrong batch.

Public API:
- ``StaleClaim``               — re-exported from models for convenience
- ``detect_stale_claims()``    — detects issues claimed with no live worktree
- ``PRViolation``              — typed result for a single ordering violation
- ``detect_out_of_order_prs()``— main detection coroutine; called by poller
                                  and the /api/intelligence/pr-violations route
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from agentception.models import StaleClaim
from agentception.readers.github import get_active_label, get_issue, get_open_prs_with_body

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Matches "Closes #NNN", "Close #NNN", "closes #NNN" in PR bodies.
_CLOSES_PATTERN: re.Pattern[str] = re.compile(r"[Cc]loses?\s+#(\d+)")


# ---------------------------------------------------------------------------
# Stale-claim detection
# ---------------------------------------------------------------------------


async def detect_stale_claims(
    wip_issues: list[dict[str, object]],
    worktrees_dir: Path,
) -> list[StaleClaim]:
    """Detect issues with ``agent:wip`` label but no corresponding worktree.

    For each open issue labelled ``agent:wip``, computes the expected worktree
    path ``worktrees_dir / f"issue-{number}"``.  When that path does not exist
    on the filesystem, the issue is classified as a stale claim.

    Parameters
    ----------
    wip_issues:
        List of issue dicts as returned by
        :func:`~agentception.readers.github.get_wip_issues`.
        Each dict must contain at minimum ``number`` (int) and ``title`` (str).
    worktrees_dir:
        Root directory where worktrees are created, e.g.
        ``~/.agentception/worktrees/maestro``.

    Returns
    -------
    list[StaleClaim]
        One entry per stale issue, sorted ascending by issue number.
        Empty list when all wip issues have live worktrees.
    """
    claims: list[StaleClaim] = []

    for issue in wip_issues:
        num = issue.get("number")
        title = issue.get("title", "")
        if not isinstance(num, int):
            logger.warning("⚠️  Skipping wip issue with non-int number: %r", num)
            continue
        if not isinstance(title, str):
            title = str(title)

        expected_path = worktrees_dir / f"issue-{num}"
        if not expected_path.exists():
            logger.debug(
                "⚠️  Stale claim detected: issue #%d has no worktree at %s",
                num,
                expected_path,
            )
            claims.append(
                StaleClaim(
                    issue_number=num,
                    issue_title=title,
                    worktree_path=str(expected_path),
                )
            )

    claims.sort(key=lambda c: c.issue_number)
    return claims


# ---------------------------------------------------------------------------
# Out-of-order PR detection
# ---------------------------------------------------------------------------


class PRViolation(BaseModel):
    """A PR whose linked issue belongs to a pipeline phase that is no longer active.

    ``expected_label`` is the currently active agentception/* label.
    ``actual_label``   is the label carried by the issue the PR closes.
    ``linked_issue``   is the issue number parsed from 'Closes #N' in the PR body.
    """

    pr_number: int
    pr_title: str
    expected_label: str
    actual_label: str
    linked_issue: int | None


def _parse_closes(body: str) -> int | None:
    """Return the first issue number from a 'Closes #NNN' reference, or None.

    Only the first reference is returned — a PR closing multiple issues is
    rare in this codebase, and the first reference is always the primary one.
    """
    match = _CLOSES_PATTERN.search(body)
    return int(match.group(1)) if match else None


async def _get_issue_ac_label(issue_number: int) -> str | None:
    """Return the first ``agentception/*`` label of an issue, or None.

    Returns None when the issue cannot be fetched (e.g. deleted or private)
    or when it carries no agentception/* label.
    """
    try:
        issue = await get_issue(issue_number)
    except RuntimeError:
        logger.warning("⚠️  Could not fetch issue #%d for label check", issue_number)
        return None

    labels = issue.get("labels", [])
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, str) and label.startswith("agentception/"):
                return label
    return None


async def detect_out_of_order_prs() -> list[PRViolation]:
    """Detect open PRs whose linked issue belongs to a non-active pipeline phase.

    Algorithm per PR:
    1. Parse ``Closes #NNN`` from the PR body.
    2. Fetch that issue's ``agentception/*`` label from GitHub.
    3. Compare against the currently active label (lowest-numbered open phase).
    4. Mismatch → append a ``PRViolation``.

    PRs with no ``Closes #N`` reference or whose linked issue carries no
    ``agentception/*`` label are silently skipped — they cannot be classified.

    Returns an empty list when there is no active label or no violations.
    """
    active_label = await get_active_label()
    if not active_label:
        logger.debug("🔍 No active label — skipping out-of-order PR check")
        return []

    open_prs = await get_open_prs_with_body()
    violations: list[PRViolation] = []

    for pr in open_prs:
        pr_number = pr.get("number")
        if not isinstance(pr_number, int):
            continue

        pr_title = pr.get("title", "")
        if not isinstance(pr_title, str):
            pr_title = str(pr_title)

        body = pr.get("body", "")
        if not isinstance(body, str):
            body = ""

        linked_issue = _parse_closes(body)
        if linked_issue is None:
            logger.debug("🔍 PR #%d has no 'Closes #N' — skipping", pr_number)
            continue

        actual_label = await _get_issue_ac_label(linked_issue)
        if actual_label is None:
            continue

        if actual_label != active_label:
            logger.warning(
                "⚠️  PR #%d links issue #%d with label %r (active=%r)",
                pr_number,
                linked_issue,
                actual_label,
                active_label,
            )
            violations.append(
                PRViolation(
                    pr_number=pr_number,
                    pr_title=pr_title,
                    expected_label=active_label,
                    actual_label=actual_label,
                    linked_issue=linked_issue,
                )
            )

    return violations
