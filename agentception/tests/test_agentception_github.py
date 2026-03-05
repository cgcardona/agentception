from __future__ import annotations

"""Tests for agentception/readers/github.py.

All GitHub interactions are mocked via ``unittest.mock.AsyncMock`` and
``patch`` — no real ``gh`` subprocess is ever invoked.  The subprocess
interface is thin (``asyncio.create_subprocess_exec``), so patching it at the
module level gives complete control over return values and exit codes.

Run targeted:
    pytest agentception/tests/test_agentception_github.py -v
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agentception.readers.github as gh_module
from agentception.readers.github import (
    _cache,
    _cache_invalidate,
    add_wip_label,
    clear_wip_label,
    close_pr,
    get_active_label,
    get_issue,
    get_issue_body,
    get_open_issues,
    get_open_prs,
    get_wip_issues,
    gh_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_process(stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock asyncio subprocess with the given stdout/stderr/returncode."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache_before_each() -> None:
    """Ensure a clean cache state for every test.

    Without this, cache hits from earlier tests would contaminate later ones.
    """
    _cache.clear()


# ---------------------------------------------------------------------------
# gh_json — caching behaviour
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_hit_skips_subprocess() -> None:
    """A second call with the same cache_key must NOT invoke the subprocess again."""
    payload = [{"number": 1, "title": "Example"}]

    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(json.dumps(payload).encode()),
    ) as mock_exec:
        # First call — subprocess runs.
        result1 = await gh_json(
            ["issue", "list", "--repo", "cgcardona/agentception", "--json", "number,title"],
            ".",
            "test_key",
        )
        # Second call — should use cache.
        result2 = await gh_json(
            ["issue", "list", "--repo", "cgcardona/agentception", "--json", "number,title"],
            ".",
            "test_key",
        )

    assert mock_exec.call_count == 1
    assert result1 == payload
    assert result2 == payload


@pytest.mark.anyio
async def test_cache_invalidated_after_write() -> None:
    """close_pr / clear_wip_label must empty the cache so next read is fresh."""
    # Pre-populate cache.
    _cache["stale_key"] = ("stale_value", time.monotonic() + 30)

    # Patch the subprocess so close_pr succeeds.
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b""),
    ):
        await close_pr(42, "closing")

    assert len(_cache) == 0


@pytest.mark.anyio
async def test_gh_json_raises_on_nonzero_exit() -> None:
    """gh_json must raise RuntimeError when the subprocess exits non-zero."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"", returncode=1, stderr=b"Not Found"),
    ):
        with pytest.raises(RuntimeError, match="gh command failed"):
            await gh_json(["issue", "list"], ".", "fail_key")


# ---------------------------------------------------------------------------
# get_open_issues
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_open_issues_filters_by_label() -> None:
    """get_open_issues(label=...) must pass --label to gh and return parsed list."""
    issues = [
        {"number": 10, "title": "Issue A", "labels": [], "body": ""},
        {"number": 11, "title": "Issue B", "labels": [], "body": ""},
    ]

    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(json.dumps(issues).encode()),
    ) as mock_exec:
        result = await get_open_issues(label="batch-01")

    # Verify --label was passed.
    call_args = mock_exec.call_args[0]  # positional args to create_subprocess_exec
    assert "--label" in call_args
    assert "batch-01" in call_args

    assert len(result) == 2
    assert result[0]["number"] == 10


@pytest.mark.anyio
async def test_get_open_issues_no_label() -> None:
    """get_open_issues() without a label must NOT pass --label."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"[]"),
    ) as mock_exec:
        result = await get_open_issues()

    call_args = mock_exec.call_args[0]
    assert "--label" not in call_args
    assert result == []


# ---------------------------------------------------------------------------
# get_wip_issues
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_wip_issues_empty() -> None:
    """get_wip_issues() must return an empty list when no agent:wip issues exist."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"[]"),
    ):
        result = await get_wip_issues()

    assert result == []


@pytest.mark.anyio
async def test_get_wip_issues_passes_label() -> None:
    """get_wip_issues() must delegate to get_open_issues with label='agent:wip'."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"[]"),
    ) as mock_exec:
        await get_wip_issues()

    call_args = mock_exec.call_args[0]
    assert "agent:wip" in call_args


# ---------------------------------------------------------------------------
# get_active_label
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_active_label_returns_first_match_from_config() -> None:
    """get_active_label() returns the first label in pipeline-config active_labels_order
    that has an open issue — not the lexicographically lowest label."""
    from agentception.models import PipelineConfig
    from agentception.readers import pipeline_config as _pc

    mock_config = PipelineConfig(
        coordinator_limits={"engineering-coordinator": 1, "qa-coordinator": 1},
        pool_size=4,
        active_labels_order=["ac-ui/0-critical-bugs", "ac-ui/1-design-tokens", "ac-ui/2-data-model"],
    )
    # Issues only have ac-ui/1 and ac-ui/2 labels (phase 0 is all done)
    label_names = ["ac-ui/1-design-tokens", "ac-ui/2-data-model", "enhancement"]

    with (
        patch.object(_pc, "read_pipeline_config", return_value=mock_config),
        patch(
            "agentception.readers.github.asyncio.create_subprocess_exec",
            return_value=_make_process(json.dumps(label_names).encode()),
        ),
    ):
        result = await get_active_label()

    # ac-ui/0 has no open issues, so ac-ui/1 is the first match
    assert result == "ac-ui/1-design-tokens"


@pytest.mark.anyio
async def test_get_active_label_returns_none_when_no_configured_labels_have_issues() -> None:
    """get_active_label() returns None when no label in active_labels_order has open issues."""
    from agentception.models import PipelineConfig
    from agentception.readers import pipeline_config as _pc

    mock_config = PipelineConfig(
        coordinator_limits={"engineering-coordinator": 1, "qa-coordinator": 1},
        pool_size=4,
        active_labels_order=["ac-ui/0-critical-bugs", "ac-ui/1-design-tokens"],
    )
    # No ac-ui/* issues open
    label_names_no_match = ["enhancement", "batch-01", "bug"]

    with (
        patch.object(_pc, "read_pipeline_config", return_value=mock_config),
        patch(
            "agentception.readers.github.asyncio.create_subprocess_exec",
            return_value=_make_process(json.dumps(label_names_no_match).encode()),
        ),
    ):
        result = await get_active_label()

    assert result is None


# ---------------------------------------------------------------------------
# get_open_prs
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_open_prs_returns_list() -> None:
    """get_open_prs() must return a list of PR dicts targeting dev."""
    prs = [{"number": 5, "title": "feat: something", "headRefName": "feat/x", "labels": []}]

    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(json.dumps(prs).encode()),
    ) as mock_exec:
        result = await get_open_prs()

    call_args = mock_exec.call_args[0]
    assert "--base" in call_args
    assert "dev" in call_args
    assert result == prs


# ---------------------------------------------------------------------------
# get_issue_body
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_issue_body_returns_string() -> None:
    """get_issue_body(N) must return the issue body string from gh output."""
    expected_body = "This is the issue body."

    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(json.dumps({"body": expected_body}).encode()),
    ):
        result = await get_issue_body(42)

    assert result == expected_body


# ---------------------------------------------------------------------------
# close_pr
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_close_pr_passes_comment() -> None:
    """close_pr() must pass --comment to gh pr close."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b""),
    ) as mock_exec:
        await close_pr(99, "closing: no longer needed")

    call_args = mock_exec.call_args[0]
    assert "pr" in call_args
    assert "close" in call_args
    assert "--comment" in call_args
    assert "closing: no longer needed" in call_args


@pytest.mark.anyio
async def test_close_pr_raises_on_failure() -> None:
    """close_pr() must raise RuntimeError when gh exits non-zero."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"", returncode=1, stderr=b"Forbidden"),
    ):
        with pytest.raises(RuntimeError, match="gh pr close failed"):
            await close_pr(1, "test")


# ---------------------------------------------------------------------------
# clear_wip_label
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_clear_wip_label_passes_remove_label() -> None:
    """clear_wip_label() must pass --remove-label agent:wip to gh."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b""),
    ) as mock_exec:
        await clear_wip_label(613)

    call_args = mock_exec.call_args[0]
    assert "--remove-label" in call_args
    assert "agent:wip" in call_args


@pytest.mark.anyio
async def test_clear_wip_label_invalidates_cache() -> None:
    """clear_wip_label() must empty the cache as a side effect."""
    _cache["some_key"] = ("value", time.monotonic() + 60)

    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b""),
    ):
        await clear_wip_label(613)

    assert len(_cache) == 0


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_issue_returns_dict() -> None:
    """get_issue() must return a dict with state, title, and labels for a valid issue."""
    payload = json.dumps({"number": 42, "state": "OPEN", "title": "Fix it", "labels": ["enhancement"]})
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(payload.encode()),
    ):
        result = await get_issue(42)

    assert isinstance(result, dict)
    assert result["state"] == "OPEN"
    assert result["title"] == "Fix it"


@pytest.mark.anyio
async def test_get_issue_raises_on_failure() -> None:
    """get_issue() must raise RuntimeError when gh exits with a non-zero status."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"", returncode=1),
    ):
        with pytest.raises(RuntimeError, match="gh command failed"):
            await get_issue(9999)


# ---------------------------------------------------------------------------
# add_wip_label
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_add_wip_label_passes_add_label() -> None:
    """add_wip_label() must pass --add-label agent:wip to gh."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b""),
    ) as mock_exec:
        await add_wip_label(42)

    call_args = mock_exec.call_args[0]
    assert "--add-label" in call_args
    assert "agent:wip" in call_args


@pytest.mark.anyio
async def test_add_wip_label_invalidates_cache() -> None:
    """add_wip_label() must empty the cache as a side effect."""
    _cache["some_key"] = ("value", time.monotonic() + 60)

    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b""),
    ):
        await add_wip_label(42)

    assert len(_cache) == 0


@pytest.mark.anyio
async def test_add_wip_label_raises_on_failure() -> None:
    """add_wip_label() must raise RuntimeError when gh exits with a non-zero status."""
    with patch(
        "agentception.readers.github.asyncio.create_subprocess_exec",
        return_value=_make_process(b"", returncode=1),
    ):
        with pytest.raises(RuntimeError, match="gh issue edit"):
            await add_wip_label(42)
