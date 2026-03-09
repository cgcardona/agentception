"""Shell-execution tools exposed to the agent loop.

``run_command`` runs an arbitrary shell command inside the AgentCeption
container and returns stdout, stderr, and exit code as a structured dict.

``git_commit_and_push`` is a higher-level helper that consolidates the
four-step git workflow (checkout branch, add, commit, push) into one atomic
tool call, reducing the turn cost of the standard commit-and-PR pattern from
four turns to one.

Safety is enforced via a denylist of obviously destructive patterns rather
than an allowlist — the model needs broad access (git, pytest, mypy, rg, gh,
docker, npm, python3, …) so an allowlist would be too brittle.  Catastrophic
accidents (``rm -rf /``, fork bombs, privilege escalation) are blocked.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum captured output per stream to prevent memory exhaustion.
_MAX_OUTPUT_BYTES = 32_768  # 32 KiB

# Command timeout — generous for slow operations (full pytest suite, npm builds).
_DEFAULT_TIMEOUT = 300  # 5 minutes

# Substrings that make a command unconditionally dangerous, checked on the
# lowercase stripped command string.  Exact-match substring search is
# intentionally conservative to avoid false positives.
_BLOCKED_PATTERNS: frozenset[str] = frozenset(
    {
        "rm -rf /",
        "rm -rf ~",
        "rm -rf $home",
        ":(){ :|:& };:",  # fork bomb
        "sudo ",
        "sudo\t",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "mkfs",
        "wipefs",
        "> /dev/sd",
        "dd if=",
        "chmod -r /",
        "chown -r /",
    }
)


def _is_safe(command: str) -> tuple[bool, str]:
    """Return *(safe, reason)*.  ``safe`` is ``False`` when the command matches
    a blocked pattern.
    """
    lower = command.lower().strip()
    for pattern in _BLOCKED_PATTERNS:
        if pattern in lower:
            return False, f"Blocked pattern detected: {pattern!r}"
    return True, ""


async def run_command(
    command: str,
    cwd: str | Path | None = None,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, object]:
    """Execute *command* in a subprocess and return structured output.

    The command is run via the system shell (``/bin/sh -c``), which allows
    pipes, redirections, and compound expressions.  stdout and stderr are
    captured and truncated to 32 KiB each to avoid blowing the model's
    context window.

    Args:
        command: Shell command string to execute.
        cwd: Working directory.  Defaults to the current process directory.
        timeout: Maximum seconds to wait before killing the process.

    Returns:
        ``{"ok": True, "stdout": str, "stderr": str, "exit_code": int,
        "stdout_truncated": bool, "stderr_truncated": bool}`` on success, or
        ``{"ok": False, "error": str}`` when the command is blocked, timed
        out, or fails to launch.
    """
    safe, reason = _is_safe(command)
    if not safe:
        logger.warning("⚠️ run_command blocked — %s", reason)
        return {"ok": False, "error": reason}

    cwd_path = Path(cwd) if cwd else None
    logger.info("✅ run_command — %s (cwd=%s)", shlex.quote(command), cwd_path)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd_path,
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout)
            )
        except asyncio.TimeoutError:
            # Kill the process then wait for it to exit.
            # Avoid calling communicate() again — the cancelled coroutine may
            # leave asyncio pipe transports in an inconsistent state.  proc.wait()
            # waits only for the exit code (via SIGCHLD), not via pipe reads.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            logger.warning("⚠️ run_command timed out after %ds: %s", timeout, command)
            return {"ok": False, "error": f"Command timed out after {timeout}s: {command!r}"}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"Command not found: {exc}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    out_truncated = len(raw_out) > _MAX_OUTPUT_BYTES
    err_truncated = len(raw_err) > _MAX_OUTPUT_BYTES
    stdout = raw_out[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = raw_err[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    exit_code: int = proc.returncode if proc.returncode is not None else -1

    logger.info(
        "✅ run_command done — exit=%d stdout=%d stderr=%d",
        exit_code,
        len(stdout),
        len(stderr),
    )
    return {
        "ok": True,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
    }


async def _git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a git sub-command and return *(exit_code, stdout, stderr)*."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    code: int = proc.returncode if proc.returncode is not None else -1
    return code, raw_out.decode("utf-8", errors="replace"), raw_err.decode("utf-8", errors="replace")


async def git_commit_and_push(
    branch: str,
    commit_message: str,
    paths: list[str],
    worktree_path: Path,
    *,
    base: str = "origin/dev",
) -> dict[str, object]:
    """Create a branch, stage files, commit, and push in one atomic call.

    Replaces the four-turn run_command pattern::

        git checkout -b <branch> <base>
        git add <paths>
        git commit -m <message>
        git push -u origin <branch>

    If the worktree is already on *branch*, the checkout step is skipped so
    the tool is idempotent when called twice.

    Args:
        branch: Name of the feature branch to create (e.g. ``fix/typo``).
        commit_message: Commit message string.
        paths: List of paths to stage (passed to ``git add``).
        worktree_path: Absolute path to the git worktree root.
        base: Ref to branch from (default ``origin/dev``).

    Returns:
        ``{"ok": True, "branch": str, "sha": str, "stdout": str}`` on
        success, or ``{"ok": False, "error": str, "stderr": str}`` on any
        git failure.
    """
    if not paths:
        return {"ok": False, "error": "git_commit_and_push: 'paths' must be a non-empty list"}

    logger.info(
        "✅ git_commit_and_push — branch=%s paths=%s cwd=%s",
        branch,
        paths,
        worktree_path,
    )

    # Determine current branch.
    code, current, err = await _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    if code != 0:
        return {"ok": False, "error": "git_commit_and_push: could not determine current branch", "stderr": err}
    current = current.strip()

    if current != branch:
        # Try to create the branch from base; if it already exists, just switch.
        code, out, err = await _git(["checkout", "-b", branch, base], worktree_path)
        if code != 0:
            # Branch might already exist locally — try plain checkout.
            code2, out2, err2 = await _git(["checkout", branch], worktree_path)
            if code2 != 0:
                return {
                    "ok": False,
                    "error": f"git_commit_and_push: checkout failed",
                    "stderr": err + "\n" + err2,
                }

    # Stage the requested paths.
    code, out, err = await _git(["add", "--", *paths], worktree_path)
    if code != 0:
        return {"ok": False, "error": "git_commit_and_push: git add failed", "stderr": err}

    # Commit.
    code, out, err = await _git(["commit", "-m", commit_message], worktree_path)
    if code != 0:
        return {"ok": False, "error": "git_commit_and_push: git commit failed", "stderr": err}

    # Push and set upstream.
    code, push_out, push_err = await _git(["push", "-u", "origin", branch], worktree_path)
    if code != 0:
        return {"ok": False, "error": "git_commit_and_push: git push failed", "stderr": push_err}

    # Retrieve the new commit SHA.
    code, sha, _ = await _git(["rev-parse", "HEAD"], worktree_path)
    sha = sha.strip() if code == 0 else "(unknown)"

    logger.info("✅ git_commit_and_push — pushed %s → origin/%s", sha[:12], branch)
    return {"ok": True, "branch": branch, "sha": sha, "stdout": push_out}
