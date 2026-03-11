"""Tests for agentception/services/auto_redispatch.py.

Covers:
- _count_prior_rejections: zero, one, multiple rejections in task_description
- _build_enhanced_body: correct structure and ordering
- auto_redispatch_after_rejection:
    - max attempts guard
    - bad PR URL guard
    - happy path: closes PR, fetches issue, dispatches developer
    - close_pr failure is non-fatal (continues to dispatch)
    - dispatch HTTP error is logged and swallowed

Run targeted:
    pytest agentception/tests/test_auto_redispatch.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentception.services.auto_redispatch import (
    _REJECTION_MARKER,
    _build_enhanced_body,
    _count_prior_rejections,
    auto_redispatch_after_rejection,
)


# ---------------------------------------------------------------------------
# _count_prior_rejections
# ---------------------------------------------------------------------------


def test_count_prior_rejections_none() -> None:
    """Returns 0 when task_description is None."""
    assert _count_prior_rejections(None) == 0


def test_count_prior_rejections_empty() -> None:
    """Returns 0 when task_description is an empty string."""
    assert _count_prior_rejections("") == 0


def test_count_prior_rejections_no_markers() -> None:
    """Returns 0 when no rejection markers are present."""
    assert _count_prior_rejections("normal issue body without rejections") == 0


def test_count_prior_rejections_one() -> None:
    """Returns 1 when exactly one rejection marker is present."""
    body = f"{_REJECTION_MARKER} 1 (Grade: C)\n\nsome feedback\n\n---\n\noriginal"
    assert _count_prior_rejections(body) == 1


def test_count_prior_rejections_two() -> None:
    """Returns 2 when two rejection markers are present (second attempt)."""
    body = (
        f"{_REJECTION_MARKER} 2 (Grade: C)\n\nfeedback2\n\n---\n\n"
        f"{_REJECTION_MARKER} 1 (Grade: D)\n\nfeedback1\n\n---\n\noriginal"
    )
    assert _count_prior_rejections(body) == 2


# ---------------------------------------------------------------------------
# _build_enhanced_body
# ---------------------------------------------------------------------------


def test_build_enhanced_body_prepends_rejection_section() -> None:
    """Rejection section must appear before the original body."""
    result = _build_enhanced_body(
        original_body="## Original spec",
        reviewer_feedback="1. Missing test\n2. dir() smell",
        grade="C",
        attempt=1,
    )
    rejection_pos = result.find(_REJECTION_MARKER)
    original_pos = result.find("## Original spec")
    assert rejection_pos < original_pos


def test_build_enhanced_body_contains_grade_and_attempt() -> None:
    """Output must include the grade and attempt number."""
    result = _build_enhanced_body(
        original_body="body",
        reviewer_feedback="defect",
        grade="D",
        attempt=2,
    )
    assert "Grade: D" in result
    assert "Attempt 2" in result


def test_build_enhanced_body_contains_feedback() -> None:
    """Reviewer feedback must appear verbatim in the output."""
    feedback = "1. Missing SCSS move\n2. No regression test"
    result = _build_enhanced_body("body", feedback, "C", 1)
    assert feedback in result


def test_build_enhanced_body_original_body_preserved() -> None:
    """Original issue body must appear intact in the output."""
    original = "## Objective\nDo the thing.\n\n## AC\n- [ ] item"
    result = _build_enhanced_body(original, "defect", "C", 1)
    assert original in result


# ---------------------------------------------------------------------------
# auto_redispatch_after_rejection
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 37
_PR_URL = "https://github.com/cgcardona/agentception/pull/569"
_GRADE = "C"
_FEEDBACK = "1. dir() smell\n2. SCSS not moved\n3. No regression test"


@pytest.mark.anyio
async def test_auto_redispatch_max_attempts_guard() -> None:
    """No dispatch or PR-close occurs when max attempts is reached."""
    with (
        patch(
            "agentception.services.auto_redispatch.get_agent_run_task_description",
            new_callable=AsyncMock,
            # 3 prior rejections → attempt 4 → over the limit
            return_value=(
                f"{_REJECTION_MARKER} 1\n"
                f"{_REJECTION_MARKER} 2\n"
                f"{_REJECTION_MARKER} 3\n"
            ),
        ),
        patch(
            "agentception.services.auto_redispatch.close_pr",
            new_callable=AsyncMock,
        ) as mock_close,
        patch(
            "agentception.services.auto_redispatch.get_issue",
            new_callable=AsyncMock,
        ) as mock_get_issue,
    ):
        await auto_redispatch_after_rejection(
            issue_number=_ISSUE_NUMBER,
            pr_url=_PR_URL,
            reviewer_feedback=_FEEDBACK,
            grade=_GRADE,
        )
    mock_close.assert_not_called()
    mock_get_issue.assert_not_called()


@pytest.mark.anyio
async def test_auto_redispatch_bad_pr_url_guard() -> None:
    """No dispatch occurs when the PR URL cannot be parsed."""
    with (
        patch(
            "agentception.services.auto_redispatch.get_agent_run_task_description",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.services.auto_redispatch.close_pr",
            new_callable=AsyncMock,
        ) as mock_close,
        patch("agentception.services.auto_redispatch.asyncio.sleep", new_callable=AsyncMock),
    ):
        await auto_redispatch_after_rejection(
            issue_number=_ISSUE_NUMBER,
            pr_url="https://github.com/cgcardona/agentception/issues/37",  # not a pull URL
            reviewer_feedback=_FEEDBACK,
            grade=_GRADE,
        )
    mock_close.assert_not_called()


@pytest.mark.anyio
async def test_auto_redispatch_happy_path() -> None:
    """Closes PR, fetches issue, dispatches developer on the happy path."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with (
        patch(
            "agentception.services.auto_redispatch.get_agent_run_task_description",
            new_callable=AsyncMock,
            return_value=None,  # first attempt
        ),
        patch(
            "agentception.services.auto_redispatch.close_pr",
            new_callable=AsyncMock,
        ) as mock_close,
        patch(
            "agentception.services.auto_redispatch.get_issue",
            new_callable=AsyncMock,
            return_value={"title": "Fix transcripts", "body": "## Original"},
        ) as mock_get_issue,
        patch("agentception.services.auto_redispatch.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.services.auto_redispatch.settings") as mock_settings,
        patch("agentception.services.auto_redispatch.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.gh_repo = "cgcardona/agentception"
        mock_settings.ac_api_key = "test-key"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await auto_redispatch_after_rejection(
            issue_number=_ISSUE_NUMBER,
            pr_url=_PR_URL,
            reviewer_feedback=_FEEDBACK,
            grade=_GRADE,
        )

    mock_close.assert_called_once_with(569, comment=mock_close.call_args[1]["comment"])
    mock_get_issue.assert_called_once_with(_ISSUE_NUMBER)
    mock_client.post.assert_called_once()
    payload = mock_client.post.call_args[1]["json"]
    assert payload["role"] == "developer"
    assert payload["issue_number"] == _ISSUE_NUMBER
    assert _REJECTION_MARKER in payload["issue_body"]
    assert _FEEDBACK in payload["issue_body"]


@pytest.mark.anyio
async def test_auto_redispatch_close_pr_failure_is_nonfatal() -> None:
    """A close_pr failure must not prevent the developer dispatch."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with (
        patch(
            "agentception.services.auto_redispatch.get_agent_run_task_description",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.services.auto_redispatch.close_pr",
            new_callable=AsyncMock,
            side_effect=RuntimeError("GitHub unavailable"),
        ),
        patch(
            "agentception.services.auto_redispatch.get_issue",
            new_callable=AsyncMock,
            return_value={"title": "Fix", "body": "body"},
        ),
        patch("agentception.services.auto_redispatch.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.services.auto_redispatch.settings") as mock_settings,
        patch("agentception.services.auto_redispatch.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.gh_repo = "cgcardona/agentception"
        mock_settings.ac_api_key = "test-key"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await auto_redispatch_after_rejection(
            issue_number=_ISSUE_NUMBER,
            pr_url=_PR_URL,
            reviewer_feedback=_FEEDBACK,
            grade=_GRADE,
        )

    # Dispatch must still have been called despite close_pr raising.
    mock_client.post.assert_called_once()


@pytest.mark.anyio
async def test_auto_redispatch_http_error_is_swallowed() -> None:
    """An HTTP error from the dispatch endpoint must be logged, not raised."""
    with (
        patch(
            "agentception.services.auto_redispatch.get_agent_run_task_description",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agentception.services.auto_redispatch.close_pr", new_callable=AsyncMock),
        patch(
            "agentception.services.auto_redispatch.get_issue",
            new_callable=AsyncMock,
            return_value={"title": "Fix", "body": "body"},
        ),
        patch("agentception.services.auto_redispatch.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.services.auto_redispatch.settings") as mock_settings,
        patch("agentception.services.auto_redispatch.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_settings.gh_repo = "cgcardona/agentception"
        mock_settings.ac_api_key = "test-key"

        mock_request = MagicMock(spec=httpx.Request)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.text = "internal error"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=mock_request, response=mock_resp)
        )
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise.
        await auto_redispatch_after_rejection(
            issue_number=_ISSUE_NUMBER,
            pr_url=_PR_URL,
            reviewer_feedback=_FEEDBACK,
            grade=_GRADE,
        )
