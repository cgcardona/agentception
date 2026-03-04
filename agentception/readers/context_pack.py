"""Lightweight GitHub context pack for plan generation (Step 1.A).

Fetches just enough repository context to help Claude avoid duplicating
existing work and reuse existing labels — without vector search or RAG.

Data fetched (all via existing ``gh`` CLI calls):
  - Repo name and slug (from config)
  - All existing label names (so Claude reuses them)
  - Up to 25 open issue titles (so Claude doesn't recreate existing work)
  - Up to 10 recently merged PR titles (for recent delivery context)

This is intentionally thin: the goal is signal about what already exists,
not full-text retrieval.  The entire pack is injected as a short section
at the top of the user prompt before passing to the LLM.

All fetches are best-effort — a GitHub error returns an empty pack rather
than failing the plan request.
"""
from __future__ import annotations

import logging

from agentception.config import settings as _cfg

logger = logging.getLogger(__name__)

_MAX_OPEN_ISSUES = 25
_MAX_MERGED_PRS = 10


async def build_context_pack() -> str:
    """Fetch repo context and return a formatted string to prepend to the plan prompt.

    Returns an empty string if the repo cannot be reached or all fetches fail,
    so plan generation degrades gracefully without breaking the request.
    """
    from agentception.readers.github import (
        get_open_issues,
        get_merged_prs,
        gh_json,
    )

    repo = _cfg.gh_repo
    sections: list[str] = [f"## Repository context: {repo}\n"]

    # ── Labels ───────────────────────────────────────────────────────────────
    try:
        raw_labels = await gh_json(
            ["label", "list", "--repo", repo, "--json", "name", "--limit", "100"],
            "[.[].name]",
            "context_pack:labels",
        )
        if isinstance(raw_labels, list):
            label_names = [str(n) for n in raw_labels if isinstance(n, str)]
            if label_names:
                sections.append(
                    "### Existing labels (reuse these — do not invent new ones)\n"
                    + ", ".join(label_names)
                    + "\n"
                )
    except Exception as exc:
        logger.warning("⚠️ context_pack: could not fetch labels: %s", exc)

    # ── Open issues ───────────────────────────────────────────────────────────
    try:
        open_issues = await get_open_issues()
        if open_issues:
            titles = [
                f"- #{i.get('number', '?')} {str(i.get('title', '')).strip()}"
                for i in open_issues[:_MAX_OPEN_ISSUES]
                if isinstance(i, dict)
            ]
            if titles:
                sections.append(
                    f"### Open issues (do not duplicate these — {len(open_issues)} total, showing {len(titles)})\n"
                    + "\n".join(titles)
                    + "\n"
                )
    except Exception as exc:
        logger.warning("⚠️ context_pack: could not fetch open issues: %s", exc)

    # ── Recent merged PRs ─────────────────────────────────────────────────────
    try:
        merged_prs = await get_merged_prs()
        if merged_prs:
            pr_lines = [
                f"- #{p.get('number', '?')} {str(p.get('title', '')).strip()}"
                for p in merged_prs[:_MAX_MERGED_PRS]
                if isinstance(p, dict)
            ]
            if pr_lines:
                sections.append(
                    "### Recently merged PRs (for delivery context)\n"
                    + "\n".join(pr_lines)
                    + "\n"
                )
    except Exception as exc:
        logger.warning("⚠️ context_pack: could not fetch merged PRs: %s", exc)

    if len(sections) == 1:
        # Only the header — nothing fetched successfully.
        return ""

    return "\n".join(sections)
