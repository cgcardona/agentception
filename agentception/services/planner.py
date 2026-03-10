from __future__ import annotations

"""Planner service — converts a GitHub issue into an immutable ExecutionPlan.

The planner makes a single structured LLM call.  It receives the issue body
and the contents of every file it needs to touch, then outputs a JSON
``ExecutionPlan`` describing the minimal set of atomic file operations
required to implement the issue.

The plan is intentionally minimal:

- One operation per discrete file edit.
- No validators, docstrings, or additional tests unless the issue explicitly
  requests them.
- Parameters are exact: ``old_string`` / ``after`` must match the verbatim
  text in the pre-loaded file content so the executor can call the tool
  without reading the file first.

Separation of concerns
----------------------
Planner  → creative reasoning about the codebase (reads files, infers context)
Executor → mechanical determinism (applies plan, no codebase access)
"""

import json
import logging
from pathlib import Path

from agentception.models import ExecutionPlan, PlanOperation
from agentception.services.llm import call_anthropic

logger = logging.getLogger(__name__)

# Maximum characters of file content injected into the planner prompt per file.
_FILE_CHAR_LIMIT: int = 12_000

# Hard cap on operations in one plan — prevents planner over-engineering.
_MAX_OPERATIONS: int = 20

_PLANNER_SYSTEM_PROMPT = """\
You are a minimal-change planning agent. Your only job is to produce the
smallest possible set of atomic file operations that implement the given
GitHub issue — nothing more.

Rules:
- Implement ONLY what the issue explicitly requests.
- Do not add validators, docstrings, or extra tests unless the issue says so.
- Do not create new files unless the issue says to create them.
- Do not improve surrounding code.
- Each operation maps to one tool call. Parameters must be exact — the
  executor cannot read files, so every string you emit must match the
  file content verbatim.

Output ONLY a JSON object with this exact schema (no markdown fences, no
surrounding text):

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

Use "replace_in_file" for edits to existing files (preferred — most precise).
Use "insert_after_in_file" when you need to append after a known anchor line.
Use "write_file" only when creating a brand-new file.

old_string for replace_in_file must be unique in the file and must appear
verbatim in the pre-loaded file content below.
"""


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
            suffix = f"\n... (truncated at {_FILE_CHAR_LIMIT} chars)" if len(content) > _FILE_CHAR_LIMIT else ""
            parts.append(
                f"### {rel_path}\n\n```\n{truncated}{suffix}\n```"
            )

    return "\n\n".join(parts)


def _parse_plan_json(raw: str, run_id: str, issue_number: int) -> ExecutionPlan | None:
    """Parse the LLM response into an ExecutionPlan.

    Strips markdown fences if present, then extracts the first valid JSON object
    using ``JSONDecoder.raw_decode`` so trailing text (explanations, notes) never
    causes a parse error.  Returns None on any parse or validation error.
    """
    text = raw.strip()

    # Remove markdown code fences regardless of where they appear.
    lines = text.splitlines()
    text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()

    start = text.find("{")
    if start == -1:
        logger.warning("⚠️ planner: no JSON object found in response")
        return None

    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        logger.warning("⚠️ planner: JSON parse error — %s", exc)
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


async def generate_execution_plan(
    run_id: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    worktree_path: Path,
    file_paths: list[str],
) -> ExecutionPlan | None:
    """Call the LLM once to produce a minimal ExecutionPlan for *run_id*.

    Reads each file in *file_paths* from the worktree and injects the contents
    into the planner prompt so the LLM can emit verbatim ``old_string`` /
    ``after`` values that the executor can use without reading files.

    Args:
        run_id: Agent run identifier (e.g. ``"issue-501"``).
        issue_number: GitHub issue number.
        issue_title: Issue title string.
        issue_body: Raw Markdown issue body.
        worktree_path: Absolute path to the git worktree on disk.
        file_paths: Relative paths of files the planner should read.

    Returns:
        A validated :class:`ExecutionPlan`, or ``None`` if the planner call
        or parsing fails.  Callers should fall back to the developer role
        when ``None`` is returned.
    """
    # Read file contents from the worktree.
    file_contents: dict[str, str] = {}
    for rel in file_paths:
        full = worktree_path / rel
        try:
            file_contents[rel] = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("⚠️ planner: could not read %s — %s", rel, exc)

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
            max_tokens=4096,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ planner: LLM call failed — %s", exc)
        return None

    plan = _parse_plan_json(raw, run_id, issue_number)
    if plan is None:
        logger.warning("⚠️ planner: could not parse ExecutionPlan from LLM response")
        return None

    logger.info(
        "✅ planner: generated plan for run_id=%s — %d operation(s): %s",
        run_id,
        len(plan.operations),
        [f"{op.tool}({op.file})" for op in plan.operations],
    )
    return plan
