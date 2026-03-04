"""Tests for A/B results dashboard computation and route (AC-505).

Covers the three acceptance criteria from issue #638:
- test_compute_ab_results_empty: empty waves produce zero-value results for both variants.
- test_compute_ab_results_assigns_correct_variant: even-second batch → variant A,
  odd-second batch → variant B.
- test_ab_page_returns_200: GET /ab-testing returns HTTP 200.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_agentception_ab_results.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.intelligence.ab_results import (
    ABVariantResult,
    _average_grade,
    _extract_grade,
    compute_ab_results,
)
from agentception.models import AgentNode, AgentStatus
from agentception.telemetry import WaveSummary


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client wrapping the AgentCeption FastAPI app."""
    with TestClient(app) as c:
        yield c


def _make_wave(batch_id: str, issues: list[int], prs_opened: int = 1) -> WaveSummary:
    """Build a minimal WaveSummary for testing purposes."""
    import time

    now = time.time()
    return WaveSummary(
        batch_id=batch_id,
        started_at=now - 600,
        ended_at=now - 60,
        issues_worked=issues,
        prs_opened=prs_opened,
        prs_merged=0,
        estimated_tokens=0,
        estimated_cost_usd=0.0,
        agents=[
            AgentNode(
                id=f"agent-{issues[0]}",
                role="python-developer",
                status=AgentStatus.DONE,
                issue_number=issues[0],
                batch_id=batch_id,
            )
        ] if issues else [],
    )


# ── _extract_grade ────────────────────────────────────────────────────────────


def test_extract_grade_backtick_format() -> None:
    """_extract_grade must parse the reviewer's standard comment format."""
    assert _extract_grade("✅ **Review complete — Grade: `A`**") == "A"


def test_extract_grade_plain_format() -> None:
    """_extract_grade must handle plain 'Grade: X' without backticks."""
    assert _extract_grade("Grade: B — solid implementation.") == "B"


def test_extract_grade_returns_none_when_absent() -> None:
    """_extract_grade returns None when no grade pattern is present."""
    assert _extract_grade("No grade information in this text.") is None


# ── _average_grade ────────────────────────────────────────────────────────────


def test_average_grade_empty_returns_none() -> None:
    """_average_grade returns None for an empty list."""
    assert _average_grade([]) is None


def test_average_grade_single_grade() -> None:
    """_average_grade returns the single grade unchanged."""
    assert _average_grade(["A"]) == "A"


def test_average_grade_mixed() -> None:
    """_average_grade averages correctly: A + C = B (numeric mean 3)."""
    result = _average_grade(["A", "C"])
    assert result == "B"


# ── compute_ab_results: empty ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_compute_ab_results_empty() -> None:
    """compute_ab_results returns zero-value results for both variants when there are no waves."""
    empty_versions: dict[str, object] = {"versions": {}, "ab_mode": {}}
    empty_waves: list[WaveSummary] = []
    empty_prs: list[dict[str, object]] = []

    with (
        patch(
            "agentception.intelligence.ab_results.read_role_versions",
            new=AsyncMock(return_value=empty_versions),
        ),
        patch(
            "agentception.intelligence.ab_results.aggregate_waves",
            new=AsyncMock(return_value=empty_waves),
        ),
        patch(
            "agentception.intelligence.ab_results.get_merged_prs",
            new=AsyncMock(return_value=empty_prs),
        ),
    ):
        variant_a, variant_b = await compute_ab_results()

    assert variant_a.variant == "A"
    assert variant_a.prs_opened == 0
    assert variant_a.prs_merged == 0
    assert variant_a.avg_grade is None
    assert variant_a.merge_rate == 0.0
    assert variant_a.batch_ids == []

    assert variant_b.variant == "B"
    assert variant_b.prs_opened == 0
    assert variant_b.prs_merged == 0
    assert variant_b.avg_grade is None
    assert variant_b.merge_rate == 0.0
    assert variant_b.batch_ids == []


# ── compute_ab_results: variant assignment ────────────────────────────────────


@pytest.mark.anyio
async def test_compute_ab_results_assigns_correct_variant() -> None:
    """Even-second BATCH_ID maps to variant A; odd-second maps to variant B.

    Given one wave with an even-second batch (→ A, issue 100) and one with an
    odd-second batch (→ B, issue 200), compute_ab_results must place their
    stats in the correct variant buckets.  A merged PR for issue 100 (branch
    feat/issue-100) is attributed to variant A; one for issue 200 goes to B.
    """
    # Even second (00) → variant A.
    wave_a = _make_wave("eng-20260302T120000Z-aaaa", [100], prs_opened=1)
    # Odd second (01) → variant B.
    wave_b = _make_wave("eng-20260302T120001Z-bbbb", [200], prs_opened=2)

    merged_prs: list[dict[str, object]] = [
        {
            "number": 50,
            "headRefName": "feat/issue-100",
            "body": "✅ Review complete — Grade: `A`\n\nCloses #100",
            "mergedAt": "2026-03-02T12:01:00Z",
        },
        {
            "number": 51,
            "headRefName": "feat/issue-200",
            "body": "Closes #200",  # no grade in body
            "mergedAt": "2026-03-02T12:02:00Z",
        },
    ]

    # PR 51 has no grade in body; simulate a comment with a grade.
    async def fake_get_pr_comments(pr_number: int) -> list[str]:
        if pr_number == 51:
            return ["✅ Review complete — Grade: `B`\nMerged at: 2026-03-02T12:02:30Z"]
        return []

    empty_versions: dict[str, object] = {
        "versions": {},
        "ab_mode": {"variant_a_sha": "abc123", "variant_b_sha": "def456"},
    }

    with (
        patch(
            "agentception.intelligence.ab_results.read_role_versions",
            new=AsyncMock(return_value=empty_versions),
        ),
        patch(
            "agentception.intelligence.ab_results.aggregate_waves",
            new=AsyncMock(return_value=[wave_a, wave_b]),
        ),
        patch(
            "agentception.intelligence.ab_results.get_merged_prs",
            new=AsyncMock(return_value=merged_prs),
        ),
        patch(
            "agentception.intelligence.ab_results.get_pr_comments",
            side_effect=fake_get_pr_comments,
        ),
    ):
        variant_a, variant_b = await compute_ab_results()

    # Variant A — even-second batch, issue 100.
    assert variant_a.variant == "A"
    assert "eng-20260302T120000Z-aaaa" in variant_a.batch_ids
    assert variant_a.prs_opened == 1
    assert variant_a.prs_merged == 1
    assert variant_a.avg_grade == "A"
    assert variant_a.merge_rate == 1.0
    assert variant_a.role_sha == "abc123"

    # Variant B — odd-second batch, issue 200.
    assert variant_b.variant == "B"
    assert "eng-20260302T120001Z-bbbb" in variant_b.batch_ids
    assert variant_b.prs_opened == 2
    assert variant_b.prs_merged == 1
    assert variant_b.avg_grade == "B"
    assert variant_b.merge_rate == 0.5
    assert variant_b.role_sha == "def456"


# ── GET /ab-testing → 200 ────────────────────────────────────────────────────


def test_ab_page_returns_200(client: TestClient) -> None:
    """GET /ab-testing must return HTTP 200 with variant comparison cards."""
    mock_a = ABVariantResult(
        variant="A",
        role_sha="abc123def456",
        batch_ids=["eng-20260302T120000Z-aaaa"],
        prs_opened=3,
        prs_merged=3,
        avg_grade="A",
        merge_rate=1.0,
    )
    mock_b = ABVariantResult(
        variant="B",
        role_sha="def456abc123",
        batch_ids=["eng-20260302T120001Z-bbbb"],
        prs_opened=2,
        prs_merged=1,
        avg_grade="B",
        merge_rate=0.5,
    )

    with patch(
        "agentception.routes.ui.ab_testing.compute_ab_results",
        new=AsyncMock(return_value=(mock_a, mock_b)),
    ):
        response = client.get("/ab-testing")

    assert response.status_code == 200
    html = response.text
    # Both variant cards must be present.
    assert "Variant A" in html
    assert "Variant B" in html
    # Winner badge must appear for A (higher merge rate).
    assert "Winner" in html
    # Batch IDs must appear in the breakdown table.
    assert "eng-20260302T120000Z-aaaa" in html
    assert "eng-20260302T120001Z-bbbb" in html
