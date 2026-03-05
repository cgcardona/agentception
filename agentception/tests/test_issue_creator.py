from __future__ import annotations

"""Tests for agentception.readers.issue_creator.

All tests mock the gh subprocess so no real GitHub API calls are made.
The test suite verifies:
  - Correct gh commands are invoked for issue creation and label bootstrap.
  - SSE event sequence (start → label → issue → done).
  - Blocked-by body edits are triggered for issues with depends_on.
  - A gh failure during issue creation yields an error event and halts.
  - A gh failure during body edit is non-fatal (logged, iteration continues).
"""

from collections.abc import AsyncIterator
from typing import TypeGuard
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.models import PlanIssue, PlanPhase, PlanSpec
from agentception.readers.issue_creator import (
    DoneEvent,
    FilingErrorEvent,
    IssueFileEvent,
    IssueEvent,
    LabelEvent,
    StartEvent,
    file_issues,
)


def _is_start(e: IssueFileEvent) -> TypeGuard[StartEvent]:
    return e["t"] == "start"


def _is_label(e: IssueFileEvent) -> TypeGuard[LabelEvent]:
    return e["t"] == "label"


def _is_issue(e: IssueFileEvent) -> TypeGuard[IssueEvent]:
    return e["t"] == "issue"


def _is_done(e: IssueFileEvent) -> TypeGuard[DoneEvent]:
    return e["t"] == "done"


def _is_error(e: IssueFileEvent) -> TypeGuard[FilingErrorEvent]:
    return e["t"] == "error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    initiative: str = "test-initiative",
    *,
    with_depends_on: bool = False,
) -> PlanSpec:
    """Build a minimal two-phase PlanSpec for testing."""
    phase0_issues = [
        PlanIssue(
            id="test-initiative-p0-001",
            title="Setup CI",
            body="Configure CI pipeline.",
        ),
    ]
    phase1_issues = [
        PlanIssue(
            id="test-initiative-p1-001",
            title="Add feature flag",
            body="Wire feature flags.",
            depends_on=["test-initiative-p0-001"] if with_depends_on else [],
        ),
    ]
    return PlanSpec(
        initiative=initiative,
        phases=[
            PlanPhase(
                label="0-foundation",
                description="Foundations",
                depends_on=[],
                issues=phase0_issues,
            ),
            PlanPhase(
                label="1-features",
                description="Features",
                depends_on=["0-foundation"],
                issues=phase1_issues,
            ),
        ],
    )


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Create a fake asyncio subprocess mock."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _issue_url(number: int) -> bytes:
    """Simulate the plain-text URL that gh issue create prints to stdout."""
    return f"https://github.com/test/repo/issues/{number}\n".encode()


async def _collect(gen: AsyncIterator[IssueFileEvent]) -> list[IssueFileEvent]:
    """Drain an async generator into a list."""
    return [event async for event in gen]


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_file_issues_emits_start_event() -> None:
    """The first event must always be 'start' with total and initiative."""
    spec = _make_spec()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(stdout=_issue_url(42)),
        ),
    ):
        events = await _collect(file_issues(spec))

    assert _is_start(events[0])
    start = events[0]
    assert start["total"] == 2
    assert start["initiative"] == "test-initiative"


@pytest.mark.anyio
async def test_file_issues_emits_label_event() -> None:
    """A 'label' event is emitted before any issues are created."""
    spec = _make_spec()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(stdout=_issue_url(42)),
        ),
    ):
        events = await _collect(file_issues(spec))

    label_events = [e for e in events if _is_label(e)]
    assert label_events, "Expected at least one 'label' event"
    label = label_events[0]
    assert isinstance(label["text"], str) and label["text"]


@pytest.mark.anyio
async def test_file_issues_emits_issue_events_for_each_issue() -> None:
    """An 'issue' event is emitted for each created issue."""
    spec = _make_spec()
    call_count = 0

    def fake_proc(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _mock_proc(stdout=_issue_url(100 + call_count))

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        events = await _collect(file_issues(spec))

    issue_events = [e for e in events if _is_issue(e)]
    assert len(issue_events) == 2
    numbers = {e["number"] for e in issue_events}
    assert len(numbers) == 2, "Each issue should get a distinct GitHub number"


@pytest.mark.anyio
async def test_file_issues_emits_done_event_last() -> None:
    """The final event is always 'done' with total and issues list."""
    spec = _make_spec()
    call_count = 0

    def fake_proc(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _mock_proc(stdout=_issue_url(200 + call_count))

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        events = await _collect(file_issues(spec))

    assert _is_done(events[-1])
    done = events[-1]
    assert done["total"] == 2
    assert done["initiative"] == "test-initiative"
    assert len(done["issues"]) == 2
    assert isinstance(done["batch_id"], str) and done["batch_id"]


# ---------------------------------------------------------------------------
# Tests: depends_on / blocked-by editing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_file_issues_edits_body_for_depends_on() -> None:
    """An issue with depends_on gets a gh issue edit call after creation."""
    spec = _make_spec(with_depends_on=True)

    # gh calls: label bootstrap + 2 issue creates + 1 issue edit
    create_count = 0
    edit_calls: list[list[str]] = []

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal create_count
        cmd = list(args)
        if "create" in cmd:
            create_count += 1
            return _mock_proc(stdout=_issue_url(300 + create_count))
        if "edit" in cmd:
            edit_calls.append(cmd)
            return _mock_proc()
        # label create
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        events = await _collect(file_issues(spec))

    assert len(edit_calls) == 1, "Expected exactly one gh issue edit for the dependent issue"
    # The edit call should include --body with "Blocked by:"
    body_arg = next(
        (edit_calls[0][i + 1] for i, a in enumerate(edit_calls[0]) if a == "--body"),
        None,
    )
    assert body_arg is not None and "Blocked by:" in body_arg

    blocked_events = [e for e in events if e["t"] == "blocked"]
    assert len(blocked_events) == 1


@pytest.mark.anyio
async def test_file_issues_no_edit_when_no_depends_on() -> None:
    """No gh issue edit is called when no issue has depends_on."""
    spec = _make_spec(with_depends_on=False)
    edit_calls: list[list[str]] = []
    create_count = 0

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal create_count
        cmd = list(args)
        if "create" in cmd:
            create_count += 1
            return _mock_proc(stdout=_issue_url(400 + create_count))
        if "edit" in cmd:
            edit_calls.append(cmd)
            return _mock_proc()
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        await _collect(file_issues(spec))

    assert edit_calls == [], "No gh issue edit expected when there are no depends_on"


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_file_issues_yields_error_on_label_failure() -> None:
    """A label bootstrap failure yields an 'error' event and stops the stream."""
    spec = _make_spec()

    with patch(
        "agentception.readers.issue_creator.ensure_label_exists",
        side_effect=RuntimeError("rate limited"),
    ):
        events = await _collect(file_issues(spec))

    assert _is_start(events[0])
    assert _is_label(events[1])
    assert _is_error(events[2])
    error = events[2]
    assert "rate limited" in error["detail"]
    # No issue events should have been emitted.
    assert not any(_is_issue(e) for e in events)


@pytest.mark.anyio
async def test_file_issues_yields_error_on_create_failure() -> None:
    """A gh issue create failure yields an 'error' event and stops the stream."""
    spec = _make_spec()

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        if "create" in list(args):
            return _mock_proc(returncode=1, stderr=b"gh: repository not found")
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        events = await _collect(file_issues(spec))

    error_events = [e for e in events if _is_error(e)]
    assert error_events, "Expected an error event after gh issue create failure"
    assert "gh issue create failed" in error_events[0]["detail"]


# ---------------------------------------------------------------------------
# Tests: scoped label format (new canonical scheme)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bootstrap_labels_creates_scoped_phase_labels() -> None:
    """_bootstrap_labels creates '{initiative}/{N}-{slug}' labels, not bare phase labels.
    Also ensures the pipeline-gate labels (pipeline-active, blocked) are created.
    """
    spec = _make_spec(initiative="ac-build")
    created_labels: list[str] = []

    async def fake_ensure(label: str, _color: str, _desc: str) -> None:
        created_labels.append(label)

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", side_effect=fake_ensure),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(stdout=_issue_url(1)),
        ),
    ):
        await _collect(file_issues(spec))

    # Scoped phase labels must be present.
    assert "ac-build/0-foundation" in created_labels
    assert "ac-build/1-features" in created_labels
    # Global (unscoped) phase labels must NOT be created.
    assert "0-foundation" not in created_labels
    assert "1-features" not in created_labels
    # Initiative label itself must be created.
    assert "ac-build" in created_labels
    # Pipeline-gate labels must always be bootstrapped.
    assert "pipeline-active" in created_labels
    assert "blocked" in created_labels


@pytest.mark.anyio
async def test_file_issues_uses_scoped_labels_on_gh_create() -> None:
    """gh issue create is called with [initiative, initiative/phase-N] labels."""
    spec = _make_spec(initiative="ac-workflow")
    create_calls: list[list[str]] = []
    call_count = 0

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        cmd = list(args)
        if "create" in cmd:
            call_count += 1
            create_calls.append(cmd)
            return _mock_proc(stdout=_issue_url(10 + call_count))
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        await _collect(file_issues(spec))

    assert create_calls, "Expected gh issue create calls"
    for idx, cmd in enumerate(create_calls):
        label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
        # Every issue gets the initiative label and a scoped phase label.
        assert "ac-workflow" in label_args, "Initiative label must be present"
        scoped = [lbl for lbl in label_args if lbl.startswith("ac-workflow/")]
        assert scoped, "Scoped phase label must be present"
        # Global phase-N labels must NOT be passed.
        bare_phase = [lbl for lbl in label_args if lbl.startswith("phase-") and "/" not in lbl]
        assert bare_phase == [], f"Unexpected global phase label(s): {bare_phase}"
        # Every issue gets exactly one pipeline-gate label.
        gate = [lbl for lbl in label_args if lbl in ("pipeline-active", "blocked")]
        assert len(gate) == 1, f"Expected exactly one gate label, got {gate}"


@pytest.mark.anyio
async def test_file_issues_phase_gate_labels_by_phase_position() -> None:
    """Phase 0 issues get 'pipeline-active'; phase 1+ issues get 'blocked'."""
    spec = _make_spec(initiative="ac-workflow")
    # Capture (phase_scoped_label, gate_label) per create call.
    phase_gate_pairs: list[tuple[str, str]] = []

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        cmd = list(args)
        if "create" in cmd:
            label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
            scoped = next((lbl for lbl in label_args if lbl.startswith("ac-workflow/")), "")
            gate = next((lbl for lbl in label_args if lbl in ("pipeline-active", "blocked")), "")
            phase_gate_pairs.append((scoped, gate))
            return _mock_proc(stdout=_issue_url(len(phase_gate_pairs)))
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        await _collect(file_issues(spec))

    assert phase_gate_pairs, "No create calls recorded"
    for scoped, gate in phase_gate_pairs:
        if scoped.endswith("/0-foundation"):
            assert gate == "pipeline-active", f"Phase-0 issue got {gate!r} instead of pipeline-active"
        else:
            assert gate == "blocked", f"Non-phase-0 issue got {gate!r} instead of blocked"
