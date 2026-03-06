from __future__ import annotations

"""Multi-signal PR↔Issue linker with auditable provenance.

Discovers candidate links between pull requests and issues using five
signals (in decreasing confidence order):

1. **Explicit metadata** — PR labels like ``agent:issue-123`` (confidence 100).
2. **Body closes references** — ``Closes/Fixes/Resolves #N`` (confidence 95).
3. **Branch regex** — ``feat/issue-{N}-*`` (confidence 90).
4. **Run pr_number** — an agent run claims this PR (confidence 85).
5. **Title mention** — ``#123`` or ``issue 123`` in PR title (confidence 60).

Each candidate is persisted as a row in ``ac_pr_issue_links`` with method,
confidence, and evidence.  The ``best_pr_for_issue`` function chooses the
canonical PR for each issue using deterministic precedence rules.
"""

import json
import logging
import re
from typing import TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_CLOSES_RE = re.compile(
    r"(?i)(?:closes|fixes|resolves)\s+(?:[\w\-]+/[\w\-]+)?#(\d+)"
)
"""Matches ``Closes #17``, ``fixes owner/repo#123``, ``Resolves #42``."""

_FEAT_ISSUE_BRANCH_RE = re.compile(r"feat/issue-(\d+)")
"""Matches ``feat/issue-17-some-slug`` and extracts the issue number."""

_AGENT_ISSUE_LABEL_RE = re.compile(r"agent:issue-(\d+)")
"""Matches ``agent:issue-123`` label on PRs (explicit metadata signal)."""

_TITLE_ISSUE_RE = re.compile(
    r"(?:^|\W)#(\d+)(?:\W|$)|\bissue[\s-]?(\d+)\b", re.IGNORECASE
)
"""Loose match for ``#123`` or ``issue 123`` / ``issue-123`` in PR title."""


# ---------------------------------------------------------------------------
# Candidate link TypedDict
# ---------------------------------------------------------------------------


class CandidateLink(TypedDict):
    """One candidate PR↔Issue link produced by the linker."""

    repo: str
    pr_number: int
    issue_number: int
    link_method: str
    confidence: int
    evidence_json: str


# ---------------------------------------------------------------------------
# Minimal PR/Run row shapes expected by the linker
# ---------------------------------------------------------------------------


class PRRow(TypedDict):
    """Minimal PR fields needed for link discovery."""

    number: int
    title: str
    head_ref: str | None
    base_ref: str | None
    body: str
    labels: list[str]


class RunRow(TypedDict):
    """Minimal run fields needed for the run_pr_number signal."""

    id: str
    issue_number: int | None
    pr_number: int | None


# ---------------------------------------------------------------------------
# Core link discovery
# ---------------------------------------------------------------------------


def discover_links_for_pr(
    pr: PRRow,
    repo: str,
    runs_by_pr: dict[int, list[RunRow]] | None = None,
) -> list[CandidateLink]:
    """Produce all candidate issue links for a single PR.

    Parameters
    ----------
    pr:
        PR row with at least number, title, head_ref, body, labels.
    repo:
        Repository slug (e.g. ``owner/repo``).
    runs_by_pr:
        Optional lookup: ``{pr_number: [RunRow, ...]}`` for the run_pr_number signal.

    Returns
    -------
    list[CandidateLink]
        Candidate links sorted by confidence descending.
    """
    candidates: list[CandidateLink] = []
    pr_num = pr["number"]

    # Signal 1: explicit metadata labels (agent:issue-123)
    for label in pr["labels"]:
        m = _AGENT_ISSUE_LABEL_RE.match(label)
        if m:
            candidates.append(CandidateLink(
                repo=repo,
                pr_number=pr_num,
                issue_number=int(m.group(1)),
                link_method="explicit",
                confidence=100,
                evidence_json=json.dumps({"label": label}),
            ))

    # Signal 2: body closes references
    body = pr["body"] or ""
    for m in _CLOSES_RE.finditer(body):
        issue_num = int(m.group(1))
        candidates.append(CandidateLink(
            repo=repo,
            pr_number=pr_num,
            issue_number=issue_num,
            link_method="body_closes",
            confidence=95,
            evidence_json=json.dumps({"matched_text": m.group(0).strip()}),
        ))

    # Signal 3: branch regex
    head_ref = pr["head_ref"] or ""
    branch_match = _FEAT_ISSUE_BRANCH_RE.match(head_ref)
    if branch_match:
        issue_num = int(branch_match.group(1))
        candidates.append(CandidateLink(
            repo=repo,
            pr_number=pr_num,
            issue_number=issue_num,
            link_method="branch_regex",
            confidence=90,
            evidence_json=json.dumps({"head_ref": head_ref}),
        ))

    # Signal 4: run pr_number
    if runs_by_pr:
        for run in runs_by_pr.get(pr_num, []):
            if run["issue_number"] is not None:
                candidates.append(CandidateLink(
                    repo=repo,
                    pr_number=pr_num,
                    issue_number=run["issue_number"],
                    link_method="run_pr_number",
                    confidence=85,
                    evidence_json=json.dumps({"run_id": run["id"]}),
                ))

    # Signal 5: title mention
    title = pr["title"] or ""
    for m in _TITLE_ISSUE_RE.finditer(title):
        issue_num = int(m.group(1) or m.group(2))
        already_found = any(c["issue_number"] == issue_num for c in candidates)
        if not already_found:
            candidates.append(CandidateLink(
                repo=repo,
                pr_number=pr_num,
                issue_number=issue_num,
                link_method="title_mention",
                confidence=60,
                evidence_json=json.dumps({"matched_text": m.group(0).strip()}),
            ))

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Best-PR selection for an issue
# ---------------------------------------------------------------------------

_PR_STATE_PRIORITY: dict[str, int] = {
    "open": 0,
    "merged": 1,
    "closed": 2,
    "draft": 0,
    "unknown": 3,
}


class BestPR(TypedDict):
    """The canonical PR associated with an issue after link resolution."""

    pr_number: int
    pr_state: str
    pr_base: str | None
    pr_head_ref: str | None
    link_method: str
    confidence: int


class PRInfo(TypedDict):
    """Minimal PR metadata for best-PR selection."""

    number: int
    state: str
    base_ref: str | None
    head_ref: str | None


def best_pr_for_issue(
    issue_number: int,
    links: list[CandidateLink],
    pr_info: dict[int, PRInfo],
) -> BestPR | None:
    """Choose the best PR for a given issue from candidate links.

    Precedence (highest wins):
    1. PR state: open > merged > closed
    2. Higher confidence link method
    3. Most recently created (highest PR number as proxy)
    """
    issue_links = [l for l in links if l["issue_number"] == issue_number]
    if not issue_links:
        return None

    def sort_key(link: CandidateLink) -> tuple[int, int, int]:
        pr_num = link["pr_number"]
        info = pr_info.get(pr_num)
        state_priority = _PR_STATE_PRIORITY.get(
            info["state"] if info else "unknown", 99
        )
        return (state_priority, -link["confidence"], -pr_num)

    issue_links.sort(key=sort_key)
    best = issue_links[0]
    info = pr_info.get(best["pr_number"])

    return BestPR(
        pr_number=best["pr_number"],
        pr_state=info["state"] if info else "unknown",
        pr_base=info["base_ref"] if info else None,
        pr_head_ref=info["head_ref"] if info else None,
        link_method=best["link_method"],
        confidence=best["confidence"],
    )
