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
import json
import logging
import re
import sys
import time
from pathlib import Path

from sqlalchemy import select

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACAgentRun
from agentception.db.queries import get_run_by_id
from agentception.db.persist import accumulate_token_usage, persist_agent_messages_async
from agentception.mcp.build_commands import build_cancel_run, build_complete_run
from agentception.mcp.log_tools import log_run_error, log_run_step, log_file_edit_event
from agentception.workflow.status import is_terminal
from agentception.mcp.prompts import get_prompt
from agentception.mcp.server import TOOLS, call_tool_async
from agentception.mcp.types import ACToolResult
from agentception.models import AgentTaskSpec, FileEditEvent
from agentception.services.llm import (
    ToolCall,
    ToolDefinition,
    ToolFunction,
    ToolResponse,
    _HAIKU_MODEL,
    _MODEL,
    call_anthropic,
    call_anthropic_with_tools,
)
from agentception.services.code_indexer import search_codebase
from agentception.services.github_mcp_client import GitHubMCPClient
from agentception.tools.definitions import (
    FILE_TOOL_DEFS,
    FIND_CALL_SITES_TOOL_DEF,
    GIT_COMMIT_AND_PUSH_TOOL_DEF,
    READ_SYMBOL_TOOL_DEF,
    READ_WINDOW_TOOL_DEF,
    SEARCH_CODEBASE_TOOL_DEF,
    SHELL_TOOL_DEF,
    UPDATE_WORKING_MEMORY_TOOL_DEF,
)
from agentception.services.working_memory import (
    WorkingMemory,
    merge_memory,
    read_memory,
    render_memory,
    write_memory,
)
from agentception.tools.file_tools import (
    find_call_sites,
    insert_after_in_file,
    list_directory,
    read_file,
    read_file_lines,
    read_symbol,
    read_window,
    replace_in_file,
    search_text,
    write_file,
)
from agentception.tools.shell_tools import git_commit_and_push, run_command

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
_DEFAULT_MAX_ITERATIONS = 100

# ---------------------------------------------------------------------------
# Loop guard — runtime enforcement for write-first behaviour
# ---------------------------------------------------------------------------

# Tools that constitute meaningful progress for loop-guard purposes.
# Any iteration containing at least one of these resets the no-write counter.
# run_command is included because the developer agent uses it for git operations
# (commit, push) — without it the guard fires immediately after every shell
# command and forces redundant file writes.
# create_pull_request and build_complete_run are terminal actions; including
# them prevents the guard from firing after the agent has already finished.
_WRITE_TOOL_NAMES: frozenset[str] = frozenset({
    "replace_in_file",
    "write_file",
    "insert_after_in_file",
    "git_commit_and_push",
    "run_command",
    "create_pull_request",
    "build_complete_run",
    "build_cancel_run",
})

# Subset of write tools that mutate a specific file and carry a `file_path`
# argument.  Used by the write-journal to record which files have been touched
# so the agent can see that evidence even after the history is pruned.
_FILE_MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "replace_in_file",
    "write_file",
    "insert_after_in_file",
})

# Tools whose arguments carry a search query we want to track for
# repeated symbol searches (symbol-absence heuristic).
_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({
    "search_codebase",
    "search_text",
})

# Minimum consecutive no-write iterations before the loop-guard fires.
# The actual per-run threshold is max(_LOOP_GUARD_THRESHOLD, max_iterations // 10),
# computed at loop start — so a 20-iteration executor gets 2, a 100-iteration
# developer gets 10, a 200-iteration deep task gets 20. Floor at 2 so the
# guard always fires eventually even on minimal budgets.
_LOOP_GUARD_THRESHOLD: int = 2

# Number of iterations remaining at which the final-stretch warning fires.
# Once remaining <= this value, the agent is told to stop exploring and ship.
_FINAL_STRETCH_THRESHOLD: int = 15

# ---------------------------------------------------------------------------
# Pytest hard-stop — mechanically enforced once pytest exits 0
# ---------------------------------------------------------------------------
# When a run_command call containing "pytest" returns exit_code=0, the loop
# arms a hard-stop interrupt.  On every subsequent iteration, these read-only
# tools are intercepted and returned as synthetic errors — the same mechanism
# used by the loop guard — so the agent cannot enter a post-test audit loop.
# The stop is disarmed if the agent writes new code (file-mutating tools)
# after the passing test run, because the new code is untested.
_PYTEST_STOP_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_file_lines",
    "search_text",
    "search_codebase",
    "list_directory",
    "find_call_sites",
    "read_symbol",
    "read_window",
    "update_working_memory",
})

# Injected every turn once pytest_clean_since is set.
_PYTEST_STOP_OVERRIDE: str = (
    "🛑 HARD STOP — pytest exited 0 on iteration {iteration}.\n\n"
    "Step 3 of your execution contract is now mechanically enforced. "
    "File reads, searches, and diagnostics are LOCKED.\n\n"
    "Your only permitted actions:\n"
    "1. Commit and push — use `run_command` with `git add -A && git commit -m '...' && git push origin HEAD` "
    "(or `git_commit_and_push` if it appears in your tool list).\n"
    "2. `create_pull_request` — open a PR against dev.\n"
    "3. `build_complete_run` — mark the run complete.\n\n"
    "Any read or search tool call will be rejected with a synthetic error. "
    "Do not audit. Do not re-verify. Commit and ship."
)

# ---------------------------------------------------------------------------
# Developer agent — minimal tool surface
# ---------------------------------------------------------------------------
# Cursor completes developer tasks in 5–15 tool calls because it has only
# read/write/run tools.  Our agents were exposed to 74 tools (logging,
# GitHub MCP, semantic search, working-memory…) and spent 60-70% of their
# budget calling non-coding tools.
#
# When the loaded task role is "developer", the tool list is filtered to this
# allowlist before the main loop starts.  The agent cannot even see bookkeeping
# tools, so it cannot call them.  Fewer choices = faster decisions = fewer
# wasted iterations.
_DEVELOPER_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    # Read tools — allowed in normal mode; stripped by loop guard after 2 reads.
    "read_file",
    "read_file_lines",
    "search_text",
    "list_directory",
    # Write tools — always permitted; calling any resets the guard counter.
    "write_file",
    "replace_in_file",
    "insert_after_in_file",
    # Execution — run mypy, tests, git commands.
    "run_command",
    # Completion — the only way to end the loop.
    "build_complete_run",
    "build_cancel_run",
    # PR — open a pull request when done.
    "create_pull_request",
    "add_issue_comment",
})

# ---------------------------------------------------------------------------
# Reviewer agent — read-only surface for gatekeeping PRs
# ---------------------------------------------------------------------------
# The reviewer inspects a diff, reads files for context, runs mypy/pytest,
# then either merges (grade A/B) or rejects (grade C/D/F) by calling
# build_complete_run.  It must never write, create PRs, or spawn children.
# The allowlist is intentionally narrow: fewer choices = faster decisions.
#
# GitHub MCP tools are loaded dynamically; their names must exactly match
# what the GitHub MCP server advertises (see mcps/user-github/tools/).
_REVIEWER_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    # Read — inspect files in the worktree for context.
    "read_file",
    "read_file_lines",
    "search_text",
    "list_directory",
    # Shell — git diff, mypy, pytest (read-only commands).
    "run_command",
    # GitHub MCP — inspect issue and PR, post review, merge.
    "issue_read",
    "pull_request_read",
    "pull_request_review_write",
    "merge_pull_request",
    "add_issue_comment",
    "issue_write",
    "update_pull_request",
    "list_pull_requests",
    # Completion — the only two ways to end the loop.
    "build_complete_run",
    "build_cancel_run",
})

# Hard cap on reviewer iterations.  With warmup pre-loading all context,
# a reviewer grades and acts in ≤5 iterations.  20 gives headroom for edge
# cases where the reviewer needs to request changes and wait for context.
_REVIEWER_MAX_ITERATIONS = 20

# When the loop guard fires, these tools remain available.  The set must
# include run_command and git_commit_and_push so a guarded agent can still
# run mypy/pytest to verify its own writes and then commit and push to ship.
# Without them, a guarded agent writes code with no path to verify or deliver
# it — the guard would correctly fire, force a write, then fire again forever.
_GUARD_PERMITTED_TOOL_NAMES: frozenset[str] = frozenset({
    # Code-mutation tools — the primary target of the guard.
    "write_file",
    "replace_in_file",
    "insert_after_in_file",
    # Shell — needed to run mypy/pytest (verification) and git (delivery).
    "run_command",
    "git_commit_and_push",
    # Completion — the only way to close the loop.
    "build_complete_run",
    "build_cancel_run",
    "create_pull_request",
    "add_issue_comment",
})

# System-block text still injected alongside tool narrowing so the model
# understands WHY its tool palette shrank.
_LOOP_GUARD_OVERRIDE = """\
⚠️  LOOP GUARD — {n} ITERATIONS WITHOUT WRITING CODE

The files you need are already in your task briefing under "Pre-loaded Files".
Read-only tools have been removed. You can only call write tools right now.

Take the first uncompleted item from your next_steps. Write the implementation
using write_file or replace_in_file. If the symbol does not exist, create it —
absence is the task, not a blocker. Writing resets this guard.
"""

# Injected every turn (via extra_blocks) once any file has been written.
# Lives outside the prunable history window so the agent always knows which
# files it has already modified, even after the middle of history is dropped.
_WRITE_JOURNAL_HEADER = """\
📝  FILES MODIFIED THIS SESSION — do NOT re-implement these; they are already done.

{entries}

If a file you need to check is listed above, read only the specific lines that
are relevant — do not re-read the whole file.  All writes in the list are
already committed to disk.  Your next action should be the NEXT uncompleted
task, not a re-implementation of what is listed.
"""

# Injected on every iteration once the remaining budget falls to or below
# _FINAL_STRETCH_THRESHOLD.  Tells the agent to stop exploring and ship.
_FINAL_STRETCH_WARNING: str = (
    "⚠️ FINAL STRETCH — {remaining} iterations remaining.\n\n"
    "Stop all discovery, reading, and planning immediately.\n"
    "You must now:\n"
    "1. Run mypy on every file you modified.\n"
    "2. Run pytest on the affected test modules.\n"
    "3. Fix any errors found.\n"
    "4. git add -A && git commit.\n"
    "5. git push && create_pull_request && build_complete_run.\n\n"
    "Do NOT call read_file, read_file_lines, search_text, or "
    "search_codebase. Only write/fix/commit/push/PR tools are permitted."
)

# Injected on every iteration where last_input_tokens exceeds
# _CONTEXT_PRESSURE_THRESHOLD.  Tells the agent the context window is filling
# and to avoid expensive reads that accelerate truncation.
_CONTEXT_PRESSURE_WARNING: str = (
    "⚠️ CONTEXT PRESSURE — {tokens_k}K input tokens consumed this turn.\n\n"
    "The context window is filling. To avoid truncation:\n"
    "- Do NOT read large files. Read only specific line ranges.\n"
    "- Do NOT repeat searches you have already run.\n"
    "- Prefer replace_in_file over write_file (smaller diffs).\n"
    "- Complete your remaining work and call build_complete_run soon.\n"
    "Remaining context budget: approximately {remaining_k}K tokens."
)

# Injected when the agent has searched for the same query twice.
_SYMBOL_ABSENCE_OVERRIDE = """\
⚠️  SYMBOL ABSENCE — "{query}" NOT FOUND AFTER REPEATED SEARCH

You have searched for this term more than once. It does not exist in the codebase.

Stop searching. Create it. Write the minimal implementation now.
A new class, function, or field written incorrectly and then fixed is faster
than searching a third time for something that was never there.
"""

# Per-tool character limits applied before tool results enter history.
# File-read outputs can be 10-50k chars; search outputs 5-15k.  Applying a
# single flat cap of 3k was the root cause of agents re-reading files they
# had already fetched — the first read was truncated and looked incomplete.
# These limits are generous but finite; the agent still sees the full start
# of every result with a clear truncation marker when the limit is hit.
_TOOL_RESULT_CHAR_LIMITS: dict[str, int] = {
    "read_file": 12_000,
    "read_file_lines": 12_000,
    "read_symbol": 8_000,   # full function bodies; generous but bounded
    "read_window": 10_000,  # centered window, slightly more than read_file_lines
    "find_call_sites": 5_000,
    "search_codebase": 8_000,
    "search_text": 5_000,
    "run_command": 5_000,
    "git_commit_and_push": 3_000,
    "list_directory": 2_000,
}
_DEFAULT_TOOL_RESULT_CHARS: int = 3_000

# When the message history (excluding system) exceeds this count, old turns
# are dropped from the middle.  The first user message (task briefing) and the
# most-recent _HISTORY_TAIL messages are always kept.
_MAX_HISTORY_MESSAGES: int = 20
_HISTORY_TAIL: int = 14
_MAX_INPUT_TOKEN_ESTIMATE: int = 140_000  # token-budget prune target
_CONTEXT_PRESSURE_THRESHOLD: int = 100_000  # warn threshold (used in a later issue)

# ---------------------------------------------------------------------------
# Token-rate guard — proactive pacing between consecutive LLM calls.
# ---------------------------------------------------------------------------

# Minimum seconds between consecutive LLM calls.  A fixed cadence beats a
# reactive burst-then-sleep TPM guard.  Calibrated for **Tier 4** (2M input /
# 400K output TPM, 4K RPM): the default 0.5 s floor allows ~10 concurrent agents
# at ~1 000 output tokens per turn before the output-TPM cap is reached.
# Tunable via the ``AC_MIN_TURN_DELAY_SECS`` env var (see config.py).
_last_llm_call_at: float = 0.0


async def _enforce_turn_delay() -> None:
    """Sleep until settings.ac_min_turn_delay_secs has elapsed since the last LLM call.

    The timestamp is updated by the caller *after* call_anthropic_with_tools
    returns, so retry backoff inside the LLM call does not eat into the next
    window.  If the previous turn's tool dispatch already consumed the full
    window this returns immediately.
    """
    min_delay = settings.ac_min_turn_delay_secs
    elapsed = time.monotonic() - _last_llm_call_at
    wait = min_delay - elapsed
    if wait > 0.0:
        logger.info("⏳ agent_loop: inter-turn delay — sleeping %.1fs", wait)
        await asyncio.sleep(wait)


def _record_llm_call() -> None:
    """Stamp the completion time of the most recent LLM call."""
    global _last_llm_call_at
    _last_llm_call_at = time.monotonic()


# Local tool names — dispatched to file/shell functions rather than MCP.
_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "read_file_lines",
        "read_symbol",
        "read_window",
        "find_call_sites",
        "replace_in_file",
        "insert_after_in_file",
        "write_file",
        "list_directory",
        "search_text",
        "run_command",
        "git_commit_and_push",
        "search_codebase",
        "update_working_memory",
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

    role_prompt = _load_role_prompt(task.role, task.prompt_variant)
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

    all_tool_defs = _build_tool_definitions(extra_tools=github_tools)

    # Developer agents use a minimal tool surface — only coding tools.
    # Reviewer agents get a read-and-GitHub-only surface — no file writes.
    # All other roles (planner, etc.) get the full tool catalogue.
    if task.role == "developer":
        tool_defs = [
            t for t in all_tool_defs
            if t["function"]["name"] in _DEVELOPER_TOOL_ALLOWLIST
        ]
        logger.info(
            "✅ agent_loop: developer tool surface — %d tools (of %d total stripped to allowlist)",
            len(tool_defs), len(all_tool_defs),
        )
    elif task.role == "reviewer":
        tool_defs = [
            t for t in all_tool_defs
            if t["function"]["name"] in _REVIEWER_TOOL_ALLOWLIST
        ]
        # Also enforce a tighter iteration cap so a stuck reviewer self-terminates
        # well before the global ceiling of 100 turns.
        max_iterations = min(max_iterations, _REVIEWER_MAX_ITERATIONS)
        logger.info(
            "✅ agent_loop: reviewer tool surface — %d tools (of %d total), "
            "iteration cap set to %d",
            len(tool_defs), len(all_tool_defs), max_iterations,
        )
    else:
        tool_defs = all_tool_defs

    initial_message = await _fetch_task_briefing(run_id, task, worktree_path)

    messages: list[dict[str, object]] = [{"role": "user", "content": initial_message}]

    logger.info(
        "✅ agent_loop start — run_id=%s issue=%d tools=%d (github_mcp=%d)",
        run_id,
        issue_number,
        len(tool_defs),
        len(github_tool_names),
    )

    # Pre-loop context injection — role-specific, runs before iteration 1.
    #
    # developer → skip recon (task briefing supplies all context)
    # reviewer  → deterministic warmup: diff + mypy + pytest + issue pre-computed
    #             and injected so the reviewer needs 0 discovery tool calls
    # all other → LLM-driven recon (reads/searches the agent requests)
    if task.role == "developer":
        pass  # no recon needed — task briefing supplies all context
    elif task.role == "reviewer":
        _gh_repo_raw = task.gh_repo or settings.gh_repo
        _gh_repo = str(_gh_repo_raw) if isinstance(_gh_repo_raw, str) else ""
        _owner, _, _repo_name = _gh_repo.partition("/")
        _pr_branch = task.branch or f"feat/issue-{issue_number}"
        await _run_reviewer_warmup(
            worktree_path=worktree_path,
            pr_branch=_pr_branch,
            issue_number=issue_number,
            messages=messages,
            github_client=github_client,
            owner=_owner,
            repo=_repo_name,
        )
    else:
        await _run_recon_phase(run_id, worktree_path, messages, system_prompt)

    # Loop-guard state — reset by any write tool call, incremented every
    # iteration that produces only reads/searches/memory-updates.
    # Disabled for reviewer: the reviewer's workflow is inherently
    # read-heavy (diff, issue, code) before taking a single merge/reject
    # action.  The guard was designed for code-writers; applying it to a
    # reviewer intercepts merge_pull_request and forces confusing retries.
    # The iteration ceiling (100) is the backstop for runaway reviewers.
    loop_guard_enabled: bool = task.role != "reviewer"
    # Scale the guard threshold with the iteration budget so a 10-iteration
    # developer (threshold=2) gets tighter enforcement than a 100-iteration
    # developer working through a large file (threshold=10).  Floor at 2 so
    # the guard always fires eventually, even on minimal budgets.
    loop_guard_threshold: int = max(2, max_iterations // 10)
    iterations_since_write: int = 0
    # Maps normalised search query → how many times it has been used this run.
    # When a query appears >= 2 times the symbol is declared absent and the
    # agent is instructed to create it rather than search again.
    search_query_counts: dict[str, int] = {}
    # Tracks which absent symbols have already triggered an injection so we
    # don't spam the same message every subsequent iteration.
    symbol_absence_injected: set[str] = set()
    # Write journal: file path → number of mutations applied this session.
    # Injected into extra_blocks every turn so it survives history pruning.
    files_written: dict[str, int] = {}
    # Set to the iteration number when pytest last exited 0.  None means the
    # hard-stop interrupt is disarmed.  Reset when new code is written after
    # the clean run, or when a subsequent pytest run fails.
    pytest_clean_since: int | None = None
    # Real input token count from the most recent LLM response.  Seeded at 0
    # so the first iteration's _prune_history call skips the token-budget path.
    last_input_tokens: int = 0

    for iteration in range(1, max_iterations + 1):
        await log_run_step(
            issue_number,
            f"Step {iteration}",
            run_id,
        )

        # Guard: if an MCP tool (e.g. build_cancel_run called by the agent
        # itself) has already transitioned this run to a terminal state, stop
        # the loop before the next LLM call.  Without this check the reaper
        # would eventually kill the worktree while the loop is still running.
        _run_row = await get_run_by_id(run_id)
        if _run_row is not None and is_terminal(_run_row["status"]):
            logger.info(
                "✅ agent_loop: run %s is already in terminal state %r — stopping loop",
                run_id,
                _run_row["status"],
            )
            await github_client.close()
            return

        # Proactive inter-turn pacing.  _last_llm_call_at is stamped *after*
        # call_anthropic_with_tools returns (including any retry backoff), so
        # the full _MIN_TURN_DELAY_SECS gap is always preserved between the end
        # of one LLM interaction and the start of the next.
        await _enforce_turn_delay()

        # Read working memory and render it as a secondary system block.
        # This is injected fresh every turn OUTSIDE the prunable history so
        # the agent always has its scratch-pad regardless of how many turns
        # have been pruned.  The main system-prompt cache is not invalidated
        # because the working memory is a separate, un-cached block.
        memory = read_memory(worktree_path)
        extra_blocks: list[dict[str, object]] = []
        if memory:
            extra_blocks.append({"type": "text", "text": render_memory(memory)})

        # Write journal — injected outside the prunable history window so the
        # agent always knows which files it has already modified.  Without this,
        # once the history is pruned to _HISTORY_TAIL messages the agent loses
        # evidence of its own writes and loops re-implementing the same code.
        if files_written:
            entries = "\n".join(
                f"  • {path} ({count} write{'s' if count > 1 else ''})"
                for path, count in sorted(files_written.items())
            )
            extra_blocks.append({
                "type": "text",
                "text": _WRITE_JOURNAL_HEADER.format(entries=entries),
            })

        # Loop-guard enforcement — fires when the agent has not written any code
        # for loop_guard_threshold consecutive iterations (scaled to max_iterations).
        #
        # The tool list is intentionally kept CONSTANT across all iterations
        # (no narrowing when guard fires).  Changing the tool list busts
        # Anthropic's prompt cache on the tool-catalogue block, turning every
        # guarded turn from a cheap cache-read into a full cache-write.
        # Enforcement is via interception only: calls to non-write tools during
        # guard mode are rejected with a synthetic error.
        #
        # Instead, interception alone enforces the guard: the model is sent the
        # full tool list, but any call to a non-permitted tool during guard mode
        # is caught AFTER the LLM response and returned as a synthetic error.
        # The model sees the error, understands it cannot read, and calls a
        # write tool on the next turn — same behavioural outcome, no cache bust.
        guard_active = (
            loop_guard_enabled
            and iteration > loop_guard_threshold
            and iterations_since_write >= loop_guard_threshold
        )
        # Always pass the full (constant) tool list so the cache key is stable.
        active_tool_defs: list[ToolDefinition] = tool_defs
        if guard_active:
            override_text = _LOOP_GUARD_OVERRIDE.format(n=iterations_since_write)
            extra_blocks.append({"type": "text", "text": override_text})
            logger.warning(
                "⚠️ loop_guard fired — run_id=%s iteration=%d iterations_since_write=%d"
                " (interception-only, tool list unchanged for cache stability)",
                run_id, iteration, iterations_since_write,
            )

        # Symbol-absence injection — fires once per repeated search query.
        for query, count in search_query_counts.items():
            if count >= 2 and query not in symbol_absence_injected:
                absence_text = _SYMBOL_ABSENCE_OVERRIDE.format(query=query)
                extra_blocks.append({"type": "text", "text": absence_text})
                symbol_absence_injected.add(query)
                logger.warning(
                    "⚠️ symbol_absence fired — run_id=%s query=%r count=%d",
                    run_id, query, count,
                )

        # Final-stretch escalation — fires on every iteration once the remaining
        # budget falls to or below _FINAL_STRETCH_THRESHOLD.  Independent of
        # loop_guard: both can be active simultaneously.
        remaining: int = max_iterations - iteration
        if remaining <= _FINAL_STRETCH_THRESHOLD:
            extra_blocks.append({
                "type": "text",
                "text": _FINAL_STRETCH_WARNING.format(remaining=remaining),
            })
            logger.warning(
                "⚠️ final_stretch — run_id=%s iteration=%d remaining=%d",
                run_id, iteration, remaining,
            )

        # Context-pressure warning — fires on every iteration where the previous
        # turn consumed more than _CONTEXT_PRESSURE_THRESHOLD input tokens.
        # last_input_tokens is 0 on the first iteration (before any LLM call),
        # so the condition is naturally False and the warning is never shown then.
        if last_input_tokens > _CONTEXT_PRESSURE_THRESHOLD:
            tokens_k = last_input_tokens // 1000
            remaining_k = max(0, (200_000 - last_input_tokens) // 1000)
            extra_blocks.append({
                "type": "text",
                "text": _CONTEXT_PRESSURE_WARNING.format(
                    tokens_k=tokens_k, remaining_k=remaining_k
                ),
            })
            logger.warning(
                "⚠️ context_pressure — run_id=%s iter=%d input_tokens=%d",
                run_id,
                iteration,
                last_input_tokens,
            )

        # Pytest hard-stop escalation — fires every iteration after pytest
        # exits 0.  Independent of loop_guard and final_stretch.  Not applied
        # to reviewers (whose workflow is intentionally read-heavy).
        _pytest_clean_iter = pytest_clean_since
        pytest_stop_active: bool = (
            _pytest_clean_iter is not None and task.role != "reviewer"
        )
        if pytest_stop_active and _pytest_clean_iter is not None:
            extra_blocks.append({
                "type": "text",
                "text": _PYTEST_STOP_OVERRIDE.format(iteration=_pytest_clean_iter),
            })
            logger.warning(
                "⚠️ pytest_stop active — run_id=%s current_iter=%d armed_at=%d",
                run_id, iteration, _pytest_clean_iter,
            )

        try:
            bounded = await _prune_history(_truncate_tool_results(messages), last_input_tokens=last_input_tokens)
            _active_model = _HAIKU_MODEL if task.role == "reviewer" else _MODEL
            response: ToolResponse = await call_anthropic_with_tools(
                bounded,
                system=system_prompt,
                tools=active_tool_defs,
                model=_active_model,
                extra_system_blocks=extra_blocks or None,
            )
        except Exception as exc:
            _record_llm_call()  # stamp even on error so next delay is measured correctly
            logger.exception("❌ agent_loop LLM error on iteration %d", iteration)
            await github_client.close()
            await log_run_error(issue_number, f"LLM error: {exc}", run_id)
            await build_cancel_run(run_id)
            return

        _record_llm_call()  # stamp after successful response — this is the reference point for the next delay

        # Track real input tokens so _prune_history can apply the token-budget
        # path on the *next* iteration if the context is growing too large.
        last_input_tokens = int(response.get("input_tokens", 0) or 0)

        # Accumulate real token counts for cost tracking.  Fire-and-forget so
        # a DB hiccup never interrupts the agent loop.
        asyncio.create_task(
            accumulate_token_usage(
                run_id=run_id,
                input_tokens=response.get("input_tokens", 0),
                output_tokens=response.get("output_tokens", 0),
                cache_write_tokens=response.get("cache_creation_input_tokens", 0),
                cache_read_tokens=response.get("cache_read_input_tokens", 0),
            ),
            name=f"token-accum-{run_id}-{iteration}",
        )

        last_input_tokens = response.get("input_tokens", 0)
        logger.info(
            "📊 context: iter=%d input_tokens=%d output_tokens=%d cache_hit=%d",
            iteration,
            last_input_tokens,
            response.get("output_tokens", 0),
            response.get("cache_read_input_tokens", 0),
        )

        # Append assistant message to history before persisting so the full
        # conversation (including the new assistant reply) is written to DB.
        # persist_agent_messages_async is fire-and-forget via asyncio.create_task.
        assistant_msg: dict[str, object] = {"role": "assistant", "content": response["content"]}
        if response["tool_calls"]:
            assistant_msg["tool_calls"] = list(response["tool_calls"])
        messages.append(assistant_msg)

        await persist_agent_messages_async(run_id, messages)

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

        if response["stop_reason"] in ("tool_calls", "length") and response["tool_calls"]:
            # During guard mode: intercept read-only tool calls and return
            # synthetic error results BEFORE dispatching.  The model "remembers"
            # tools from prior iterations and may call them even when they are
            # absent from active_tool_defs — returning an error forces it to
            # acknowledge the constraint and switch to a write tool.
            tc_to_dispatch: list[ToolCall] = []
            synthetic_errors: list[dict[str, object]] = []
            if guard_active:
                for tc in response["tool_calls"]:
                    tc_name = tc["function"]["name"]
                    if tc_name not in _GUARD_PERMITTED_TOOL_NAMES:
                        synthetic_errors.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps({
                                "ok": False,
                                "error": (
                                    f"GUARD MODE: '{tc_name}' is unavailable. "
                                    f"You have not written code for "
                                    f"{iterations_since_write} iterations. "
                                    "Call write_file or replace_in_file to "
                                    "implement code; that will restore all tools "
                                    "including run_command and read_file."
                                ),
                            }),
                        })
                    else:
                        tc_to_dispatch.append(tc)
                if synthetic_errors:
                    logger.warning(
                        "⚠️ loop_guard intercepted %d read call(s) — run_id=%s",
                        len(synthetic_errors), run_id,
                    )
            else:
                tc_to_dispatch = list(response["tool_calls"])

            # Pass 2: pytest hard-stop interception (independent of loop guard).
            # Any read/search tool that made it past Pass 1 is blocked here when
            # pytest_stop_active.  This runs on tc_to_dispatch (not the full list)
            # so guard-intercepted tools are not double-counted.
            if pytest_stop_active:
                unblocked: list[ToolCall] = []
                for tc in tc_to_dispatch:
                    tc_name = tc["function"]["name"]
                    if tc_name in _PYTEST_STOP_BLOCKED_TOOLS:
                        synthetic_errors.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps({
                                "ok": False,
                                "error": (
                                    f"HARD STOP: pytest exited 0 on iteration "
                                    f"{pytest_clean_since}. Reading, searching, "
                                    "and diagnostics are locked. Commit via "
                                    "run_command (git add/commit/push) or "
                                    "git_commit_and_push if available, then "
                                    "create_pull_request and build_complete_run."
                                ),
                            }),
                        })
                    else:
                        unblocked.append(tc)
                if len(unblocked) < len(tc_to_dispatch):
                    logger.warning(
                        "⚠️ pytest_stop intercepted %d tool call(s) — run_id=%s iteration=%d",
                        len(tc_to_dispatch) - len(unblocked), run_id, iteration,
                    )
                tc_to_dispatch = unblocked

            tool_results: list[dict[str, object]] = []
            if tc_to_dispatch:
                tool_results = await _dispatch_tool_calls(
                    tc_to_dispatch,
                    worktree_path,
                    run_id,
                    github_client=github_client,
                    github_tool_names=github_tool_names,
                )
            messages.extend(synthetic_errors + tool_results)

            # Loop-guard bookkeeping: track writes and repeated searches.
            tool_names_this_iter: set[str] = {
                tc["function"]["name"] for tc in response["tool_calls"]
            }
            if tool_names_this_iter & _WRITE_TOOL_NAMES:
                iterations_since_write = 0
            else:
                iterations_since_write += 1

            # Write-journal bookkeeping: record which files were mutated so
            # the agent can see that evidence even after history is pruned.
            for tc in response["tool_calls"]:
                if tc["function"]["name"] not in _FILE_MUTATING_TOOL_NAMES:
                    continue
                try:
                    write_args: dict[str, object] = json.loads(tc["function"]["arguments"])
                    fp = str(write_args.get("file_path", "")).strip()
                    if fp:
                        files_written[fp] = files_written.get(fp, 0) + 1
                except (json.JSONDecodeError, AttributeError):
                    pass

            # Emit file_edit events to the DB so the inspector SSE stream picks
            # them up. One event per file write, in tool-call order.
            # event_type: "file_edit" | payload: FileEditEvent.model_dump()
            mem = read_memory(worktree_path)
            raw_written: object = mem.get("files_written", []) if mem else []
            written_events: list[FileEditEvent] = (
                [e for e in raw_written if isinstance(e, FileEditEvent)]
                if isinstance(raw_written, list) else []
            )
            for tc in response["tool_calls"]:
                if tc["function"]["name"] not in _FILE_MUTATING_TOOL_NAMES:
                    continue
                try:
                    tc_args: dict[str, object] = json.loads(tc["function"]["arguments"])
                    path_raw = str(
                        tc_args.get("path", tc_args.get("file_path", ""))
                    ).strip()
                except (json.JSONDecodeError, AttributeError):
                    path_raw = ""
                if not path_raw:
                    continue
                matching = [e for e in written_events if e.path == path_raw]
                if matching:
                    await log_file_edit_event(
                        issue_number,
                        matching[-1],
                        agent_run_id=run_id,
                    )

            # Accumulate search queries for symbol-absence detection.
            for tc in response["tool_calls"]:
                if tc["function"]["name"] not in _SEARCH_TOOL_NAMES:
                    continue
                try:
                    search_args: dict[str, object] = json.loads(tc["function"]["arguments"])
                    raw_query = search_args.get("query", search_args.get("pattern", ""))
                    query_str = str(raw_query).strip()[:100]
                    if query_str:
                        search_query_counts[query_str] = (
                            search_query_counts.get(query_str, 0) + 1
                        )
                except (json.JSONDecodeError, AttributeError):
                    pass

            # Pytest hard-stop bookkeeping.
            #
            # Arm: if any dispatched run_command call ran pytest and exited 0,
            # set pytest_clean_since so the hard-stop fires next iteration.
            # tc_to_dispatch and tool_results are parallel (asyncio.gather order).
            for tc, tr in zip(tc_to_dispatch, tool_results):
                if tc["function"]["name"] != "run_command":
                    continue
                try:
                    cmd_args: dict[str, object] = json.loads(tc["function"]["arguments"])
                    cmd_str = str(cmd_args.get("command", ""))
                except (json.JSONDecodeError, AttributeError):
                    continue
                if "pytest" not in cmd_str:
                    continue
                try:
                    result_data: dict[str, object] = json.loads(
                        str(tr.get("content", "{}"))
                    )
                    exit_code = result_data.get("exit_code")
                    if exit_code == 0:
                        pytest_clean_since = iteration
                        logger.info(
                            "✅ pytest_stop armed — run_id=%s iteration=%d",
                            run_id, iteration,
                        )
                    elif exit_code is not None and pytest_clean_since is not None:
                        # A subsequent pytest run failed — disarm so the agent
                        # can fix the code without being blocked.
                        logger.info(
                            "⚠️ pytest_stop disarmed (pytest failed) — run_id=%s"
                            " iteration=%d exit_code=%s",
                            run_id, iteration, exit_code,
                        )
                        pytest_clean_since = None
                except (json.JSONDecodeError, AttributeError, ValueError):
                    pass

            # Disarm if the agent wrote new code after the clean test run.
            # Only file-mutating tools (not git or run_command) trigger a reset,
            # and only when the write occurs AFTER the iteration that armed the stop.
            if (
                pytest_clean_since is not None
                and iteration > pytest_clean_since
                and tool_names_this_iter & _FILE_MUTATING_TOOL_NAMES
            ):
                logger.info(
                    "⚠️ pytest_stop disarmed (new code written) — run_id=%s"
                    " iteration=%d armed_at=%d",
                    run_id, iteration, pytest_clean_since,
                )
                pytest_clean_since = None

            continue

        # stop_reason="length" with no tool calls means the response was
        # genuinely truncated mid-generation — nothing actionable was produced.
        # (If tool_calls were present we already handled it in the branch above.)
        if response["stop_reason"] == "length":
            logger.warning(
                "⚠️ agent_loop stop_reason=length with no tool calls on iteration %d"
                " — response truncated mid-generation, injecting recovery hint",
                iteration,
            )
            # Inject a synthetic tool result that tells the model to produce a
            # smaller response.  This is cheaper than cancelling — the agent
            # may still finish the task in the next turn.
            messages.append({
                "role": "user",
                "content": (
                    "⚠️ Your previous response was cut off (max output tokens reached) "
                    "before you issued a tool call.  Please issue ONE tool call now — "
                    "no reasoning preamble.  If you need to write a large block of code, "
                    "split it into smaller replace_in_file calls targeting specific "
                    "sections rather than rewriting the whole file."
                ),
            })
            continue

        # Truly unexpected stop reason — cancel.
        logger.warning(
            "⚠️ agent_loop unexpected stop_reason=%r on iteration %d",
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
            prompt_variant=run.prompt_variant,
        )
    except Exception as exc:
        logger.error("❌ _load_task_from_db error for run_id=%s: %s", run_id, exc)
        return None


def _load_role_prompt(role: str | None, variant: str | None = None) -> str:
    """Return the Markdown content of the role file for *role*.

    When *variant* is provided and non-empty, the function first looks for a
    variant-specific file ``{role}-{variant}.md`` and returns its content if
    found.  If the variant file does not exist, it falls back to the base
    ``{role}.md`` file — the same behaviour as when *variant* is ``None``.

    Falls back to an empty string when the role is unknown or the file is
    missing, so the agent still has the system prompt's runtime note.
    """
    if not role:
        logger.warning("⚠️ _load_role_prompt — no role specified")
        return ""

    roles_dir = settings.repo_dir / ".agentception" / "roles"

    # Try the variant file first when a variant is requested.
    if variant:
        candidate = roles_dir / f"{role}-{variant}.md"
        if candidate.exists():
            logger.info("Loading role file: %s (variant=%s)", candidate, variant)
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "⚠️ _load_role_prompt — OS error reading %s: %s", candidate, exc
                )
                # Fall through to the base file.

    # Base (default) role file.
    role_path = roles_dir / f"{role}.md"
    logger.info("Loading role file: %s (variant=%s)", role_path, variant)
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
  - ✅ `python3 -m pytest agentception/tests/test_foo.py` (target specific test file)
  - ✅ `mypy --follow-imports=silent agentception/foo.py agentception/bar.py` (changed files only)
  - ❌ `docker compose exec agentception python3 -m pytest` (wrong — you are already inside)
  - ❌ `python3 -m mypy agentception/` (NEVER — full directory scan OOM-kills the container)
  - ❌ `python3 -m mypy agentception/ tests/` (NEVER — same reason)
  **mypy rule:** always use `mypy --follow-imports=silent <file1> <file2> …` on only the files
  you modified.  Full directory scans spawn a subprocess that cold-loads the entire project
  type graph (~1-2 GB extra RSS) on top of the loaded ONNX model weights and crash the container.
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
    tool_defs.append(GIT_COMMIT_AND_PUSH_TOOL_DEF)
    tool_defs.append(SEARCH_CODEBASE_TOOL_DEF)
    tool_defs.append(READ_SYMBOL_TOOL_DEF)
    tool_defs.append(READ_WINDOW_TOOL_DEF)
    tool_defs.append(FIND_CALL_SITES_TOOL_DEF)
    tool_defs.append(UPDATE_WORKING_MEMORY_TOOL_DEF)

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
# Recon phase — structured exploration before the main execution loop
# ---------------------------------------------------------------------------

# System addendum injected for the single pre-loop planning call.
# The model is asked to emit a compact JSON plan — no implementation yet.
_RECON_SYSTEM_ADDENDUM = """
---

## RECON MODE — output a JSON exploration plan ONLY

Before any implementation, emit exactly ONE JSON object:

```json
{
  "files": ["<relative paths of files most likely to need editing or serve as patterns — max 8>"],
  "searches": ["<natural language queries for search_codebase — focus on patterns/helpers to copy — max 5>"],
  "plan": "<one sentence: your implementation approach>"
}
```

Rules:
- Output ONLY the JSON object, nothing else.
- Do not implement anything yet.
- Maximum 8 files and 5 searches.
- Prefer files you know you will edit over files you are merely curious about.
- Note: files explicitly mentioned in the issue body are pre-loaded automatically — do not repeat them unless you need additional context beyond what is already injected.
"""

# Matches relative file paths that appear verbatim in issue text.
# Covers agentception/, tests/, scripts/, docs/ trees and common extensions.
_EXPLICIT_FILE_RE = re.compile(
    r"\b((?:agentception|tests|scripts|docs)/[\w/.-]+\.(?:py|md|j2|yaml|yml|ts|scss|html))\b"
)

# Characters to include per file in the recon bundle.
# 25 000 chars ≈ 600–700 lines — covers most source files completely.
_RECON_FILE_CHAR_LIMIT = 25_000


def _extract_explicit_file_paths(text: str) -> list[str]:
    """Return deduplicated relative file paths mentioned verbatim in *text*.

    Scans for paths matching ``agentception/…``, ``tests/…``, ``scripts/…``, or
    ``docs/…`` with a recognised extension.  Order is preserved; duplicates are
    removed.

    IMPORTANT: only the portion of *text* before the first ``\\n---\\n`` separator
    is scanned.  Everything after that separator is injected context (code chunks
    from semantic search, pre-loaded file content) — scanning it would pick up
    paths from *those* files and falsely label them as explicitly requested,
    causing the recon phase to load irrelevant files and pollute the agent's
    context.
    """
    sep = text.find("\n---\n")
    scan_text = text[:sep] if sep != -1 else text
    seen: set[str] = set()
    paths: list[str] = []
    for match in _EXPLICIT_FILE_RE.finditer(scan_text):
        p = match.group(1)
        if p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


class _ReconPlan:
    """Parsed output of the recon planning call."""

    __slots__ = ("files", "searches", "plan")

    def __init__(self, files: list[str], searches: list[str], plan: str) -> None:
        self.files = files
        self.searches = searches
        self.plan = plan


def _parse_recon_json(raw: str) -> _ReconPlan | None:
    """Extract and parse the JSON exploration plan from the model response.

    The model may wrap the JSON in markdown fences — we strip those first.
    Returns ``None`` when the response cannot be parsed.
    """
    text = raw.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        inner = [l for l in lines if not l.startswith("```")]
        text = "\n".join(inner).strip()

    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        data: object = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    files_raw = data.get("files", [])
    searches_raw = data.get("searches", [])
    plan_raw = data.get("plan", "")

    files: list[str] = (
        [f for f in files_raw if isinstance(f, str)][:8]
        if isinstance(files_raw, list)
        else []
    )
    searches: list[str] = (
        [s for s in searches_raw if isinstance(s, str)][:5]
        if isinstance(searches_raw, list)
        else []
    )
    plan_str = str(plan_raw) if isinstance(plan_raw, str) else ""

    if not files and not searches:
        return None

    return _ReconPlan(files=files, searches=searches, plan=plan_str)


async def _shell_capture(cmd: str, cwd: Path, timeout: int = 300) -> str:
    """Run *cmd* in a shell and return combined stdout+stderr as a string.

    Used by the reviewer warmup to pre-compute context before iteration 1.
    Never raises — failures are returned as an error string so the warmup
    bundle is always well-formed.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return f"(command timed out after {timeout}s)"
        return raw.decode(errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(error running command: {exc})"


async def _run_reviewer_warmup(
    worktree_path: Path,
    pr_branch: str,
    issue_number: int,
    messages: list[dict[str, object]],
    github_client: GitHubMCPClient | None,
    owner: str,
    repo: str,
) -> None:
    """Pre-compute all review signal and inject it into messages[0].

    Runs five deterministic steps — no LLM call, no discovery loop — and
    appends the results as a single bundle to the reviewer's initial message.
    After this runs the reviewer starts iteration 1 with everything it needs:

    1. git diff — the exact set of changes to review
    2. mypy — type-check result (run once, never again)
    3. pytest — targeted at test files for changed modules (run once)
    4. GitHub issue — acceptance criteria to verify against
    5. Changed file list — quick overview before the full diff

    With all five pre-loaded the reviewer can grade and act in ≤5 iterations
    instead of spending 30–40 iterations re-discovering the same information.

    Failures at any step are non-fatal: the partial bundle is still injected
    so the reviewer degrades gracefully rather than starting cold.
    """
    logger.info(
        "✅ reviewer_warmup: starting pre-computation for branch=%r issue=%d",
        pr_branch,
        issue_number,
    )

    sections: list[str] = []

    # ── 1. Setup: fetch + checkout ──────────────────────────────────────────
    await _shell_capture(
        f"git fetch origin --quiet && git checkout {pr_branch} --quiet 2>&1 || true",
        cwd=worktree_path,
    )

    # ── 2. Changed file list ─────────────────────────────────────────────────
    changed_files_raw = await _shell_capture(
        "git diff origin/dev...HEAD --name-only",
        cwd=worktree_path,
    )
    if changed_files_raw:
        sections.append(f"### Changed files\n```\n{changed_files_raw}\n```")

    # ── 3. Full diff ─────────────────────────────────────────────────────────
    diff_raw = await _shell_capture(
        "git diff origin/dev...HEAD",
        cwd=worktree_path,
        timeout=60,
    )
    if diff_raw:
        # Cap at 40 000 chars — enough for any realistic PR without blowing context.
        if len(diff_raw) > 40_000:
            diff_raw = diff_raw[:40_000] + "\n\n… (diff truncated at 40 000 chars)"
        sections.append(f"### Full diff\n```diff\n{diff_raw}\n```")

    # ── 4. mypy — scoped to changed files only ───────────────────────────────
    # Running `python3 -m mypy agentception/` spawns a fresh subprocess that
    # cold-starts the full project type graph (~1-2 GB RSS on top of the
    # existing ONNX model baseline), reliably OOM-killing the container.
    # Use --follow-imports=silent on only the files changed in this PR.
    changed_py_all = [
        f for f in changed_files_raw.splitlines()
        if f.endswith(".py") and f.startswith("agentception/")
    ]
    if changed_py_all:
        mypy_targets = " ".join(changed_py_all)
        mypy_raw = await _shell_capture(
            f"mypy --follow-imports=silent {mypy_targets} 2>&1",
            cwd=worktree_path,
            timeout=60,
        )
    else:
        mypy_raw = "(no Python files changed)"
    sections.append(f"### mypy\n```\n{mypy_raw or '(no output)'}\n```")

    # ── 5. pytest — targeted at changed test modules only ────────────────────
    # Never fall back to the full agentception/tests/ suite — that runs all
    # tests as a subprocess and adds significant memory pressure. If there are
    # no specific test targets, skip pytest and note that in the bundle.
    changed_src = [
        f for f in changed_py_all
        if "/test_" not in f
    ]
    test_targets: list[str] = []
    # Add any changed test files directly.
    test_targets.extend(
        f for f in changed_py_all if "/test_" in f
    )
    # Add corresponding test files for changed source modules.
    for fpath in changed_src:
        module = Path(fpath).stem
        candidate = f"agentception/tests/test_{module}.py"
        if (worktree_path / candidate).exists() and candidate not in test_targets:
            test_targets.append(candidate)

    if test_targets:
        pytest_cmd = f"python3 -m pytest {' '.join(test_targets)} -v --tb=short 2>&1"
        pytest_raw = await _shell_capture(pytest_cmd, cwd=worktree_path, timeout=120)
        sections.append(f"### pytest\n```\n{pytest_raw or '(no output)'}\n```")
    else:
        sections.append("### pytest\n```\n(no test targets identified for changed files)\n```")

    # ── 6. GitHub issue ───────────────────────────────────────────────────────
    if github_client is not None:
        try:
            issue_text = await github_client.call_tool(
                "issue_read",
                {"owner": owner, "repo": repo, "issueNumber": issue_number},
            )
            sections.append(f"### Issue #{issue_number}\n{issue_text}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ reviewer_warmup: issue_read failed — %s", exc)

    if not sections:
        logger.warning("⚠️ reviewer_warmup: no sections produced — reviewer starts cold")
        return

    bundle = (
        "## Pre-loaded Review Context\n\n"
        + "\n\n".join(sections)
        + "\n\n---\n\n"
        "**All signal above was pre-computed. "
        "Do NOT re-run mypy, pytest, or git diff — you already have the output. "
        "Do NOT re-read files already visible in the diff. "
        "Grade immediately and call build_complete_run.**"
    )

    original_content = str(messages[0].get("content", ""))
    messages[0] = {
        **dict(messages[0]),
        "content": original_content + "\n\n---\n\n" + bundle,
    }

    logger.info(
        "✅ reviewer_warmup: injected %d sections into initial message",
        len(sections),
    )


async def _run_recon_phase(
    run_id: str,
    worktree_path: Path,
    messages: list[dict[str, object]],
    system_prompt: str,
) -> None:
    """Execute a structured recon phase before the main agent loop begins.

    1. Call the LLM once with a planning-only system addendum to obtain a
       JSON exploration plan: which files to read, which searches to run.
    2. Execute all requested reads and searches concurrently.
    3. Append a compact bundle of results to ``messages[0]`` so the model
       starts iteration 1 with context already in view.
    4. Update working memory with discovered files and the high-level plan.

    This phase runs outside the iteration counter and the inter-turn delay.
    A failure at any point is non-fatal: the loop proceeds without recon context.
    """
    # Grab the task briefing text from the initial message.
    task_text_raw = messages[0].get("content", "") if messages else ""
    task_text = str(task_text_raw)[:6_000]  # cap for the planning prompt

    # ── Step 0: auto-detect explicitly named files in the task text ──────────
    # File paths written verbatim in the issue body (e.g. "agentception/services/
    # code_indexer.py") are loaded in full before any LLM planning call.  Only
    # the portion of the task text *before* the first injected-context separator
    # is scanned — see _extract_explicit_file_paths for why.
    explicit_files = _extract_explicit_file_paths(task_text)
    if explicit_files:
        logger.info(
            "✅ recon: auto-detected %d explicit file(s) from issue body: %s",
            len(explicit_files),
            explicit_files,
        )
        # When the issue body already names the files to touch, we have
        # everything we need.  Skip the LLM planning call — it consistently
        # recommends wrong files (e.g. "register in main.py") and adds
        # searches that find unrelated code, poisoning the agent's context.
        plan = _ReconPlan(
            files=explicit_files[:8],
            searches=[],
            plan="Explicit files pre-loaded from issue body.",
        )
    else:
        # No explicitly named files — fall back to LLM planning so the agent
        # at least starts with *some* codebase context for free-form tasks.
        try:
            raw_plan = await call_anthropic(
                task_text,
                system_prompt=system_prompt + _RECON_SYSTEM_ADDENDUM,
                max_tokens=500,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ recon phase: planning call failed — %s", exc)
            return

        parsed = _parse_recon_json(raw_plan)
        if parsed is None:
            logger.warning("⚠️ recon phase: could not parse plan from LLM response")
            return
        plan = parsed

    logger.info(
        "✅ recon: files=%d searches=%d — %s",
        len(plan.files),
        len(plan.searches),
        plan.plan,
    )

    # ── Execute all reads and searches concurrently ──────────────────────────

    async def _read_one(rel_path: str) -> str | None:
        path = worktree_path / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            # Load full file up to _RECON_FILE_CHAR_LIMIT so the agent starts
            # with complete source rather than a truncated slice that forces
            # piecemeal re-reading across many subsequent iterations.
            return text[:_RECON_FILE_CHAR_LIMIT]
        except OSError:
            return None

    async def _search_one(query: str) -> list[dict[str, object]]:
        import os
        import psutil as _psutil
        _p = _psutil.Process(os.getpid())
        _rss_before = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 recon._search_one START query=%r RSS=%dMB", query[:60], _rss_before)
        # Prefer the worktree-scoped collection so results include any files
        # the agent has already written.  Fall back to the main "code"
        # collection if the worktree collection doesn't exist yet (indexing
        # runs in the background and may not have finished).
        _wt_collection = f"worktree-{run_id}"
        try:
            results = await search_codebase(query, 5, collection=_wt_collection)
            if not results:
                results = await search_codebase(query, 5)
            _rss_after = _p.memory_info().rss // 1024 // 1024
            logger.warning("📊 recon._search_one DONE query=%r RSS=%dMB (+%dMB)", query[:60], _rss_after, _rss_after - _rss_before)
            return [
                {"file": m["file"], "chunk": m["chunk"][:800], "score": m["score"]}
                for m in results
            ]
        except Exception:  # noqa: BLE001
            try:
                results = await search_codebase(query, 5)
                _rss_after = _p.memory_info().rss // 1024 // 1024
                logger.warning("📊 recon._search_one FALLBACK DONE query=%r RSS=%dMB (+%dMB)", query[:60], _rss_after, _rss_after - _rss_before)
                return [
                    {"file": m["file"], "chunk": m["chunk"][:800], "score": m["score"]}
                    for m in results
                ]
            except Exception:  # noqa: BLE001
                logger.warning("📊 recon._search_one FAILED query=%r", query[:60])
                return []

    import os as _os
    import psutil as _psutil_recon
    _p_recon = _psutil_recon.Process(_os.getpid())
    logger.warning("📊 recon: before gather RSS=%dMB files=%d searches=%d",
                   _p_recon.memory_info().rss // 1024 // 1024, len(plan.files), len(plan.searches))

    file_tasks = [_read_one(f) for f in plan.files]
    search_tasks = [_search_one(q) for q in plan.searches]

    raw_file_results: list[str | None | BaseException] = list(
        await asyncio.gather(*file_tasks, return_exceptions=True)
    )
    logger.warning("📊 recon: after file gather RSS=%dMB", _p_recon.memory_info().rss // 1024 // 1024)
    raw_search_results: list[object] = list(
        await asyncio.gather(*search_tasks, return_exceptions=True)
    )
    logger.warning("📊 recon: after search gather RSS=%dMB", _p_recon.memory_info().rss // 1024 // 1024)

    # ── Bundle results ────────────────────────────────────────────────────────

    sections: list[str] = []

    if plan.plan:
        sections.append(f"**Recon plan:** {plan.plan}")

    discovered_files: list[str] = []

    for rel_path, result in zip(plan.files, raw_file_results):
        if isinstance(result, str):
            sections.append(f"### `{rel_path}`\n```\n{result}\n```")
            discovered_files.append(rel_path)
        else:
            sections.append(f"### `{rel_path}`\n_(could not read)_")

    for query, raw_sr in zip(plan.searches, raw_search_results):
        if not isinstance(raw_sr, list):
            continue
        search_hits: list[dict[str, object]] = [
            item for item in raw_sr if isinstance(item, dict)
        ]
        if search_hits:
            chunks = "\n\n".join(
                f"**{m.get('file', '?')}** (score={m.get('score', 0.0)!s:.5})\n"
                f"```\n{m.get('chunk', '')}\n```"
                for m in search_hits
            )
            sections.append(f"### Search: _{query}_\n{chunks}")

    if not sections:
        logger.info("✅ recon: no usable results; skipping context injection")
        return

    bundle = "## Pre-execution Recon Bundle\n\n" + "\n\n".join(sections)

    # Append to the initial user message (no alternation issues — still one user msg).
    original_content = str(messages[0].get("content", ""))
    messages[0] = {
        **dict(messages[0]),
        "content": original_content + "\n\n---\n\n" + bundle,
    }

    # ── Update working memory ─────────────────────────────────────────────────

    existing_memory = read_memory(worktree_path)
    mem_update = WorkingMemory()
    if plan.plan:
        mem_update["plan"] = plan.plan
    if discovered_files:
        mem_update["files_examined"] = discovered_files
    if mem_update:
        merged = merge_memory(existing_memory, mem_update)
        write_memory(worktree_path, merged)

    logger.info(
        "✅ recon phase complete — injected %d sections, %d files in memory",
        len(sections),
        len(discovered_files),
    )


# ---------------------------------------------------------------------------
# Context management — keep token count bounded across iterations
# ---------------------------------------------------------------------------


def _build_tool_id_map(messages: list[dict[str, object]]) -> dict[str, str]:
    """Build a mapping from tool_call_id → tool_name by scanning assistant messages.

    Required by :func:`_truncate_tool_results` to look up which tool produced
    each result so the correct per-tool character limit can be applied.
    """
    mapping: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for tc in calls:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id", "")
            fn = tc.get("function", {})
            if isinstance(fn, dict) and isinstance(tc_id, str) and tc_id:
                name = fn.get("name", "")
                if isinstance(name, str):
                    mapping[tc_id] = name
    return mapping


def _truncate_tool_results(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Truncate oversized tool-result content using per-tool character limits.

    Applies generous but finite caps per tool type so the agent is never
    blinded by truncation while still keeping tokens bounded:

    - File reads (``read_file``, ``read_file_lines``): 12 000 chars
    - Semantic search (``search_codebase``): 8 000 chars
    - Shell / text search: 5 000 chars
    - Everything else: 3 000 chars

    The tool name is resolved via the ``tool_call_id`` ↔ name mapping built
    from the preceding assistant messages.  The model still sees the beginning
    of every result with a clear truncation marker at the cut point.
    """
    tool_id_map = _build_tool_id_map(messages)
    out: list[dict[str, object]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = str(msg.get("tool_call_id", ""))
            tool_name = tool_id_map.get(tc_id, "")
            limit = _TOOL_RESULT_CHAR_LIMITS.get(tool_name, _DEFAULT_TOOL_RESULT_CHARS)
            raw = msg.get("content", "")
            if isinstance(raw, str) and len(raw) > limit:
                msg = dict(msg)
                msg["content"] = (
                    raw[:limit]
                    + f"\n... [truncated — {len(raw) - limit} chars omitted]"
                )
        out.append(msg)
    return out


async def _summarise_history(
    dropped_messages: list[dict[str, object]],
) -> str:
    """Summarise dropped messages via a single non-streaming LLM call.

    Returns a bullet-point summary string, or "" on any failure.
    """
    try:
        payload = json.dumps(dropped_messages, indent=0)[-20_000:]
        return await call_anthropic(
            payload,
            system_prompt=(
                "You are a concise summariser. Summarise the agent's actions "
                "and findings so far in bullet points. Focus on files modified, "
                "errors fixed, and decisions made. Maximum 800 tokens."
            ),
            max_tokens=1000,
        )
    except Exception:
        logger.warning("_summarise_history failed — falling back to plain prune")
        return ""


async def _prune_history(
    messages: list[dict[str, object]],
    *,
    last_input_tokens: int = 0,
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

    When ``last_input_tokens`` exceeds ``_MAX_INPUT_TOKEN_ESTIMATE``, a second
    token-budget pass runs after the count guard, dropping messages from the
    front of the tail (preserving ``messages[0]``) until the estimated token
    count falls below the threshold.
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

    # Token-budget path: only entered when the LLM reported > _MAX_INPUT_TOKEN_ESTIMATE
    # input tokens last turn.  Uses a character-count heuristic (4 chars ≈ 1 token).
    if last_input_tokens > _MAX_INPUT_TOKEN_ESTIMATE:
        prunable = list(messages)  # work on a copy; messages[0] is anchored
        estimated = sum(len(json.dumps(m)) // 4 for m in prunable)
        dropped: list[dict[str, object]] = []
        while estimated > _MAX_INPUT_TOKEN_ESTIMATE and len(prunable) > _HISTORY_TAIL + 1:
            msg = prunable.pop(1)  # index 0 = task briefing, always kept
            dropped.append(msg)
            estimated -= len(json.dumps(msg)) // 4
        if len(dropped) > 4:
            summary = await _summarise_history(dropped)
            if summary:
                prunable.insert(1, {
                    "role": "user",
                    "content": f"[Context checkpoint]\n{summary}",
                })
        logger.warning(
            "⚠️ context prune: estimated %d tokens exceeds threshold — pruned to %d messages",
            estimated,
            len(prunable),
        )
        return prunable

    return messages[:1] + tail


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _auto_track_file_read(file_path: Path, worktree_path: Path) -> None:
    """Record *file_path* in the agent's working memory after a successful read.

    This is runtime-owned state: the loop updates ``files_examined`` after
    every successful read without depending on the agent to remember to call
    ``update_working_memory``.  Never raises — tracking failure is silently
    ignored so the caller's result is unaffected.

    Paths are stored relative to *worktree_path* when possible (shorter, cleaner).
    """
    try:
        try:
            rel = str(file_path.relative_to(worktree_path))
        except ValueError:
            rel = str(file_path)
        existing = read_memory(worktree_path)
        current: list[str] = list(existing.get("files_examined", [])) if existing else []
        if rel not in current:
            current.append(rel)
            update = WorkingMemory(files_examined=current)
            merged = merge_memory(existing, update)
            write_memory(worktree_path, merged)
    except Exception:  # noqa: BLE001
        pass


def _session_writes_note(worktree_path: Path) -> dict[str, str]:
    """Return a dict with a 'session_writes' key listing all files written so far.

    Merged into every successful write tool result so the list stays visible
    in the conversation history — not just in the secondary memory block —
    where the agent's recency attention is strongest.
    """
    try:
        mem = read_memory(worktree_path)
        raw_written = list(mem.get("files_written", [])) if mem else []
        # files_written entries are FileEditEvent objects after issue #679.
        written_paths = [e.path for e in raw_written if isinstance(e, FileEditEvent)]
        if written_paths:
            return {"session_writes": "Already written this session — do NOT re-implement: " + ", ".join(written_paths)}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _auto_track_file_write(rel_path: str, worktree_path: Path) -> None:
    """Record *rel_path* in ``files_written`` after every successful file write.

    Mirrors ``_auto_track_file_read`` — runtime-owned, never raises.  The agent
    sees this list at the top of its working memory every iteration so it cannot
    accidentally re-implement something it already wrote.
    """
    import datetime

    try:
        existing = read_memory(worktree_path)
        current: list[FileEditEvent] = list(existing.get("files_written", [])) if existing else []
        already_tracked = any(e.path == rel_path for e in current if isinstance(e, FileEditEvent))
        if not already_tracked:
            current.append(FileEditEvent(
                timestamp=datetime.datetime.utcnow(),
                path=rel_path,
                diff="",
                lines_omitted=0,
            ))
            update = WorkingMemory(files_written=current)
            merged = merge_memory(existing, update)
            write_memory(worktree_path, merged)
    except Exception:  # noqa: BLE001
        pass


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

    When the model batches multiple tool calls in one response they are
    dispatched concurrently via :func:`asyncio.gather` so the wall-clock
    time equals the slowest single call rather than the sum of all calls.
    """
    async def _run_one(tc: ToolCall) -> dict[str, object]:
        try:
            result = await _dispatch_single_tool(
                tc,
                worktree_path,
                run_id,
                github_client=github_client,
                github_tool_names=github_tool_names,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ _dispatch_tool_calls: tool=%s raised %s — returning error",
                tc.get("function", {}).get("name", "?"),
                exc,
            )
            result = {"ok": False, "error": str(exc)}
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": json.dumps(result),
        }

    results: list[dict[str, object]] = await asyncio.gather(
        *(_run_one(tc) for tc in tool_calls)
    )
    return list(results)


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

    try:
        args: dict[str, object] = json.loads(args_str)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid tool arguments (JSON parse error): {exc}"}

    # Log tool name + the single most useful arg so watch_run.py can show
    # what was searched / read / written without needing a second log line.
    _KEY_ARG: dict[str, str] = {
        "search_codebase": "query",
        "search_text": "pattern",
        "read_file": "path",
        "read_file_lines": "path",
        "write_file": "path",
        "replace_in_file": "path",
        "insert_after_in_file": "path",
        "list_directory": "path",
    }
    _key = _KEY_ARG.get(name)
    _val = args.get(_key) if _key else None
    _arg_tag = f" {_key}={str(_val)!r}" if isinstance(_val, str) else ""
    logger.info("✅ dispatch_tool — run_id=%s tool=%s%s", run_id, name, _arg_tag)

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
        result = read_file(path)
        if result.get("ok"):
            _auto_track_file_read(path, worktree_path)
        return result

    if name == "read_file_lines":
        path_raw = args.get("path")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "read_file_lines: 'path' must be a string"}
        start_raw = args.get("start_line")
        end_raw = args.get("end_line")
        if not isinstance(start_raw, int):
            return {"ok": False, "error": "read_file_lines: 'start_line' must be an integer"}
        if not isinstance(end_raw, int):
            return {"ok": False, "error": "read_file_lines: 'end_line' must be an integer"}
        resolved = _resolve(path_raw, worktree_path)
        result = read_file_lines(resolved, start_raw, end_raw)
        if result.get("ok"):
            _auto_track_file_read(resolved, worktree_path)
        return result

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
        allow = bool(allow_raw) if isinstance(allow_raw, (bool, int)) else False
        result = replace_in_file(
            _resolve(path_raw, worktree_path),
            old_raw,
            new_raw,
            allow_multiple=allow,
        )
        if result.get("ok"):
            _auto_track_file_write(path_raw, worktree_path)
            result = {**result, **_session_writes_note(worktree_path)}
        return result

    if name == "insert_after_in_file":
        path_raw = args.get("path")
        anchor_raw = args.get("anchor")
        new_content_raw = args.get("new_content")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "insert_after_in_file: 'path' must be a string"}
        if not isinstance(anchor_raw, str):
            return {"ok": False, "error": "insert_after_in_file: 'anchor' must be a string"}
        if not isinstance(new_content_raw, str):
            return {"ok": False, "error": "insert_after_in_file: 'new_content' must be a string"}
        result = insert_after_in_file(
            _resolve(path_raw, worktree_path),
            anchor_raw,
            new_content_raw,
        )
        if result.get("ok"):
            _auto_track_file_write(path_raw, worktree_path)
            result = {**result, **_session_writes_note(worktree_path)}
        return result

    if name == "write_file":
        path_raw = args.get("path")
        content_raw = args.get("content")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "write_file: 'path' must be a string"}
        if not isinstance(content_raw, str):
            return {"ok": False, "error": "write_file: 'content' must be a string"}
        result = write_file(_resolve(path_raw, worktree_path), content_raw)
        if result.get("ok"):
            _auto_track_file_write(path_raw, worktree_path)
            result = {**result, **_session_writes_note(worktree_path)}
        return result

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

    if name == "git_commit_and_push":
        branch_raw = args.get("branch")
        msg_raw = args.get("commit_message")
        paths_raw = args.get("paths")
        base_raw = args.get("base", "origin/dev")
        if not isinstance(branch_raw, str):
            return {"ok": False, "error": "git_commit_and_push: 'branch' must be a string"}
        if not isinstance(msg_raw, str):
            return {"ok": False, "error": "git_commit_and_push: 'commit_message' must be a string"}
        # Coerce a bare string (e.g. ".") to a single-element list so the model
        # doesn't have to retry just because it forgot the JSON array brackets.
        if isinstance(paths_raw, str):
            paths_raw = [paths_raw]
        if not isinstance(paths_raw, list) or not paths_raw:
            return {"ok": False, "error": "git_commit_and_push: 'paths' must be a non-empty array"}
        str_paths = [p for p in paths_raw if isinstance(p, str)]
        if len(str_paths) != len(paths_raw):
            return {"ok": False, "error": "git_commit_and_push: all entries in 'paths' must be strings"}
        base = base_raw if isinstance(base_raw, str) else "origin/dev"
        return await git_commit_and_push(
            branch_raw,
            msg_raw,
            str_paths,
            worktree_path,
            base=base,
        )

    if name == "search_codebase":
        query_raw = args.get("query")
        if not isinstance(query_raw, str):
            return {"ok": False, "error": "search_codebase: 'query' must be a string"}
        n_raw = args.get("n_results", 5)
        n_results = int(n_raw) if isinstance(n_raw, int) else 5
        collection_raw = args.get("collection")
        collection_arg: str | None = collection_raw if isinstance(collection_raw, str) else None
        matches = await search_codebase(query_raw, n_results, collection=collection_arg)
        return {"ok": True, "matches": matches}

    if name == "read_symbol":
        path_raw = args.get("path")
        symbol_raw = args.get("symbol_name")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "read_symbol: 'path' must be a string"}
        if not isinstance(symbol_raw, str):
            return {"ok": False, "error": "read_symbol: 'symbol_name' must be a string"}
        resolved = _resolve(path_raw, worktree_path)
        result = read_symbol(resolved, symbol_raw)
        if result.get("ok"):
            _auto_track_file_read(resolved, worktree_path)
        return result

    if name == "read_window":
        path_raw = args.get("path")
        center_raw = args.get("center_line")
        if not isinstance(path_raw, str):
            return {"ok": False, "error": "read_window: 'path' must be a string"}
        if not isinstance(center_raw, int):
            return {"ok": False, "error": "read_window: 'center_line' must be an integer"}
        before_raw = args.get("before", 80)
        after_raw = args.get("after", 120)
        before = int(before_raw) if isinstance(before_raw, int) else 80
        after = int(after_raw) if isinstance(after_raw, int) else 120
        resolved = _resolve(path_raw, worktree_path)
        result = read_window(resolved, center_raw, before=before, after=after)
        if result.get("ok"):
            _auto_track_file_read(resolved, worktree_path)
        return result

    if name == "find_call_sites":
        symbol_raw = args.get("symbol_name")
        if not isinstance(symbol_raw, str):
            return {"ok": False, "error": "find_call_sites: 'symbol_name' must be a string"}
        n_raw = args.get("n_results", 30)
        n_results_fc = int(n_raw) if isinstance(n_raw, int) else 30
        return await find_call_sites(symbol_raw, worktree_path, n_results=n_results_fc)

    if name == "update_working_memory":
        update = WorkingMemory()
        plan_raw = args.get("plan")
        if isinstance(plan_raw, str):
            update["plan"] = plan_raw
        files_raw = args.get("files_examined")
        if isinstance(files_raw, list) and all(isinstance(f, str) for f in files_raw):
            update["files_examined"] = list(files_raw)
        findings_raw = args.get("findings")
        if isinstance(findings_raw, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in findings_raw.items()
        ):
            update["findings"] = dict(findings_raw)
        decisions_raw = args.get("decisions")
        if isinstance(decisions_raw, list) and all(isinstance(d, str) for d in decisions_raw):
            update["decisions"] = list(decisions_raw)
        next_steps_raw = args.get("next_steps")
        if isinstance(next_steps_raw, list) and all(isinstance(s, str) for s in next_steps_raw):
            update["next_steps"] = list(next_steps_raw)
        blockers_raw = args.get("blockers")
        if isinstance(blockers_raw, list) and all(isinstance(b, str) for b in blockers_raw):
            update["blockers"] = list(blockers_raw)
        existing = read_memory(worktree_path)
        merged = merge_memory(existing, update)
        write_memory(worktree_path, merged)
        return {"ok": True, "result": "Working memory updated."}

    return {"ok": False, "error": f"Unknown local tool: {name!r}"}
