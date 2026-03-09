"""Cursor-free agent execution loop.

Replaces Cursor as the agent runtime.  An LLM on Anthropic's infrastructure
does the reasoning; file operations, shell commands, and MCP tool calls execute
locally inside this container.

Pipeline
--------
1. Resolve the worktree path from ``settings.worktrees_dir / run_id``.
2. Load task context from the ``ACAgentRun`` DB row via ``_load_task`` (DB-only).
3. Load the role file from ``settings.repo_dir / ".agentception/roles/{role}.md"``.
4. Assemble the system prompt: role content + cognitive architecture context +
   runtime environment note (Python commands run directly, not via docker exec).
5. Build the combined tool catalogue: local file/shell tools + all MCP tools.
6. Run the multi-turn conversation loop via
   :func:`~agentception.services.llm.call_anthropic_with_tools`, dispatching
   tool calls until the model returns ``stop_reason == "stop"`` or the
   iteration ceiling is hit.
7. On completion: call :func:`~agentception.mcp.build_commands.build_complete_run`.
   On iteration limit or unrecoverable error: call
   :func:`~agentception.mcp.log_tools.log_run_error` then
   :func:`~agentception.mcp.build_commands.build_cancel_run`.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import sys
import time
from pathlib import Path

from sqlalchemy import select

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACAgentRun
from agentception.mcp.build_commands import build_cancel_run, build_complete_run
from agentception.mcp.log_tools import log_run_error, log_run_step
from agentception.mcp.prompts import get_prompt
from agentception.mcp.server import TOOLS, call_tool_async
from agentception.mcp.types import ACToolResult
from agentception.models import AgentTaskSpec
from agentception.services.llm import (
    ToolCall,
    ToolDefinition,
    ToolFunction,
    ToolResponse,
    call_anthropic_with_tools,
)
from agentception.services.code_indexer import search_codebase
from agentception.services.github_mcp_client import GitHubMCPClient
from agentception.tools.definitions import FILE_TOOL_DEFS, SEARCH_CODEBASE_TOOL_DEF, SHELL_TOOL_DEF
from agentception.tools.file_tools import (
    list_directory,
    read_file,
    replace_in_file,
    search_text,
    write_file,
)
from agentception.tools.shell_tools import run_command

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cognitive architecture expansion — resolve_arch.py lives under scripts/ and
# is not a proper Python package.  We add its directory to sys.path once so
# that `import resolve_arch` works without restructuring the repo.
# ---------------------------------------------------------------------------
_RESOLVE_ARCH_DIR = Path(__file__).parent.parent.parent / "scripts" / "gen_prompts"
if str(_RESOLVE_ARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESOLVE_ARCH_DIR))

# Hard cap on conversation turns.  Each iteration is one LLM call.
_DEFAULT_MAX_ITERATIONS = 50

# Tool results longer than this are truncated before being stored in history.
# read_file / run_command can easily dump 50k+ chars; keeping them unbounded
# inflates every subsequent input token count linearly.
_MAX_TOOL_RESULT_CHARS: int = 3_000

# When the message history (excluding system) exceeds this count, old turns
# are dropped from the middle.  The first user message (task briefing) and the
# most-recent _HISTORY_TAIL messages are always kept.
_MAX_HISTORY_MESSAGES: int = 20
_HISTORY_TAIL: int = 14

# ---------------------------------------------------------------------------
# Token-rate guard — keeps input token consumption under the Tier 1 limit
# (30 000 tokens/minute).  We target 27 000 to leave a safety margin.
# ---------------------------------------------------------------------------

# Target: 90 % of the 30 000 input-token/minute Tier-1 limit.
_INPUT_TPM_TARGET: int = 27_000
_TPM_WINDOW_SECS: float = 60.0

# Rolling window of (monotonic_timestamp, input_tokens) pairs.
# Module-level; acceptable for single-agent-per-process deployments.
_tpm_window: collections.deque[tuple[float, int]] = collections.deque()


def _tpm_record_and_get_sleep(input_tokens: int) -> float:
    """Record *input_tokens* in the rolling 60-second window.

    Returns the number of seconds the caller should sleep before making
    the next API call.  Returns ``0.0`` when no throttling is needed.

    Uses ``time.monotonic`` so the window is immune to clock adjustments.
    """
    now = time.monotonic()
    while _tpm_window and now - _tpm_window[0][0] >= _TPM_WINDOW_SECS:
        _tpm_window.popleft()
    _tpm_window.append((now, input_tokens))
    total = sum(t for _, t in _tpm_window)
    if total >= _INPUT_TPM_TARGET and _tpm_window:
        oldest_ts = _tpm_window[0][0]
        sleep_secs = _TPM_WINDOW_SECS - (now - oldest_ts) + 1.0
        return max(0.0, sleep_secs)
    return 0.0


# Local tool names — dispatched to file/shell functions rather than MCP.
_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "replace_in_file",
        "write_file",
        "list_directory",
        "search_text",
        "run_command",
        "search_codebase",
    }
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

    task = await _load_task(run_id, worktree_path)
    if task is None:
        logger.error("❌ agent_loop — no task context for run_id=%s", run_id)
        await build_cancel_run(run_id)
        return

    issue_number = task.issue_number or 0

    role_prompt = _load_role_prompt(task.role)
    system_prompt = _build_system_prompt(role_prompt, task.cognitive_arch or "")

    # Initialise the GitHub MCP client and fetch its tool definitions.
    # Failures here are non-fatal — the agent runs without GitHub MCP tools
    # and falls back to the AgentCeption MCP tools for GitHub mutations.
    github_client = GitHubMCPClient()
    github_tool_names: frozenset[str] = frozenset()
    try:
        github_tools = await github_client.list_tools()
        github_tool_names = frozenset(t["function"]["name"] for t in github_tools)
    except RuntimeError as exc:
        logger.warning("⚠️ GitHub MCP server unavailable — %s. GitHub reads will use gh CLI.", exc)
        github_tools = []

    tool_defs = _build_tool_definitions(extra_tools=github_tools)
    initial_message = await _fetch_task_briefing(run_id, task, worktree_path)

    messages: list[dict[str, object]] = [{"role": "user", "content": initial_message}]

    logger.info(
        "✅ agent_loop start — run_id=%s issue=%d tools=%d (github_mcp=%d)",
        run_id,
        issue_number,
        len(tool_defs),
        len(github_tool_names),
    )

    for iteration in range(1, max_iterations + 1):
        await log_run_step(
            issue_number,
            f"Iteration {iteration}/{max_iterations}",
            run_id,
        )

        try:
            bounded = _prune_history(_truncate_tool_results(messages))
            response: ToolResponse = await call_anthropic_with_tools(
                bounded,
                system=system_prompt,
                tools=tool_defs,
            )
        except Exception as exc:
            logger.exception("❌ agent_loop LLM error on iteration %d", iteration)
            await github_client.close()
            await log_run_error(issue_number, f"LLM error: {exc}", run_id)
            await build_cancel_run(run_id)
            return

        # Token-rate guard — self-throttle before the next iteration if we are
        # approaching the 30 000 input-tokens/minute Tier-1 limit.
        sleep_secs = _tpm_record_and_get_sleep(response.get("input_tokens", 0))
        if sleep_secs > 0.0:
            logger.warning(
                "⚠️ agent_loop: TPM guard — sleeping %.1fs to stay under rate limit",
                sleep_secs,
            )
            await asyncio.sleep(sleep_secs)

        # Append assistant message to history.
        assistant_msg: dict[str, object] = {"role": "assistant", "content": response["content"]}
        if response["tool_calls"]:
            assistant_msg["tool_calls"] = list(response["tool_calls"])
        messages.append(assistant_msg)

        if response["stop_reason"] == "stop":
            logger.info("✅ agent_loop complete — run_id=%s iterations=%d", run_id, iteration)
            await github_client.close()
            await build_complete_run(
                issue_number=issue_number,
                pr_url="",
                summary=response["content"][:500] if response["content"] else "Agent completed.",
                agent_run_id=run_id,
            )
            return

        if response["stop_reason"] == "tool_calls":
            tool_results = await _dispatch_tool_calls(
                response["tool_calls"],
                worktree_path,
                run_id,
                github_client=github_client,
                github_tool_names=github_tool_names,
            )
            messages.extend(tool_results)
            continue

        # Unexpected stop reason (e.g. "length").
        logger.warning(
            "⚠️ agent_loop unexpected stop_reason=%s on iteration %d",
            response["stop_reason"],
            iteration,
        )
        await github_client.close()
        await log_run_error(
            issue_number,
            f"Unexpected stop_reason={response['stop_reason']!r} on iteration {iteration}",
            run_id,
        )
        await build_cancel_run(run_id)
        return

    # Reached iteration ceiling.
    logger.error("❌ agent_loop iteration limit reached — run_id=%s", run_id)
    await github_client.close()
    await log_run_error(
        issue_number,
        f"Agent loop exceeded {max_iterations} iterations without completing.",
        run_id,
    )
    await build_cancel_run(run_id)


# ---------------------------------------------------------------------------
# Task loading helpers
# ---------------------------------------------------------------------------


async def _load_task(run_id: str, worktree_path: Path) -> AgentTaskSpec | None:
    """Load task context for *run_id* from the ``ACAgentRun`` DB row.

    All task context lives in the DB.
    Returns ``None`` when no row exists, logging the error.
    """
    return await _load_task_from_db(run_id)


async def _load_task_from_db(run_id: str) -> AgentTaskSpec | None:
    """Build an ``AgentTaskSpec`` from the ``ACAgentRun`` DB row for *run_id*.

    Returns ``None`` when no row is found.  Never raises — errors are logged
    so the loop can surface a clean cancellation instead of crashing.
    """
    try:
        async with get_session() as session:
            run: ACAgentRun | None = await session.scalar(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
        if run is None:
            logger.error("❌ _load_task_from_db — no DB row for run_id=%s", run_id)
            return None
        return AgentTaskSpec(
            id=run.id,
            role=run.role,
            cognitive_arch=run.cognitive_arch,
            issue_number=run.issue_number,
            pr_number=run.pr_number,
            branch=run.branch,
            worktree=run.worktree_path,
            batch_id=run.batch_id,
            parent_run_id=run.parent_run_id,
            tier=run.tier,
            org_domain=run.org_domain,
            spawn_mode=run.spawn_mode,
            task_description=run.task_description,
            gh_repo=run.gh_repo,
            is_resumed=run.is_resumed,
            coord_fingerprint=run.coord_fingerprint,
        )
    except Exception as exc:
        logger.error("❌ _load_task_from_db error for run_id=%s: %s", run_id, exc)
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
- The repository is mounted at `/app`.  Your worktree path is provided in your
  initial message.  Read `ac://runs/{run_id}/context` for your full task context.
- Git operations run in the worktree directory.
- Use `run_command` for shell execution.  Use `read_file` / `write_file` for files.
- Use GitHub MCP tools (`get_issue`, `list_issues`, `add_issue_comment`,
  `create_pull_request`, `merge_pull_request`, etc.) for all GitHub operations.
  Do NOT shell out to `gh` CLI for anything the MCP tools can do.

## Memory Discipline

Your conversation history is your memory.  Before calling `read_file`,
`list_directory`, or `run_command`, check whether you already have that
information in the conversation.  **Do not re-read a file or re-run a command
you have already executed** — the output is already in your context.
Re-reading wastes tokens and burns iteration budget.  Use what you know.
"""


def _expand_cognitive_arch(cognitive_arch: str) -> str:
    """Expand a ``cognitive_arch`` slug string into the full identity block.

    Calls ``resolve_arch.assemble()`` which renders the figure's
    ``prompt_injection.prefix``, governing heuristic, failure modes, archetype
    profile, skill domain fragments, and ``prompt_injection.suffix`` into a
    single Markdown block — the complete cognitive identity for this agent.

    Falls back gracefully: if the arch string is empty, a skill ID is unknown,
    or any other error occurs, returns the raw string (or empty string) so the
    agent loop never crashes on a resolution failure.

    Args:
        cognitive_arch: String like ``"guido_van_rossum:python:fastapi"`` or
            ``"linus_torvalds,shannon:htmx:jinja2"``.

    Returns:
        Full multi-section Markdown cognitive identity block, typically
        5 000–12 000 characters.  Empty string when *cognitive_arch* is empty.
    """
    if not cognitive_arch:
        return ""
    try:
        # resolve_arch is not a package — imported via sys.path manipulation above.
        import resolve_arch  # noqa: PLC0415
        figure_ids, skill_ids = resolve_arch.parse_cognitive_arch(cognitive_arch)
        return str(resolve_arch.assemble(figure_ids, skill_ids, mode="implementer"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ _expand_cognitive_arch: falling back to raw string — %s: %s",
            type(exc).__name__, exc,
        )
        return cognitive_arch.strip()


def _build_system_prompt(role_prompt: str, cognitive_arch: str) -> str:
    """Assemble the full system prompt from role definition and cognitive identity.

    The system prompt has three layers, all injected before the first user
    message and cached by Anthropic's prompt-caching infrastructure:

    1. **Role definition** — the agent's operational instructions (what to do,
       how to communicate, what tools to use, what to never do).
    2. **Cognitive identity** — the fully-expanded cognitive architecture block:
       figure ``prompt_injection.prefix`` (first-person identity statement),
       governing heuristic, failure modes with compensations, archetype profile,
       skill domain prompt fragments, and figure ``prompt_injection.suffix``
       (personal review checklist).  This is ~5 000–12 000 characters of rich,
       hand-crafted identity text that shapes every reasoning step.
    3. **Runtime environment note** — where the agent is running and how to
       invoke Python/Docker/git correctly.

    The cognitive identity block is expanded here — not fetched via MCP — so
    it is always present from turn 1, benefits from prompt caching, and never
    depends on the agent deciding to call a resource.  The ``ac://arch/*`` MCP
    resources remain available for mid-task introspection and for coordinators
    browsing figures to assign to child agents.

    Args:
        role_prompt: Raw Markdown content of the agent's role file.
        cognitive_arch: Cognitive architecture string (e.g. ``"guido_van_rossum:python"``).

    Returns:
        A single multi-part system prompt string ready to be sent as the
        ``system`` field of an Anthropic API call.
    """
    parts: list[str] = []

    if role_prompt:
        parts.append(role_prompt.strip())

    expanded = _expand_cognitive_arch(cognitive_arch)
    if expanded:
        parts.append(f"---\n\n{expanded.strip()}")

    parts.append(_RUNTIME_ENV_NOTE.strip())

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Initial user message
# ---------------------------------------------------------------------------


async def _fetch_task_briefing(run_id: str, task: AgentTaskSpec, worktree_path: Path) -> str:
    """Fetch the initial agent message via the ``task/briefing`` MCP prompt.

    Calls ``get_prompt("task/briefing", {"run_id": run_id})`` so the briefing
    is rendered from the DB — no file read, no inline text construction.  This
    is the correct MCP Prompts usage: the client (the loop) calls
    ``prompts/get`` and uses the result as the first user message.

    Falls back to a minimal inline message when the prompt cannot be resolved
    (e.g. during DB downtime), so the loop degrades gracefully rather than
    refusing to start.

    Args:
        run_id: The run ID passed to the ``task/briefing`` prompt.
        task: Merged task context (used only for the fallback message).
        worktree_path: Container-side worktree path (used only for fallback).

    Returns:
        The first user message string for the agent conversation.
    """
    try:
        result = await get_prompt("task/briefing", {"run_id": run_id})
        if result is not None and result["messages"]:
            text: object = result["messages"][0]["content"]["text"]
            if isinstance(text, str) and text.strip():
                logger.info("✅ agent_loop — task/briefing prompt resolved for run_id=%s", run_id)
                return text
    except Exception as exc:
        logger.warning("⚠️ agent_loop — task/briefing prompt failed: %s", exc)

    # Fallback: minimal inline message so the loop can still start.
    logger.warning(
        "⚠️ agent_loop — falling back to inline briefing for run_id=%s", run_id
    )
    role = task.role or "unknown"
    issue_ref = f"#{task.issue_number}" if task.issue_number else "(no issue)"
    return (
        f"You are a **{role}** agent (run `{run_id}`) working on issue {issue_ref}.\n\n"
        f"Your worktree is at: `{worktree_path}`\n\n"
        f"Read `ac://runs/{run_id}/context` for your full task context, "
        f"then proceed with your work."
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


def _build_tool_definitions(
    extra_tools: list[ToolDefinition] | None = None,
) -> list[ToolDefinition]:
    """Build the combined tool list: local tools + AgentCeption MCP tools + GitHub MCP tools.

    Order: local file/shell tools → AgentCeption MCP tools → GitHub MCP tools.
    Local tools take precedence; names already present are not duplicated.
    """
    tool_defs: list[ToolDefinition] = list(FILE_TOOL_DEFS)
    tool_defs.append(SHELL_TOOL_DEF)
    tool_defs.append(SEARCH_CODEBASE_TOOL_DEF)

    seen: set[str] = {t["function"]["name"] for t in tool_defs}

    for mcp_tool in TOOLS:
        name: object = mcp_tool.get("name")
        if not isinstance(name, str) or name in seen:
            continue
        description: object = mcp_tool.get("description", "")
        input_schema: object = mcp_tool.get("inputSchema", {})
        if not isinstance(description, str) or not isinstance(input_schema, dict):
            continue
        tool_defs.append(_mcp_tool_to_openai(name, description, input_schema))
        seen.add(name)

    for gh_tool in extra_tools or []:
        gh_name = gh_tool["function"]["name"]
        if gh_name not in seen:
            tool_defs.append(gh_tool)
            seen.add(gh_name)

    return tool_defs


# ---------------------------------------------------------------------------
# Context management — keep token count bounded across iterations
# ---------------------------------------------------------------------------


def _truncate_tool_results(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Truncate oversized tool-result content to ``_MAX_TOOL_RESULT_CHARS``.

    Tool calls such as ``read_file`` and ``run_command`` can return tens of
    thousands of characters.  Every subsequent LLM turn pays full input-token
    price for that content, so we cap it here.  The model still sees the
    beginning and a clear truncation marker.
    """
    out: list[dict[str, object]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            raw = msg.get("content", "")
            if isinstance(raw, str) and len(raw) > _MAX_TOOL_RESULT_CHARS:
                msg = dict(msg)
                msg["content"] = (
                    raw[:_MAX_TOOL_RESULT_CHARS]
                    + f"\n... [truncated — {len(raw) - _MAX_TOOL_RESULT_CHARS} chars omitted]"
                )
        out.append(msg)
    return out


def _prune_history(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop old turns from the middle of the message history.

    Keeps:
    - The first message (always the task briefing — acts as a persistent anchor).
    - The most-recent ``_HISTORY_TAIL`` messages, trimmed so they start on an
      ``assistant`` turn.

    Starting on an assistant turn is required: the Anthropic API enforces
    strict user→assistant alternation.  Inserting a sentinel ``user`` message
    before a ``tool`` message would produce two consecutive non-assistant
    messages and result in a 400.  We instead splice directly from the first
    ``assistant`` message in the tail so the structure is always:

        user (task briefing) → assistant → tool(s) → assistant → …
    """
    if len(messages) <= _MAX_HISTORY_MESSAGES:
        return messages

    tail = messages[-_HISTORY_TAIL:]

    # Advance past any leading tool/user messages in the tail so we start on
    # an assistant turn.  This preserves the required alternating structure.
    start = next(
        (i for i, m in enumerate(tail) if m.get("role") == "assistant"),
        0,
    )
    tail = tail[start:]

    if not tail:
        return messages  # safety: nothing to prune without breaking structure

    return messages[:1] + tail


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool_calls(
    tool_calls: list[ToolCall],
    worktree_path: Path,
    run_id: str,
    *,
    github_client: GitHubMCPClient | None = None,
    github_tool_names: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Execute each tool call and return a list of tool-result messages.

    Routing priority:
    1. Local file/shell tools → dispatched directly.
    2. GitHub MCP tool names  → forwarded to :class:`GitHubMCPClient`.
    3. Everything else         → forwarded to :func:`~agentception.mcp.server.call_tool_async`.

    Args:
        tool_calls: Tool calls returned by the model.
        worktree_path: Worktree root used as the default cwd for shell calls
            and the base for resolving relative file paths.
        run_id: Used for logging only.
        github_client: Initialised GitHub MCP client (optional).
        github_tool_names: Set of tool names routed to the GitHub MCP server.

    Returns:
        A list of ``{"role": "tool", "tool_call_id": str, "content": str}``
        messages ready to extend the conversation history.
    """
    results: list[dict[str, object]] = []
    for tc in tool_calls:
        result = await _dispatch_single_tool(
            tc,
            worktree_path,
            run_id,
            github_client=github_client,
            github_tool_names=github_tool_names,
        )
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
    *,
    github_client: GitHubMCPClient | None = None,
    github_tool_names: frozenset[str] = frozenset(),
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

    if name in _LOCAL_TOOL_NAMES:
        return await _dispatch_local_tool(name, args, worktree_path)

    if name in github_tool_names and github_client is not None:
        try:
            text = await github_client.call_tool(name, args)
            return {"ok": True, "result": text}
        except RuntimeError as exc:
            logger.error("❌ github_mcp tool %s failed: %s", name, exc)
            return {"ok": False, "error": str(exc)}

    return _mcp_result_to_dict(await call_tool_async(name, args))


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

    if name == "replace_in_file":
        path_raw = args.get("path")
        old_raw = args.get("old_string")
        new_raw = args.get("new_string")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "replace_in_file: 'path' must be a string"}
        if not isinstance(old_raw, str):
            return {"ok": False, "error": "replace_in_file: 'old_string' must be a string"}
        if not isinstance(new_raw, str):
            return {"ok": False, "error": "replace_in_file: 'new_string' must be a string"}
        allow_raw = args.get("allow_multiple", False)
        allow = bool(allow_raw) if isinstance(allow_raw, bool) else False
        return replace_in_file(
            _resolve(path_raw, worktree_path),
            old_raw,
            new_raw,
            allow_multiple=allow,
        )

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
