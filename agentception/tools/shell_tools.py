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

A second, smarter check blocks commands that are not destructive but are
known to OOM-kill the container due to its memory profile (see
``_check_oom_risk``).

Secret redaction is applied to all command output before it is returned to
the agent.  This prevents credentials captured in stdout/stderr (e.g. ``env``
output) from entering the agent's conversation history or being persisted to
the DB.  See ``_redact_secrets``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db import activity_events
from agentception.types import JsonValue

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
        # Prevent agents from destroying the app or all worktrees in one shot.
        "rm -rf /app",
        "rm -rf /worktrees",
        # Reverse-shell patterns.
        "nc -e ",
        "nc\t-e ",
        "/dev/tcp/",
        "/dev/udp/",
        # Raw credential dumps (env alone is fine, but these are explicit exfil).
        "curl.*env",
        "wget.*env",
    }
)

# Names of environment variables whose values are sensitive.  Their values
# are redacted from command output before it reaches the agent.
_SECRET_ENV_VAR_NAMES: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "DATABASE_URL",
        "DB_PASSWORD",
        "AC_API_KEY",
        "HF_TOKEN",
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "PRIVATE_KEY",
        "SECRET_KEY",
    }
)

# Pattern to redact KEY=VALUE pairs for known secret env vars.
# Built once at module import from _SECRET_ENV_VAR_NAMES.
_SECRET_KEY_VALUE_RE: re.Pattern[str] = re.compile(
    r"(?m)^("
    + "|".join(re.escape(k) for k in sorted(_SECRET_ENV_VAR_NAMES))
    + r")=([^\n]*)",
    re.IGNORECASE,
)

# GitHub PAT pattern: ghp_ followed by 36 base62 chars.
_GH_PAT_RE: re.Pattern[str] = re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")

# Anthropic API key pattern: sk-ant- prefix.
_ANTHROPIC_KEY_RE: re.Pattern[str] = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b")

# Generic bearer token pattern (Authorization header leak).
_BEARER_RE: re.Pattern[str] = re.compile(
    r"\bBearer\s+[A-Za-z0-9._/+=-]{20,}\b", re.IGNORECASE
)


def _redact_secrets(text: str) -> str:
    """Mask credential values in command output before returning to the agent.

    Targets:
    - ``KEY=value`` lines for known secret environment variable names.
    - GitHub PAT tokens (``ghp_…``).
    - Anthropic API keys (``sk-ant-…``).
    - Bearer tokens in Authorization-style header lines.

    This is a defence-in-depth measure.  The primary control is the path
    sandbox that prevents agents from reading secret files.  This layer
    ensures that if an agent issues a command that incidentally captures
    credentials in output (e.g. ``env``, ``printenv``), those values are
    stripped before they enter the conversation history or the DB.
    """
    text = _SECRET_KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _GH_PAT_RE.sub("[REDACTED_GH_TOKEN]", text)
    text = _ANTHROPIC_KEY_RE.sub("[REDACTED_ANTHROPIC_KEY]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED_TOKEN]", text)
    return text

# Matches any mypy invocation that targets a directory rather than specific
# files.  The container runs ONNX embedding models that consume ~5.7 GB RSS.
# Spawning a mypy subprocess that cold-loads the full project type graph adds
# another ~1.5-2 GB and deterministically OOM-kills the container.
#
# Safe form:  mypy --follow-imports=silent <file1> <file2> ...
# Unsafe form: mypy agentception/   mypy agentception/ tests/   etc.
#
# The regex matches the unsafe form: a mypy invocation where at least one
# positional argument ends with "/" (a directory) or is exactly "agentception",
# "tests", or "agentception/tests" — the common culprits.  It does NOT match
# when --follow-imports=silent is present AND no directory args appear, so the
# correct invocation is always allowed through.
_MYPY_DIR_SCAN_RE: re.Pattern[str] = re.compile(
    r"""
    (?:python3?\s+-m\s+)?   # optional: python3 -m  or  python -m
    mypy\b                  # the mypy invocation
    (?!.*--follow-imports=silent.*\s+\S+\.py)  # NOT already safe form
    .*                      # any flags
    (?:
        \bagentception/?    # directory arg: agentception  or  agentception/
      | \btests/?           # directory arg: tests  or  tests/
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_MYPY_OOM_ERROR = (
    "BLOCKED — mypy directory scan would OOM-kill the container.\n"
    "\n"
    "The container runs ONNX embedding models (~5.7 GB RSS). Spawning "
    "`mypy agentception/` or `mypy agentception/ tests/` cold-loads the "
    "full project type graph in a new subprocess (~1.5-2 GB extra) and "
    "crashes the container.\n"
    "\n"
    "Use the scoped form instead:\n"
    "  mypy --follow-imports=silent agentception/path/to/file1.py agentception/path/to/file2.py\n"
    "\n"
    "Only list the files YOU modified — not entire directories."
)

# Commands that run grep (direct or in a pipeline). Blocked so the agent uses
# the search_text tool (ripgrep) instead — structured output and .gitignore-aware.
_GREP_CMD_RE: re.Pattern[str] = re.compile(r"(^|\|)\s*grep\b", re.IGNORECASE)

_GREP_BLOCKED_MESSAGE = (
    "run_command(grep) is not allowed. Use the search_text tool instead: "
    "it uses ripgrep, respects .gitignore, and returns file names and line numbers. "
    "Call search_text(pattern=..., directory=...) for codebase search."
)


def _check_oom_risk(command: str) -> tuple[bool, str]:
    """Return *(safe, reason)* for commands that are not destructive but are
    known to cause OOM crashes due to the container's memory profile.

    Currently guards against mypy full-directory scans.  Returns ``(False,
    human-readable explanation)`` when the command matches a known OOM pattern.
    """
    if _MYPY_DIR_SCAN_RE.search(command):
        return False, _MYPY_OOM_ERROR
    return True, ""


def _is_safe(command: str) -> tuple[bool, str]:
    """Return *(safe, reason)*.  ``safe`` is ``False`` when the command matches
    a blocked pattern or a known OOM-risk pattern.
    """
    lower = command.lower().strip()
    for pattern in _BLOCKED_PATTERNS:
        if pattern in lower:
            return False, f"Blocked pattern detected: {pattern!r}"
    return _check_oom_risk(command)


async def run_command(
    command: str,
    cwd: str | Path | None = None,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    run_id: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, JsonValue]:
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

    if _GREP_CMD_RE.search(command.strip()):
        logger.warning("⚠️ run_command blocked — grep; use search_text")
        return {"ok": False, "error": _GREP_BLOCKED_MESSAGE}

    cwd_path = Path(cwd) if cwd else None
    logger.info("✅ run_command — %s (cwd=%s)", shlex.quote(command), cwd_path)
    # activity event — see docs/reference/activity-events.md
    if run_id and session is not None:
        try:
            activity_events.persist_activity_event(
                session,
                run_id,
                "shell_start",
                {
                    "cmd_preview": command[:200],
                    "cwd": str(cwd_path) if cwd_path else "",
                },
            )
            await session.flush()
        except Exception as exc:
            logger.warning("⚠️ persist shell_start failed: %s", exc)

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
    stdout = _redact_secrets(raw_out[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"))
    stderr = _redact_secrets(raw_err[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"))
    exit_code: int = proc.returncode if proc.returncode is not None else -1

    logger.info(
        "✅ run_command done — exit=%d stdout=%d stderr=%d",
        exit_code,
        len(stdout),
        len(stderr),
    )
    # activity event — see docs/reference/activity-events.md
    if run_id and session is not None:
        try:
            stdout_preview = "\n".join(stdout.splitlines()[:30])[:2000]
            stderr_preview = "\n".join(stderr.splitlines()[:10])[:500] if stderr.strip() else ""
            activity_events.persist_activity_event(
                session,
                run_id,
                "shell_done",
                {
                    "exit_code": exit_code,
                    "stdout_bytes": len(raw_out),
                    "stderr_bytes": len(raw_err),
                    "stdout_preview": stdout_preview,
                    "stderr_preview": stderr_preview,
                },
            )
            await session.flush()
        except Exception as exc:
            logger.warning("⚠️ persist shell_done failed: %s", exc)

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
    run_id: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, JsonValue]:
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
                    "error": "git_commit_and_push: checkout failed",
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
    # activity event — see docs/reference/activity-events.md
    if run_id and session is not None:
        try:
            activity_events.persist_activity_event(
                session,
                run_id,
                "git_push",
                {"branch": branch},
            )
            await session.flush()
        except Exception as exc:
            logger.warning("⚠️ persist git_push failed: %s", exc)

    return {"ok": True, "branch": branch, "sha": sha, "stdout": push_out}
