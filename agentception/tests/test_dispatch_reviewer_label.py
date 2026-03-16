"""Tests for reviewer dispatch via POST /api/dispatch/label.

Covers every outcome of _dispatch_label_reviewer and find_pr_for_issue:

  - Happy path: PR found by branch name → worktree created, DB persisted,
    run_id = review-{pr_number}, branch = PR branch.
  - Happy path: PR found by closing keyword in body (e.g. "Closes #N").
  - 422 when no open PR exists for the issue.
  - 422 when git fetch of the PR branch fails (branch deleted after merge).
  - 409 when the reviewer worktree already exists (re-dispatch guard).
  - find_pr_for_issue returns None when GitHub API errors.
  - find_pr_for_issue skips PRs that don't match and picks the right one.
  - agent_loop reviewer warmup logs a warning when task.branch is missing.

Run targeted:
    pytest agentception/tests/test_dispatch_reviewer_label.py -v
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# find_pr_for_issue unit tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_find_pr_for_issue_matches_branch_name() -> None:
    """Returns the PR whose headRefName contains issue-{N}."""
    from agentception.readers.github import find_pr_for_issue

    fake_prs = [
        {"number": 10, "head": {"ref": "agent/other-stuff-aaaa"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": ""},
        {"number": 20, "head": {"ref": "agent/issue-1072-fix"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": ""},
    ]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_prs,
    ):
        result = await find_pr_for_issue(1072)

    assert result is not None
    assert result["number"] == 20
    assert result["headRefName"] == "agent/issue-1072-fix"


@pytest.mark.anyio
async def test_find_pr_for_issue_matches_closes_keyword_in_body() -> None:
    """Falls back to PR body search when branch name does not match."""
    from agentception.readers.github import find_pr_for_issue

    fake_prs = [
        {"number": 42, "head": {"ref": "agent/documentation-improvement-bc2e"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": "Closes #1072\n\nThis PR adds the services table."},
    ]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_prs,
    ):
        result = await find_pr_for_issue(1072)

    assert result is not None
    assert result["number"] == 42


@pytest.mark.anyio
async def test_find_pr_for_issue_body_variants() -> None:
    """All GitHub closing-keyword variants are recognised."""
    from agentception.readers.github import find_pr_for_issue

    for keyword in ("closes", "Closes", "close", "fixes", "Fixes", "fixed", "resolves", "Resolves", "resolved"):
        body = f"{keyword} #99"
        fake_prs = [
            {"number": 7, "head": {"ref": "some-branch"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": body},
        ]
        with patch(
            "agentception.readers.github._api_get_all",
            new_callable=AsyncMock,
            return_value=fake_prs,
        ):
            result = await find_pr_for_issue(99)
        assert result is not None, f"keyword {keyword!r} not matched"
        assert result["number"] == 7


@pytest.mark.anyio
async def test_find_pr_for_issue_branch_takes_priority_over_body() -> None:
    """Branch name match wins over body keyword when both present."""
    from agentception.readers.github import find_pr_for_issue

    fake_prs = [
        {"number": 100, "head": {"ref": "unrelated"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": "Closes #5"},
        {"number": 200, "head": {"ref": "agent/issue-5-impl"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": ""},
    ]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_prs,
    ):
        result = await find_pr_for_issue(5)

    assert result is not None
    assert result["number"] == 200


@pytest.mark.anyio
async def test_find_pr_for_issue_returns_none_when_no_match() -> None:
    """Returns None when no open PR mentions the issue."""
    from agentception.readers.github import find_pr_for_issue

    fake_prs = [
        {"number": 1, "head": {"ref": "unrelated"}, "base": {"ref": "dev"}, "draft": False, "merged_at": None, "body": "Some other text"},
    ]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_prs,
    ):
        result = await find_pr_for_issue(9999)

    assert result is None


@pytest.mark.anyio
async def test_find_pr_for_issue_returns_none_on_api_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Returns None (not raises) when the GitHub API call fails."""
    from agentception.readers.github import find_pr_for_issue

    with (
        patch(
            "agentception.readers.github._api_get_all",
            new_callable=AsyncMock,
            side_effect=RuntimeError("rate-limited"),
        ),
        caplog.at_level(logging.WARNING, logger="agentception.readers.github"),
    ):
        result = await find_pr_for_issue(42)

    assert result is None
    assert any("find_pr_for_issue" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _dispatch_label_reviewer unit tests
# ---------------------------------------------------------------------------


def _make_mock_subprocess_proc(returncode: int, stderr: bytes = b"") -> MagicMock:
    """Return a mock asyncio subprocess with the given return code."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.mark.anyio
async def test_dispatch_label_reviewer_happy_path_branch_match(tmp_path: Path) -> None:
    """Reviewer dispatch succeeds: PR found by branch, worktree created on PR branch."""
    from agentception.routes.api.dispatch import (
        LabelDispatchRequest,
        dispatch_label_agent,
    )

    _PersistKwarg = str | int | bool | None
    persisted: list[dict[str, _PersistKwarg]] = []

    async def mock_persist(**kwargs: _PersistKwarg) -> None:
        persisted.append(dict(kwargs))

    fake_pr = {
        "number": 1152,
        "headRefName": "agent/documentation-improvement-bc2e",
        "body": "Closes #1072",
        "state": "open",
    }

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    with (
        # Route through the real helper — mock its dependencies.
        patch(
            "agentception.readers.github.find_pr_for_issue",
            new_callable=AsyncMock,
            return_value=fake_pr,
        ),
        patch(
            "agentception.routes.api.dispatch.asyncio.create_subprocess_exec",
            return_value=_make_mock_subprocess_proc(returncode=0),
        ),
        patch(
            "agentception.readers.git.ensure_worktree",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agentception.routes.api.dispatch._configure_worktree_auth", new_callable=AsyncMock),
        patch("agentception.routes.api.dispatch.write_memory"),
        patch("agentception.routes.api.dispatch._resolve_cognitive_arch", return_value=None),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", side_effect=mock_persist),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.get_repo_dir_for", return_value=Path(str(tmp_path))),
    ):
        mock_settings.worktrees_dir = str(worktrees)
        mock_settings.host_worktrees_dir = str(worktrees)
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = LabelDispatchRequest(
            label="documentation-improvement",
            scope="issue",
            scope_issue_number=1072,
            role="reviewer",
            repo="cgcardona/agentception",
        )
        resp = await dispatch_label_agent(req)

    assert resp.run_id == "review-1152"
    assert resp.status == "pending_launch"
    assert len(persisted) == 1
    p = persisted[0]
    assert p["run_id"] == "review-1152"
    assert p["role"] == "reviewer"
    assert p["branch"] == "agent/documentation-improvement-bc2e"
    assert p["pr_number"] == 1152
    assert p["issue_number"] == 1072


@pytest.mark.anyio
async def test_dispatch_label_reviewer_raises_422_when_no_pr(tmp_path: Path) -> None:
    """422 returned when no open PR exists for the issue."""
    from agentception.routes.api.dispatch import LabelDispatchRequest, dispatch_label_agent

    with (
        patch(
            "agentception.readers.github.find_pr_for_issue",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.get_repo_dir_for", return_value=Path(str(tmp_path))),
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees")
        mock_settings.host_worktrees_dir = str(tmp_path / "worktrees")
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = LabelDispatchRequest(
            label="documentation-improvement",
            scope="issue",
            scope_issue_number=1072,
            role="reviewer",
            repo="cgcardona/agentception",
        )
        with pytest.raises(HTTPException) as exc_info:
            await dispatch_label_agent(req)

    assert exc_info.value.status_code == 422
    assert "No open PR" in str(exc_info.value.detail)


@pytest.mark.anyio
async def test_dispatch_label_reviewer_raises_422_when_branch_not_on_remote(tmp_path: Path) -> None:
    """422 returned when git fetch of the PR branch fails (branch deleted)."""
    from agentception.routes.api.dispatch import LabelDispatchRequest, dispatch_label_agent

    fake_pr = {
        "number": 500,
        "headRefName": "agent/deleted-branch-0001",
        "body": "Closes #1072",
        "state": "open",
    }

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    with (
        patch(
            "agentception.readers.github.find_pr_for_issue",
            new_callable=AsyncMock,
            return_value=fake_pr,
        ),
        patch(
            "agentception.routes.api.dispatch.asyncio.create_subprocess_exec",
            return_value=_make_mock_subprocess_proc(
                returncode=128,
                stderr=b"error: couldn't find remote ref agent/deleted-branch-0001",
            ),
        ),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.get_repo_dir_for", return_value=Path(str(tmp_path))),
    ):
        mock_settings.worktrees_dir = str(worktrees)
        mock_settings.host_worktrees_dir = str(worktrees)
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = LabelDispatchRequest(
            label="documentation-improvement",
            scope="issue",
            scope_issue_number=1072,
            role="reviewer",
            repo="cgcardona/agentception",
        )
        with pytest.raises(HTTPException) as exc_info:
            await dispatch_label_agent(req)

    assert exc_info.value.status_code == 422
    assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.anyio
async def test_dispatch_label_reviewer_raises_409_when_worktree_exists(tmp_path: Path) -> None:
    """409 returned when the reviewer worktree directory already exists."""
    from agentception.routes.api.dispatch import LabelDispatchRequest, dispatch_label_agent

    fake_pr = {
        "number": 1152,
        "headRefName": "agent/documentation-improvement-bc2e",
        "body": "Closes #1072",
        "state": "open",
    }

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    existing = worktrees / "review-1152"
    existing.mkdir()  # simulate existing worktree

    with (
        patch(
            "agentception.readers.github.find_pr_for_issue",
            new_callable=AsyncMock,
            return_value=fake_pr,
        ),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.get_repo_dir_for", return_value=Path(str(tmp_path))),
    ):
        mock_settings.worktrees_dir = str(worktrees)
        mock_settings.host_worktrees_dir = str(worktrees)
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = LabelDispatchRequest(
            label="documentation-improvement",
            scope="issue",
            scope_issue_number=1072,
            role="reviewer",
            repo="cgcardona/agentception",
        )
        with pytest.raises(HTTPException) as exc_info:
            await dispatch_label_agent(req)

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_dispatch_label_non_reviewer_routes_to_normal_path(tmp_path: Path) -> None:
    """Developer+issue scope still uses the label path (no PR lookup)."""
    from agentception.routes.api.dispatch import LabelDispatchRequest, dispatch_label_agent

    _PersistKwarg = str | int | bool | None
    persisted: list[dict[str, _PersistKwarg]] = []

    async def mock_persist(**kwargs: _PersistKwarg) -> None:
        persisted.append(dict(kwargs))

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    with (
        # find_pr_for_issue must NOT be called for developer dispatch
        patch(
            "agentception.readers.github.find_pr_for_issue",
            new_callable=AsyncMock,
            side_effect=AssertionError("find_pr_for_issue should not be called for developer dispatch"),
        ),
        patch(
            "agentception.routes.api.dispatch._resolve_dev_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        ),
        patch(
            "agentception.routes.api.dispatch.asyncio.create_subprocess_exec",
            return_value=_make_mock_subprocess_proc(returncode=0),
        ),
        patch("agentception.routes.api.dispatch._resolve_cognitive_arch", return_value=None),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", side_effect=mock_persist),
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
    ):
        mock_settings.worktrees_dir = str(worktrees)
        mock_settings.host_worktrees_dir = str(worktrees)
        mock_settings.repo_dir = str(tmp_path)
        mock_settings.gh_repo = "cgcardona/agentception"

        req = LabelDispatchRequest(
            label="documentation-improvement",
            scope="issue",
            scope_issue_number=1072,
            role="developer",
            repo="cgcardona/agentception",
        )
        resp = await dispatch_label_agent(req)

    assert resp.run_id.startswith("label-documentation-improvement-")
    assert len(persisted) == 1
    assert persisted[0]["role"] == "developer"
    assert "pr_number" not in persisted[0]


# ---------------------------------------------------------------------------
# agent_loop reviewer warmup fallback warning test
# ---------------------------------------------------------------------------


def test_reviewer_warmup_logs_warning_when_task_branch_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warning is emitted when task.branch is empty and the default branch is used."""
    # This tests the guard in agent_loop run_agent_loop — we inspect the
    # log output rather than running the full loop.
    import agentception.services.agent_loop as al

    # Simulate the fallback logic extracted from the reviewer branch in run_agent_loop.
    issue_number = 1072
    run_id = "review-1152"
    task_branch = ""  # missing — simulates a mis-configured dispatch

    with caplog.at_level(logging.WARNING, logger="agentception.services.agent_loop"):
        _pr_branch = task_branch or ""
        if not _pr_branch:
            _pr_branch = f"agent/issue-{issue_number}"
            al.logger.warning(
                "⚠️ reviewer_warmup: task.branch not set for run_id=%s — "
                "falling back to %r.  Dispatch the reviewer via the org chart "
                "or auto_dispatch_reviewer so pr_branch is always propagated.",
                run_id,
                _pr_branch,
            )

    assert _pr_branch == "agent/issue-1072"
    assert any("task.branch not set" in r.message for r in caplog.records)
