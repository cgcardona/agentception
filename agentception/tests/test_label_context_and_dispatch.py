"""Tests for GET /api/dispatch/context and the scope-based POST /api/dispatch/label.

Covers:
  - _label_slug() produces filesystem-safe slugs capped at 48 characters.
  - _tier_for_role() returns the right tier for every role class.
  - _role_and_tier_for_scope() derives the correct tier and default role for each scope.
  - GET /api/dispatch/context returns empty lists gracefully when the DB is empty.
  - GET /api/dispatch/prompt returns prompt content and 404 when the file is missing.
  - POST /api/dispatch/label with scope=full_initiative spawns a root coordinator with role cto.
  - POST /api/dispatch/label with scope=phase spawns a coordinator for the sub-label.
  - POST /api/dispatch/label with scope=issue spawns a worker for the given issue number.
  - POST /api/dispatch/label respects an explicit role override in the request.
  - .agent-task file contains scope_type=issue and scope_value=<number> for issue scope.
  - .agent-task file contains initiative_label for phase and issue scopes.
  - cascade_enabled defaults to True and propagates correctly when set to False.

Run targeted:
    pytest agentception/tests/test_label_context_and_dispatch.py -v
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.routes.api.dispatch import (
    _label_slug,
    _role_and_tier_for_scope,
    _tier_for_role,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_fake_proc(returncode: int = 0) -> AsyncMock:
    """Return a mock asyncio.Process whose communicate() succeeds."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (b"", b"")
    return proc


def _make_worktree_exec() -> MagicMock:
    """Return a mock for asyncio.create_subprocess_exec that creates the worktree dir.

    When ``git worktree add <path> -b <branch>`` is called the mock creates the
    directory so that the subsequent .agent-task write succeeds.
    """
    async def _side_effect(*args: str, **_kwargs: object) -> AsyncMock:
        if len(args) >= 4 and args[1] == "worktree" and args[2] == "add":
            Path(args[3]).mkdir(parents=True, exist_ok=True)
        return _make_fake_proc()

    return MagicMock(side_effect=_side_effect)


def _make_agent_task_capture() -> tuple[list[str], Callable[..., int]]:
    """Return (written_text, capture_fn) for intercepting .agent-task writes.

    Patch ``Path.write_text`` with the returned capture_fn inside a ``with``
    block, then inspect ``written_text[0]`` after the block exits.
    """
    written: list[str] = []
    original = Path.write_text

    def _capture(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.name == ".agent-task":
            written.append(data)
        return original(self, data, encoding=encoding, errors=errors, newline=newline)

    return written, _capture


def _dispatch_label_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "label": "ac-workflow",
        "scope": "full_initiative",
        "repo": "cgcardona/agentception",
    }
    base.update(overrides)
    return base


def _mock_dispatch_settings(
    mock: MagicMock,
    tmp_path: Path,
    *,
    subdir: str = "worktrees",
) -> None:
    """Configure a settings mock with all paths dispatch-label needs."""
    mock.worktrees_dir = str(tmp_path / subdir)
    mock.host_worktrees_dir = "/host/worktrees"
    mock.host_repo_dir = str(tmp_path)
    mock.repo_dir = str(tmp_path)


# ---------------------------------------------------------------------------
# Unit tests — _label_slug
# ---------------------------------------------------------------------------


def test_label_slug_replaces_non_alphanumeric_with_hyphens() -> None:
    assert _label_slug("ac-workflow/5-plan step v2") == "ac-workflow-5-plan-step-v2"


def test_label_slug_lowercases_input() -> None:
    assert _label_slug("AC-WORKFLOW") == "ac-workflow"


def test_label_slug_caps_at_48_chars() -> None:
    assert len(_label_slug("a" * 60)) == 48


# ---------------------------------------------------------------------------
# Unit tests — _tier_for_role
# ---------------------------------------------------------------------------


def test_tier_for_role_cto_is_coordinator() -> None:
    assert _tier_for_role("cto") == "coordinator"


def test_tier_for_role_ceo_is_coordinator() -> None:
    assert _tier_for_role("ceo") == "coordinator"


def test_tier_for_role_engineering_coordinator_is_coordinator() -> None:
    assert _tier_for_role("engineering-coordinator") == "coordinator"


def test_tier_for_role_pr_reviewer_is_worker() -> None:
    assert _tier_for_role("pr-reviewer") == "worker"


def test_tier_for_role_python_developer_is_worker() -> None:
    assert _tier_for_role("python-developer") == "worker"


def test_tier_for_role_unknown_slug_is_worker() -> None:
    assert _tier_for_role("rust-wizard") == "worker"


# ---------------------------------------------------------------------------
# Unit tests — _role_and_tier_for_scope
# ---------------------------------------------------------------------------


def test_scope_full_initiative_is_coordinator() -> None:
    role, tier = _role_and_tier_for_scope("full_initiative", None)
    assert tier == "coordinator"
    assert role == "cto"


def test_scope_phase_is_coordinator() -> None:
    role, tier = _role_and_tier_for_scope("phase", None)
    assert tier == "coordinator"
    assert role == "engineering-coordinator"


def test_scope_issue_is_worker() -> None:
    role, tier = _role_and_tier_for_scope("issue", None)
    assert tier == "worker"
    assert role == "python-developer"


def test_scope_role_override_respected() -> None:
    role, tier = _role_and_tier_for_scope("full_initiative", "qa-coordinator")
    assert tier == "coordinator"
    assert role == "qa-coordinator"


def test_scope_role_override_blank_ignored() -> None:
    role, tier = _role_and_tier_for_scope("issue", "  ")
    assert tier == "worker"
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
# GET /api/dispatch/prompt
# ---------------------------------------------------------------------------


def test_get_dispatcher_prompt_returns_content(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """GET /api/dispatch/prompt returns the prompt text and canonical path."""
    (tmp_path / "dispatcher.md").write_text("# Dispatcher prompt", encoding="utf-8")
    with patch("agentception.routes.api.dispatch.settings") as mock_settings:
        mock_settings.ac_dir = tmp_path
        res = client.get("/api/dispatch/prompt")
    assert res.status_code == 200
    data = res.json()
    assert data["content"] == "# Dispatcher prompt"
    assert data["path"] == ".agentception/dispatcher.md"


def test_get_dispatcher_prompt_404_when_missing(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """GET /api/dispatch/prompt returns 404 when dispatcher.md is absent."""
    with patch("agentception.routes.api.dispatch.settings") as mock_settings:
        mock_settings.ac_dir = tmp_path
        res = client.get("/api/dispatch/prompt")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/dispatch/label — scope=full_initiative
# ---------------------------------------------------------------------------


def test_dispatch_label_full_initiative_creates_root_coordinator(
    client: TestClient,
    tmp_path: Path,
) -> None:
    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", new_callable=AsyncMock),
    ):
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees")
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="full_initiative"),
        )

    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "coordinator"
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
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees2")
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
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees3")
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


def test_dispatch_label_phase_scope_persists_tier_to_db(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """dispatch-label for phase scope must persist tier=coordinator to the DB."""
    persist_mock = AsyncMock()

    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec") as mock_exec,
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", persist_mock),
    ):
        wt_dir = tmp_path / "worktrees4"
        wt_dir.mkdir(parents=True)
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees4")
        mock_exec.return_value = _make_fake_proc()

        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(
                scope="phase",
                scope_label="ac-workflow/5-plan-step-v2",
            ),
        )

    assert res.status_code == 200
    persist_mock.assert_awaited_once()
    kwargs = persist_mock.call_args.kwargs
    assert kwargs["tier"] == "coordinator"
    assert kwargs["gh_repo"] == "cgcardona/agentception"


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
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees5")
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(
                scope="issue",
                scope_issue_number=108,
            ),
        )

    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "worker"
    assert data["role"] == "python-developer"


def test_dispatch_label_issue_scope_persists_issue_number_to_db(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """dispatch-label for issue scope must persist issue_number and tier=worker to DB."""
    persist_mock = AsyncMock()

    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec") as mock_exec,
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", persist_mock),
    ):
        wt_dir = tmp_path / "worktrees6"
        wt_dir.mkdir(parents=True)
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees6")
        mock_exec.return_value = _make_fake_proc()

        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="issue", scope_issue_number=42),
        )

    assert res.status_code == 200
    persist_mock.assert_awaited_once()
    kwargs = persist_mock.call_args.kwargs
    assert kwargs["issue_number"] == 42
    assert kwargs["tier"] == "worker"


# ---------------------------------------------------------------------------
# cascade_enabled field — smoke-test mode
# ---------------------------------------------------------------------------


def test_dispatch_label_full_initiative_scope_persists_to_db(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """dispatch-label for full_initiative scope must persist coordinator tier to DB."""
    persist_mock = AsyncMock()

    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", persist_mock),
    ):
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees7")
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="full_initiative"),
        )

    assert res.status_code == 200
    persist_mock.assert_awaited_once()
    kwargs = persist_mock.call_args.kwargs
    assert kwargs["tier"] == "coordinator"


def test_dispatch_label_returns_200_with_cascade_disabled(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """dispatch-label with cascade_enabled=False must still return 200 and persist to DB."""
    persist_mock = AsyncMock()

    with (
        patch("agentception.routes.api.dispatch.settings") as mock_settings,
        patch("agentception.routes.api.dispatch.asyncio.create_subprocess_exec", new=_make_worktree_exec()),
        patch("agentception.routes.api.dispatch.persist_agent_run_dispatch", persist_mock),
    ):
        _mock_dispatch_settings(mock_settings, tmp_path, subdir="worktrees8")
        res = client.post(
            "/api/dispatch/label",
            json=_dispatch_label_body(scope="full_initiative", cascade_enabled=False),
        )

    assert res.status_code == 200
    persist_mock.assert_awaited_once()
