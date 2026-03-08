from __future__ import annotations

"""MCP tool: plan_advance_phase — atomically gate a phase transition on GitHub.

Validates that all issues in *from_phase* for the given *initiative* are
closed, then unlocks all issues in *to_phase* for that same initiative by
removing the configured blocked label and adding the configured active label.

Label names are read from ``pipeline-config.json`` so they remain configurable
without a code change:

- ``phase_advance_blocked_label`` (default ``"pipeline/gated"``) — removed from
  to-phase issues that were waiting on the previous phase.
- ``phase_advance_active_label`` (default ``"pipeline/active"``) — added to
  to-phase issues to signal they are ready for dispatch.

This function is async and must be called from the ``call_tool_async``
dispatcher, never from the synchronous ``call_tool`` path.
"""

import asyncio
import logging

from agentception.config import settings
from agentception.readers.github import (
    add_label_to_issue,
    gh_json,
    remove_label_from_issue,
)
from agentception.readers.pipeline_config import read_pipeline_config

logger = logging.getLogger(__name__)


async def plan_advance_phase(
    initiative: str,
    from_phase: str,
    to_phase: str,
) -> dict[str, object]:
    """Advance a phase gate by unlocking all *to_phase* issues.

    Steps:

    1. Fetch all GitHub issues labelled with both *from_phase* **and**
       *initiative*.
    2. If any fetched issue is still open, return a structured error containing
       the open issue numbers — no labels are mutated.
    3. Fetch all GitHub issues labelled with both *to_phase* **and**
       *initiative*.
    4. For each *to_phase* issue: remove ``phase_advance_blocked_label`` and
       add ``phase_advance_active_label`` (both read from
       ``pipeline-config.json``).
    5. Return ``{"advanced": True, "unlocked_count": N}``.

    Args:
        initiative: The initiative label shared by all phase issues
                    (e.g. ``"agentception-ux-phase1b-to-phase3"``).
        from_phase: The phase label that must be fully closed before advancing
                    (e.g. ``"phase-1"``).
        to_phase:   The phase label whose issues become active on success
                    (e.g. ``"phase-2"``).

    Returns:
        On success: ``{"advanced": True, "unlocked_count": int}``
        On gate blocked: ``{"advanced": False, "error": str,
        "open_issues": list[int]}``
    """
    config = await read_pipeline_config()
    blocked_label = config.phase_advance_blocked_label
    active_label = config.phase_advance_active_label

    repo = settings.gh_repo

    # 1. Fetch all from_phase + initiative issues (all states).
    from_phase_issues = await _fetch_issues_with_labels(
        repo, [from_phase, initiative]
    )

    # 2. Gate check — all from_phase issues must be closed.
    open_issues: list[int] = []
    for issue in from_phase_issues:
        if issue.get("state") == "OPEN":
            num = issue.get("number")
            if isinstance(num, int):
                open_issues.append(num)
    if open_issues:
        logger.warning(
            "⚠️ plan_advance_phase: gate blocked — %d open issue(s) in %r: %s",
            len(open_issues),
            from_phase,
            open_issues,
        )
        return {
            "advanced": False,
            "error": (
                f"Cannot advance: {len(open_issues)} open issue(s) remain in "
                f"phase {from_phase!r} for initiative {initiative!r}."
            ),
            "open_issues": open_issues,
        }

    # 3. Fetch all to_phase + initiative issues.
    to_phase_issues = await _fetch_issues_with_labels(
        repo, [to_phase, initiative]
    )
    if not to_phase_issues:
        logger.info(
            "✅ plan_advance_phase: no to_phase issues to unlock for %r / %r",
            initiative,
            to_phase,
        )
        return {"advanced": True, "unlocked_count": 0}

    # 4. Unlock each to_phase issue concurrently.
    issue_numbers: list[int] = []
    for issue in to_phase_issues:
        num = issue.get("number")
        if isinstance(num, int):
            issue_numbers.append(num)
    unlock_tasks = [
        _unlock_issue(n, blocked_label, active_label) for n in issue_numbers
    ]
    await asyncio.gather(*unlock_tasks)

    unlocked_count = len(unlock_tasks)
    logger.info(
        "✅ plan_advance_phase: advanced %r → %r; unlocked %d issue(s)",
        from_phase,
        to_phase,
        unlocked_count,
    )
    return {"advanced": True, "unlocked_count": unlocked_count}


async def _fetch_issues_with_labels(
    repo: str, labels: list[str]
) -> list[dict[str, object]]:
    """Fetch GitHub issues that carry every label in *labels*.

    ``gh issue list --label`` ANDs multiple ``--label`` flags — only issues
    with all of the given labels are returned.

    Args:
        repo:   GitHub repository in ``owner/name`` format.
        labels: Label names that must all be present on matching issues.

    Returns:
        List of issue dicts; each has at minimum ``number`` (int) and
        ``state`` (``"OPEN"`` or ``"CLOSED"``).

    Raises:
        RuntimeError: When ``gh`` exits with a non-zero status.
    """
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--json", "number,state",
        "--limit", "200",
    ]
    for label in labels:
        args += ["--label", label]

    cache_key = f"plan_advance_phase:labels={'|'.join(sorted(labels))}"
    result = await gh_json(args, ".", cache_key)
    if not isinstance(result, list):
        raise RuntimeError(
            "_fetch_issues_with_labels: expected list from gh, "
            f"got {type(result).__name__}"
        )
    return [item for item in result if isinstance(item, dict)]


async def _unlock_issue(
    issue_number: int,
    blocked_label: str,
    active_label: str,
) -> None:
    """Remove *blocked_label* and add *active_label* on a single issue.

    Label removal is best-effort: if the issue does not carry *blocked_label*,
    the ``remove_label_from_issue`` call logs a debug message and returns
    without raising.  The active label is always applied.

    Args:
        issue_number:  GitHub issue number.
        blocked_label: Label name to remove (e.g. ``"pipeline/gated"``).
        active_label:  Label name to add (e.g. ``"pipeline/active"``).
    """
    await remove_label_from_issue(issue_number, blocked_label)
    await add_label_to_issue(issue_number, active_label)
    logger.info(
        "✅ _unlock_issue: #%d — removed %r, added %r",
        issue_number,
        blocked_label,
        active_label,
    )
