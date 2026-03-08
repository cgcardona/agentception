from __future__ import annotations

"""Tests for the manual spawn endpoint and issue-picker UI (AC-103).

Covers POST /api/control/spawn and GET /agents/spawn.
All GitHub calls and git operations are mocked — no live network, no
filesystem side-effects.

Also covers TOML output of _build_agent_task(), _build_coordinator_task(),
and _build_conductor_task() (AC-49): each builder must emit valid TOML that
round-trips through tomllib into the correct TaskFile fields.

Run targeted:
    pytest agentception/tests/test_agentception_spawn.py -v
"""

import tomllib
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import SpawnRequest, TaskFile, VALID_ROLES
from agentception.routes.api._shared import (
    _build_agent_task,
    _build_coordinator_task,
    _build_conductor_task,
)
from agentception.services.spawn_child import _build_child_task


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


# ── Helper: build a fake open issue dict ──────────────────────────────────────

def _open_issue(
    number: int,
    title: str = "Test issue",
    labels: list[str] | None = None,
) -> dict[str, object]:
    """Return a minimal open-issue dict as returned by get_issue()."""
    return {
        "number": number,
        "state": "OPEN",
        "title": title,
        "labels": labels or [],
    }


def _open_issue_list(
    number: int,
    title: str = "Test issue",
    label_names: list[str] | None = None,
) -> dict[str, object]:
    """Return a minimal open-issue dict as returned by get_open_issues()."""
    label_objs: list[object] = [
        {"name": name} for name in (label_names or [])
    ]
    return {
        "number": number,
        "title": title,
        "labels": label_objs,
        "body": "",
    }


# ── POST /api/control/spawn — success ─────────────────────────────────────────


def test_spawn_creates_worktree_and_task_file(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn must create a worktree and return SpawnResult on success."""
    worktree_dir = tmp_path / "worktrees" / "agentception"
    worktree_dir.mkdir(parents=True)
    # Simulate what `git worktree add` would do: create the directory.
    expected_worktree = worktree_dir / "issue-42"

    async def _fake_exec(*args: object, **kwargs: object) -> MagicMock:
        # Only simulate `git worktree add` creating the directory.  Other
        # subprocesses (e.g. `gh` CLI calls from the background poller) must
        # NOT create the worktree prematurely or the pre-flight existence check
        # will fire a spurious 409 before the spawn handler even starts.
        is_worktree_add = "worktree" in args and "add" in args
        mock = MagicMock()
        mock.returncode = 0

        async def _fake_communicate() -> tuple[bytes, bytes]:
            if is_worktree_add:
                expected_worktree.mkdir(parents=True, exist_ok=True)
            return (b"", b"")

        mock.communicate = _fake_communicate
        return mock

    with (
        patch(
            "agentception.routes.api.control.get_issue",
            return_value=_open_issue(42, "Fix the thing"),
        ),
        patch(
            "agentception.routes.api.control.get_issue_body",
            new_callable=AsyncMock,
            return_value="Refactor the config module to use fastapi settings.",
        ),
        patch(
            "agentception.routes.api.control.get_active_label",
            new_callable=AsyncMock,
            return_value="phase/1",
        ),
        patch("agentception.routes.api.control.add_wip_label", new_callable=AsyncMock),
        patch(
            "agentception.routes.api.control.settings.worktrees_dir",
            worktree_dir,
        ),
        patch(
            "agentception.routes.api.control.settings.host_worktrees_dir",
            worktree_dir,
        ),
        patch(
            "agentception.routes.api.control.settings.repo_dir",
            Path("/fake/repo"),
        ),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_exec,
        ),
    ):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 42, "role": "python-developer"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["spawned"] == 42
    assert "issue-42" in data["worktree"]
    assert "issue-42" in data["host_worktree"]
    assert data["branch"] == "feat/issue-42"
    # TOML output: check key = value format
    assert "issue_number = 42" in data["agent_task"]
    assert 'branch = "feat/issue-42"' in data["agent_task"]
    assert 'role = "python-developer"' in data["agent_task"]
    assert "cognitive_arch =" in data["agent_task"]
    # Verify the .agent-task file was actually written to disk.
    task_file = expected_worktree / ".agent-task"
    assert task_file.exists()
    assert "issue_number = 42" in task_file.read_text()


# ── POST /api/control/spawn — already claimed → 409 ──────────────────────────


def test_spawn_already_claimed_returns_409(client: TestClient) -> None:
    """POST /api/control/spawn must return 409 when the issue already has agent/wip."""
    with patch(
        "agentception.routes.api.control.get_issue",
        return_value=_open_issue(42, "Fix the thing", labels=["agent/wip", "enhancement"]),
    ):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 42},
        )

    assert response.status_code == 409
    assert "already claimed" in response.json()["detail"]


# ── POST /api/control/spawn — issue not found → 404 ──────────────────────────


def test_spawn_invalid_issue_returns_404(client: TestClient) -> None:
    """POST /api/control/spawn must return 404 when gh cannot find the issue."""
    with patch(
        "agentception.routes.api.control.get_issue",
        side_effect=RuntimeError("issue not found"),
    ):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 99999},
        )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_spawn_closed_issue_returns_404(client: TestClient) -> None:
    """POST /api/control/spawn must return 404 when the issue is closed."""
    closed = _open_issue(42)
    closed["state"] = "CLOSED"

    with patch("agentception.routes.api.control.get_issue", return_value=closed):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 42},
        )

    assert response.status_code == 404
    assert "not open" in response.json()["detail"]


# ── POST /api/control/spawn — invalid role → 422 ─────────────────────────────


def test_spawn_invalid_role_returns_422(client: TestClient) -> None:
    """POST /api/control/spawn must return 422 for an unrecognised role."""
    response = client.post(
        "/api/control/spawn",
        json={"issue_number": 42, "role": "chaos-monkey"},
    )
    assert response.status_code == 422


# ── POST /api/control/spawn — worktree already exists → 409 ──────────────────


def test_spawn_existing_worktree_returns_409(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn must return 409 when the worktree directory already exists."""
    worktrees = tmp_path / "worktrees" / "agentception"
    # Pre-create the issue worktree so the endpoint sees it exists
    existing = worktrees / "issue-42"
    existing.mkdir(parents=True)

    with (
        patch(
            "agentception.routes.api.control.get_issue",
            return_value=_open_issue(42),
        ),
        patch("agentception.routes.api.control.add_wip_label", new_callable=AsyncMock),
        patch("agentception.routes.api.control.settings.worktrees_dir", worktrees),
    ):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 42},
        )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


# ── GET /agents/spawn — form renders ─────────────────────────────────────────


def test_spawn_form_renders_issue_list(client: TestClient) -> None:
    """GET /agents/spawn must embed issue data in the Alpine data-issues attribute.

    Issues are rendered client-side by Alpine.js from the JSON in data-issues,
    so we check the JSON payload — not rendered HTML text.
    Issue 102 is not in the fake list (the query layer excludes claimed issues).
    """
    fake_issues = [
        {"number": 100, "title": "Issue Alpha", "labels": [], "claimed": False},
        {"number": 101, "title": "Issue Beta", "labels": [], "claimed": False},
    ]

    with patch(
        "agentception.db.queries.get_board_issues",
        new=AsyncMock(return_value=fake_issues),
    ):
        response = client.get("/agents/spawn")

    assert response.status_code == 200
    html = response.text
    # Issue data lives in the data-issues JSON attribute (Alpine hydration).
    assert '"number": 100' in html or "100" in html
    assert "Issue Alpha" in html
    assert "Issue Beta" in html
    # Issue 102 is excluded by the query layer; its number must not appear.
    assert "102" not in html


def test_spawn_form_renders_role_options(client: TestClient) -> None:
    """GET /agents/spawn form must include all valid role options."""
    with patch(
        "agentception.db.queries.get_board_issues",
        new=AsyncMock(return_value=[]),
    ):
        response = client.get("/agents/spawn")

    assert response.status_code == 200
    html = response.text
    for role in VALID_ROLES:
        assert role in html


def test_spawn_form_renders_empty_state_gracefully(client: TestClient) -> None:
    """GET /agents/spawn must render without error when there are no unclaimed issues."""
    with patch(
        "agentception.db.queries.get_board_issues",
        new=AsyncMock(return_value=[]),
    ):
        response = client.get("/agents/spawn")

    assert response.status_code == 200
    assert "AgentCeption" in response.text


# ── HTML success panel (Accept: text/html) ────────────────────────────────────


def test_spawn_returns_html_when_accept_text_html(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn with Accept: text/html must return the success partial."""
    worktree_dir = tmp_path / "worktrees" / "agentception"
    worktree_dir.mkdir(parents=True)
    expected_worktree = worktree_dir / "issue-55"

    async def _fake_exec(*args: object, **kwargs: object) -> MagicMock:
        is_worktree_add = "worktree" in args and "add" in args
        mock = MagicMock()
        mock.returncode = 0

        async def _fake_communicate() -> tuple[bytes, bytes]:
            if is_worktree_add:
                expected_worktree.mkdir(parents=True, exist_ok=True)
            return (b"", b"")

        mock.communicate = _fake_communicate
        return mock

    with (
        patch(
            "agentception.routes.api.control.get_issue",
            return_value=_open_issue(55, "HTML test issue"),
        ),
        patch(
            "agentception.routes.api.control.get_issue_body",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.api.control.get_active_label",
            new_callable=AsyncMock,
            return_value="phase/4",
        ),
        patch("agentception.routes.api.control.add_wip_label", new_callable=AsyncMock),
        patch("agentception.routes.api.control.settings.worktrees_dir", worktree_dir),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktree_dir),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 55, "role": "python-developer"},
            headers={"Accept": "text/html, application/json"},
        )

    assert response.status_code == 200
    # HTML path must return text/html content.
    assert "text/html" in response.headers.get("content-type", "")
    html = response.text
    # Success panel must include the agent detail link and key information.
    assert "/agents/issue-55" in html
    assert "55" in html
    assert "View agent" in html
    assert "spawn-form-container" in html
    # spawned_at timestamp must be present.
    assert "UTC" in html


def test_spawn_returns_json_without_html_accept(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /api/control/spawn without Accept: text/html must still return JSON."""
    worktree_dir = tmp_path / "worktrees" / "agentception"
    worktree_dir.mkdir(parents=True)
    expected_worktree = worktree_dir / "issue-56"

    async def _fake_exec(*args: object, **kwargs: object) -> MagicMock:
        is_worktree_add = "worktree" in args and "add" in args
        mock = MagicMock()
        mock.returncode = 0

        async def _fake_communicate() -> tuple[bytes, bytes]:
            if is_worktree_add:
                expected_worktree.mkdir(parents=True, exist_ok=True)
            return (b"", b"")

        mock.communicate = _fake_communicate
        return mock

    with (
        patch(
            "agentception.routes.api.control.get_issue",
            return_value=_open_issue(56, "JSON path test"),
        ),
        patch(
            "agentception.routes.api.control.get_issue_body",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.api.control.get_active_label",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch("agentception.routes.api.control.add_wip_label", new_callable=AsyncMock),
        patch("agentception.routes.api.control.settings.worktrees_dir", worktree_dir),
        patch("agentception.routes.api.control.settings.host_worktrees_dir", worktree_dir),
        patch("agentception.routes.api.control.settings.repo_dir", Path("/fake/repo")),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        response = client.post(
            "/api/control/spawn",
            json={"issue_number": 56, "role": "python-developer"},
        )

    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")
    data = response.json()
    assert data["spawned"] == 56
    assert "spawned_at" in data


def test_spawn_result_includes_spawned_at() -> None:
    """SpawnResult must include a spawned_at timestamp field (defaults to empty string)."""
    from agentception.models import SpawnResult
    result = SpawnResult(
        spawned=1,
        worktree="/wt/issue-1",
        host_worktree="/host/issue-1",
        branch="feat/issue-1",
        agent_task="ISSUE_NUMBER=1\n",
    )
    assert isinstance(result.spawned_at, str)


# ── SpawnRequest model validation ─────────────────────────────────────────────


def test_spawn_request_default_role() -> None:
    """SpawnRequest must default the role to python-developer."""
    req = SpawnRequest(issue_number=1)
    assert req.role == "python-developer"


def test_spawn_request_accepts_all_valid_roles() -> None:
    """SpawnRequest must accept every role in VALID_ROLES."""
    for role in VALID_ROLES:
        req = SpawnRequest(issue_number=1, role=role)
        assert req.role == role


def test_spawn_request_rejects_unknown_role() -> None:
    """SpawnRequest must raise ValueError for an unrecognised role."""
    with pytest.raises(ValueError):
        SpawnRequest(issue_number=1, role="hacker")


# ── TOML builder tests (AC-49) ─────────────────────────────────────────────


def _fake_worktree(tmp_path: Path, name: str = "issue-99") -> Path:
    p = tmp_path / name
    p.mkdir(parents=True)
    return p


def test_build_agent_task_emits_valid_toml(tmp_path: Path) -> None:
    """_build_agent_task() must produce output that tomllib.loads() accepts."""
    wt = _fake_worktree(tmp_path)
    output = _build_agent_task(
        issue_number=99,
        title="Test issue",
        role="python-developer",
        worktree=wt,
        host_worktree=wt,
        branch="feat/issue-99",
    )
    parsed = tomllib.loads(output)
    assert isinstance(parsed, dict)
    assert parsed["task"]["workflow"] == "issue-to-pr"
    assert parsed["task"]["version"] == "2.0"
    assert parsed["target"]["issue_number"] == 99
    assert parsed["agent"]["role"] == "python-developer"
    assert parsed["worktree"]["branch"] == "feat/issue-99"


def test_build_agent_task_round_trips_to_task_file(tmp_path: Path) -> None:
    """_build_agent_task() output must round-trip through TaskFile with correct fields."""
    wt = _fake_worktree(tmp_path)
    output = _build_agent_task(
        issue_number=77,
        title="Round-trip issue",
        role="python-developer",
        worktree=wt,
        host_worktree=wt,
        branch="feat/issue-77",
        phase_label="ac-test/0-foundation",
        depends_on=[10, 11],
        cognitive_arch="turing:python",
        wave_id="batch-123",
    )
    parsed = tomllib.loads(output)
    # Manually map TOML sections to TaskFile just as _build_task_file_from_toml does.
    task_sec = parsed.get("task", {})
    agent_sec = parsed.get("agent", {})
    repo_sec = parsed.get("repo", {})
    pipeline_sec = parsed.get("pipeline", {})
    spawn_sec = parsed.get("spawn", {})
    target_sec = parsed.get("target", {})
    worktree_sec = parsed.get("worktree", {})

    tf = TaskFile(
        task=task_sec.get("workflow"),
        id=task_sec.get("id"),
        attempt_n=task_sec.get("attempt_n", 0),
        required_output=task_sec.get("required_output"),
        on_block=task_sec.get("on_block"),
        role=agent_sec.get("role"),
        tier=agent_sec.get("tier"),
        org_domain=agent_sec.get("org_domain"),
        cognitive_arch=agent_sec.get("cognitive_arch"),
        gh_repo=repo_sec.get("gh_repo"),
        base=repo_sec.get("base"),
        batch_id=pipeline_sec.get("batch_id"),
        wave=pipeline_sec.get("wave"),
        spawn_mode=spawn_sec.get("mode"),
        spawn_sub_agents=spawn_sec.get("sub_agents", False),
        issue_number=target_sec.get("issue_number"),
        depends_on=list(target_sec.get("depends_on", [])),
        closes_issues=list(target_sec.get("closes", [])),
        worktree=worktree_sec.get("path"),
        branch=worktree_sec.get("branch"),
        linked_pr=worktree_sec.get("linked_pr"),
    )
    assert tf.task == "issue-to-pr"
    assert tf.role == "python-developer"
    assert tf.tier == "worker"
    assert tf.cognitive_arch == "turing:python"
    assert tf.issue_number == 77
    assert tf.depends_on == [10, 11]
    assert tf.closes_issues == [77]
    assert tf.branch == "feat/issue-77"
    assert tf.batch_id == "batch-123"
    assert tf.spawn_sub_agents is False
    assert tf.spawn_mode == "chain"
    assert tf.attempt_n == 0
    assert tf.required_output == "pr_url"


def test_build_coordinator_task_emits_valid_toml(tmp_path: Path) -> None:
    """_build_coordinator_task() must produce output that tomllib.loads() accepts."""
    wt = _fake_worktree(tmp_path, "coord-abc")
    output = _build_coordinator_task(
        slug="coord-abc",
        plan_text="We need to build a billing system.\n- Stripe integration\n- Invoices",
        label_prefix="q2-billing",
        worktree=wt,
        host_worktree=wt,
        branch="feat/coord-abc",
    )
    parsed = tomllib.loads(output)
    assert isinstance(parsed, dict)
    assert parsed["task"]["workflow"] == "bugs-to-issues"
    assert parsed["task"]["version"] == "2.0"
    assert parsed["agent"]["role"] == "coordinator"
    assert parsed["spawn"]["sub_agents"] is True
    assert "Stripe" in parsed["plan_draft"]["dump"]
    assert parsed["plan_draft"]["label_prefix"] == "q2-billing"


def test_build_coordinator_task_without_label_prefix(tmp_path: Path) -> None:
    """_build_coordinator_task() with empty label_prefix omits label_prefix field."""
    wt = _fake_worktree(tmp_path, "coord-nolabel")
    output = _build_coordinator_task(
        slug="coord-nolabel",
        plan_text="Simple plan.",
        label_prefix="",
        worktree=wt,
        host_worktree=wt,
        branch="feat/coord-nolabel",
    )
    parsed = tomllib.loads(output)
    assert "label_prefix" not in parsed.get("plan_draft", {})
    assert parsed["plan_draft"]["dump"] == "Simple plan."


def test_build_conductor_task_emits_valid_toml(tmp_path: Path) -> None:
    """_build_conductor_task() must produce output that tomllib.loads() accepts."""
    wt = _fake_worktree(tmp_path, "conductor-wave-1")
    output = _build_conductor_task(
        wave_id="wave-2026-001",
        phases=["ac-build/phase-0", "ac-build/phase-1"],
        org="engineering",
        worktree=wt,
        host_worktree=wt,
        branch="feat/conductor-wave-1",
    )
    parsed = tomllib.loads(output)
    assert isinstance(parsed, dict)
    assert parsed["task"]["workflow"] == "conductor"
    assert parsed["task"]["version"] == "2.0"
    assert parsed["agent"]["role"] == "conductor"
    assert parsed["agent"]["tier"] == "coordinator"
    assert parsed["spawn"]["sub_agents"] is True
    assert parsed["target"]["phases"] == ["ac-build/phase-0", "ac-build/phase-1"]
    assert parsed["target"]["org"] == "engineering"


def test_build_conductor_task_without_org(tmp_path: Path) -> None:
    """_build_conductor_task() with org=None omits the org field from [target]."""
    wt = _fake_worktree(tmp_path, "conductor-noorg")
    output = _build_conductor_task(
        wave_id="wave-noorg",
        phases=["phase-0"],
        org=None,
        worktree=wt,
        host_worktree=wt,
        branch="feat/conductor-noorg",
    )
    parsed = tomllib.loads(output)
    assert "org" not in parsed.get("target", {})


def test_build_agent_task_depends_on_empty_list(tmp_path: Path) -> None:
    """_build_agent_task() with no depends_on emits an empty TOML array."""
    wt = _fake_worktree(tmp_path, "issue-no-deps")
    output = _build_agent_task(
        issue_number=5,
        title="No deps",
        role="python-developer",
        worktree=wt,
        host_worktree=wt,
        branch="feat/issue-5",
    )
    parsed = tomllib.loads(output)
    assert parsed["target"]["depends_on"] == []


def test_build_agent_task_depends_on_list(tmp_path: Path) -> None:
    """_build_agent_task() passes a list of int deps into TOML target.depends_on."""
    wt = _fake_worktree(tmp_path, "issue-with-deps")
    output = _build_agent_task(
        issue_number=50,
        title="Has deps",
        role="python-developer",
        worktree=wt,
        host_worktree=wt,
        branch="feat/issue-50",
        depends_on=[10, 20, 30],
    )
    parsed = tomllib.loads(output)
    assert parsed["target"]["depends_on"] == [10, 20, 30]


def test_build_agent_task_file_ownership_as_toml_array(tmp_path: Path) -> None:
    """_build_agent_task() writes file_ownership as a TOML string array in [target]."""
    wt = _fake_worktree(tmp_path, "issue-ownership")
    output = _build_agent_task(
        issue_number=60,
        title="Ownership issue",
        role="python-developer",
        worktree=wt,
        host_worktree=wt,
        branch="feat/issue-60",
        file_ownership=["agentception/routes/api/_shared.py", "agentception/services/toml_task.py"],
    )
    parsed = tomllib.loads(output)
    assert parsed["target"]["file_ownership"] == [
        "agentception/routes/api/_shared.py",
        "agentception/services/toml_task.py",
    ]


def test_build_agent_task_file_ownership_defaults_to_empty_array(tmp_path: Path) -> None:
    """_build_agent_task() without file_ownership emits an empty TOML array."""
    wt = _fake_worktree(tmp_path, "issue-no-ownership")
    output = _build_agent_task(
        issue_number=61,
        title="No ownership",
        role="python-developer",
        worktree=wt,
        host_worktree=wt,
        branch="feat/issue-61",
    )
    parsed = tomllib.loads(output)
    assert parsed["target"]["file_ownership"] == []


# ---------------------------------------------------------------------------
# _build_child_task — TOML output regression (swim lane fix)
# ---------------------------------------------------------------------------


def test_build_child_task_issue_scope_emits_valid_toml(tmp_path: Path) -> None:
    """_build_child_task() must emit valid TOML for issue-scoped agents.

    Regression: spawn_child previously emitted KEY=VALUE which parse_agent_task()
    (now TOML-only) silently dropped, leaving no ACAgentRun row and cards stuck
    in the Todo swim lane.
    """
    output = _build_child_task(
        run_id="issue-48-abc123",
        role="python-developer",
        tier="worker",
        org_domain="engineering",
        scope_type="issue",
        scope_value="48",
        gh_repo="owner/repo",
        branch="feat/issue-48-a1b2",
        worktree_path="/worktrees/issue-48-abc123",
        host_worktree_path="/host/worktrees/issue-48-abc123",
        batch_id="issue-48-20260306T120000Z-1234",
        parent_run_id="coord-xyz",
        cognitive_arch="von_neumann:python",
        coord_fingerprint="Engineering Coordinator · batch-001",
        issue_title="Migrate parse_agent_task()",
        issue_number=48,
    )
    # Must be parseable as TOML (not KEY=VALUE)
    parsed = tomllib.loads(output)
    assert parsed["task"]["workflow"] == "issue-to-pr"
    assert parsed["agent"]["role"] == "python-developer"
    assert parsed["agent"]["tier"] == "worker"
    assert parsed["agent"]["org_domain"] == "engineering"
    assert parsed["agent"]["cognitive_arch"] == "von_neumann:python"
    assert parsed["repo"]["gh_repo"] == "owner/repo"
    assert parsed["pipeline"]["batch_id"] == "issue-48-20260306T120000Z-1234"
    assert parsed["pipeline"]["parent_run_id"] == "coord-xyz"
    assert parsed["pipeline"]["coord_fingerprint"] == "Engineering Coordinator · batch-001"
    assert parsed["target"]["issue_number"] == 48
    assert parsed["worktree"]["branch"] == "feat/issue-48-a1b2"
    assert parsed["meta"]["host_role_file"].endswith("python-developer.md")


def test_build_child_task_pr_scope_emits_valid_toml() -> None:
    """_build_child_task() must emit valid TOML for PR-scoped (reviewer) agents."""
    output = _build_child_task(
        run_id="pr-142-def456",
        role="pr-reviewer",
        tier="worker",
        org_domain="qa",
        scope_type="pr",
        scope_value="142",
        gh_repo="owner/repo",
        branch="review/pr-142-c3d4",
        worktree_path="/worktrees/pr-142-def456",
        host_worktree_path="/host/worktrees/pr-142-def456",
        batch_id="pr-142-20260306T120000Z-5678",
        parent_run_id="coord-abc",
        cognitive_arch="hopper:python",
        pr_number=142,
    )
    parsed = tomllib.loads(output)
    assert parsed["task"]["workflow"] == "pr-review"
    assert parsed["agent"]["tier"] == "worker"
    assert parsed["agent"]["org_domain"] == "qa"
    assert parsed["target"]["pr_number"] == 142


def test_build_child_task_label_scope_emits_valid_toml() -> None:
    """_build_child_task() must emit valid TOML for label-scoped (coordinator) agents."""
    output = _build_child_task(
        run_id="coord-ac-workflow-ghi789",
        role="engineering-coordinator",
        tier="coordinator",
        org_domain="engineering",
        scope_type="label",
        scope_value="ac-workflow/1-toml-migration",
        gh_repo="owner/repo",
        branch="agent/ac-workflow-e5f6",
        worktree_path="/worktrees/coord-ac-workflow-ghi789",
        host_worktree_path="/host/worktrees/coord-ac-workflow-ghi789",
        batch_id="label-ac-workflow-20260306T120000Z-9abc",
        parent_run_id="",
        cognitive_arch="von_neumann:python",
    )
    parsed = tomllib.loads(output)
    assert parsed["task"]["workflow"] == "coordinator"
    assert parsed["agent"]["role"] == "engineering-coordinator"
    assert parsed["target"]["scope_type"] == "label"
    assert parsed["target"]["scope_value"] == "ac-workflow/1-toml-migration"
