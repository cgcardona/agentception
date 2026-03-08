from __future__ import annotations

"""E2E tests: Plan v2 pipeline — steps 1 and 5 (issue #46).

AgentCeption has zero LLM calls in the plan pipeline.

1. AgentCeption writes .agent-task (TOML with workflow="plan-spec") — POST /api/plan/draft
2. Cursor's agent picks it up (Cursor's concern — not tested here)
3. Cursor calls plan_get_schema() MCP tool to get the PlanSpec format
4. Cursor writes YAML to output path
5. AgentCeption poller detects output path, emits plan_draft_ready SSE

This test file covers steps 1 and 5.  Steps 2–4 are Cursor's responsibility.

SLO guarded: 100% of ``plan_draft_ready`` SSE events must carry the correct
``draft_id`` and ``yaml_text`` that match what POST /api/plan/draft created.
Any drift between the API and the poller represents a silent data loss for the
user who submitted a plan.

Run targeted:
    pytest agentception/tests/test_plan_draft_poller.py -v
"""

import asyncio
import time
import tomllib
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import agentception.poller as poller_module
from agentception.app import app
from agentception.models import PipelineState, PlanDraftEvent
from agentception.poller import (
    GitHubBoard,
    broadcast,
    scan_plan_draft_worktrees,
    subscribe,
    tick,
    unsubscribe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc_mock(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock that behaves like an asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


def _empty_board() -> GitHubBoard:
    """Return a GitHubBoard with no issues, PRs, or WIP issues."""
    return GitHubBoard(
        active_label=None,
        open_issues=[],
        open_prs=[],
        wip_issues=[],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Async httpx client wrapping the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture()
def reset_plan_draft_tracking() -> Generator[None, None, None]:
    """Clear module-level deduplication sets before and after each test.

    Without this, a draft_id emitted in one test persists across tests and
    produces false negatives (the second test sees the draft_id already in
    _emitted_ready_drafts and skips it).  This is an SRE concern: test isolation
    is a reliability invariant — flaky tests are unreliable alert paths.
    """
    poller_module._emitted_ready_drafts.clear()
    poller_module._emitted_timeout_drafts.clear()
    yield
    poller_module._emitted_ready_drafts.clear()
    poller_module._emitted_timeout_drafts.clear()


# ---------------------------------------------------------------------------
# E2E: POST /api/plan/draft → Cursor writes OUTPUT_PATH → poller emits SSE
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_poller_detects_output_path_and_emits_sse_event(
    async_client: AsyncClient,
    tmp_path: Path,
    reset_plan_draft_tracking: None,
) -> None:
    """E2E: POST /api/plan/draft → Cursor writes OUTPUT_PATH → plan_draft_ready SSE.

    Covers steps 1 and 5 of the Plan v2 pipeline:

    # AgentCeption has zero LLM calls in the plan pipeline.
    # 1. AgentCeption writes .agent-task (WORKFLOW=plan-spec) — POST /api/plan/draft
    # 2. Cursor's agent picks it up (Cursor's concern — not tested here)
    # 3. Cursor calls plan_get_schema() MCP tool to get the PlanSpec format
    # 4. Cursor writes YAML to OUTPUT_PATH
    # 5. AgentCeption poller detects OUTPUT_PATH, emits plan_draft_ready SSE
    # This test covers steps 1, 5. Steps 2–4 are Cursor's responsibility.

    SLO: Every plan_draft_ready event must carry the exact draft_id and yaml_text
    that were established by POST /api/plan/draft.  A mismatch here means the
    Plan UI would display the wrong draft or lose the YAML entirely.
    """
    proc_mock = _make_proc_mock(returncode=0)

    # ── Step 1: POST /api/plan/draft ─────────────────────────────────────────
    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ),
        patch(
            "agentception.routes.api.plan.settings.worktrees_dir",
            tmp_path,
        ),
        patch(
            "agentception.routes.api.plan.settings.host_worktrees_dir",
            tmp_path,
        ),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": "Build a song about the ocean with gentle waves"},
        )

    assert response.status_code == 200
    body = response.json()
    draft_id: str = body["draft_id"]
    output_path: str = body["output_path"]

    # Confirm the .agent-task was written to the worktree directory.
    task_file = tmp_path / f"plan-draft-{draft_id}" / ".agent-task"
    assert task_file.exists(), ".agent-task not written by POST /api/plan/draft"
    task_content = task_file.read_text(encoding="utf-8")
    assert f'draft_id = "{draft_id}"' in task_content
    assert output_path in task_content
    assert 'workflow = "plan-spec"' in task_content

    # ── Step 4 (simulated): Cursor writes YAML to OUTPUT_PATH ────────────────
    yaml_content = "initiative: ocean-song\nphases: []\n"
    Path(output_path).write_text(yaml_content, encoding="utf-8")

    # ── Step 5: Poller scans and detects OUTPUT_PATH ──────────────────────────
    with patch("agentception.poller.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        events = await scan_plan_draft_worktrees()

    # Exactly one plan_draft_ready event must be emitted.
    assert len(events) == 1, f"Expected 1 event, got {len(events)}: {events}"
    ev = events[0]
    assert ev.event == "plan_draft_ready"
    assert ev.draft_id == draft_id, (
        f"draft_id mismatch: poller got {ev.draft_id!r}, "
        f"but POST /api/plan/draft returned {draft_id!r}"
    )
    assert ev.yaml_text == yaml_content, (
        f"yaml_text mismatch: poller got {ev.yaml_text!r}, "
        f"but Cursor wrote {yaml_content!r}"
    )
    assert ev.output_path == output_path


@pytest.mark.anyio
async def test_plan_draft_ready_event_not_reemitted_across_ticks(
    async_client: AsyncClient,
    tmp_path: Path,
    reset_plan_draft_tracking: None,
) -> None:
    """plan_draft_ready must not be emitted again on the second poller tick.

    SLO: Duplicate SSE events cause duplicate UI toasts/modals.  The deduplication
    guarantee is part of the reliability contract — one event per draft.
    """
    proc_mock = _make_proc_mock(returncode=0)

    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ),
        patch("agentception.routes.api.plan.settings.worktrees_dir", tmp_path),
        patch("agentception.routes.api.plan.settings.host_worktrees_dir", tmp_path),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": "A rainy afternoon playlist"},
        )

    assert response.status_code == 200
    body = response.json()
    output_path: str = body["output_path"]
    Path(output_path).write_text("initiative: rainy\nphases: []\n", encoding="utf-8")

    with patch("agentception.poller.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        first_events = await scan_plan_draft_worktrees()
        second_events = await scan_plan_draft_worktrees()

    assert len(first_events) == 1
    assert first_events[0].event == "plan_draft_ready"
    # Second tick must be empty — the dedup set prevents re-emission.
    assert second_events == [], (
        f"Expected no events on second tick, got: {second_events}"
    )


@pytest.mark.anyio
async def test_plan_draft_ready_event_reaches_sse_subscriber(
    reset_plan_draft_tracking: None,
) -> None:
    """A plan_draft_ready event in PipelineState reaches every SSE subscriber.

    SLO: Zero dropped plan_draft_ready events to connected clients.  If broadcast()
    fails to deliver to a subscriber, the UI misses the plan-ready signal and the
    user must manually refresh to see their plan.
    """
    q = subscribe()
    try:
        plan_event = PlanDraftEvent(
            event="plan_draft_ready",
            draft_id="e2e-test-draft-id",
            yaml_text="initiative: test\nphases: []\n",
            output_path="/tmp/test/.plan-output.yaml",
        )
        state = PipelineState(
            active_label=None,
            issues_open=0,
            prs_open=0,
            agents=[],
            alerts=[],
            polled_at=time.time(),
            plan_draft_events=[plan_event],
        )
        await broadcast(state)
        received = await asyncio.wait_for(q.get(), timeout=1.0)

        assert received is state
        assert len(received.plan_draft_events) == 1
        ev = received.plan_draft_events[0]
        assert ev.event == "plan_draft_ready"
        assert ev.draft_id == "e2e-test-draft-id"
        assert ev.yaml_text == "initiative: test\nphases: []\n"
    finally:
        unsubscribe(q)


@pytest.mark.anyio
async def test_poller_tick_broadcasts_plan_draft_ready_to_sse_subscriber(
    async_client: AsyncClient,
    tmp_path: Path,
    reset_plan_draft_tracking: None,
) -> None:
    """Full E2E: tick() broadcasts plan_draft_ready to SSE subscriber after file appears.

    Covers steps 1 and 5 end-to-end through the complete tick() pipeline:

    # AgentCeption has zero LLM calls in the plan pipeline.
    # 1. AgentCeption writes .agent-task (WORKFLOW=plan-spec) — POST /api/plan/draft
    # 2. Cursor's agent picks it up (Cursor's concern — not tested here)
    # 3. Cursor calls plan_get_schema() MCP tool to get the PlanSpec format
    # 4. Cursor writes YAML to OUTPUT_PATH
    # 5. AgentCeption poller detects OUTPUT_PATH, emits plan_draft_ready SSE
    # This test covers steps 1, 5. Steps 2–4 are Cursor's responsibility.

    SLO: plan_draft_ready events must propagate through the tick/broadcast
    pipeline to all active SSE subscribers within one polling cycle.
    """
    proc_mock = _make_proc_mock(returncode=0)

    # Step 1: POST /api/plan/draft
    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ),
        patch("agentception.routes.api.plan.settings.worktrees_dir", tmp_path),
        patch("agentception.routes.api.plan.settings.host_worktrees_dir", tmp_path),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": "A lo-fi hip-hop beat for study sessions"},
        )

    assert response.status_code == 200
    body = response.json()
    draft_id: str = body["draft_id"]
    output_path: str = body["output_path"]

    # Step 4 (simulated): Cursor writes YAML to OUTPUT_PATH
    yaml_content = "initiative: lofi-study\nphases: []\n"
    Path(output_path).write_text(yaml_content, encoding="utf-8")

    # Subscribe to SSE before tick fires
    q = subscribe()
    try:
        board = _empty_board()
        with (
            patch("agentception.poller.list_active_worktrees", new_callable=AsyncMock, return_value=[]),
            patch("agentception.poller.build_github_board", new_callable=AsyncMock, return_value=board),
            patch("agentception.poller.detect_out_of_order_prs", new_callable=AsyncMock, return_value=[]),
            patch("agentception.poller.settings") as mock_settings,
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.gh_repo = "test/repo"
            mock_settings.poll_interval_seconds = 5
            # Step 5: tick() runs scan_plan_draft_worktrees + broadcasts
            state = await tick()

        # The state returned by tick() must include the plan_draft_ready event
        assert any(
            ev.event == "plan_draft_ready" and ev.draft_id == draft_id
            for ev in state.plan_draft_events
        ), (
            f"Expected plan_draft_ready for draft {draft_id!r} in tick() state, "
            f"got: {state.plan_draft_events}"
        )
        # The SSE subscriber must have received the broadcast
        received = await asyncio.wait_for(q.get(), timeout=1.0)
        assert any(
            ev.event == "plan_draft_ready" and ev.draft_id == draft_id
            for ev in received.plan_draft_events
        ), (
            f"SSE subscriber did not receive plan_draft_ready for draft {draft_id!r}"
        )
        assert any(ev.yaml_text == yaml_content for ev in received.plan_draft_events)
    finally:
        unsubscribe(q)


@pytest.mark.anyio
async def test_plan_draft_agent_task_structure_is_poller_compatible(
    async_client: AsyncClient,
    tmp_path: Path,
) -> None:
    """The .agent-task written by POST /api/plan/draft is parseable by scan_plan_draft_worktrees.

    This is a structural contract test: it verifies that the TOML format
    used by the route and the parser in the poller are always in sync.  A field
    name mismatch (e.g., missing output.draft_id) would cause the poller to
    silently skip every plan-draft worktree, resulting in no plan_draft_ready
    events ever being emitted — a silent failure that would only surface via
    user complaint.
    """
    proc_mock = _make_proc_mock(returncode=0)

    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ),
        patch("agentception.routes.api.plan.settings.worktrees_dir", tmp_path),
        patch("agentception.routes.api.plan.settings.host_worktrees_dir", tmp_path),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": "Structure verification test"},
        )

    assert response.status_code == 200
    body = response.json()
    draft_id: str = body["draft_id"]

    # Parse the .agent-task using the same TOML logic as scan_plan_draft_worktrees
    task_file = tmp_path / f"plan-draft-{draft_id}" / ".agent-task"
    content = task_file.read_text(encoding="utf-8")

    data = tomllib.loads(content)
    output_sec = data.get("output", {})
    assert isinstance(output_sec, dict)
    draft_id_in_file = output_sec.get("draft_id", "")
    output_path_in_file = output_sec.get("path", "")

    assert draft_id_in_file == draft_id, (
        f"output.draft_id in .agent-task ({draft_id_in_file!r}) "
        f"does not match API response draft_id ({draft_id!r})"
    )
    assert isinstance(output_path_in_file, str) and output_path_in_file.endswith(
        ".plan-output.yaml"
    ), (
        f"output.path must end with .plan-output.yaml, got: {output_path_in_file!r}"
    )
    assert f"plan-draft-{draft_id}" in output_path_in_file, (
        f"output.path must contain the plan-draft slug, got: {output_path_in_file!r}"
    )
