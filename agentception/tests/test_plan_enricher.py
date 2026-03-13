from __future__ import annotations

"""Unit tests for agentception.readers.plan_enricher."""

from unittest.mock import AsyncMock, patch

import pytest

from agentception.models import PlanIssue, PlanPhase, PlanSpec
from agentception.readers.plan_enricher import enrich_plan_with_codebase_context
from agentception.services.code_indexer import SearchMatch


def _make_spec(body: str = "Do the thing.") -> PlanSpec:
    return PlanSpec(
        initiative="test-plan",
        phases=[
            PlanPhase(
                label="0-foundation",
                description="Foundation phase.",
                issues=[
                    PlanIssue(id="issue-1", title="Add foo", body=body),
                ],
            )
        ],
    )


def _make_match(file: str = "agentception/foo.py", chunk: str = "# def my_func\nx = 1") -> SearchMatch:
    return SearchMatch(
        file=file,
        chunk=chunk,
        score=0.9,
        start_line=10,
        end_line=20,
    )


@pytest.mark.anyio
async def test_enrich_appends_locations_section() -> None:
    """When search returns results, body contains '## Relevant codebase locations'."""
    spec = _make_spec()
    matches = [_make_match(), _make_match(file="agentception/bar.py", chunk="# class MyClass\n")]

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(return_value=matches),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    body = result.phases[0].issues[0].body
    assert "## Relevant codebase locations" in body
    assert "agentception/foo.py" in body
    assert "agentception/bar.py" in body


@pytest.mark.anyio
async def test_enrich_empty_results_leaves_body_unchanged() -> None:
    """When search returns [], body is identical to the original."""
    original_body = "Do the thing."
    spec = _make_spec(original_body)

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(return_value=[]),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    assert result.phases[0].issues[0].body == original_body


@pytest.mark.anyio
async def test_enrich_search_raises_leaves_body_unchanged() -> None:
    """When search raises, body is unchanged and no exception propagates."""
    original_body = "Do the thing."
    spec = _make_spec(original_body)

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(side_effect=RuntimeError("qdrant unavailable")),
    ):
        result = await enrich_plan_with_codebase_context(spec)  # must not raise

    assert result.phases[0].issues[0].body == original_body


@pytest.mark.anyio
async def test_enrich_extracts_symbol_name_from_chunk() -> None:
    """Label ends with '\u2014 my_func' when chunk starts with '# def my_func'."""
    spec = _make_spec()
    match = _make_match(chunk="# def my_func\nx = 1")

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(return_value=[match]),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    body = result.phases[0].issues[0].body
    assert "— my_func" in body


@pytest.mark.anyio
async def test_contention_adds_depends_on() -> None:
    """Two issues in same phase sharing a file → second (lex) gains depends_on entry."""
    shared_file = "agentception/shared.py"
    spec = PlanSpec(
        initiative="test",
        phases=[
            PlanPhase(
                label="0-phase",
                description="phase",
                issues=[
                    PlanIssue(id="issue-a", title="A", body="body a"),
                    PlanIssue(id="issue-b", title="B", body="body b"),
                ],
            )
        ],
    )

    def side_effect(title: str, n_results: int = 5) -> list[SearchMatch]:
        return [_make_match(file=shared_file)]

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(side_effect=side_effect),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    issues = result.phases[0].issues
    issue_b = next(i for i in issues if i.id == "issue-b")
    assert "issue-a" in issue_b.depends_on


@pytest.mark.anyio
async def test_no_contention_leaves_depends_on_unchanged() -> None:
    """Two issues with disjoint file sets → depends_on lists unchanged."""
    spec = PlanSpec(
        initiative="test",
        phases=[
            PlanPhase(
                label="0-phase",
                description="phase",
                issues=[
                    PlanIssue(id="issue-a", title="A", body="body a"),
                    PlanIssue(id="issue-b", title="B", body="body b"),
                ],
            )
        ],
    )

    call_count = 0

    def side_effect(title: str, n_results: int = 5) -> list[SearchMatch]:
        nonlocal call_count
        call_count += 1
        file = "agentception/a.py" if call_count % 2 == 1 else "agentception/b.py"
        return [_make_match(file=file)]

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(side_effect=side_effect),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    for issue in result.phases[0].issues:
        assert issue.depends_on == []


@pytest.mark.anyio
async def test_cross_phase_contention_ignored() -> None:
    """Same file in different phases → no cross-phase depends_on injected."""
    shared_file = "agentception/shared.py"
    spec = PlanSpec(
        initiative="test",
        phases=[
            PlanPhase(
                label="0-phase",
                description="phase 0",
                issues=[PlanIssue(id="issue-a", title="A", body="body a")],
            ),
            PlanPhase(
                label="1-phase",
                description="phase 1",
                issues=[PlanIssue(id="issue-b", title="B", body="body b")],
            ),
        ],
    )

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(return_value=[_make_match(file=shared_file)]),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    for phase in result.phases:
        for issue in phase.issues:
            assert issue.depends_on == []


@pytest.mark.anyio
async def test_contention_deduplicates_depends_on() -> None:
    """Dependency already present → not duplicated."""
    shared_file = "agentception/shared.py"
    spec = PlanSpec(
        initiative="test",
        phases=[
            PlanPhase(
                label="0-phase",
                description="phase",
                issues=[
                    PlanIssue(id="issue-a", title="A", body="body a"),
                    PlanIssue(id="issue-b", title="B", body="body b", depends_on=["issue-a"]),
                ],
            )
        ],
    )

    with patch(
        "agentception.readers.plan_enricher.search_codebase",
        new=AsyncMock(return_value=[_make_match(file=shared_file)]),
    ):
        result = await enrich_plan_with_codebase_context(spec)

    issue_b = next(i for i in result.phases[0].issues if i.id == "issue-b")
    assert issue_b.depends_on.count("issue-a") == 1

