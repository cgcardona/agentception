from __future__ import annotations

"""Planner service — converts a GitHub issue into an immutable ExecutionPlan.

Pipeline
--------
The planner is a small tool-equipped agent (max 8 turns) rather than a
single-shot full-file-dump call.  It receives only the issue title and body,
then uses three tools to gather exactly the context it needs:

1. ``search_codebase`` — Qdrant semantic search returning function/class-level
   chunks.  Typically sufficient to produce verbatim ``old_string`` anchors
   without reading the whole file.

2. ``read_file_lines`` — reads a specific line range when the chunk alone
   is not enough to produce a unique anchor.

3. ``submit_plan`` — validates and stores the ``ExecutionPlan``, terminating
   the loop.

This keeps the planner prompt small regardless of file size.  For a 1,200-line
file, a semantic search returns the 30-40 relevant lines; the old approach
loaded all 1,200 unconditionally (114k-char prompts on issue #407).
"""

import json
import logging
from pathlib import Path

from agentception.models import ExecutionPlan, PlanOperation
from agentception.services.code_indexer import SearchMatch, search_codebase
from agentception.services.llm import (
    ToolDefinition,
    ToolFunction,
    call_anthropic_with_tools,
)
from agentception.tools.file_tools import read_file_lines

logger = logging.getLogger(__name__)

_MAX_OPERATIONS: int = 50
_MAX_PLANNER_TURNS: int = 8

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM_PROMPT = """\
You are a minimal-change planning agent.

## Goal

Convert the GitHub issue into an ExecutionPlan — a minimal ordered list of
file operations for the executor to apply mechanically.

## How to work

1. Call search_codebase for each distinct code gap in the issue.  Qdrant
   returns function/class-level chunks — usually enough to produce a verbatim
   old_string anchor without reading the whole file.
2. Call read_file_lines only when the chunk did not include the exact lines
   you need for a unique anchor.
3. Call submit_plan exactly once, with all operations verified against real
   file content.  This ends the session.

## Operation rules

- Implement ONLY what the issue explicitly requests.
- old_string must appear exactly once in the file — verify uniqueness from
  the chunk or read_file_lines output before emitting.
- old_string / new_string / after / content must be verbatim — the executor
  cannot read files and will fail if the string doesn't match exactly.
- Prefer replace_in_file for edits.  Use insert_after_in_file only for a
  pure append-after-anchor (never after a bare class/def header line whose
  indented body immediately follows — that would detach the body and cause a
  SyntaxError).  Use write_file only for brand-new files.
- Do not add what is not asked.  Do not improve surrounding code.
"""

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI format — llm.py converts to Anthropic internally)
# ---------------------------------------------------------------------------

_OPERATION_ITEM_SCHEMA: dict[str, object] = {
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
}

_PLANNER_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="search_codebase",
            description=(
                "Semantic search over the codebase. Returns function/class-level "
                "code chunks. Use this first for each gap in the issue."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of the code to find",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of results (default 5, max 8)",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="read_file_lines",
            description=(
                "Read a specific line range from a file in the repo. "
                "Use only when a search chunk did not include the exact anchor text you need."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from repo root (e.g. agentception/readers/github.py)",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read (1-indexed)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read (inclusive)",
                    },
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="submit_plan",
            description=(
                "Submit the final ExecutionPlan. Call this exactly once when you "
                "have verified all anchor strings against real file content. "
                "Terminates the planning session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": _OPERATION_ITEM_SCHEMA,
                        "description": "Ordered list of file operations for the executor",
                    },
                },
                "required": ["operations"],
                "additionalProperties": False,
            },
        ),
    ),
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_operations(
    ops_raw: object,
    run_id: str,
    issue_number: int,
) -> ExecutionPlan | None:
    """Validate the operations list from submit_plan and build an ExecutionPlan."""
    if not isinstance(ops_raw, list):
        logger.warning("⚠️ planner: submit_plan 'operations' is not a list")
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
        logger.warning("⚠️ planner: no valid operations in submitted plan")
        return None

    try:
        return ExecutionPlan(
            run_id=run_id, issue_number=issue_number, operations=operations
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ planner: ExecutionPlan construction failed — %s", exc)
        return None


def _format_search_results(results: list[SearchMatch]) -> str:
    """Format Qdrant search results into a compact string for the LLM."""
    if not results:
        return "No results found."
    parts: list[str] = []
    for r in results:
        file_path = r["file"]
        score = r["score"]
        chunk = r["chunk"]
        start = r["start_line"]
        end = r["end_line"]
        parts.append(f"### {file_path} (lines {start}–{end}, score={score:.2f})\n```\n{chunk}\n```")
    return "\n\n".join(parts)


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
    """Run the tool-equipped planner loop and return an ExecutionPlan.

    The planner receives only the issue title and body, then uses
    ``search_codebase``, ``read_file_lines``, and ``submit_plan`` to gather
    exactly the context it needs — no full file dumps.

    Args:
        run_id: Agent run identifier (e.g. ``"issue-407"``).
        issue_number: GitHub issue number.
        issue_title: Issue title string.
        issue_body: Raw Markdown issue body.
        worktree_path: Absolute path to the git worktree on disk.
        file_paths: Currently unused (kept for interface compatibility).
            Previously used as seed paths for file discovery; the tool-based
            planner discovers context organically via search_codebase.

    Returns:
        A validated :class:`ExecutionPlan`, or ``None`` if the planner loop
        ends without calling ``submit_plan``.  Callers fall back to the
        developer role on ``None``.
    """
    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": f"# Issue: {issue_title}\n\n{issue_body.strip()}",
        }
    ]
    plan_result: ExecutionPlan | None = None

    logger.info(
        "✅ planner: starting tool loop for run_id=%s issue=%d (max_turns=%d)",
        run_id,
        issue_number,
        _MAX_PLANNER_TURNS,
    )

    for turn in range(_MAX_PLANNER_TURNS):
        try:
            response = await call_anthropic_with_tools(
                messages,
                system=_PLANNER_SYSTEM_PROMPT,
                tools=_PLANNER_TOOLS,
                max_tokens=4096,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ planner: LLM call failed on turn %d — %s", turn, exc)
            return None

        # Append the assistant turn to history.
        messages.append(
            {
                "role": "assistant",
                "content": response["content"],
                "tool_calls": response["tool_calls"],
            }
        )

        if response["stop_reason"] != "tool_calls":
            logger.warning(
                "⚠️ planner: stopped with reason=%r on turn %d without submit_plan",
                response["stop_reason"],
                turn,
            )
            break

        # Dispatch each tool call and collect results.
        done = False
        tool_result_messages: list[dict[str, object]] = []

        for tc in response["tool_calls"]:
            tool_name = tc["function"]["name"]
            try:
                args: object = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}

            if tool_name == "search_codebase":
                query = str(args.get("query", ""))
                n = min(int(args.get("n", 5)), 8)
                try:
                    results = await search_codebase(query, n_results=n)
                    result_text = _format_search_results(results)
                except Exception as exc:  # noqa: BLE001
                    result_text = f"Search failed: {exc}"
                logger.info(
                    "✅ planner: search_codebase(q=%r, n=%d) → %d results",
                    query,
                    n,
                    len(results) if "results" in dir() else 0,
                )

            elif tool_name == "read_file_lines":
                rel_path = str(args.get("path", ""))
                start = int(args.get("start_line", 1))
                end = int(args.get("end_line", start + 50))
                abs_path = worktree_path / rel_path
                read_result = read_file_lines(abs_path, start, end)
                if read_result.get("ok"):
                    result_text = f"### {rel_path} (lines {start}–{end})\n```\n{read_result['content']}\n```"
                else:
                    result_text = f"Error: {read_result.get('error', 'unknown')}"
                logger.info(
                    "✅ planner: read_file_lines(%s, %d-%d)", rel_path, start, end
                )

            elif tool_name == "submit_plan":
                ops_raw = args.get("operations", [])
                plan_result = _validate_operations(ops_raw, run_id, issue_number)
                if plan_result is not None:
                    result_text = json.dumps(
                        {
                            "ok": True,
                            "operation_count": len(plan_result.operations),
                            "operations": [
                                f"{op.tool}({op.file})" for op in plan_result.operations
                            ],
                        }
                    )
                    logger.info(
                        "✅ planner: submit_plan accepted — %d operation(s): %s",
                        len(plan_result.operations),
                        [f"{op.tool}({op.file})" for op in plan_result.operations],
                    )
                else:
                    result_text = json.dumps({"ok": False, "error": "validation failed"})
                    logger.warning("⚠️ planner: submit_plan rejected — validation failed")
                done = True

            else:
                result_text = f"Unknown tool: {tool_name}"
                logger.warning("⚠️ planner: unknown tool call: %s", tool_name)

            tool_result_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                }
            )

        messages.extend(tool_result_messages)

        if done:
            break

    if plan_result is None:
        logger.warning(
            "⚠️ planner: loop ended without submit_plan for run_id=%s", run_id
        )
    return plan_result
