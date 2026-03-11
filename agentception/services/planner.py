from __future__ import annotations

"""Planner service — converts a GitHub issue into an immutable ExecutionPlan.

Pipeline
--------
1. **Discovery** — Qdrant semantic search against the main ``code`` collection
   surfaces the most relevant files.  Any file paths explicitly named in the
   issue body (seed paths) are merged in and prioritised.

2. **Full file reads** — each discovered file is read in full from the
   worktree.  Chunks from Qdrant are only used for discovery; the planner
   always receives complete file content so it can emit verbatim
   ``old_string`` / ``after`` anchors that match the real file byte-for-byte.

3. **Plan generation** — a single LLM call receives the issue text plus all
   file contents and returns a JSON ``ExecutionPlan``.  The system prompt
   instils a shortest-path / minimal-change mental model so the planner emits
   only what the issue explicitly requests.

Separation of concerns
-----------------------
Planner  → creative reasoning (reads files, infers context, generates plan)
Executor → mechanical determinism (applies plan, no codebase access)
"""

import json
import logging
from pathlib import Path

from agentception.models import ExecutionPlan, PlanOperation
from agentception.services.code_indexer import search_codebase
from agentception.services.llm import call_anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters of file content injected per file.  75 000 chars ≈ 3 000
# lines — covers the largest files in this codebase (e.g. mcp/server.py at
# ~1 600 lines / 63 K chars) in full without truncation.
_FILE_CHAR_LIMIT: int = 75_000

# Maximum files to inject into the planner prompt.  Qdrant results beyond
# this cap are discarded to keep the prompt within a sane token budget.
_MAX_FILES: int = 6

# Number of Qdrant results to request for file discovery.  Fetching slightly
# more than _MAX_FILES lets us filter out non-existent files and still fill
# the cap.
_DISCOVERY_SEARCH_RESULTS: int = 10

# Hard cap on operations in one plan.  50 supports large refactors such as
# eliminating all cast() calls across a single file (up to ~40 sites).
_MAX_OPERATIONS: int = 50

# ---------------------------------------------------------------------------
# Structured-output JSON schema for ExecutionPlan
# ---------------------------------------------------------------------------
# Passed to call_anthropic via the structured-outputs-2025-11-13 beta so the
# model is *guaranteed* to emit valid JSON matching this schema — no prose
# preamble, no markdown fences, no format errors.

_EXECUTION_PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": ["replace_in_file", "insert_after_in_file", "write_file"],
                    },
                    "file": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "after": {"type": "string"},
                    "text": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["tool", "file"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["operations"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM_PROMPT = """\
You are a minimal-change planning agent.

## Mental model — shortest path

You find the shortest path from the current codebase state to the
spec-compliant state described by the issue, and then you stop.

Every operation you emit must be provably required by the issue. An
operation you cannot derive directly from the issue text is not on the
shortest path and must be omitted. You do not improve surrounding code. You
do not add what is not asked. Before emitting each operation, ask yourself:
if I remove this operation, does the plan still satisfy the issue? If yes,
remove it. The measure of a correct plan is its minimality.

## Rules

- Implement ONLY what the issue explicitly requests.
- Do not add validators, docstrings, or extra tests unless the issue says so.
- Do not create new files unless the issue says to create them.
- Do not improve or refactor surrounding code.
- Each operation maps to one tool call. Parameters must be verbatim — the
  executor cannot read files, so every string you emit must appear exactly
  in the pre-loaded file content below.
- old_string must be unique within the file so the replacement is
  unambiguous.
- **Verify every identifier against the pre-loaded files.** When the issue
  names a class attribute, database column, function, or method (e.g.
  "return the task_context field"), search the pre-loaded file contents for
  that exact name. If it does not appear, find the correct name in the
  pre-loaded files and use that instead. Never trust the issue spec for
  attribute or field names — always use what the code actually defines.

## Output format

Output ONLY a JSON object with this exact schema — no markdown fences, no
surrounding text, no explanation:

{
  "operations": [
    {
      "tool": "replace_in_file",
      "file": "relative/path/to/file.py",
      "old_string": "exact existing text to replace",
      "new_string": "exact replacement text"
    },
    {
      "tool": "insert_after_in_file",
      "file": "relative/path/to/file.py",
      "after": "exact line to insert after",
      "text": "text to insert"
    },
    {
      "tool": "write_file",
      "file": "relative/path/to/new_file.py",
      "content": "complete file content"
    }
  ]
}

Prefer replace_in_file for edits to existing files — it is the most
precise operation and easiest for the executor to apply correctly.
Use insert_after_in_file when appending after a known anchor line.
Use write_file only when creating a brand-new file.
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _discover_files(
    query: str,
    seed_paths: list[str],
    worktree_path: Path,
    run_id: str,
) -> list[str]:
    """Return the ordered list of files the planner should read.

    Strategy
    --------
    1. Start with *seed_paths* — files explicitly named in the issue body or
       supplied by the caller.  These are always included and kept in front.
    2. Run a semantic search against the main Qdrant ``code`` collection to
       find additional relevant files.  The worktree is freshly created from
       ``origin/dev``, so the main collection's content is identical.
    3. Filter search results to files that actually exist in the worktree (a
       file mentioned by Qdrant but absent on disk would cause a read error).
    4. Return the union of seed paths and discovered paths, seed paths first,
       capped at ``_MAX_FILES``.

    Qdrant unavailability is treated as a non-fatal warning — the planner
    falls back to seed paths only, which is still better than nothing.
    """
    # Use a dict to preserve insertion order while deduplicating.
    discovered: dict[str, None] = {p: None for p in seed_paths}

    try:
        matches = await search_codebase(query, n_results=_DISCOVERY_SEARCH_RESULTS)
        for match in matches:
            file_path = match["file"]
            # Only include files that exist in the worktree — Qdrant may
            # reference paths from a slightly different repo state.
            if (worktree_path / file_path).exists():
                discovered[file_path] = None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ planner: Qdrant discovery failed for run_id=%s — %s (seed paths only)",
            run_id,
            exc,
        )

    # Always include db/models.py when any db/ file is present — it is the
    # ground truth for column names and must be visible to the planner so it
    # never uses a field name from the issue spec that doesn't exist in code.
    db_models = "agentception/db/models.py"
    has_db_file = any("db/" in p or "models" in p for p in discovered)
    if has_db_file and db_models not in discovered and (worktree_path / db_models).exists():
        # Insert after seed paths but before the rest so it counts toward the cap.
        seed_count = len(seed_paths)
        keys = list(discovered.keys())
        keys.insert(seed_count, db_models)
        discovered = {k: None for k in keys}

    result = list(discovered.keys())[:_MAX_FILES]
    logger.info(
        "✅ planner: discovered %d file(s) for run_id=%s: %s",
        len(result),
        run_id,
        result,
    )
    return result


def _build_planner_prompt(
    issue_title: str,
    issue_body: str,
    file_contents: dict[str, str],
) -> str:
    """Build the user message for the planner LLM call."""
    parts: list[str] = []
    parts.append(f"# Issue: {issue_title}\n\n{issue_body.strip()}")

    if file_contents:
        parts.append("## Pre-loaded file contents\n")
        for rel_path, content in file_contents.items():
            truncated = content[:_FILE_CHAR_LIMIT]
            suffix = (
                f"\n... (truncated at {_FILE_CHAR_LIMIT} chars)"
                if len(content) > _FILE_CHAR_LIMIT
                else ""
            )
            parts.append(f"### {rel_path}\n\n```\n{truncated}{suffix}\n```")

    return "\n\n".join(parts)


def _repair_json(text: str) -> str:
    """Apply lightweight repairs to common LLM JSON defects.

    Handles the two most frequent patterns:
    - Trailing commas before ``]`` or ``}`` — e.g. ``[1, 2,]`` → ``[1, 2]``
    - Bare (unquoted) object keys at the start of a value — e.g.
      ``{ tool: "x" }`` → ``{ "tool": "x" }``

    We cannot safely repair unescaped double-quotes inside string values
    (e.g. ``"old_string": "if x == "y""``), so those remain a hard failure.
    """
    import re as _re

    # Strip trailing commas before ] or }.
    text = _re.sub(r",(\s*[\]\}])", r"\1", text)
    # Quote bare identifier keys.  Use a capturing group instead of
    # look-behind to avoid the fixed-width restriction in Python's re.
    # Matches: optional whitespace, a bare word, optional whitespace, colon —
    # but only when preceded by { or , (captured in group 1).
    text = _re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', lambda m: m.group(1) + '"' + m.group(2) + '"' + m.group(3), text)
    return text


def _parse_plan_json(raw: str, run_id: str, issue_number: int) -> ExecutionPlan | None:
    """Parse the LLM response into an ExecutionPlan.

    Strips markdown fences if present, then extracts the first valid JSON
    object using ``JSONDecoder.raw_decode`` so trailing text (explanations,
    notes) never causes a parse error.  Applies lightweight JSON repair on
    the first failure before giving up.  Returns ``None`` on any parse or
    validation error.
    """
    text = raw.strip()

    # Remove markdown code fences regardless of where they appear.
    lines = text.splitlines()
    text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()

    # Search for the ExecutionPlan root key rather than the first bare "{".
    # The model sometimes emits prose reasoning before the JSON block
    # (e.g. "I'll implement ac://runs/{run_id}/task…").  The bare "{" in
    # "{run_id}" or similar patterns would be picked up as the JSON start,
    # causing an immediate parse failure.  Anchoring on '{"operations"'
    # skips any leading prose and finds the actual plan object.
    start = text.find('{"operations"')
    if start == -1:
        logger.warning(
            "⚠️ planner: no ExecutionPlan JSON found in response (first 200 chars): %r", raw[:200]
        )
        return None

    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        logger.warning(
            "⚠️ planner: JSON parse error — %s — attempting repair (first 300 chars): %r",
            exc,
            text[start : start + 300],
        )
        repaired = _repair_json(text)
        repair_start = repaired.find('{"operations"')
        if repair_start == -1:
            repair_start = repaired.find("{")
        try:
            data, _ = decoder.raw_decode(repaired, repair_start)
        except json.JSONDecodeError as exc2:
            logger.warning("⚠️ planner: JSON repair failed — %s", exc2)
            return None

    if not isinstance(data, dict):
        logger.warning("⚠️ planner: JSON root is not an object")
        return None

    ops_raw = data.get("operations", [])
    if not isinstance(ops_raw, list):
        logger.warning("⚠️ planner: 'operations' is not a list")
        return None

    operations: list[PlanOperation] = []
    for i, op in enumerate(ops_raw[:_MAX_OPERATIONS]):
        if not isinstance(op, dict):
            logger.warning("⚠️ planner: operation %d is not an object — skipping", i)
            continue
        try:
            operations.append(PlanOperation.model_validate(op))
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ planner: operation %d invalid — %s — skipping", i, exc)

    if not operations:
        logger.warning("⚠️ planner: no valid operations parsed from response")
        return None

    try:
        return ExecutionPlan(
            run_id=run_id,
            issue_number=issue_number,
            operations=operations,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ planner: ExecutionPlan construction failed — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_execution_plan(
    run_id: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    worktree_path: Path,
    file_paths: list[str],
) -> ExecutionPlan | None:
    """Call the LLM once to produce a minimal ExecutionPlan for *run_id*.

    Three-phase pipeline:

    1. **Discovery** — ``_discover_files`` merges *file_paths* (explicit seeds
       from the issue body) with Qdrant semantic search results to build the
       final list of files to read.
    2. **Full file reads** — each discovered file is read in full from the
       worktree so the LLM can emit verbatim ``old_string`` / ``after`` values
       that the executor can apply without ever reading the file itself.
    3. **Plan generation** — a single ``call_anthropic`` call returns a JSON
       ``ExecutionPlan`` which is validated and returned.

    Args:
        run_id: Agent run identifier (e.g. ``"issue-501"``).
        issue_number: GitHub issue number.
        issue_title: Issue title string.
        issue_body: Raw Markdown issue body.
        worktree_path: Absolute path to the git worktree on disk.
        file_paths: Seed paths — files explicitly named in the issue body.
            These are always included and prioritised over Qdrant results.

    Returns:
        A validated :class:`ExecutionPlan`, or ``None`` if any phase fails.
        Callers should fall back to the developer role when ``None`` is
        returned.
    """
    # Phase 1: File discovery.
    discovery_query = f"{issue_title}\n\n{issue_body[:600]}"
    all_file_paths = await _discover_files(
        discovery_query, file_paths, worktree_path, run_id
    )

    # Phase 2: Full file reads from the worktree.
    file_contents: dict[str, str] = {}
    for rel in all_file_paths:
        full = worktree_path / rel
        try:
            file_contents[rel] = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("⚠️ planner: could not read %s — %s", rel, exc)

    # Phase 3: Plan generation.
    user_message = _build_planner_prompt(issue_title, issue_body, file_contents)

    logger.info(
        "✅ planner: calling LLM for run_id=%s issue=%d files=%d",
        run_id,
        issue_number,
        len(file_contents),
    )

    try:
        raw = await call_anthropic(
            user_message,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            max_tokens=16384,
            json_schema=_EXECUTION_PLAN_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ planner: LLM call failed — %s", exc)
        return None

    plan = _parse_plan_json(raw, run_id, issue_number)
    if plan is None:
        logger.warning("⚠️ planner: could not parse ExecutionPlan from LLM response")
        return None

    # Validate and auto-correct operation file paths.  The LLM sometimes
    # generates paths with a spurious leading component (e.g.
    # ``agentception/.cursor/mcp.json`` instead of ``.cursor/mcp.json``).
    # For read/modify operations we can fix this by stripping leading
    # components until the file is found; write_file creates new files so
    # there is nothing to validate against.
    for op in plan.operations:
        if op.tool in ("replace_in_file", "insert_after_in_file"):
            if not (worktree_path / op.file).exists():
                parts = Path(op.file).parts
                corrected: str | None = None
                for i in range(1, len(parts)):
                    candidate = str(Path(*parts[i:]))
                    if (worktree_path / candidate).exists():
                        corrected = candidate
                        break
                if corrected is not None:
                    logger.warning(
                        "⚠️ planner: corrected path %r → %r for run_id=%s",
                        op.file,
                        corrected,
                        run_id,
                    )
                    op.file = corrected
                else:
                    logger.warning(
                        "⚠️ planner: path not found in worktree: %r (run_id=%s) — executor will fail",
                        op.file,
                        run_id,
                    )

    logger.info(
        "✅ planner: generated plan for run_id=%s — %d operation(s): %s",
        run_id,
        len(plan.operations),
        [f"{op.tool}({op.file})" for op in plan.operations],
    )
    return plan
