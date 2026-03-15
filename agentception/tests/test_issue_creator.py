from __future__ import annotations

"""Tests for agentception.readers.issue_creator.

All tests mock the gh subprocess so no real GitHub API calls are made.
The test suite verifies:
  - _embed_skills / _embed_cognitive_arch body helpers.
  - Correct gh commands are invoked for issue creation and label bootstrap.
  - SSE event sequence (start → label → issue → done).
  - Blocked-by body edits are triggered for issues with depends_on.
  - A gh failure during issue creation yields an error event and halts.
  - A gh failure during body edit is non-fatal (logged, iteration continues).
  - _gh_create_issue edge cases: empty stdout, malformed URL.
  - DB persistence calls are made with correct data after a successful run.
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
    _scoped_label,
    _embed_cognitive_arch,
    _embed_phase_gate,
    _embed_skills,
    _gh_create_issue,
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
        patch(
            "agentception.readers.issue_creator.enrich_plan_with_codebase_context",
            new_callable=AsyncMock,
            return_value=spec,
        ),
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(stdout=_issue_url(42)),
        ),
        patch("agentception.readers.issue_creator.upsert_issues", new_callable=AsyncMock),
        patch(
            "agentception.readers.issue_creator.persist_initiative_phases",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.readers.issue_creator.persist_issue_depends_on",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.readers.issue_creator.persist_plan_issues",
            new_callable=AsyncMock,
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
    """An issue with depends_on gets a body edit and a blocked/deps label add."""
    spec = _make_spec(with_depends_on=True)

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
        # label create / list / other
        return _mock_proc()

    add_label_mock = AsyncMock()
    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("agentception.readers.issue_creator.add_label_to_issue", add_label_mock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        events = await _collect(file_issues(spec))

    # One body-edit call for the "Blocked by:" line.
    assert len(edit_calls) == 1, "Expected exactly one gh issue edit for the body"
    body_arg = next(
        (edit_calls[0][i + 1] for i, a in enumerate(edit_calls[0]) if a == "--body"),
        None,
    )
    assert body_arg is not None and "Blocked by:" in body_arg

    # Separate add_label_to_issue call stamps blocked/deps on the dep issue.
    add_label_mock.assert_awaited_once()
    call_args = add_label_mock.call_args
    assert call_args is not None
    assert call_args.args[1] == "blocked/deps"

    blocked_events = [e for e in events if e["t"] == "blocked"]
    assert len(blocked_events) == 1


@pytest.mark.anyio
async def test_file_issues_still_yields_blocked_event_when_label_stamp_fails() -> None:
    """BlockedEvent is still emitted even when add_label_to_issue raises.

    Regression: the old shared try/except swallowed both the label failure and
    the BlockedEvent.  After the fix, body edit and label stamp are independent:
    a label failure only loses the label (poller re-stamps it), not the event.
    """
    spec = _make_spec(with_depends_on=True)

    create_count = 0

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal create_count
        cmd = list(args)
        if "create" in cmd:
            create_count += 1
            return _mock_proc(stdout=_issue_url(500 + create_count))
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch(
            "agentception.readers.issue_creator.add_label_to_issue",
            side_effect=RuntimeError("label API down"),
        ),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        events = await _collect(file_issues(spec))

    # BlockedEvent must still be yielded even though label stamp failed.
    blocked_events = [e for e in events if e["t"] == "blocked"]
    assert len(blocked_events) == 1, (
        "BlockedEvent must be emitted regardless of label-stamp failure"
    )


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
    Also ensures the pipeline-gate labels (pipeline/active, pipeline/gated, blocked/deps) are created.
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
    assert "pipeline/active" in created_labels
    assert "pipeline/gated" in created_labels
    assert "blocked/deps" in created_labels


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
        gate = [lbl for lbl in label_args if lbl in ("pipeline/active", "pipeline/gated")]
        assert len(gate) == 1, f"Expected exactly one gate label, got {gate}"


@pytest.mark.anyio
async def test_file_issues_phase_gate_labels_by_phase_position() -> None:
    """Phase 0 issues get 'pipeline/active'; phase 1+ issues get 'pipeline/gated'."""
    spec = _make_spec(initiative="ac-workflow")
    # Capture (phase_scoped_label, gate_label) per create call.
    phase_gate_pairs: list[tuple[str, str]] = []

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        cmd = list(args)
        if "create" in cmd:
            label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
            scoped = next((lbl for lbl in label_args if lbl.startswith("ac-workflow/")), "")
            gate = next((lbl for lbl in label_args if lbl in ("pipeline/active", "pipeline/gated")), "")
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
            assert gate == "pipeline/active", f"Phase-0 issue got {gate!r} instead of pipeline/active"
        else:
            assert gate == "pipeline/gated", f"Non-phase-0 issue got {gate!r} instead of pipeline/gated"


def test_embed_phase_gate_appends_blocking_notice() -> None:
    """Phase-gate footer names the blocking phase so agents know what to wait for."""
    body = _embed_phase_gate("Implement the widget.", "ac-build/0-foundation")
    assert "ac-build/0-foundation" in body
    assert "Phase gate" in body
    assert body.startswith("Implement the widget.")


@pytest.mark.anyio
async def test_file_issues_phase1_body_contains_phase_gate_notice() -> None:
    """Phase 1+ issue bodies include a phase-gate notice naming the prior phase."""
    spec = _make_spec(initiative="ac-workflow")
    body_calls: list[tuple[str, str]] = []  # (scoped_phase_label, body_text)

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        cmd = list(args)
        if "create" in cmd:
            label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
            scoped = next((lbl for lbl in label_args if lbl.startswith("ac-workflow/")), "")
            body_idx = cmd.index("--body") + 1 if "--body" in cmd else -1
            body_text = cmd[body_idx] if body_idx >= 0 else ""
            body_calls.append((scoped, body_text))
            return _mock_proc(stdout=_issue_url(len(body_calls)))
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
    ):
        await _collect(file_issues(spec))

    assert body_calls, "No create calls recorded"
    for scoped, body in body_calls:
        if scoped.endswith("/0-foundation"):
            assert "Phase gate" not in body, "Phase-0 issues must not have a gate notice"
        else:
            assert "Phase gate" in body, f"Phase 1+ issue body is missing gate notice: {body!r}"
            assert "ac-workflow/0-foundation" in body, (
                f"Phase 1+ body should name the blocking phase: {body!r}"
            )


# ---------------------------------------------------------------------------
# Unit tests: body-embed helpers
# ---------------------------------------------------------------------------


def test_embed_skills_appends_html_comment() -> None:
    """Skills are embedded as a machine-readable HTML comment invisible in the GitHub UI."""
    body = _embed_skills("## Context\nDo the thing.", ["python", "fastapi"])
    assert "<!-- ac:skills: fastapi, python -->" in body or "<!-- ac:skills: python, fastapi -->" in body
    assert body.startswith("## Context")


def test_embed_skills_empty_list_returns_body_unchanged() -> None:
    """Empty skills list → body is returned unchanged."""
    original = "## Context\nDo the thing."
    assert _embed_skills(original, []) == original


def test_embed_skills_single_skill() -> None:
    """A single skill is embedded without a trailing comma."""
    body = _embed_skills("Body.", ["python"])
    assert "<!-- ac:skills: python -->" in body


def test_embed_skills_preserves_original_body_content() -> None:
    """The original body text is preserved before the injected comment."""
    original = "## Context\nImplement auth.\n\n## Acceptance\n- [ ] Tests pass."
    result = _embed_skills(original, ["python"])
    assert original in result


def test_embed_cognitive_arch_appends_html_comment() -> None:
    """Cognitive arch string is embedded as a machine-readable HTML comment."""
    body = _embed_cognitive_arch("## Context\nDo the thing.", "barbara_liskov:fastapi:python")
    assert "<!-- ac:cognitive_arch: barbara_liskov:fastapi:python -->" in body
    assert body.startswith("## Context")


def test_embed_cognitive_arch_empty_string_returns_body_unchanged() -> None:
    """Empty cognitive_arch → body is returned unchanged."""
    original = "## Context\nDo the thing."
    assert _embed_cognitive_arch(original, "") == original


def test_embed_cognitive_arch_preserves_original_body_content() -> None:
    """The original body text is fully preserved when the arch comment is appended."""
    original = "## Context\nBuild the user model.\n\n## Notes\n- Use SQLAlchemy."
    result = _embed_cognitive_arch(original, "jeff_dean:python")
    assert original in result


# ---------------------------------------------------------------------------
# Unit tests: _gh_create_issue edge cases
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gh_create_issue_raises_on_empty_stdout() -> None:
    """gh issue create returning empty stdout → RuntimeError."""
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b""),
    ):
        with pytest.raises(RuntimeError, match="empty output"):
            await _gh_create_issue("owner/repo", "Title", "Body", [])


@pytest.mark.anyio
async def test_gh_create_issue_raises_on_nonzero_exit() -> None:
    """gh issue create non-zero exit code → RuntimeError with stderr details."""
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_mock_proc(returncode=1, stderr=b"repository not found"),
    ):
        with pytest.raises(RuntimeError, match="gh issue create failed"):
            await _gh_create_issue("owner/repo", "Title", "Body", [])


@pytest.mark.anyio
async def test_gh_create_issue_raises_on_malformed_url() -> None:
    """gh issue create returning a non-numeric suffix → RuntimeError."""
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"https://github.com/owner/repo/issues/abc\n"),
    ):
        with pytest.raises(RuntimeError, match="Could not parse issue number"):
            await _gh_create_issue("owner/repo", "Title", "Body", [])


@pytest.mark.anyio
async def test_gh_create_issue_returns_number_and_url() -> None:
    """gh issue create returning a valid URL → (number, url) tuple."""
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"https://github.com/owner/repo/issues/99\n"),
    ):
        number, url = await _gh_create_issue("owner/repo", "Title", "Body", ["label-a"])

    assert number == 99
    assert url == "https://github.com/owner/repo/issues/99"


# ---------------------------------------------------------------------------
# Integration: DB persistence is called after a successful run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_file_issues_calls_persist_initiative_phases() -> None:
    """persist_initiative_phases is called once with the correct phase data."""
    spec = _make_spec(initiative="ac-build")
    call_count = 0

    def fake_proc(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _mock_proc(stdout=_issue_url(700 + call_count))

    persist_mock = AsyncMock()
    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
        patch("agentception.readers.issue_creator.persist_initiative_phases", persist_mock),
        patch(
            "agentception.readers.issue_creator.persist_issue_depends_on",
            new_callable=AsyncMock,
        ),
    ):
        await _collect(file_issues(spec))

    persist_mock.assert_awaited_once()
    call_kwargs = persist_mock.call_args
    assert call_kwargs is not None
    # Verify the repo, initiative, batch_id kwargs and that both phases are present.
    repo_arg: str = call_kwargs.kwargs.get("repo") or call_kwargs.args[0]
    assert "/" in repo_arg, "repo must be org/repo format"
    initiative_arg: str = call_kwargs.kwargs.get("initiative") or call_kwargs.args[1]
    assert initiative_arg == "ac-build"
    batch_id_arg: str = call_kwargs.kwargs.get("batch_id") or call_kwargs.args[2]
    assert batch_id_arg.startswith("batch-")
    phases_arg: list[object] = (
        call_kwargs.kwargs.get("phases") or call_kwargs.args[3]
    )
    assert len(phases_arg) == 2


@pytest.mark.anyio
async def test_file_issues_calls_persist_issue_depends_on_for_deps() -> None:
    """persist_issue_depends_on is called with the correct blocker map when depends_on is set."""
    spec = _make_spec(with_depends_on=True)
    create_count = 0

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal create_count
        cmd = list(args)
        if "create" in cmd:
            create_count += 1
            return _mock_proc(stdout=_issue_url(800 + create_count))
        return _mock_proc()

    persist_deps_mock = AsyncMock()
    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("agentception.readers.issue_creator.add_label_to_issue", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
        patch(
            "agentception.readers.issue_creator.persist_initiative_phases",
            new_callable=AsyncMock,
        ),
        patch("agentception.readers.issue_creator.persist_issue_depends_on", persist_deps_mock),
    ):
        await _collect(file_issues(spec))

    persist_deps_mock.assert_awaited_once()
    # The deps dict must map issue numbers → blocker numbers.
    call_args = persist_deps_mock.call_args
    assert call_args is not None
    deps_arg: dict[int, list[int]] = call_args.args[1]
    # There must be exactly one blocked issue with one blocker.
    assert len(deps_arg) == 1
    blockers = next(iter(deps_arg.values()))
    assert len(blockers) == 1


@pytest.mark.anyio
async def test_file_issues_body_edit_failure_is_non_fatal() -> None:
    """When _gh_edit_body fails (non-zero exit) iteration continues and done is still emitted."""
    spec = _make_spec(with_depends_on=True)
    create_count = 0

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal create_count
        cmd = list(args)
        if "create" in cmd:
            create_count += 1
            return _mock_proc(stdout=_issue_url(900 + create_count))
        if "edit" in cmd:
            # Simulate body edit failure.
            return _mock_proc(returncode=1, stderr=b"gh: body edit failed")
        return _mock_proc()

    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("agentception.readers.issue_creator.add_label_to_issue", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
        patch(
            "agentception.readers.issue_creator.persist_initiative_phases",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.readers.issue_creator.persist_issue_depends_on",
            new_callable=AsyncMock,
        ),
    ):
        events = await _collect(file_issues(spec))

    # Body edit failure must not abort the stream — done event must still arrive.
    done_events = [e for e in events if e["t"] == "done"]
    assert done_events, "done event must be emitted even when body edit fails"


@pytest.mark.anyio
async def test_file_issues_immediately_upserts_created_issues() -> None:
    """upsert_issues is called before the done event so the Ship board is
    pre-seeded without waiting for the next poller tick."""
    spec = _make_spec()
    call_count = 0

    def fake_proc(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _mock_proc(stdout=_issue_url(1000 + call_count))

    upsert_mock = AsyncMock(return_value=2)
    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
        patch("agentception.readers.issue_creator.upsert_issues", upsert_mock),
        patch(
            "agentception.readers.issue_creator.persist_initiative_phases",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.readers.issue_creator.persist_issue_depends_on",
            new_callable=AsyncMock,
        ),
    ):
        events = await _collect(file_issues(spec))

    # upsert_issues must have been called exactly once, with the two created issues.
    upsert_mock.assert_awaited_once()
    call_kwargs = upsert_mock.call_args
    assert call_kwargs is not None
    issues_arg: list[object] = call_kwargs.kwargs.get("issues") or call_kwargs.args[0]
    assert len(issues_arg) == 2, "both created issues must be pre-seeded"
    for raw in issues_arg:
        assert isinstance(raw, dict)
        assert isinstance(raw.get("number"), int)
        assert raw.get("state") == "open"
        labels = raw.get("labels")
        assert isinstance(labels, list) and labels  # at least initiative + phase labels

    # Done event must still arrive.
    done_events = [e for e in events if e["t"] == "done"]
    assert done_events, "done event must be emitted after upsert"


@pytest.mark.anyio
async def test_file_issues_upsert_includes_blocked_deps_label() -> None:
    """When an issue has depends_on and blocked/deps is stamped on GitHub,
    the immediate upsert must include 'blocked/deps' in its label list."""
    spec = _make_spec(with_depends_on=True)
    create_count = 0

    def fake_proc(*args: str, **_kwargs: object) -> MagicMock:
        nonlocal create_count
        cmd = list(args)
        if "create" in cmd:
            create_count += 1
            return _mock_proc(stdout=_issue_url(1100 + create_count))
        if "edit" in cmd:
            return _mock_proc()
        return _mock_proc()

    upsert_mock = AsyncMock(return_value=2)
    with (
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
        patch("agentception.readers.issue_creator.add_label_to_issue", new_callable=AsyncMock),
        patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
        patch("agentception.readers.issue_creator.upsert_issues", upsert_mock),
        patch(
            "agentception.readers.issue_creator.persist_initiative_phases",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.readers.issue_creator.persist_issue_depends_on",
            new_callable=AsyncMock,
        ),
    ):
        await _collect(file_issues(spec))

    upsert_mock.assert_awaited_once()
    call_kwargs = upsert_mock.call_args
    assert call_kwargs is not None
    issues_arg: list[object] = call_kwargs.kwargs.get("issues") or call_kwargs.args[0]
    # The blocked issue must include 'blocked/deps' in its label list.
    blocked_entries = [
        raw for raw in issues_arg
        if isinstance(raw, dict) and "blocked/deps" in (raw.get("labels") or [])
    ]
    assert blocked_entries, "blocked issue must carry 'blocked/deps' in the upserted label list"


# ── _scoped_label truncation ──────────────────────────────────────────────────


def test_scoped_label_short_names_unchanged() -> None:
    """Short initiative+phase combinations are returned verbatim."""
    assert _scoped_label("ac-build", "phase-0") == "ac-build/phase-0"


def test_scoped_label_truncates_to_50_chars() -> None:
    """Labels longer than 50 characters are truncated to exactly 50.

    Regression test: GitHub rejects label names > 50 chars with 422.
    e.g. 'context-window-management/2-checkpoint-summarisation' is 52 chars.
    """
    result = _scoped_label("context-window-management", "2-checkpoint-summarisation")
    assert len(result) == 50
    assert result == "context-window-management/2-checkpoint-summarisati"


def test_scoped_label_always_contains_separator() -> None:
    """The '/' separator is always present in the returned label."""
    result = _scoped_label("a" * 30, "b" * 30)
    assert "/" in result
    assert len(result) == 50
