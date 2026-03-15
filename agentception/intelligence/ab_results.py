from __future__ import annotations

"""A/B results dashboard computation for AgentCeption (AC-505).

Aggregates pipeline wave data and correlates each wave with an A/B role
variant (A = even-second BATCH_ID, B = odd-second BATCH_ID).  For each
variant, computes outcome metrics: PRs opened, PRs merged, average reviewer
grade, and merge rate.  This enables the Engineering VP to compare two role
prompt variants against hard outcome signals rather than impressions.

The grade is extracted from merged-PR body text and PR review comments using
a regex that matches patterns like "Grade: `A`" or "Grade: A".  When no grade
can be parsed, ``avg_grade`` is ``None``.

Typical call site (route handler)::

    from agentception.intelligence.ab_results import compute_ab_results

    variant_a, variant_b = await compute_ab_results()
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel

from agentception.intelligence.ab_mode import _is_even_batch
from agentception.types import JsonValue
from agentception.intelligence.role_versions import read_role_versions
from agentception.readers.github import get_merged_prs, get_pr_comments
from agentception.telemetry import aggregate_waves

logger = logging.getLogger(__name__)

# Matches patterns like "Grade: `A`", "Grade: A", "Grade B" in reviewer comments.
_GRADE_RE = re.compile(r"\bGrade[:\s]+[`*]?([A-F])[`*]?", re.IGNORECASE)

# Numeric mapping used to average letter grades.
_GRADE_TO_NUM: dict[str, int] = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}
_NUM_TO_GRADE: dict[int, str] = {v: k for k, v in _GRADE_TO_NUM.items()}


class ABVariantResult(BaseModel):
    """Outcome metrics for one A/B role variant across all applicable batches.

    ``variant`` is ``"A"`` (even-second BATCH_ID) or ``"B"`` (odd-second).
    ``role_sha`` is the git SHA of the role file version active during these
    batches, sourced from ``role-versions.json``; empty string when unknown.
    ``avg_grade`` is the mean letter grade across all reviewer-graded PRs in
    this variant; ``None`` when no graded PRs are found.
    ``merge_rate`` is ``prs_merged / prs_opened``; ``0.0`` when ``prs_opened``
    is zero to avoid division-by-zero.
    """

    variant: Literal["A", "B"]
    role_sha: str
    batch_ids: list[str]
    prs_opened: int
    prs_merged: int
    avg_grade: str | None
    merge_rate: float


def _extract_grade(text: str) -> str | None:
    """Return the first letter grade A–F found in ``text``, or ``None``.

    Recognises patterns produced by the QA reviewer agent:
    ``Grade: `A```, ``Grade: A``, ``Grade A``, case-insensitively.
    """
    match = _GRADE_RE.search(text)
    return match.group(1).upper() if match else None


def _average_grade(grades: list[str]) -> str | None:
    """Convert letter grades to a numeric average and back.

    Returns ``None`` for an empty input list.  Uses a 4-point scale:
    A=4, B=3, C=2, D=1, F=0.  The averaged value is rounded to the nearest
    integer before converting back to a letter so ``["A", "B"]`` → ``"B"``
    (average 3.5, rounds to 4 → ``"A"`` with Python ``round`` banker's
    rounding; kept as-is — both outcomes are valid approximations).
    """
    if not grades:
        return None
    nums = [_GRADE_TO_NUM.get(g.upper(), 0) for g in grades]
    return _NUM_TO_GRADE.get(round(sum(nums) / len(nums)), "F")


async def _fetch_grade_for_pr(pr: dict[str, JsonValue]) -> str | None:
    """Try to extract a reviewer grade for the given PR.

    First checks the PR body, then falls back to fetching PR comments.
    Returns ``None`` when no grade pattern is found in either place.
    """
    body = str(pr.get("body") or "")
    grade = _extract_grade(body)
    if grade:
        return grade

    pr_number = pr.get("number")
    if not isinstance(pr_number, int):
        return None

    try:
        comments = await get_pr_comments(pr_number)
        for comment in comments:
            grade = _extract_grade(comment)
            if grade:
                return grade
    except Exception as exc:
        logger.debug("⚠️  Could not fetch comments for PR #%s: %s", pr_number, exc)

    return None


async def compute_ab_results() -> tuple[ABVariantResult, ABVariantResult]:
    """Return ``(variant_a, variant_b)`` outcome metrics.

    Aggregates all recorded waves, assigns each to variant A or B based on
    the parity of the BATCH_ID timestamp's seconds component, then correlates
    with merged-PR data from GitHub to compute per-variant outcome metrics.

    Waves whose BATCH_ID cannot be parsed are silently skipped — they cannot
    be attributed to either variant and should not pollute the comparison.

    GitHub calls (merged PRs, PR comments) are attempted on a best-effort
    basis: failures are logged at WARNING level and result in zero merged PRs /
    no grades rather than a hard error so the dashboard always loads.
    """
    # ── Role SHA attribution ───────────────────────────────────────────────
    versions_data = await read_role_versions()
    ab_cfg = versions_data.get("ab_mode")
    variant_a_sha = ""
    variant_b_sha = ""
    if isinstance(ab_cfg, dict):
        raw_a = ab_cfg.get("variant_a_sha")
        raw_b = ab_cfg.get("variant_b_sha")
        variant_a_sha = str(raw_a) if raw_a else ""
        variant_b_sha = str(raw_b) if raw_b else ""

    # ── Wave aggregation ───────────────────────────────────────────────────
    waves = await aggregate_waves()

    a_batch_ids: list[str] = []
    b_batch_ids: list[str] = []
    a_prs_opened = 0
    b_prs_opened = 0
    a_issues: set[int] = set()
    b_issues: set[int] = set()

    for wave in waves:
        is_even = _is_even_batch(wave.batch_id)
        if is_even is None:
            # Unparseable batch_id — cannot assign variant; skip.
            logger.debug("⚠️  Skipping wave with unparseable batch_id: %r", wave.batch_id)
            continue
        if is_even:
            a_batch_ids.append(wave.batch_id)
            a_prs_opened += wave.prs_opened
            a_issues.update(wave.issues_worked)
        else:
            b_batch_ids.append(wave.batch_id)
            b_prs_opened += wave.prs_opened
            b_issues.update(wave.issues_worked)

    # ── Merged-PR correlation ──────────────────────────────────────────────
    # Match merged PRs to variants via the issue number embedded in the branch
    # name (``feat/issue-NNN``).  Grade is extracted from PR body + comments.
    a_merged = 0
    b_merged = 0
    a_grades: list[str] = []
    b_grades: list[str] = []

    try:
        merged_prs = await get_merged_prs()
        for pr in merged_prs:
            branch = str(pr.get("headRefName") or "")
            issue_match = re.search(r"issue-(\d+)", branch)
            if not issue_match:
                continue
            issue_num = int(issue_match.group(1))

            if issue_num in a_issues:
                a_merged += 1
                grade = await _fetch_grade_for_pr(pr)
                if grade:
                    a_grades.append(grade)
            elif issue_num in b_issues:
                b_merged += 1
                grade = await _fetch_grade_for_pr(pr)
                if grade:
                    b_grades.append(grade)
    except Exception as exc:
        logger.warning("⚠️  Could not fetch merged PRs for A/B results: %s", exc)

    # ── Assemble results ───────────────────────────────────────────────────
    return (
        ABVariantResult(
            variant="A",
            role_sha=variant_a_sha,
            batch_ids=a_batch_ids,
            prs_opened=a_prs_opened,
            prs_merged=a_merged,
            avg_grade=_average_grade(a_grades),
            merge_rate=a_merged / a_prs_opened if a_prs_opened > 0 else 0.0,
        ),
        ABVariantResult(
            variant="B",
            role_sha=variant_b_sha,
            batch_ids=b_batch_ids,
            prs_opened=b_prs_opened,
            prs_merged=b_merged,
            avg_grade=_average_grade(b_grades),
            merge_rate=b_merged / b_prs_opened if b_prs_opened > 0 else 0.0,
        ),
    )
