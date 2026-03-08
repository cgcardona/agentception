"""Shell-execution tool exposed to the agent loop.

``run_command`` runs a shell command inside the AgentCeption container and
returns its stdout, stderr, and exit code as a structured dict.

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
