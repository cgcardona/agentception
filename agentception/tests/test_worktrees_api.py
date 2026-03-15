"""Tests for agentception/routes/api/worktrees.py.

Covers DELETE /api/worktrees/{slug} — the single HTTP endpoint in the module:

- 404 when slug is not found in the worktree list
- 400 when slug refers to the main worktree
- Success: non-locked worktree → only remove + prune spawned (2 subprocesses)
- Success: locked worktree → unlock + remove + prune spawned (3 subprocesses)
- Remove failure → falls back to shutil.rmtree; if rmtree also fails, deleted=False
- Both git and shutil fail → deleted=False, pruned=False, error set
- Prune failure → pruned=False, deleted status reflects remove outcome
- Response shape: DeleteWorktreeResult has {slug, deleted, pruned, db_cleared, error}
- slug in response always matches the path parameter

All calls to list_git_worktrees, asyncio.create_subprocess_exec, Path.exists,
and shutil.rmtree are mocked so no real filesystem ops occur.

Run targeted:
    pytest agentception/tests/test_worktrees_api.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

_LIST_WT = "agentception.readers.git.list_git_worktrees"
_SUBPROCESS = "agentception.routes.api.worktrees.asyncio.create_subprocess_exec"
_PATH_EXISTS = "agentception.routes.api.worktrees.Path"
_RMTREE = "agentception.routes.api.worktrees.shutil.rmtree"
_GET_RUN_ID = "agentception.routes.api.worktrees.get_run_id_for_worktree_path"
_CLEAR_WP = "agentception.routes.api.worktrees.clear_run_worktree_path"


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client — app lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


# ── Helpers ───────────────────────────────────────────────────────────────────


def _wt(slug: str, *, is_main: bool = False, locked: bool = False) -> dict[str, object]:
    """Minimal worktree dict matching the shape returned by list_git_worktrees."""
    return {
        "path": f"/worktrees/{slug}",
        "slug": slug,
        "is_main": is_main,
        "locked": locked,
    }


def _proc(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a MagicMock simulating a finished asyncio subprocess."""
    p = MagicMock()
    p.returncode = returncode
    p.communicate = AsyncMock(return_value=(b"", stderr))
    return p


# ── 404 / 400 guard rails ─────────────────────────────────────────────────────


def test_delete_worktree_404_when_list_empty(client: TestClient) -> None:
    """DELETE returns 404 when list_git_worktrees returns no worktrees."""
    with patch(_LIST_WT, new=AsyncMock(return_value=[])):
        resp = client.delete("/api/worktrees/no-such-slug")
    assert resp.status_code == 404


def test_delete_worktree_404_detail_contains_slug(client: TestClient) -> None:
    """404 detail string includes the requested slug so the caller knows what was missing."""
    with patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-999")])):
        resp = client.delete("/api/worktrees/missing-slug")
    assert resp.status_code == 404
    assert "missing-slug" in resp.json()["detail"]


def test_delete_worktree_404_when_slug_not_in_list(client: TestClient) -> None:
    """DELETE returns 404 when worktrees exist but none match the requested slug."""
    worktrees = [_wt("issue-100"), _wt("issue-200")]
    with patch(_LIST_WT, new=AsyncMock(return_value=worktrees)):
        resp = client.delete("/api/worktrees/issue-999")
    assert resp.status_code == 404


def test_delete_worktree_400_for_main_worktree(client: TestClient) -> None:
    """DELETE returns 400 when the slug refers to the main worktree."""
    with patch(_LIST_WT, new=AsyncMock(return_value=[_wt("agentception", is_main=True)])):
        resp = client.delete("/api/worktrees/agentception")
    assert resp.status_code == 400
    assert "main worktree" in resp.json()["detail"].lower()


# ── Success: non-locked worktree ──────────────────────────────────────────────


def test_delete_non_locked_returns_200(client: TestClient) -> None:
    """DELETE /api/worktrees/{slug} returns 200 for a valid non-locked worktree."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-610")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
    ):
        resp = client.delete("/api/worktrees/issue-610")
    assert resp.status_code == 200


def test_delete_non_locked_deleted_and_pruned(client: TestClient) -> None:
    """Non-locked worktree: deleted=True and pruned=True when both git calls succeed."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-610")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
    ):
        resp = client.delete("/api/worktrees/issue-610")
    body = resp.json()
    assert body["deleted"] is True
    assert body["pruned"] is True
    assert body["error"] is None


def test_delete_non_locked_slug_in_response(client: TestClient) -> None:
    """Response slug always matches the path parameter, not a computed value."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-777")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
    ):
        resp = client.delete("/api/worktrees/issue-777")
    assert resp.json()["slug"] == "issue-777"


def test_delete_non_locked_spawns_exactly_two_subprocesses(client: TestClient) -> None:
    """Non-locked worktree triggers exactly 2 subprocess calls: remove + prune."""
    calls: list[tuple[object, ...]] = []

    async def capture(*args: object, **_: object) -> MagicMock:
        calls.append(args)
        return _proc(0)

    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-333")])),
        patch(_SUBPROCESS, side_effect=capture),
    ):
        client.delete("/api/worktrees/issue-333")

    assert len(calls) == 2
    # First call: git worktree remove --force
    assert "remove" in calls[0]
    # Second call: git worktree prune
    assert "prune" in calls[1]


# ── Success: locked worktree ──────────────────────────────────────────────────


def test_delete_locked_returns_200(client: TestClient) -> None:
    """DELETE /api/worktrees/{slug} returns 200 for a locked worktree."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-711", locked=True)])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0), _proc(0)]),
    ):
        resp = client.delete("/api/worktrees/issue-711")
    assert resp.status_code == 200


def test_delete_locked_deleted_and_pruned(client: TestClient) -> None:
    """Locked worktree: deleted=True and pruned=True when all git calls succeed."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-711", locked=True)])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0), _proc(0)]),
    ):
        resp = client.delete("/api/worktrees/issue-711")
    body = resp.json()
    assert body["deleted"] is True
    assert body["pruned"] is True
    assert body["error"] is None


def test_delete_locked_spawns_three_subprocesses(client: TestClient) -> None:
    """Locked worktree triggers exactly 3 subprocess calls: unlock + remove + prune."""
    calls: list[tuple[object, ...]] = []

    async def capture(*args: object, **_: object) -> MagicMock:
        calls.append(args)
        return _proc(0)

    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-444", locked=True)])),
        patch(_SUBPROCESS, side_effect=capture),
    ):
        client.delete("/api/worktrees/issue-444")

    assert len(calls) == 3
    assert "unlock" in calls[0]
    assert "remove" in calls[1]
    assert "prune" in calls[2]


# ── Failure: remove fails → shutil.rmtree fallback ───────────────────────────


def test_delete_remove_failure_rmtree_fallback_succeeds(client: TestClient) -> None:
    """When git remove fails but the dir exists, shutil.rmtree is tried and deleted=True."""
    path_mock = MagicMock()
    path_mock.return_value.exists.return_value = True
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-500")])),
        patch(_SUBPROCESS, side_effect=[_proc(1, b"fatal: not a worktree"), _proc(0)]),
        patch(_PATH_EXISTS, path_mock),
        patch(_RMTREE),  # rmtree succeeds (no exception)
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-500")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert resp.json()["error"] is None


def test_delete_remove_failure_dir_already_gone_deleted_true(client: TestClient) -> None:
    """When git remove fails and the dir is gone, the worktree is treated as deleted."""
    path_mock = MagicMock()
    path_mock.return_value.exists.return_value = False
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-500")])),
        patch(_SUBPROCESS, side_effect=[_proc(1, b"fatal: not a worktree"), _proc(0)]),
        patch(_PATH_EXISTS, path_mock),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-500")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_delete_remove_failure_prune_still_runs(client: TestClient) -> None:
    """Even when git remove fails and rmtree is used, git worktree prune still runs."""
    calls: list[tuple[object, ...]] = []

    path_mock = MagicMock()
    path_mock.return_value.exists.return_value = False  # dir already gone → skip rmtree

    async def capture(*args: object, **_: object) -> MagicMock:
        calls.append(args)
        return _proc(1 if "remove" in args else 0)

    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-501")])),
        patch(_SUBPROCESS, side_effect=capture),
        patch(_PATH_EXISTS, path_mock),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        client.delete("/api/worktrees/issue-501")

    assert len(calls) == 2
    assert "prune" in calls[1]


# ── Failure: both git and shutil fail ────────────────────────────────────────


def test_delete_both_fail(client: TestClient) -> None:
    """When git remove, shutil.rmtree, and prune all fail: deleted=False, error set."""
    path_mock = MagicMock()
    path_mock.return_value.exists.return_value = True
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-300")])),
        patch(_SUBPROCESS, side_effect=[_proc(1, b"worktree busy"), _proc(1)]),
        patch(_PATH_EXISTS, path_mock),
        patch(_RMTREE, side_effect=OSError("permission denied")),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-300")
    body = resp.json()
    assert body["deleted"] is False
    assert body["pruned"] is False
    assert "worktree busy" in (body["error"] or "")


# ── Failure: prune fails ──────────────────────────────────────────────────────


def test_delete_prune_failure_pruned_false(client: TestClient) -> None:
    """When git worktree prune exits non-zero, pruned=False."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-400")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(1)]),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-400")
    body = resp.json()
    assert body["deleted"] is True
    assert body["pruned"] is False


# ── Response shape ────────────────────────────────────────────────────────────


def test_delete_response_shape(client: TestClient) -> None:
    """Response body has exactly the five DeleteWorktreeResult fields."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-111")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-111")
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"slug", "deleted", "pruned", "db_cleared", "error"}


def test_delete_error_is_none_on_success(client: TestClient) -> None:
    """error field is explicitly null (not omitted or empty string) on success."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-222")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-222")
    assert resp.json()["error"] is None


def test_delete_db_cleared_when_run_found(client: TestClient) -> None:
    """db_cleared=True when a matching run_id is found and cleared in the DB."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-555")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
        patch(_GET_RUN_ID, new=AsyncMock(return_value="issue-555")),
        patch(_CLEAR_WP, new=AsyncMock(return_value=True)),
    ):
        resp = client.delete("/api/worktrees/issue-555")
    assert resp.json()["db_cleared"] is True


def test_delete_db_cleared_false_when_no_run_found(client: TestClient) -> None:
    """db_cleared=False when no matching run_id exists in the DB."""
    with (
        patch(_LIST_WT, new=AsyncMock(return_value=[_wt("issue-556")])),
        patch(_SUBPROCESS, side_effect=[_proc(0), _proc(0)]),
        patch(_GET_RUN_ID, new=AsyncMock(return_value=None)),
    ):
        resp = client.delete("/api/worktrees/issue-556")
    assert resp.json()["db_cleared"] is False
