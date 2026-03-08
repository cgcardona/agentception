"""Unit tests for agentception.tools.shell_tools.

Covers the denylist, timeout handling, stdout/stderr capture, and exit code
reporting.  Most tests run real subprocesses; the timeout test mocks the
subprocess to avoid asyncio pipe-transport cleanup issues in the test runner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.tools.shell_tools import _is_safe, run_command


# ---------------------------------------------------------------------------
# _is_safe (synchronous helper — no subprocess needed)
# ---------------------------------------------------------------------------


class TestIsSafe:
    def test_normal_git_command_is_safe(self) -> None:
        safe, _ = _is_safe("git status")
        assert safe is True

    def test_python_command_is_safe(self) -> None:
        safe, _ = _is_safe("python3 -m pytest agentception/tests/")
        assert safe is True

    def test_rm_rf_root_is_blocked(self) -> None:
        safe, reason = _is_safe("rm -rf /")
        assert safe is False
        assert reason

    def test_rm_rf_home_is_blocked(self) -> None:
        safe, reason = _is_safe("rm -rf ~")
        assert safe is False
        assert reason

    def test_sudo_is_blocked(self) -> None:
        safe, reason = _is_safe("sudo rm something")
        assert safe is False
        assert reason

    def test_shutdown_is_blocked(self) -> None:
        safe, reason = _is_safe("shutdown now")
        assert safe is False
        assert reason

    def test_reboot_is_blocked(self) -> None:
        safe, reason = _is_safe("reboot")
        assert safe is False
        assert reason

    def test_fork_bomb_is_blocked(self) -> None:
        safe, reason = _is_safe(":(){ :|:& };: ")
        assert safe is False
        assert reason

    def test_case_insensitive_matching(self) -> None:
        safe, reason = _is_safe("SUDO something")
        assert safe is False
        assert reason

    def test_mkfs_is_blocked(self) -> None:
        safe, reason = _is_safe("mkfs.ext4 /dev/sda1")
        assert safe is False
        assert reason

    def test_rg_command_is_safe(self) -> None:
        safe, _ = _is_safe("rg --heading pattern agentception/")
        assert safe is True

    def test_npm_command_is_safe(self) -> None:
        safe, _ = _is_safe("npm run build")
        assert safe is True


# ---------------------------------------------------------------------------
# run_command (async — real subprocesses)
# ---------------------------------------------------------------------------


class TestRunCommand:
    @pytest.mark.anyio
    async def test_echo_returns_stdout(self, tmp_path: Path) -> None:
        result = await run_command("echo hello_world", tmp_path)
        assert result["ok"] is True
        assert "hello_world" in str(result["stdout"])
        assert result["exit_code"] == 0

    @pytest.mark.anyio
    async def test_exit_code_captured(self, tmp_path: Path) -> None:
        result = await run_command("false", tmp_path)
        assert result["ok"] is True
        assert result["exit_code"] != 0

    @pytest.mark.anyio
    async def test_stderr_captured(self, tmp_path: Path) -> None:
        result = await run_command("echo err >&2", tmp_path)
        assert result["ok"] is True
        assert "err" in str(result["stderr"])

    @pytest.mark.anyio
    async def test_blocked_command_returns_error(self, tmp_path: Path) -> None:
        result = await run_command("rm -rf /", tmp_path)
        assert result["ok"] is False
        assert "blocked" in str(result["error"]).lower()

    @pytest.mark.anyio
    async def test_cwd_defaults_to_none(self) -> None:
        result = await run_command("echo cwd_test")
        assert result["ok"] is True
        assert "cwd_test" in str(result["stdout"])

    @pytest.mark.anyio
    async def test_cwd_sets_working_directory(self, tmp_path: Path) -> None:
        result = await run_command("pwd", tmp_path)
        assert result["ok"] is True
        assert str(tmp_path) in str(result["stdout"])

    @pytest.mark.anyio
    async def test_timeout_kills_command_returns_error(self, tmp_path: Path) -> None:
        """run_command returns an error dict when the subprocess times out.

        The subprocess is mocked to avoid asyncio pipe-transport cleanup
        issues: real subprocesses leave asyncio pipe readers registered in
        the event loop after wait_for cancels communicate(), which causes the
        event loop selector to block.  Mocking isolates the timeout-path logic
        without touching asyncio internals.
        """
        async def _slow_communicate() -> tuple[bytes, bytes]:
            await asyncio.sleep(9999)
            return b"", b""

        proc_mock = MagicMock()
        proc_mock.returncode = -9
        proc_mock.kill = MagicMock()
        proc_mock.wait = AsyncMock(return_value=None)
        proc_mock.communicate = _slow_communicate

        with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock, return_value=proc_mock):
            result = await run_command("sleep 9999", tmp_path, timeout=1)

        assert result["ok"] is False
        assert "timed out" in str(result["error"]).lower()
        proc_mock.kill.assert_called_once()

    @pytest.mark.anyio
    async def test_truncates_large_stdout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agentception.tools.shell_tools._MAX_OUTPUT_BYTES", 10)
        # Use python3 to generate deterministic large output (avoids bash-specific syntax)
        result = await run_command("python3 -c \"print('A' * 200)\"", tmp_path)
        assert result["ok"] is True
        assert result["stdout_truncated"] is True
        assert len(str(result["stdout"])) <= 10

    @pytest.mark.anyio
    async def test_compound_command_via_shell(self, tmp_path: Path) -> None:
        result = await run_command("echo first && echo second", tmp_path)
        assert result["ok"] is True
        assert "first" in str(result["stdout"])
        assert "second" in str(result["stdout"])

    @pytest.mark.anyio
    async def test_string_cwd(self, tmp_path: Path) -> None:
        result = await run_command("echo ok", str(tmp_path))
        assert result["ok"] is True
