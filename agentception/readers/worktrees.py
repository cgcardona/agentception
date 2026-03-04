from __future__ import annotations

"""Worktree reader for AgentCeption.

Scans ``~/.agentception/worktrees/maestro/`` for active git worktrees and parses
the ``.agent-task`` file in each one to derive live agent metadata.

This is the primary filesystem signal for the poller — it tells the dashboard
which agents are actively working and what they are working on. Combine with
transcript data for richer status information.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agentception.config import settings
from agentception.models import TaskFile

logger = logging.getLogger(__name__)


async def list_active_worktrees() -> list[TaskFile]:
    """Scan the worktrees directory and return one TaskFile per active checkout.

    A worktree is considered active if it contains a ``.agent-task`` file.
    Directories without that file are silently skipped (they may be stale
    or manually created). Returns an empty list when the worktrees directory
    does not exist.
    """
    worktrees_dir: Path = settings.worktrees_dir
    if not worktrees_dir.exists():
        logger.debug("⚠️  Worktrees dir does not exist: %s", worktrees_dir)
        return []

    results: list[TaskFile] = []
    try:
        entries = list(worktrees_dir.iterdir())
    except OSError as exc:
        logger.warning("⚠️  Cannot read worktrees dir %s: %s", worktrees_dir, exc)
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        task = await parse_agent_task(entry)
        if task is not None:
            results.append(task)

    logger.debug("✅ Found %d active worktree(s)", len(results))
    return results


async def parse_agent_task(worktree_path: Path) -> TaskFile | None:
    """Parse a ``KEY=VALUE`` ``.agent-task`` file into a TaskFile model.

    Returns ``None`` when the file is absent, unreadable, or so malformed
    that no valid TaskFile can be constructed. Missing individual fields
    default gracefully — only a complete read failure returns ``None``.

    The parser is intentionally lenient: blank lines and lines without ``=``
    are silently skipped. This matches how agents write task files via heredoc
    (which may include trailing blank lines or comments).
    """
    task_file_path = worktree_path / ".agent-task"
    if not task_file_path.exists():
        return None

    try:
        content = await asyncio.get_running_loop().run_in_executor(
            None, task_file_path.read_text, "utf-8"
        )
    except OSError as exc:
        logger.warning("⚠️  Cannot read %s: %s", task_file_path, exc)
        return None

    fields: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip().upper()] = value.strip()

    try:
        return _build_task_file(fields, worktree_path)
    except Exception as exc:
        logger.warning("⚠️  Failed to build TaskFile from %s: %s", task_file_path, exc)
        return None


async def worktree_last_commit_time(worktree_path: Path) -> float:
    """Return the UNIX timestamp of the most recent commit in the worktree.

    Used for stuck-agent detection: if this value has not advanced in a
    configurable number of minutes, the agent is likely hung and should be
    flagged in the dashboard. Returns 0.0 when the worktree has no commits
    or git is unavailable.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "-1",
            "--format=%ct",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout.strip():
            return 0.0
        return float(stdout.strip())
    except (OSError, ValueError) as exc:
        logger.debug("⚠️  worktree_last_commit_time(%s) error: %s", worktree_path, exc)
        return 0.0


# ── Private helpers ────────────────────────────────────────────────────────────


def _build_task_file(fields: dict[str, str], worktree_path: Path) -> TaskFile:
    """Construct a TaskFile from the parsed KEY=VALUE map.

    Field names in .agent-task files are UPPER_SNAKE_CASE. We map them to
    the lower_snake_case Pydantic model fields here. PR review task files use
    ``PR=<number>`` for the PR number instead of ``ISSUE_NUMBER``.
    """
    closes_issues = _parse_int_list(fields.get("CLOSES_ISSUES", ""))

    raw: dict[str, str | int | bool | list[int] | None] = {
        "task": fields.get("TASK") or fields.get("WORKFLOW"),
        "gh_repo": fields.get("GH_REPO"),
        "issue_number": _parse_int(fields.get("ISSUE_NUMBER")),
        "pr_number": _parse_int(fields.get("PR")),
        "branch": fields.get("BRANCH"),
        "worktree": str(worktree_path),
        "role": fields.get("ROLE"),
        "base": fields.get("BASE"),
        "batch_id": fields.get("BATCH_ID"),
        "closes_issues": closes_issues,
        "spawn_sub_agents": _parse_bool(fields.get("SPAWN_SUB_AGENTS", "false")),
        "spawn_mode": fields.get("SPAWN_MODE"),
        "merge_after": fields.get("MERGE_AFTER"),
        "attempt_n": _parse_int(fields.get("ATTEMPT_N")) or 0,
        "required_output": fields.get("REQUIRED_OUTPUT"),
        "on_block": fields.get("ON_BLOCK"),
        "cognitive_arch": fields.get("COGNITIVE_ARCH"),
    }
    cleaned = {k: v for k, v in raw.items() if v is not None}
    return TaskFile.model_validate(cleaned)


def _parse_int(value: str | None) -> int | None:
    """Convert a string to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_int_list(value: str) -> list[int]:
    """Parse a comma-separated string of integers, skipping invalid tokens."""
    result: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if token.isdigit():
            result.append(int(token))
    return result


def _parse_bool(value: str) -> bool:
    """Interpret 'true'/'false' (case-insensitive) as bool."""
    return value.strip().lower() == "true"
