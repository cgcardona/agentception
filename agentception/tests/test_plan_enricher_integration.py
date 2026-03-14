from __future__ import annotations

"""Integration tests for enrich_plan_with_codebase_context wired into file_issues.

Verifies two behaviours:
1. When enrichment succeeds, the enriched issue bodies (containing
   '## Relevant codebase locations') are passed to the GitHub issue-creation
   function.
2. When enrichment raises, file_issues continues without re-raising — enrichment
   is best-effort and must never block issue filing.
"""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.models import PlanIssue, PlanPhase, PlanSpec
from agentception.readers.issue_creator import IssueFileEvent, file_issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_spec(initiative: str = "test-enrich") -> PlanSpec:
    """Return a single-phase, single-issue PlanSpec for testing."""
    return PlanSpec(
        initiative=initiative,
        phases=[
            PlanPhase(
                label="0-foundation",
                description="Only phase",
                depends_on=[],
                issues=[
                    PlanIssue(
                        id=f"{initiative}-p0-001",
                        title="Wire enrichment",
                        body="Original body without codebase context.",
                    ),
                ],
            )
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_filed_issue_body_contains_codebase_locations() -> None:
    """Enriched issue bodies (with '## Relevant codebase locations') reach GitHub.

    Patches enrich_plan_with_codebase_context to return a PlanSpec whose issue
    body contains the codebase-locations heading, then asserts that the body
    passed to gh issue create contains that heading.
    """
    spec = _make_minimal_spec()

    # Build an enriched copy of the spec with the expected heading in the body.
    # _make_minimal_spec() already uses the correct label format ("0-foundation").
    enriched_spec = _make_minimal_spec()
    enriched_spec.phases[0].issues[0].body = (
        "Original body without codebase context."
        "\n\n## Relevant codebase locations\n"
        "- agentception/readers/issue_creator.py lines 1-10 — file_issues"
    )

    captured_bodies: list[str] = []

    def fake_proc(*args: object, **kwargs: object) -> MagicMock:
        # Capture the --body argument from the gh CLI command.
        arg_list = list(args)
        if "--body" in arg_list:
            body_idx = arg_list.index("--body")
            captured_bodies.append(str(arg_list[body_idx + 1]))
        return _mock_proc(stdout=_issue_url(42))

    with (
        patch(
            "agentception.readers.issue_creator.enrich_plan_with_codebase_context",
            new_callable=AsyncMock,
            return_value=enriched_spec,
        ),
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
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

    # At least one issue event must have been emitted (filing succeeded).
    issue_events = [e for e in events if e["t"] == "issue"]
    assert len(issue_events) >= 1, "Expected at least one 'issue' event"

    # The body passed to gh must contain the enrichment heading.
    assert captured_bodies, "No body was captured — gh issue create was not called"
    assert any(
        "## Relevant codebase locations" in body for body in captured_bodies
    ), f"Enrichment heading not found in captured bodies: {captured_bodies}"


@pytest.mark.anyio
async def test_enrichment_failure_does_not_block_filing() -> None:
    """A RuntimeError from enrich_plan_with_codebase_context must not stop filing.

    Patches enrich_plan_with_codebase_context to raise RuntimeError("boom"),
    then asserts that file_issues still yields an 'issue' event and does not
    re-raise the exception.
    """
    spec = _make_minimal_spec()

    gh_called = False

    def fake_proc(*args: object, **kwargs: object) -> MagicMock:
        nonlocal gh_called
        gh_called = True
        return _mock_proc(stdout=_issue_url(99))

    with (
        patch(
            "agentception.readers.issue_creator.enrich_plan_with_codebase_context",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        patch("agentception.readers.issue_creator.ensure_label_exists", new_callable=AsyncMock),
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
        # Must not raise — enrichment failure is swallowed.
        events = await _collect(file_issues(spec))

    # Filing must have proceeded: at least one 'issue' event expected.
    issue_events = [e for e in events if e["t"] == "issue"]
    assert len(issue_events) >= 1, (
        "Expected at least one 'issue' event even when enrichment fails; "
        f"got events: {[e['t'] for e in events]}"
    )
    assert gh_called, "gh issue create was never called — filing was blocked by enrichment failure"
