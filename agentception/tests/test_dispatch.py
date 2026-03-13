from __future__ import annotations

import logging

import pytest

from agentception.services.auto_reviewer import extract_pr_number


def test_extract_pr_number_empty_url_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty string returns None and logs a structured warning — no exception raised."""
    with caplog.at_level(logging.WARNING, logger="agentception.services.auto_reviewer"):
        result = extract_pr_number("")
    assert result is None
    assert any("pr_url is empty" in record.message for record in caplog.records)


def test_extract_pr_number_malformed_url_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-empty but unparseable string returns None and logs a structured warning."""
    with caplog.at_level(logging.WARNING, logger="agentception.services.auto_reviewer"):
        result = extract_pr_number("not-a-url")
    assert result is None
    assert any("cannot parse PR number" in record.message for record in caplog.records)


def test_extract_pr_number_valid_url_returns_integer() -> None:
    """A valid GitHub PR URL returns the correct integer PR number."""
    result = extract_pr_number("https://github.com/org/repo/pull/42")
    assert result == 42
