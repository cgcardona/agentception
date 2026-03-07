from __future__ import annotations

"""Tests for GET /api/dispatch/context and the scope-based POST /api/dispatch/label.

Covers:
  - _role_and_tier_for_scope() derives the correct tier and default role for each scope.
  - GET /api/dispatch/context returns empty lists gracefully when the DB is empty.
  - POST /api/dispatch/label with scope=full_initiative spawns an executive with role cto.
  - POST /api/dispatch/label with scope=phase spawns a coordinator for the sub-label.
  - POST /api/dispatch/label with scope=issue spawns an engineer for the given issue number.
  - POST /api/dispatch/label respects an explicit role override in the request.
  - .agent-task file contains SCOPE_TYPE=issue and SCOPE_VALUE=<number> for issue scope.
  - .agent-task file contains INITIATIVE_LABEL for phase and issue scopes.

Run targeted:
    pytest agentception/tests/test_label_context_and_dispatch.py -v
"""

import asyncio
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.routes.api.dispatch import _role_and_tier_for_scope


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests — _role_and_tier_for_scope
# ---------------------------------------------------------------------------


def test_scope_full_initiative_is_executive() -> None:
    role, tier = _role_and_tier_for_scope("full_initiative", None)
    assert tier == "executive"
    assert role == "cto"


def test_scope_phase_is_coordinator() -> None:
    role, tier = _role_and_tier_for_scope("phase", None)
    assert tier == "coordinator"
    assert role == "engineering-coordinator"


def test_scope_issue_is_engineer() -> None:
    role, tier = _role_and_tier_for_scope("issue", None)
    assert tier == "engineer"
    assert role == "python-developer"


def test_scope_role_override_respected() -> None:
    role, tier = _role_and_tier_for_scope("full_initiative", "qa-coordinator")
    assert tier == "coordinator"
    assert role == "qa-coordinator"


def test_scope_role_override_blank_ignored() -> None:
    role, tier = _role_and_tier_for_scope("issue", "  ")
    assert tier == "engineer"
    assert role == "python-developer"


# ---------------------------------------------------------------------------
# GET /api/dispatch/context — graceful empty response
# ---------------------------------------------------------------------------


def test_label_context_returns_empty_lists_when_no_db_data(
    client: TestClient,
) -> None:
    """label-context must return {phases:[], issues:[]} even when DB is empty."""
    with patch(
        "agentception.routes.api.dispatch.get_label_context",
        new_callable=AsyncMock,
        return_value={"phases": [], "issues": []},
    ):
        res = client.get(
            "/api/dispatch/context",
            params={"label": "ac-workflow", "repo": "cgcardona/agentception"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["phases"] == []
    assert data["issues"] == []


def test_label_context_returns_phases_and_issues(client: TestClient) -> None:
    mock_ctx = {
        "phases": [{"label": "ac-workflow/5-plan-step-v2", "count": 3}],
        "issues": [{"number": 42, "title": "Fix the thing"}],
    }
    with patch(
        "agentception.routes.api.dispatch.get_label_context",
        new_callable=AsyncMock,
        return_value=mock_ctx,
    ):
        res = client.get(
            "/api/dispatch/context",
            params={"label": "ac-workflow", "repo": "cgcardona/agentception"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["phases"][0]["label"] == "ac-workflow/5-plan-step-v2"
    assert data["phases"][0]["count"] == 3
    assert data["issues"][0]["number"] == 42


# ---------------------------------------------------------------------------
# Helpers — patch out filesystem / git / DB for dispatch-label tests
# ---------------------------------------------------------------------------


def _make_fake_proc(returncode: int = 0) -> AsyncMock:
    """Return a mock asyncio.Process whose communicate() succeeds."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (b"", b"")
    return proc


def _make_worktree_exec() -> AsyncMock:
    """Return a mock for asyncio.create_subprocess_exec that creates the worktree dir.

    When ``git worktree add <path> -b <branch>`` is called the mock creates the
    directory so that the subsequent .agent-task write succeeds.
    """
    async def _side_effect(*args: str, **_kwargs: object) -> AsyncMock:
        # args: ("git", "worktree", "add", <path>, "-b", <branch>)
        if len(args) >= 4 and args[1] == "worktree" and args[2] == "add":
            Path(args[3]).mkdir(parents=True, exist_ok=True)
        return _make_fake_proc()

    mock = MagicMock(side_effect=_side_effect)
    return mock


def _dispatch_label_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "label": "ac-workflow",
        "scope": "full_initiative",
        "repo": "cgcardona/agentception",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# POST /api/dispatch/label — scope=full_initiative
# ---------------------------------------------------------------------------


def test_dispatch_label_full_initiative_creates_coordinator(
    client: TestClient,
    tmp_path: Path,
) -> None:
    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees")
        mock_settings.host_worktrees_dir = "/host/worktrees"
        mock_settings.repo_dir = str(tmp_path)
    
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="full_initiative"),
        )

    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "executive"
    assert data["role"] == "cto"
    assert data["label"] == "ac-workflow"


def test_dispatch_label_full_initiative_role_override(
    client: TestClient,
    tmp_path: Path,
) -> None:
    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees2")
        mock_settings.host_worktrees_dir = "/host/worktrees"
        mock_settings.repo_dir = str(tmp_path)
    
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="full_initiative", role="engineering-coordinator"),
        )

    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "coordinator"
    assert data["role"] == "engineering-coordinator"


# ---------------------------------------------------------------------------
# POST /api/dispatch/label — scope=phase
# ---------------------------------------------------------------------------


def test_dispatch_label_phase_scope_is_coordinator(
    client: TestClient,
    tmp_path: Path,
) -> None:
    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees3")
        mock_settings.host_worktrees_dir = "/host/worktrees"
        mock_settings.repo_dir = str(tmp_path)
    
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(
                scope="phase",
                scope_label="ac-workflow/5-plan-step-v2",
            ),
        )

    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "coordinator"
    assert data["role"] == "engineering-coordinator"
    assert data["label"] == "ac-workflow"


def test_dispatch_label_phase_scope_agent_task_scope_value(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """The .agent-task file must use the phase sub-label as SCOPE_VALUE."""
    written_text: list[str] = []

    original_write_text = Path.write_text

    def _capture_write(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.name == ".agent-task":
            written_text.append(data)
        return original_write_text(self, data, encoding=encoding, errors=errors, newline=newline)

    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec") as mock_exec,
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
        patch.object(Path, "write_text", _capture_write),
    ):
        wt_dir = tmp_path / "worktrees4"
        wt_dir.mkdir(parents=True)
        mock_settings.worktrees_dir = str(wt_dir)
        mock_settings.host_worktrees_dir = "/host/worktrees"
        mock_settings.repo_dir = str(tmp_path)
        mock_exec.return_value = _make_fake_proc()

        client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(
                scope="phase",
                scope_label="ac-workflow/5-plan-step-v2",
            ),
        )

    assert written_text, "No .agent-task file was written"
    import tomllib
    task_data = tomllib.loads(written_text[0])
    assert task_data["target"]["scope_value"] == "ac-workflow/5-plan-step-v2"
    assert task_data["target"]["initiative_label"] == "ac-workflow"
    assert task_data["agent"]["tier"] == "coordinator"


# ---------------------------------------------------------------------------
# POST /api/dispatch/label — scope=issue
# ---------------------------------------------------------------------------


def test_dispatch_label_issue_scope_is_leaf(
    client: TestClient,
    tmp_path: Path,
) -> None:
    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
    ):
        mock_settings.worktrees_dir = str(tmp_path / "worktrees5")
        mock_settings.host_worktrees_dir = "/host/worktrees"
        mock_settings.repo_dir = str(tmp_path)
    
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(
                scope="issue",
                scope_issue_number=108,
            ),
        )

    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "engineer"
    assert data["role"] == "python-developer"


def test_dispatch_label_issue_scope_agent_task_fields(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """The .agent-task file must use SCOPE_TYPE=issue and the issue number as SCOPE_VALUE."""
    written_text: list[str] = []
    original_write_text = Path.write_text

    def _capture_write(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.name == ".agent-task":
            written_text.append(data)
        return original_write_text(self, data, encoding=encoding, errors=errors, newline=newline)

    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec") as mock_exec,
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
        patch.object(Path, "write_text", _capture_write),
    ):
        wt_dir = tmp_path / "worktrees6"
        wt_dir.mkdir(parents=True)
        mock_settings.worktrees_dir = str(wt_dir)
        mock_settings.host_worktrees_dir = "/host/worktrees"
        mock_settings.repo_dir = str(tmp_path)
        mock_exec.return_value = _make_fake_proc()

        client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="issue", scope_issue_number=42),
        )

    assert written_text, "No .agent-task file was written"
    import tomllib
    task_data = tomllib.loads(written_text[0])
    assert task_data["target"]["scope_type"] == "issue"
    assert task_data["target"]["scope_value"] == "42"
    assert task_data["target"]["initiative_label"] == "ac-workflow"
    assert task_data["agent"]["tier"] == "engineer"
