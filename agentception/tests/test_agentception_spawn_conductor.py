from __future__ import annotations

"""Tests for POST /api/control/spawn-conductor (AC-832).

Covers the happy path, all documented error paths, and task-file content.
All git operations and DB calls are mocked — no live network, no filesystem
side-effects beyond the tmp_path sandbox.

Run targeted:
    pytest agentception/tests/test_agentception_spawn_conductor.py -v
"""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


# ── POST /api/control/spawn-conductor — success ────────────────────────────────


def test_spawn_conductor_success(client: TestClient, tmp_path: Path) -> None:
    """POST /api/control/spawn-conductor with valid phases must return 200."""
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    async def _fake_exec(*args: object, **kwargs: object) -> MagicMock:
        is_worktree_add = "worktree" in args and "add" in args
        mock = MagicMock()
        mock.returncode = 0

        async def _communicate() -> tuple[bytes, bytes]:
            if is_worktree_add:
                # args[-2] is str(worktree_path) passed to git worktree add
                Path(str(args[-2])).mkdir(parents=True, exist_ok=True)
            return (b"", b"")

        mock.communicate = _communicate
        return mock

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch("agentception.db.persist.persist_wave_start", new_callable=AsyncMock),
    ):
        response = client.post(
            "/api/control/spawn-conductor",
            json={"phases": ["ac-ui/1-bugs", "ac-ui/2-features"]},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["wave_id"].startswith("conductor-")
    assert "worktree" in data
    assert "host_worktree" in data
    assert data["branch"].startswith("feat/conductor-")
    assert "agent_task" in data


def test_spawn_conductor_task_file_written_to_disk(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn-conductor must write a .agent-task file into the worktree."""
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    # We need the conductor worktree path to verify the file; capture it via a
    # mutable container so the callback closure can reference it.
    created: list[Path] = []

    async def _fake_exec(*args: object, **kwargs: object) -> MagicMock:
        is_worktree_add = "worktree" in args and "add" in args
        mock = MagicMock()
        mock.returncode = 0

        async def _communicate() -> tuple[bytes, bytes]:
            if is_worktree_add:
                # args[-2] is the worktree path passed to git worktree add
                wt = Path(str(args[-2]))
                wt.mkdir(parents=True, exist_ok=True)
                created.append(wt)
            return (b"", b"")

        mock.communicate = _communicate
        return mock

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch("agentception.db.persist.persist_wave_start", new_callable=AsyncMock),
    ):
        response = client.post(
            "/api/control/spawn-conductor",
            json={"phases": ["ac-ui/1-bugs"]},
        )

    assert response.status_code == 200
    assert created, "worktree directory was never created"
    task_file = created[0] / ".agent-task"
    assert task_file.exists(), ".agent-task file not found in worktree"
    content = task_file.read_text(encoding="utf-8")
    assert 'workflow = "conductor"' in content
    assert 'phases = ["ac-ui/1-bugs"]' in content
    assert "sub_agents = true" in content
    assert 'required_output = "wave_complete"' in content


def test_spawn_conductor_task_file_multi_phase(
    client: TestClient, tmp_path: Path
) -> None:
    """PHASES field must join all requested phases with commas."""
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    created: list[Path] = []

    async def _fake_exec(*args: object, **kwargs: object) -> MagicMock:
        is_worktree_add = "worktree" in args and "add" in args
        mock = MagicMock()
        mock.returncode = 0

        async def _communicate() -> tuple[bytes, bytes]:
            if is_worktree_add:
                wt = Path(str(args[-2]))
                wt.mkdir(parents=True, exist_ok=True)
                created.append(wt)
            return (b"", b"")

        mock.communicate = _communicate
        return mock

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch("agentception.db.persist.persist_wave_start", new_callable=AsyncMock),
    ):
        phases = ["phase-a", "phase-b", "phase-c"]
        response = client.post(
            "/api/control/spawn-conductor",
            json={"phases": phases},
        )

    assert response.status_code == 200
    content = (created[0] / ".agent-task").read_text()
    assert 'phases = ["phase-a", "phase-b", "phase-c"]' in content


# ── POST /api/control/spawn-conductor — empty phases → 422 ────────────────────


def test_spawn_conductor_empty_phases_returns_422(client: TestClient) -> None:
    """POST /api/control/spawn-conductor with empty phases must return 422."""
    response = client.post(
        "/api/control/spawn-conductor",
        json={"phases": []},
    )
    assert response.status_code == 422
    assert "phases" in response.json()["detail"].lower()


# ── POST /api/control/spawn-conductor — worktree exists → 409 ─────────────────


def test_spawn_conductor_existing_worktree_returns_409(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn-conductor must return 409 when worktree slug exists.

    We freeze datetime so the wave_id is deterministic, then pre-create the
    worktree directory to trigger the 409 pre-flight check.
    """
    from datetime import datetime, timezone

    fixed_dt = datetime(2026, 3, 3, 14, 22, 1, tzinfo=timezone.utc)
    wave_id = f"conductor-{fixed_dt.strftime('%Y%m%d-%H%M%S')}"
    worktrees = tmp_path / "worktrees"
    existing = worktrees / wave_id
    existing.mkdir(parents=True)

    mock_dt = MagicMock()
    mock_dt.now.return_value = fixed_dt

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.datetime", mock_dt),
    ):
        response = client.post(
            "/api/control/spawn-conductor",
            json={"phases": ["ac-ui/1-bugs"]},
        )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


# ── POST /api/control/spawn-conductor — git failure → 500 ─────────────────────


def test_spawn_conductor_git_failure_returns_500(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn-conductor must return 500 when git worktree add fails."""
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    async def _failing_exec(*args: object, **kwargs: object) -> MagicMock:
        mock = MagicMock()
        mock.returncode = 128

        async def _communicate() -> tuple[bytes, bytes]:
            return (b"", b"fatal: branch already exists")

        mock.communicate = _communicate
        return mock

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_failing_exec),
    ):
        response = client.post(
            "/api/control/spawn-conductor",
            json={"phases": ["ac-ui/1-bugs"]},
        )

    assert response.status_code == 500
    assert "git worktree add failed" in response.json()["detail"]


# ── SpawnConductorRequest model ───────────────────────────────────────────────


def test_spawn_conductor_request_org_defaults_to_none() -> None:
    """SpawnConductorRequest.org must default to None."""
    from agentception.models import SpawnConductorRequest
    req = SpawnConductorRequest(phases=["phase-a"])
    assert req.org is None


def test_spawn_conductor_request_accepts_org() -> None:
    """SpawnConductorRequest must accept an explicit org value."""
    from agentception.models import SpawnConductorRequest
    req = SpawnConductorRequest(phases=["phase-a"], org="my-org")
    assert req.org == "my-org"


# ── SpawnConductorResult model ────────────────────────────────────────────────


def test_spawn_conductor_result_fields() -> None:
    """SpawnConductorResult must expose wave_id, worktree, host_worktree, branch, agent_task."""
    from agentception.models import SpawnConductorResult
    result = SpawnConductorResult(
        wave_id="conductor-20260303-142201",
        worktree="/wt/conductor-20260303-142201",
        host_worktree="/host/conductor-20260303-142201",
        branch="feat/conductor-20260303-142201",
        agent_task='[task]\nworkflow = "conductor"\n',
    )
    assert result.wave_id == "conductor-20260303-142201"
    assert result.branch.startswith("feat/conductor-")
