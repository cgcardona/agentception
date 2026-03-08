"""Ticket analyzer for AgentCeption.

Parses an issue body and returns structured recommendations used by the
Eng VP seed loop when generating ``.agent-task`` files.  All heuristics are
intentionally simple and rule-based so that results are deterministic and
testable without a live model call.

Typical usage::

    from agentception.intelligence.analyzer import analyze_issue

    analysis = await analyze_issue(632)
    print(analysis.recommended_role)   # "python-developer"
    print(analysis.parallelism)        # "safe"
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from agentception.readers.github import get_issue_body

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

# Files that are known to conflict when two parallel agents touch them.
# Additions here widen the definition of "high conflict risk".
_HIGH_CONFLICT_FILES: frozenset[str] = frozenset(
    {
        "agentception/app.py",
        "docs/reference/type-contracts.md",
        "alembic/versions/",   # any migration file
    }
)

# Labels in filenames/paths that hint at an alembic migration.
_MIGRATION_PATTERNS: list[str] = ["alembic/versions/", "migration", "alembic"]

# Shared config / protocol files that multiple agents frequently edit.
_SHARED_CONFIG_FILES: frozenset[str] = frozenset(
    {
        "agentception/protocol/events.py",
        "agentception/config.py",
        "agentception/models.py",
    }
)


class IssueAnalysis(BaseModel):
    """Structured analysis of a GitHub issue body.

    Produced by :func:`analyze_issue` and consumed by the Eng VP seed loop
    to determine whether an issue is safe to parallelize and which agent role
    should handle it.

    Fields
    ------
    number:
        GitHub issue number that was analysed.
    dependencies:
        Issue numbers declared in the body via ``Depends on #N`` or
        ``Blocked by #N`` patterns (sorted, deduplicated).
    parallelism:
        Whether this issue can safely run alongside others:
        ``"safe"`` — creates only new files, no shared-file risk.
        ``"risky"`` — modifies at least one shared or additive file.
        ``"serial"`` — body explicitly declares this must run alone.
    conflict_risk:
        Estimated merge-conflict risk:
        ``"none"`` — no known conflict-prone files touched.
        ``"low"`` — touches a shared config but not a high-risk additive file.
        ``"high"`` — touches a high-risk additive file (app.py, type-contracts.md …).
    modifies_files:
        File paths extracted from a ``### Files to Create / Modify`` section.
        Empty list when no such section is present.
    recommended_role:
        Role that should handle this issue:
        ``"database-architect"`` when migrations or Alembic are mentioned.
        ``"python-developer"`` otherwise.
    recommended_merge_after:
        Largest dependency issue number, or ``None`` when there are no deps.
        The Eng VP uses this to defer assignment until the dependency PR merges.
    """

    number: int
    dependencies: list[int]
    parallelism: Literal["safe", "risky", "serial"]
    conflict_risk: Literal["none", "low", "high"]
    modifies_files: list[str]
    recommended_role: Literal["python-developer", "database-architect"]
    recommended_merge_after: int | None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def analyze_issue(number: int) -> IssueAnalysis:
    """Fetch and analyse a GitHub issue, returning structured recommendations.

    Calls the GitHub API once via :func:`~agentception.readers.github.get_issue_body`,
    then applies all local heuristics synchronously.  The result is fully
    deterministic for a given issue body — no model calls are made.

    Parameters
    ----------
    number:
        GitHub issue number to analyse.

    Returns
    -------
    IssueAnalysis
        Parsed recommendations.  Falls back to safe defaults when the body
        is empty or malformed.

    Raises
    ------
    RuntimeError
        When the GitHub CLI subprocess exits with a non-zero status.
    """
    body = await get_issue_body(number)
    return _analyze_body(number, body)


# ---------------------------------------------------------------------------
# Helpers (sync — pure functions, fully testable without async)
# ---------------------------------------------------------------------------


def _analyze_body(number: int, body: str) -> IssueAnalysis:
    """Apply all analysis heuristics to a raw issue body string.

    Extracted from :func:`analyze_issue` so unit tests can call it
    synchronously without an event loop.
    """
    deps = parse_deps_from_body(body)
    files = extract_modified_files(body)
    role = infer_role(body, files)
    parallelism = infer_parallelism(body, files)
    conflict = infer_conflict_risk(files)
    merge_after = max(deps) if deps else None
    return IssueAnalysis(
        number=number,
        dependencies=sorted(set(deps)),
        parallelism=parallelism,
        conflict_risk=conflict,
        modifies_files=files,
        recommended_role=role,
        recommended_merge_after=merge_after,
    )


def parse_deps_from_body(body: str) -> list[int]:
    """Extract dependency issue numbers from an issue body.

    Recognises patterns such as:
    - ``Depends on #614``
    - ``Blocked by #614, #615``
    - ``Requires #614``
    - ``**Depends on #614**``

    Parameters
    ----------
    body:
        Raw Markdown body of the issue.

    Returns
    -------
    list[int]
        Sorted, deduplicated list of issue numbers.
    """
    # Match the keyword, then collect ALL #N references until the next
    # sentence-ending period or newline. Using a non-greedy group for the
    # text before the first # avoids the greedy-consumption bug where
    # [^.\n]* gobbles up the issue numbers before the capturing group sees them.
    keyword_pattern = re.compile(
        r"(?:depends\s+on|blocked\s+by|requires)[^.\n]*",
        re.IGNORECASE,
    )
    found: set[int] = set()
    for match in keyword_pattern.finditer(body):
        for num_match in re.finditer(r"#(\d+)", match.group(0)):
            found.add(int(num_match.group(1)))
    return sorted(found)


def extract_modified_files(body: str) -> list[str]:
    """Parse the ``### Files to Create / Modify`` section of an issue body.

    Looks for a markdown heading that contains "Files" (case-insensitive) and
    collects every list item (``-`` or ``*`` prefix, with optional backticks)
    that appears before the next heading or end-of-string.

    Parameters
    ----------
    body:
        Raw Markdown body.

    Returns
    -------
    list[str]
        Ordered list of file paths exactly as written in the issue body.
        Empty list when no such section is found.
    """
    # Find the start of a "Files" heading.
    section_match = re.search(r"^#{1,4}\s+.*[Ff]iles.*$", body, re.MULTILINE)
    if not section_match:
        return []

    rest = body[section_match.end():]
    # Collect until the next heading.
    next_heading = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
    section_text = rest[: next_heading.start()] if next_heading else rest

    files: list[str] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        # Prefer extracting a backtick-quoted path (handles "- `path/to/file.py` (new)").
        backtick_match = re.search(r"`([^`]+)`", line)
        if backtick_match:
            raw = backtick_match.group(1).strip()
        else:
            # Fall back: strip bullet, spaces, and trailing annotations like "(new)".
            raw = line.lstrip("-*").strip()
            raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
        # Discard empty lines or prose lines (heuristic: must contain a dot or slash).
        if raw and ("/" in raw or "." in raw):
            files.append(raw)
    return files


def infer_role(body: str, files: list[str]) -> Literal["python-developer", "database-architect"]:
    """Recommend an engineer role based on the issue body and file list.

    Heuristics (applied in order, first match wins):
    1. If any file path contains ``alembic`` or ``migration`` → ``"database-architect"``.
    2. If the body mentions Alembic, migration, or SQL schema keywords → ``"database-architect"``.
    3. Otherwise → ``"python-developer"``.

    Parameters
    ----------
    body:
        Raw Markdown body.
    files:
        File paths extracted by :func:`extract_modified_files`.

    Returns
    -------
    Literal["python-developer", "database-architect"]
        One of ``"python-developer"`` or ``"database-architect"``.
    """
    for path in files:
        if any(pat in path for pat in _MIGRATION_PATTERNS):
            return "database-architect"

    db_pattern = re.compile(
        r"\b(alembic|migration|migrate|sqlalchemy|db\.model|postgres)\b",
        re.IGNORECASE,
    )
    if db_pattern.search(body):
        return "database-architect"

    return "python-developer"


def infer_parallelism(body: str, files: list[str]) -> Literal["safe", "risky", "serial"]:
    """Determine whether this issue is safe to run in parallel with others.

    Rules (applied in order, first match wins):
    1. Body contains an explicit serial marker (``must run alone``, ``serial``,
       ``do not parallelize``) → ``"serial"``.
    2. Any file in *files* matches a known high-conflict or shared-config path → ``"risky"``.
    3. All files are new-file-only (body says ``(new)`` next to every listed path) → ``"safe"``.
    4. Otherwise → ``"safe"`` (files not in the known-conflict set are assumed safe).

    Parameters
    ----------
    body:
        Raw Markdown body.
    files:
        File paths extracted by :func:`extract_modified_files`.

    Returns
    -------
    Literal["safe", "risky", "serial"]
    """
    serial_pattern = re.compile(
        r"\b(must\s+run\s+alone|do\s+not\s+parallelize|serial\s+only|run\s+serially)\b",
        re.IGNORECASE,
    )
    if serial_pattern.search(body):
        return "serial"

    for path in files:
        for conflict_path in _HIGH_CONFLICT_FILES:
            if conflict_path in path or path in conflict_path:
                return "risky"
        for shared_path in _SHARED_CONFIG_FILES:
            if shared_path in path or path in shared_path:
                return "risky"

    if not files:
        # No file list — assume safe (new files are always safe).
        return "safe"

    # If the body explicitly labels all files as "(new)" → safe.
    new_file_mentions = sum(
        1 for path in files
        if re.search(
            re.escape(path) + r"\s*\(new\)",
            body,
            re.IGNORECASE,
        )
    )
    if new_file_mentions == len(files):
        return "safe"

    # Files are present but not all marked "(new)" — still safe because they
    # cleared the high-conflict and shared-config checks above.
    return "safe"


def infer_conflict_risk(files: list[str]) -> Literal["none", "low", "high"]:
    """Estimate the merge-conflict risk for this issue given its file list.

    Levels:
    - ``"high"`` — at least one file matches a known high-conflict additive path.
    - ``"low"`` — at least one file matches a shared-config path but none match
      the high-conflict set.
    - ``"none"`` — no known conflict-prone files touched.

    Parameters
    ----------
    files:
        File paths extracted by :func:`extract_modified_files`.

    Returns
    -------
    Literal["none", "low", "high"]
    """
    has_high = False
    has_low = False

    for path in files:
        for conflict_path in _HIGH_CONFLICT_FILES:
            if conflict_path in path or path in conflict_path:
                has_high = True
                break
        for shared_path in _SHARED_CONFIG_FILES:
            if shared_path in path or path in shared_path:
                has_low = True
                break

    if has_high:
        return "high"
    if has_low:
        return "low"
    return "none"
