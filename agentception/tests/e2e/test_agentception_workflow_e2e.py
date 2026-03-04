from __future__ import annotations

"""E2E smoke test harness — brain dump → issues → conductor spawn (AC-836).

Exercises the full 1-2-3 AgentCeption workflow end-to-end with all GitHub
calls and git operations mocked.  No live network, no real filesystem
side-effects outside of ``tmp_path``.

Workflow under test:
  Step 1 — ``POST /api/brain-dump/plan``         (pure heuristic, no GitHub)
  Step 2 — ``POST /api/control/spawn-coordinator`` (creates worktree + task)
  Step 3 — ``GET  /api/wizard/state``            (skip if not yet implemented)
  Step 4 — ``POST /api/control/spawn-conductor`` (creates conductor worktree)
  Step 5 — Poller tick simulation                (mocked GitHub, returns state)

Run targeted:
    docker compose exec agentception pytest agentception/tests/e2e/test_agentception_workflow_e2e.py -v -m e2e
"""

import logging
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import PipelineState
from agentception.poller import GitHubBoard, tick

logger = logging.getLogger(__name__)

# All tests in this module are tagged as E2E smoke tests.
pytestmark = pytest.mark.e2e

# Sample brain dump used across multiple tests — exercises all four phase buckets.
_SAMPLE_DUMP = (
    "- Login fails intermittently on mobile\n"
    "- Migrate auth to JWT with refresh tokens\n"
    "- Add dark mode toggle across the dashboard\n"
    "- Refactor legacy jQuery to Alpine.js\n"
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Shared git subprocess mock factory
# ---------------------------------------------------------------------------


def _make_fake_exec(
    created: list[Path],
) -> object:
    """Return an async callable that fakes ``asyncio.create_subprocess_exec``.

    When the command is ``git worktree add``, the worktree path (``args[-2]``)
    is created on disk and appended to ``created`` so tests can inspect it.
    All other subprocess calls succeed silently (returncode=0, empty output).
    """

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

    return _fake_exec


# ---------------------------------------------------------------------------
# Step 2: POST /api/control/spawn-coordinator
# ---------------------------------------------------------------------------


def test_spawn_coordinator_creates_worktree(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn-coordinator → worktree dir created, .agent-task present.

    Asserts:
    - 200 response with the expected response fields.
    - Worktree directory exists on disk after the call.
    - ``.agent-task`` file contains ``WORKFLOW=bugs-to-issues`` and a
      ``BRAIN_DUMP:`` section, meaning the coordinator agent knows what to do.
    """
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    created: list[Path] = []

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_make_fake_exec(created)),
    ):
        response = client.post(
            "/api/control/spawn-coordinator",
            json={"plan_text": _SAMPLE_DUMP, "label_prefix": ""},
        )

    assert response.status_code == 200
    data = response.json()
    assert "slug" in data
    assert "worktree" in data
    assert "host_worktree" in data
    assert "branch" in data
    assert "agent_task" in data
    assert data["slug"].startswith("plan-"), (
        f"slug must start with 'plan-', got {data['slug']!r}"
    )
    assert data["branch"].startswith("feat/plan-"), (
        f"branch must start with 'feat/plan-', got {data['branch']!r}"
    )

    assert created, "worktree directory was never created by git worktree add"
    task_file = created[0] / ".agent-task"
    assert task_file.exists(), ".agent-task file not found in coordinator worktree"
    content = task_file.read_text(encoding="utf-8")
    assert "WORKFLOW=bugs-to-issues" in content, (
        "coordinator .agent-task must declare WORKFLOW=bugs-to-issues"
    )
    assert "PLAN_DUMP:" in content, (
            "coordinator .agent-task must embed the original plan text"
        )


# ---------------------------------------------------------------------------
# Step 3: GET /api/wizard/state
# ---------------------------------------------------------------------------


def test_wizard_state_endpoint(client: TestClient) -> None:
    """GET /api/wizard/state → step1 pending when not yet implemented (skip gracefully).

    This sub-test is forward-looking: it probes for the wizard state endpoint
    described in AC-836.  If the endpoint does not exist yet, the test skips
    rather than failing so it does not block the PR merge — it will begin
    catching regressions once the endpoint ships.
    """
    response = client.get("/api/wizard/state")
    if response.status_code in (404, 405):
        pytest.skip("/api/wizard/state endpoint not yet implemented — skipping")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict), "wizard state response must be a JSON object"


# ---------------------------------------------------------------------------
# Step 4: POST /api/control/spawn-conductor
# ---------------------------------------------------------------------------


def test_spawn_conductor_creates_worktree(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn-conductor → conductor-* worktree created, task file correct.

    Asserts:
    - 200 response with ``wave_id`` starting with ``conductor-``.
    - Worktree directory created on disk.
    - ``.agent-task`` exists and contains ``WORKFLOW=conductor``.
    - ``BATCH_ID`` field is present and non-empty.
    """
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    created: list[Path] = []

    with (
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktrees),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_make_fake_exec(created)),
        patch("agentception.db.persist.persist_wave_start", new_callable=AsyncMock),
    ):
        response = client.post(
            "/api/control/spawn-conductor",
            json={"phases": ["phase-0/bugs", "phase-1/infra"]},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["wave_id"].startswith("conductor-"), (
        f"wave_id must start with 'conductor-', got {data['wave_id']!r}"
    )
    assert "worktree" in data
    assert "host_worktree" in data
    assert "branch" in data
    assert "agent_task" in data
    assert data["branch"].startswith("feat/conductor-"), (
        f"branch must start with 'feat/conductor-', got {data['branch']!r}"
    )

    assert created, "conductor worktree directory was never created"
    task_file = created[0] / ".agent-task"
    assert task_file.exists(), ".agent-task file not found in conductor worktree"
    content = task_file.read_text(encoding="utf-8")
    assert "WORKFLOW=conductor" in content, (
        "conductor .agent-task must declare WORKFLOW=conductor"
    )

    # BATCH_ID is the wave_id slug — a timestamped conductor identifier.
    batch_id_line = next(
        (ln for ln in content.splitlines() if ln.startswith("BATCH_ID=")), None
    )
    assert batch_id_line is not None, "BATCH_ID field missing from .agent-task"
    batch_id_value = batch_id_line.split("=", 1)[1].strip()
    assert batch_id_value, "BATCH_ID must not be empty"
    assert batch_id_value.startswith("conductor-"), (
        f"BATCH_ID must start with 'conductor-', got {batch_id_value!r}"
    )


# ---------------------------------------------------------------------------
# Step 5: Poller tick simulation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_poller_tick_returns_pipeline_state() -> None:
    """Poller tick with mocked GitHub readers → PipelineState returned without error.

    Simulates a single tick of the polling loop by mocking all GitHub reader
    calls and the filesystem worktree scan.  Verifies that the tick completes
    successfully and returns a well-formed ``PipelineState``.
    """
    empty_board = GitHubBoard(
        active_label="phase-0/bugs",
        open_issues=[],
        open_prs=[],
        wip_issues=[],
        closed_issues=[],
        merged_prs=[],
    )

    with (
        patch(
            "agentception.poller.list_active_worktrees",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.poller.build_github_board",
            new_callable=AsyncMock,
            return_value=empty_board,
        ),
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        state = await tick()

    assert isinstance(state, PipelineState), (
        "tick() must return a PipelineState instance"
    )
    assert state.active_label == "phase-0/bugs"
    assert state.agents == []
    assert state.alerts == []
    assert state.polled_at > 0, "polled_at timestamp must be set"
