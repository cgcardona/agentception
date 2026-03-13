from __future__ import annotations

"""Enrich a PlanSpec with codebase context from the semantic search index.

Called after Phase 1A LLM generation and before Phase 1B review.  Appends
a '## Relevant codebase locations' section to each PlanIssue body so that
developer agents have file/line grounding without needing to guess.
"""

import asyncio
import logging
import re

from agentception.models import PlanIssue, PlanSpec
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


async def _enrich_issue(issue: PlanIssue) -> None:
    """Append a codebase locations section to issue.body if search returns results."""
    try:
        results: list[SearchMatch] = await search_codebase(issue.title, n_results=5)
    except Exception:
        logger.debug("⚠️ plan_enricher: search failed for %r, skipping", issue.title)
        return
    if not results:
        return
    lines = ["\n\n## Relevant codebase locations"]
    for match in results:
        lines.append(_format_location(match))
    issue.body += "\n".join(lines)


async def enrich_plan_with_codebase_context(spec: PlanSpec) -> PlanSpec:
    """Enrich every PlanIssue in spec with semantic codebase search results.

    Runs all enrichments concurrently via asyncio.gather.  Individual
    enrichment failures are swallowed — this function never raises.
    """
    all_issues: list[PlanIssue] = [
        issue for phase in spec.phases for issue in phase.issues
    ]
    await asyncio.gather(
        *[_enrich_issue(issue) for issue in all_issues],
        return_exceptions=True,
    )
    return spec
