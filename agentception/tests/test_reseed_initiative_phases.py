from __future__ import annotations

"""Unit tests for reseed_missing_initiative_phases.

Verifies that the function:
- Reconstructs sequential phase deps from scoped labels on issues.
- Skips initiatives that already have phase metadata in initiative_phases.
- Skips initiatives with no scoped labels.
- Is idempotent: calling it twice does not overwrite existing data.

All DB interactions are mocked — no real Postgres required.

Run:
    docker compose exec agentception pytest \
        agentception/tests/test_reseed_initiative_phases.py -v
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.persist import PhaseEntry, reseed_missing_initiative_phases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_issue(labels: list[str]) -> MagicMock:
    """Return a mock ACIssue with the given label names."""
    issue = MagicMock()
    issue.labels_json = json.dumps(labels)
    return issue


def _make_session_ctx(issues: list[MagicMock], has_existing_phase: bool) -> MagicMock:
    """Build a minimal async session context manager mock.

    The function queries issues first, then checks for existing phase metadata
    per initiative.  The side_effect list mirrors that order:
    1. ``select(ACIssue)`` → issues_scalar
    2. ``select(ACInitiativePhase)`` per initiative → existing_scalar (one per initiative)

    ``has_existing_phase`` controls whether the ``ACInitiativePhase`` check
    returns an existing row (skip) or None (proceed with reseed).
    """
    session = AsyncMock()

    existing_scalar = MagicMock()
    existing_scalar.scalar_one_or_none.return_value = (
        MagicMock() if has_existing_phase else None
    )

    issues_scalar = MagicMock()
    issues_scalar.scalars.return_value.all.return_value = issues

    # Order matches function: issues first, then per-initiative phase check.
    session.execute.side_effect = [issues_scalar, existing_scalar]

    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = None
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reseed_derives_sequential_deps_from_scoped_labels() -> None:
    """When initiative_phases is empty, phases are seeded with sequential deps."""
    issues = [
        _mock_issue(["ac-build", "ac-build/0-foundation", "pipeline-active"]),
        _mock_issue(["ac-build", "ac-build/1-features", "blocked"]),
        _mock_issue(["ac-build", "ac-build/2-polish", "blocked"]),
    ]
    captured: list[tuple[str, str, str, list[PhaseEntry]]] = []

    async def fake_persist(
        repo: str, initiative: str, batch_id: str, phases: list[PhaseEntry]
    ) -> None:
        captured.append((repo, initiative, batch_id, phases))

    ctx = _make_session_ctx(issues, has_existing_phase=False)
    with (
        patch("agentception.db.persist.get_session", return_value=ctx),
        patch(
            "agentception.db.persist.persist_initiative_phases",
            side_effect=fake_persist,
        ),
    ):
        await reseed_missing_initiative_phases("owner/repo")

    assert len(captured) == 1
    repo, initiative, batch_id, phases = captured[0]
    assert repo == "owner/repo"
    assert initiative == "ac-build"
    assert batch_id.startswith("batch-")
    assert len(phases) == 3

    labels = [p["label"] for p in phases]
    assert labels == sorted(labels), "Phases must be in lexicographic order"

    # Sequential: phase[0] has no deps; phase[i] depends on phase[i-1].
    assert phases[0]["depends_on"] == []
    assert phases[1]["depends_on"] == [phases[0]["label"]]
    assert phases[2]["depends_on"] == [phases[1]["label"]]


@pytest.mark.anyio
async def test_reseed_skips_initiative_with_existing_phase_metadata() -> None:
    """If initiative_phases already has rows, the initiative is not reseeded."""
    issues = [
        _mock_issue(["ac-build", "ac-build/0-foundation", "pipeline-active"]),
    ]
    captured: list[str] = []

    async def fake_persist(
        repo: str, initiative: str, batch_id: str, phases: list[PhaseEntry]
    ) -> None:
        captured.append(initiative)

    ctx = _make_session_ctx(issues, has_existing_phase=True)
    with (
        patch("agentception.db.persist.get_session", return_value=ctx),
        patch(
            "agentception.db.persist.persist_initiative_phases",
            side_effect=fake_persist,
        ),
    ):
        await reseed_missing_initiative_phases("owner/repo")

    assert captured == [], "Should not reseed when rows already exist"


@pytest.mark.anyio
async def test_reseed_skips_initiative_with_no_scoped_labels() -> None:
    """Issues with no scoped labels produce no phase metadata."""
    issues = [
        _mock_issue(["ac-build", "pipeline-active"]),  # no ac-build/* label
    ]
    captured: list[str] = []

    async def fake_persist(
        repo: str, initiative: str, batch_id: str, phases: list[PhaseEntry]
    ) -> None:
        captured.append(initiative)

    # has_existing_phase=False so the check passes — but no scoped labels exist.
    ctx = _make_session_ctx(issues, has_existing_phase=False)
    with (
        patch("agentception.db.persist.get_session", return_value=ctx),
        patch(
            "agentception.db.persist.persist_initiative_phases",
            side_effect=fake_persist,
        ),
    ):
        await reseed_missing_initiative_phases("owner/repo")

    assert captured == [], "No scoped labels → nothing to reseed"
