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
