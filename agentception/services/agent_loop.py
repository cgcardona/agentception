"""Cursor-free agent execution loop.

Replaces Cursor as the agent runtime.  An LLM on Anthropic's infrastructure
(reached via OpenRouter) does the reasoning; file operations, shell commands,
and MCP tool calls execute locally inside this container.

Pipeline
--------
1. Resolve the worktree path from ``settings.worktrees_dir / run_id``.
2. Parse ``.agent-task`` via :func:`~agentception.readers.worktrees.parse_agent_task`.
3. Load the role file from ``settings.repo_dir / ".agentception/roles/{role}.md"``.
4. Assemble the system prompt: role content + cognitive architecture context +
   runtime environment note (Python commands run directly, not via docker exec).
5. Build the combined tool catalogue: local file/shell tools + all MCP tools.
6. Run the multi-turn conversation loop via
   :func:`~agentception.services.llm.call_openrouter_with_tools`, dispatching
   tool calls until the model returns ``stop_reason == "stop"`` or the
   iteration ceiling is hit.
7. On completion: call :func:`~agentception.mcp.build_commands.build_complete_run`.
   On iteration limit or unrecoverable error: call
   :func:`~agentception.mcp.log_tools.log_run_error` then
   :func:`~agentception.mcp.build_commands.build_cancel_run`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agentception.config import settings
from agentception.mcp.build_commands import build_cancel_run, build_complete_run
from agentception.mcp.log_tools import log_run_error, log_run_step
from agentception.mcp.server import TOOLS, call_tool_async
from agentception.mcp.types import ACToolResult
from agentception.models import TaskFile
from agentception.readers.worktrees import parse_agent_task
from agentception.services.llm import (
    ToolCall,
    ToolDefinition,
    ToolFunction,
    ToolResponse,
    call_openrouter_with_tools,
)
from agentception.services.code_indexer import search_codebase
from agentception.tools.definitions import FILE_TOOL_DEFS, SEARCH_CODEBASE_TOOL_DEF, SHELL_TOOL_DEF
from agentception.tools.file_tools import (
    list_directory,
    read_file,
    search_text,
    write_file,
)
from agentception.tools.shell_tools import run_command

logger = logging.getLogger(__name__)

# Hard cap on conversation turns.  Each iteration is one LLM call.
_DEFAULT_MAX_ITERATIONS = 50

# Local tool names — dispatched to file/shell functions rather than MCP.
_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "write_file", "list_directory", "search_text", "run_command", "search_codebase"}
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent_loop(
    run_id: str,
    *,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
) -> None:
    """Run the full agent conversation loop for *run_id*.

    This is designed to be called as a FastAPI ``BackgroundTask`` from the
    ``POST /api/runs/{run_id}/execute`` route, which has already transitioned
    the run to ``implementing``.

    Args:
        run_id: The run identifier, used to locate the worktree and task file.
        max_iterations: Upper bound on LLM turns (prevents runaway loops).
    """
    worktree_path = settings.worktrees_dir / run_id

    task = await _load_task(worktree_path)
    if task is None:
        logger.error("❌ agent_loop — no .agent-task for run_id=%s", run_id)
        await build_cancel_run(run_id)
        return

    issue_number = task.issue_number or 0

    role_prompt = _load_role_prompt(task.role)
    system_prompt = _build_system_prompt(role_prompt, task.cognitive_arch or "")
    tool_defs = _build_tool_definitions()
    initial_message = _build_initial_message(task, worktree_path)

    messages: list[dict[str, object]] = [{"role": "user", "content": initial_message}]

    logger.info(
        "✅ agent_loop start — run_id=%s issue=%d tools=%d",
        run_id,
        issue_number,
        len(tool_defs),
    )

    for iteration in range(1, max_iterations + 1):
        await log_run_step(
            issue_number,
            f"Iteration {iteration}/{max_iterations}",
            run_id,
        )

        try:
            response: ToolResponse = await call_openrouter_with_tools(
                messages,
                system=system_prompt,
                tools=tool_defs,
            )
        except Exception as exc:
            logger.exception("❌ agent_loop LLM error on iteration %d", iteration)
            await log_run_error(issue_number, f"LLM error: {exc}", run_id)
            await build_cancel_run(run_id)
            return

        # Append assistant message to history.
        assistant_msg: dict[str, object] = {"role": "assistant", "content": response["content"]}
        if response["tool_calls"]:
            assistant_msg["tool_calls"] = list(response["tool_calls"])
        messages.append(assistant_msg)

        if response["stop_reason"] == "stop":
            logger.info("✅ agent_loop complete — run_id=%s iterations=%d", run_id, iteration)
            await build_complete_run(
                issue_number=issue_number,
                pr_url="",
                summary=response["content"][:500] if response["content"] else "Agent completed.",
                agent_run_id=run_id,
            )
            return

        if response["stop_reason"] == "tool_calls":
            tool_results = await _dispatch_tool_calls(
                response["tool_calls"], worktree_path, run_id
            )
            messages.extend(tool_results)
            continue

        # Unexpected stop reason (e.g. "length").
        logger.warning(
            "⚠️ agent_loop unexpected stop_reason=%s on iteration %d",
            response["stop_reason"],
            iteration,
        )
        await log_run_error(
            issue_number,
            f"Unexpected stop_reason={response['stop_reason']!r} on iteration {iteration}",
            run_id,
        )
        await build_cancel_run(run_id)
        return

    # Reached iteration ceiling.
    logger.error("❌ agent_loop iteration limit reached — run_id=%s", run_id)
    await log_run_error(
        issue_number,
        f"Agent loop exceeded {max_iterations} iterations without completing.",
        run_id,
    )
    await build_cancel_run(run_id)


# ---------------------------------------------------------------------------
# Task loading helpers
# ---------------------------------------------------------------------------


async def _load_task(worktree_path: Path) -> TaskFile | None:
    """Parse the ``.agent-task`` file in *worktree_path*.

    Returns ``None`` and logs an error when the file is absent or malformed.
    """
    try:
        return await parse_agent_task(worktree_path)
    except Exception as exc:
        logger.error("❌ _load_task error: %s", exc)
        return None


def _load_role_prompt(role: str | None) -> str:
    """Return the Markdown content of the role file for *role*.

    Falls back to an empty string when the role is unknown or the file is
    missing, so the agent still has the system prompt's runtime note.
    """
    if not role:
        logger.warning("⚠️ _load_role_prompt — no role specified")
        return ""

    role_path = settings.repo_dir / ".agentception" / "roles" / f"{role}.md"
    try:
        return role_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("⚠️ _load_role_prompt — role file not found: %s", role_path)
        return ""
    except OSError as exc:
        logger.warning("⚠️ _load_role_prompt — OS error reading %s: %s", role_path, exc)
        return ""


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------

_RUNTIME_ENV_NOTE = """\
---
## Runtime Environment

You are running **inside the AgentCeption Docker container**, not on the host machine.

- Run Python tools **directly** — do NOT prefix with `docker compose exec agentception`.
  - ✅ `python3 -m pytest tests/`
  - ✅ `python3 -m mypy agentception/`
  - ❌ `docker compose exec agentception python3 -m pytest` (wrong — you are already inside)
- The repository is mounted at `/app`.  The worktree for your task is at the path
  in your `.agent-task` file (`[worktree] path`).
- Git operations run in the worktree directory.
- Use `run_command` for shell execution.  Use `read_file` / `write_file` for files.
"""


def _build_system_prompt(role_prompt: str, cognitive_arch: str) -> str:
    """Assemble the full system prompt from the role file and cognitive arch context.

    Args:
        role_prompt: Raw Markdown content of the agent's role file.
        cognitive_arch: Cognitive architecture context string from ``.agent-task``.

    Returns:
        A single multi-part system prompt string.
    """
    parts: list[str] = []

    if role_prompt:
        parts.append(role_prompt.strip())

    if cognitive_arch:
        parts.append(f"---\n## Cognitive Architecture Context\n\n{cognitive_arch.strip()}")

    parts.append(_RUNTIME_ENV_NOTE.strip())

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Initial user message
# ---------------------------------------------------------------------------


def _build_initial_message(task: TaskFile, worktree_path: Path) -> str:
    """Build the first user message that kicks off the agent conversation.

    Args:
        task: Parsed ``.agent-task`` data.
        worktree_path: Container-side path to the worktree directory.

    Returns:
        A brief markdown message directing the agent to read its task file.
    """
    run_id = task.id or str(worktree_path.name)
    issue_ref = f"#{task.issue_number}" if task.issue_number else "(no issue)"
    role = task.role or "unknown"
    task_file_path = worktree_path / ".agent-task"

    return (
        f"You have been dispatched to work on issue {issue_ref} "
        f"as a **{role}** agent (run `{run_id}`).\n\n"
        f"Your worktree is at: `{worktree_path}`\n"
        f"Your configuration is at: `{task_file_path}`\n\n"
        f"Start by reading your `.agent-task` file to understand your full "
        f"instructions, then proceed with your work."
    )


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------


def _mcp_tool_to_openai(tool_name: str, description: str, input_schema: dict[str, object]) -> ToolDefinition:
    """Convert an MCP ACToolDef to an OpenAI-format ToolDefinition."""
    return ToolDefinition(
        type="function",
        function=ToolFunction(
            name=tool_name,
            description=description,
            parameters=input_schema,
        ),
    )


def _build_tool_definitions() -> list[ToolDefinition]:
    """Build the combined tool list: local tools + MCP tools.

    Local tools (file/shell) are listed first so the model encounters them
    before the more specialised MCP tools.

    MCP tools that share a name with local tools are excluded (local tools
    take precedence for file operations).
    """
    tool_defs: list[ToolDefinition] = list(FILE_TOOL_DEFS)
    tool_defs.append(SHELL_TOOL_DEF)
    tool_defs.append(SEARCH_CODEBASE_TOOL_DEF)

    for mcp_tool in TOOLS:
        name: object = mcp_tool.get("name")
        if not isinstance(name, str) or name in _LOCAL_TOOL_NAMES:
            continue
        description: object = mcp_tool.get("description", "")
        input_schema: object = mcp_tool.get("inputSchema", {})
        if not isinstance(description, str) or not isinstance(input_schema, dict):
            continue
        tool_defs.append(_mcp_tool_to_openai(name, description, input_schema))

    return tool_defs


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool_calls(
    tool_calls: list[ToolCall],
    worktree_path: Path,
    run_id: str,
) -> list[dict[str, object]]:
    """Execute each tool call and return a list of tool-result messages.

    Local file/shell tools are dispatched directly.  All other tool names are
    forwarded to :func:`~agentception.mcp.server.call_tool_async`.

    Args:
        tool_calls: Tool calls returned by the model.
        worktree_path: Worktree root used as the default cwd for shell calls
            and the base for resolving relative file paths.
        run_id: Used for logging only.

    Returns:
        A list of ``{"role": "tool", "tool_call_id": str, "content": str}``
        messages ready to extend the conversation history.
    """
    results: list[dict[str, object]] = []
    for tc in tool_calls:
        result = await _dispatch_single_tool(tc, worktree_path, run_id)
        results.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result),
            }
        )
    return results


def _mcp_result_to_dict(result: ACToolResult) -> dict[str, object]:
    """Convert an :class:`~agentception.mcp.types.ACToolResult` to a plain dict.

    The model receives the text extracted from the content list so it can
    understand the tool outcome without needing knowledge of the MCP protocol.
    """
    text_parts = [
        item["text"]
        for item in result["content"]
        if item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    return {"ok": not result["isError"], "result": "\n".join(text_parts)}


async def _dispatch_single_tool(
    tool_call: ToolCall,
    worktree_path: Path,
    run_id: str,
) -> dict[str, object]:
    """Dispatch a single tool call and return its result dict.

    Returns ``{"ok": False, "error": str}`` on argument parse failure so the
    model always receives structured feedback.
    """
    name = tool_call["function"]["name"]
    args_str = tool_call["function"]["arguments"]

    logger.info("✅ dispatch_tool — run_id=%s tool=%s", run_id, name)

    try:
        args: dict[str, object] = json.loads(args_str)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid tool arguments (JSON parse error): {exc}"}

    if name not in _LOCAL_TOOL_NAMES:
        return _mcp_result_to_dict(await call_tool_async(name, args))

    return await _dispatch_local_tool(name, args, worktree_path)


async def _dispatch_local_tool(
    name: str,
    args: dict[str, object],
    worktree_path: Path,
) -> dict[str, object]:
    """Route a local tool call to the appropriate file or shell function."""

    def _resolve(raw: object, default: Path) -> Path:
        """Resolve *raw* as a path, falling back to *default*."""
        if not isinstance(raw, str) or not raw:
            return default
        p = Path(raw)
        return p if p.is_absolute() else worktree_path / p

    if name == "read_file":
        path = _resolve(args.get("path"), worktree_path)
        return read_file(path)

    if name == "write_file":
        path_raw = args.get("path")
        content_raw = args.get("content")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "write_file: 'path' must be a string"}
        if not isinstance(content_raw, str):
            return {"ok": False, "error": "write_file: 'content' must be a string"}
        return write_file(_resolve(path_raw, worktree_path), content_raw)

    if name == "list_directory":
        path = _resolve(args.get("path", "."), worktree_path)
        return list_directory(path)

    if name == "search_text":
        pattern_raw = args.get("pattern")
        if not isinstance(pattern_raw, str):
            return {"ok": False, "error": "search_text: 'pattern' must be a string"}
        directory = _resolve(args.get("directory", "."), worktree_path)
        n_results_raw = args.get("n_results", 30)
        n_results = int(n_results_raw) if isinstance(n_results_raw, int) else 30
        return await search_text(pattern_raw, directory, n_results=n_results)

    if name == "run_command":
        command_raw = args.get("command")
        if not isinstance(command_raw, str):
            return {"ok": False, "error": "run_command: 'command' must be a string"}
        cwd_raw = args.get("cwd")
        cwd = _resolve(cwd_raw, worktree_path) if cwd_raw is not None else worktree_path
        return await run_command(command_raw, cwd)

    if name == "search_codebase":
        query_raw = args.get("query")
        if not isinstance(query_raw, str):
            return {"ok": False, "error": "search_codebase: 'query' must be a string"}
        n_raw = args.get("n_results", 5)
        n_results = int(n_raw) if isinstance(n_raw, int) else 5
        matches = await search_codebase(query_raw, n_results)
        return {"ok": True, "matches": matches}

    return {"ok": False, "error": f"Unknown local tool: {name!r}"}
