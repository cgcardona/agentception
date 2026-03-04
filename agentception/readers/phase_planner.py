from __future__ import annotations

"""Phase Planner — heuristic analysis of a brain-dump into sequenced phases.

This module implements Step -1 of the coordinator workflow without requiring a
running LLM or network access.  Work items are classified by keyword into four
canonical phases and returned as a ``PlanResult`` that the Brain Dump UI can
show as a confirmation step before the user fires the full coordinator.

Why heuristic instead of LLM?
  The preview must be fast (sub-second) and available even when no API key is
  configured.  LLM quality is reserved for the coordinator agent that creates
  the actual issues — this is only a preview to help the user confirm intent.

Phase definitions (load-bearing order):
  0  Foundation & Critical Bugs  — must land before anyone builds on top
  1  Infrastructure & Core        — APIs, auth, data models that features need
  2  Features & User-Facing Work  — new capabilities, UI enhancements
  3  Polish, Tests & Tech Debt     — cleanup, docs, performance

Empty or whitespace-only dumps raise a ``ValueError``.
"""

import re

from agentception.models import PhasePreview, PlanResult

# ---------------------------------------------------------------------------
# Internal keyword sets — each item is checked (case-insensitive) against the
# lower-cased work-item text.  The first matching set wins.
# ---------------------------------------------------------------------------

_PHASE_DEFS: list[tuple[str, str, list[str]]] = [
    (
        "phase-0",
        "Foundation & Critical Bugs",
        [
            "fix", "bug", "broken", "error", "crash", "fail", "critical",
            "intermittent", "flak", "regression", "broken", "not working",
            "doesn't work", "rate limit", "hang", "hang ", "hangs",
            "reset password", "down", "outage", "security", "vulnerability",
        ],
    ),
    (
        "phase-1",
        "Infrastructure & Core",
        [
            "migrate", "migration", "auth", "jwt", "token", "session",
            "api", "endpoint", "route", "schema", "model", "db ", "database",
            "backend", "config", "infrastructure", "deploy", "docker",
            "env", "environment", "postgres", "redis", "webhook",
        ],
    ),
    (
        "phase-2",
        "Features & User-Facing Work",
        [
            "add", "new", "feature", "implement", "create", "build", "enable",
            "dark mode", "export", "notification", "slack", "pin", "star",
            "favourite", "search", "filter", "pagination", "paginate",
            "user can", "users can", "let user", "allow user",
        ],
    ),
    (
        "phase-3",
        "Polish, Tests & Tech Debt",
        [
            "refactor", "cleanup", "clean up", "remove", "deprecat", "legacy",
            "test", "tests", "doc", "docs", "document", "style", "lint",
            "performance", "perf", "optimis", "optimiz", "consolidat",
            "replace", "migrate.*from", "dead code",
        ],
    ),
]

# Labels for depends_on — each phase N depends on phase N-1.
_PHASE_LABELS = [label for label, _, _ in _PHASE_DEFS]


def _extract_items(dump: str) -> list[str]:
    """Parse a brain-dump string into individual work items.

    Handles common formats:
      - Lines starting with ``-`` or ``*`` (markdown bullets)
      - Numbered lists: ``1.``, ``1)``, ``(1)``
      - Plain non-empty lines as a fallback

    Returns a deduplicated list preserving first-occurrence order.
    Blank lines and pure whitespace are discarded.
    """
    items: list[str] = []
    seen: set[str] = set()

    bullet_re = re.compile(r"^\s*(?:[-*]|\d+[.)]\s*|\(\d+\)\s*)")

    for raw_line in dump.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip leading bullet/number markers.
        cleaned = bullet_re.sub("", line).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            items.append(cleaned)

    return items


def _classify(item: str) -> int:
    """Return the phase index (0–3) for a single work item.

    Iterates phase definitions in order; the first keyword match wins.
    Short keywords (≤ 4 chars) are matched as whole words via ``\b`` anchors
    to avoid false positives like "pin" matching inside "alpine".
    Longer keywords are matched as substrings (they are specific enough to be
    unambiguous, e.g. "refactor", "migrate", "pagination").
    Defaults to phase 2 (Features) when no keywords match — unclassified
    items are most likely feature requests.
    """
    lower = item.lower()
    for idx, (_label, _desc, keywords) in enumerate(_PHASE_DEFS):
        for kw in keywords:
            if len(kw) <= 4:
                if re.search(r"\b" + re.escape(kw) + r"\b", lower):
                    return idx
            elif kw in lower:
                return idx
    return 2  # default: Features


def plan_phases(dump: str) -> PlanResult:
    """Analyse a brain-dump string and return a phased ``PlanResult``.

    Algorithm:
      1. Extract individual work items from the dump.
      2. Classify each item into one of four canonical phases by keyword.
      3. Emit only phases that have at least one item (empty phases are omitted).
      4. Set ``depends_on`` so the phase ordering is expressed in the result.

    Raises
    ------
    ValueError
        When ``dump`` is empty or contains no extractable work items.
    """
    dump = dump.strip()
    if not dump:
        raise ValueError("brain_dump must not be empty")

    items = _extract_items(dump)
    if not items:
        raise ValueError("No extractable work items found in brain_dump")

    # Bucket items by phase.
    buckets: dict[int, list[str]] = {i: [] for i in range(len(_PHASE_DEFS))}
    for item in items:
        phase_idx = _classify(item)
        buckets[phase_idx].append(item)

    # Build phase previews, skipping empty buckets.
    phases: list[PhasePreview] = []
    emitted_labels: list[str] = []  # track which phases were actually emitted

    for idx, (label, description, _keywords) in enumerate(_PHASE_DEFS):
        bucket = buckets[idx]
        if not bucket:
            continue
        # depends_on = all previously emitted phase labels
        depends_on = list(emitted_labels)
        phases.append(
            PhasePreview(
                label=label,
                description=description,
                estimated_issue_count=len(bucket),
                depends_on=depends_on,
            )
        )
        emitted_labels.append(label)

    # If nothing was emitted (pathological input), treat everything as phase-2.
    if not phases:
        phases.append(
            PhasePreview(
                label="phase-2",
                description="Features & User-Facing Work",
                estimated_issue_count=len(items),
                depends_on=[],
            )
        )

    return PlanResult(phases=phases)
