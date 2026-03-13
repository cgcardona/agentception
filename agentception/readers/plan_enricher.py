from __future__ import annotations

"""Enrich a PlanSpec with codebase context from the semantic search index.

Called after Phase 1A LLM generation and before Phase 1B review.  Appends
a '## Relevant codebase locations' section to each PlanIssue body so that
developer agents have file/line grounding without needing to guess.

After enrichment, detects within-phase file contention: two issues in the
same phase whose search results share a file path.  The lexicographically
smaller issue ID is automatically added to the larger ID's depends_on list
so agents are serialized and do not produce merge conflicts.
"""

import asyncio
import logging
import re

from agentception.models import PlanIssue, PlanPhase, PlanSpec
from agentception.services.code_indexer import SearchMatch, search_codebase

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^#\s+(?:def|class)\s+(\w+)")


def _symbol_name(match: SearchMatch) -> str:
    """Extract the first def/class name from a chunk, or fall back to file path."""
    for line in match["chunk"].splitlines():
        m = _SYMBOL_RE.match(line)
        if m:
            return m.group(1)
    return match["file"]


def _format_location(match: SearchMatch) -> str:
    symbol = _symbol_name(match)
    return (
        f"- {match['file']} "
        f"lines {match['start_line']}-{match['end_line']} "
        f"\u2014 {symbol}"
    )


async def _enrich_issue(issue: PlanIssue) -> list[SearchMatch]:
    """Append a codebase locations section to issue.body; return matches found."""
    try:
        results: list[SearchMatch] = await search_codebase(issue.title, n_results=5)
    except Exception:
        logger.debug("\u26a0\ufe0f plan_enricher: search failed for %r, skipping", issue.title)
        return []
    if not results:
        return []
    lines = ["\n\n## Relevant codebase locations"]
    for match in results:
        lines.append(_format_location(match))
    issue.body += "\n".join(lines)
    return results


def _resolve_phase_contention(phase: PlanPhase, matches_by_id: dict[str, list[SearchMatch]]) -> None:
    """Inject depends_on edges for issues that share files within *phase*.

    Algorithm: for every pair (a, b) where file_set[a] & file_set[b] is
    non-empty, sort the two IDs lexicographically.  The smaller ID is the
    "first"; append it to the larger ID's depends_on list if not already
    present.  Self-references and duplicates are never added.
    """
    ids: list[str] = [issue.id for issue in phase.issues]
    file_sets: dict[str, set[str]] = {
        issue.id: {m["file"] for m in matches_by_id.get(issue.id, [])}
        for issue in phase.issues
    }
    issue_by_id: dict[str, PlanIssue] = {issue.id: issue for issue in phase.issues}

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if not file_sets[a] & file_sets[b]:
                continue
            first, second = (a, b) if a < b else (b, a)
            target = issue_by_id[second]
            if first not in target.depends_on:
                target.depends_on.append(first)


async def enrich_plan_with_codebase_context(spec: PlanSpec) -> PlanSpec:
    """Enrich every PlanIssue in spec with semantic codebase search results.

    Runs all enrichments concurrently via asyncio.gather.  Individual
    enrichment failures are swallowed — this function never raises.

    After enrichment, detects within-phase file contention and injects
    depends_on edges so agents touching the same files are serialized.
    Cross-phase contention is left to the planner.
    """
    all_issues: list[PlanIssue] = [
        issue for phase in spec.phases for issue in phase.issues
    ]
    raw_results: list[list[SearchMatch] | BaseException] = list(
        await asyncio.gather(
            *[_enrich_issue(issue) for issue in all_issues],
            return_exceptions=True,
        )
    )

    matches_by_id: dict[str, list[SearchMatch]] = {}
    for issue, result in zip(all_issues, raw_results):
        if isinstance(result, list):
            matches_by_id[issue.id] = result

    for phase in spec.phases:
        _resolve_phase_contention(phase, matches_by_id)

    return spec
