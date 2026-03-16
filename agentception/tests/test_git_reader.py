"""Tests for agentception.readers.git — branch classification logic."""
from __future__ import annotations

import re

import pytest

# Import the compiled regex directly so the tests stay decoupled from the
# git subprocess machinery (no live repo needed).
from agentception.readers.git import _AGENT_BRANCH_RE


# ---------------------------------------------------------------------------
# is_agent_branch — parametrized truth table
# ---------------------------------------------------------------------------

_SHOULD_MATCH = [
    # issue-scoped worker branches
    "agent/issue-176",
    "agent/issue-1",
    "agent/issue-42",
    # coordinator branches
    "agent/coord-cognitive-arch-propagation-81ac",
    "agent/coord-some-label-e072",
    # reviewer branches
    "agent/review-188-06dd",
    # label/org-chart branches
    "agent/cognitive-arch-propagation-e072",
    "agent/some-label-abc123",
    # plan integration branches
    "agent/plan-readme-section",
    "agent/plan-42-add-auth",
]

_SHOULD_NOT_MATCH = [
    "dev",
    "main",
    "feature/something",
    "fix/something",        # fix/* branches are NOT agent branches
    "feat/issue-1",         # removed — legacy naming, no longer used
    "feat/issue-42",
    "feat/brain-dump-foo",  # removed — legacy naming, no longer used
    "feat/plain",
    "feat/issue-",
    "feat/issue-abc",
    "ac/issue-176",         # removed — ac/* naming, no longer used
    "ac/coord-cognitive-arch-propagation-81ac",
    "ac/review-188-06dd",
    "",
]


@pytest.mark.parametrize("branch", _SHOULD_MATCH)
def test_agent_branch_re_matches(branch: str) -> None:
    assert _AGENT_BRANCH_RE.match(branch), (
        f"Expected _AGENT_BRANCH_RE to match {branch!r} but it did not"
    )


@pytest.mark.parametrize("branch", _SHOULD_NOT_MATCH)
def test_agent_branch_re_no_match(branch: str) -> None:
    assert not _AGENT_BRANCH_RE.match(branch), (
        f"Expected _AGENT_BRANCH_RE NOT to match {branch!r} but it did"
    )
