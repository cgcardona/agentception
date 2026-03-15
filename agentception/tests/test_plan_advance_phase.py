"""Tests for plan_advance_phase MCP tool.

Covers:
- Success path: all from_phase issues closed → labels mutated, unlocked_count returned.
- Error path: open from_phase issues remain → structured error, no labels mutated.
- Edge case: to_phase has no issues → success with unlocked_count=0.
- MCP server dispatch: plan_advance_phase routed through call_tool_async.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.mcp.plan_advance_phase import (
    _fetch_issues_with_labels,
    _unlock_issue,
    plan_advance_phase,
)
from agentception.mcp.server import call_tool_async
from agentception.models import PipelineConfig
from agentception.types import JsonValue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline_config(**overrides: str | int | bool | float | None) -> PipelineConfig:
    """Return a minimal PipelineConfig suitable for tests."""
    defaults: dict[str, JsonValue] = {
        "max_eng_vps": 1,
        "max_qa_vps": 1,
        "pool_size_per_vp": 4,
        "active_labels_order": ["phase-1", "phase-2"],
        "phase_advance_blocked_label": "pipeline/gated",
        "phase_advance_active_label": "pipeline/active",
    }
    defaults.update(overrides)
    return PipelineConfig.model_validate(defaults)


# ---------------------------------------------------------------------------
# plan_advance_phase — success path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_advance_phase_all_closed_unlocks_to_phase_issues() -> None:
    """When all from_phase issues are closed, to_phase issues are unlocked."""
    config = _make_pipeline_config()

    from_phase_data = [
        {"number": 10, "state": "CLOSED"},
        {"number": 11, "state": "CLOSED"},
    ]
    to_phase_data = [
        {"number": 20, "state": "OPEN"},
        {"number": 21, "state": "OPEN"},
    ]

    with (
        patch(
            "agentception.mcp.plan_advance_phase.read_pipeline_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "agentception.mcp.plan_advance_phase._fetch_issues_with_labels",
            new=AsyncMock(side_effect=[from_phase_data, to_phase_data]),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.remove_label_from_issue",
            new=AsyncMock(),
        ) as mock_remove,
        patch(
            "agentception.mcp.plan_advance_phase.add_label_to_issue",
            new=AsyncMock(),
        ) as mock_add,
    ):
        result = await plan_advance_phase("initiative-x", "phase-1", "phase-2")

    assert result["advanced"] is True
    assert result["unlocked_count"] == 2

    # Both to_phase issues should have had labels mutated.
    assert mock_remove.call_count == 2
    assert mock_add.call_count == 2

    remove_calls = {call.args[0] for call in mock_remove.call_args_list}
    add_calls = {call.args[0] for call in mock_add.call_args_list}
    assert remove_calls == {20, 21}
    assert add_calls == {20, 21}

    # Correct label names used.
    for call in mock_remove.call_args_list:
        assert call.args[1] == "pipeline/gated"
    for call in mock_add.call_args_list:
        assert call.args[1] == "pipeline/active"


# ---------------------------------------------------------------------------
# plan_advance_phase — error path: open from_phase issues remain
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_advance_phase_open_from_phase_issues_returns_error() -> None:
    """When from_phase has open issues, return structured error; no labels mutated."""
    config = _make_pipeline_config()

    from_phase_data = [
        {"number": 10, "state": "CLOSED"},
        {"number": 11, "state": "OPEN"},   # still open
    ]

    with (
        patch(
            "agentception.mcp.plan_advance_phase.read_pipeline_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "agentception.mcp.plan_advance_phase._fetch_issues_with_labels",
            new=AsyncMock(return_value=from_phase_data),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.remove_label_from_issue",
            new=AsyncMock(),
        ) as mock_remove,
        patch(
            "agentception.mcp.plan_advance_phase.add_label_to_issue",
            new=AsyncMock(),
        ) as mock_add,
    ):
        result = await plan_advance_phase("initiative-x", "phase-1", "phase-2")

    assert result["advanced"] is False
    assert "open_issues" in result
    assert result["open_issues"] == [11]
    assert "error" in result

    # No label mutations should have occurred.
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


# ---------------------------------------------------------------------------
# plan_advance_phase — edge case: no to_phase issues
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_advance_phase_no_to_phase_issues_returns_zero_unlocked() -> None:
    """When there are no to_phase issues, return advanced=True, unlocked_count=0."""
    config = _make_pipeline_config()

    from_phase_data = [{"number": 10, "state": "CLOSED"}]
    to_phase_data: list[dict[str, JsonValue]] = []

    with (
        patch(
            "agentception.mcp.plan_advance_phase.read_pipeline_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "agentception.mcp.plan_advance_phase._fetch_issues_with_labels",
            new=AsyncMock(side_effect=[from_phase_data, to_phase_data]),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.remove_label_from_issue",
            new=AsyncMock(),
        ) as mock_remove,
        patch(
            "agentception.mcp.plan_advance_phase.add_label_to_issue",
            new=AsyncMock(),
        ) as mock_add,
    ):
        result = await plan_advance_phase("initiative-x", "phase-1", "phase-2")

    assert result["advanced"] is True
    assert result["unlocked_count"] == 0
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


# ---------------------------------------------------------------------------
# plan_advance_phase — no from_phase issues (gate trivially passed)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_advance_phase_no_from_phase_issues_unlocks_to_phase() -> None:
    """When from_phase has zero issues (trivially all closed), advance succeeds."""
    config = _make_pipeline_config()

    from_phase_data: list[dict[str, JsonValue]] = []
    to_phase_data = [{"number": 30, "state": "OPEN"}]

    with (
        patch(
            "agentception.mcp.plan_advance_phase.read_pipeline_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "agentception.mcp.plan_advance_phase._fetch_issues_with_labels",
            new=AsyncMock(side_effect=[from_phase_data, to_phase_data]),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.remove_label_from_issue",
            new=AsyncMock(),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.add_label_to_issue",
            new=AsyncMock(),
        ) as mock_add,
    ):
        result = await plan_advance_phase("initiative-x", "phase-1", "phase-2")

    assert result["advanced"] is True
    assert result["unlocked_count"] == 1
    mock_add.assert_called_once_with(30, "pipeline/active")


# ---------------------------------------------------------------------------
# _unlock_issue unit test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unlock_issue_calls_remove_then_add() -> None:
    """_unlock_issue removes blocked_label then adds active_label in order."""
    call_order: list[str] = []

    async def fake_remove(issue_number: int, label: str) -> None:
        call_order.append(f"remove:{label}")

    async def fake_add(issue_number: int, label: str) -> None:
        call_order.append(f"add:{label}")

    with (
        patch(
            "agentception.mcp.plan_advance_phase.remove_label_from_issue",
            new=fake_remove,
        ),
        patch(
            "agentception.mcp.plan_advance_phase.add_label_to_issue",
            new=fake_add,
        ),
    ):
        await _unlock_issue(42, "pipeline/gated", "pipeline/active")

    assert call_order == ["remove:pipeline/gated", "add:pipeline/active"]


# ---------------------------------------------------------------------------
# MCP server dispatch — call_tool_async routes plan_advance_phase correctly
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_tool_async_routes_plan_advance_phase() -> None:
    """call_tool_async dispatches plan_advance_phase and returns ACToolResult."""
    expected_result: dict[str, JsonValue] = {"advanced": True, "unlocked_count": 3}

    with patch(
        "agentception.mcp.server.plan_advance_phase",
        new=AsyncMock(return_value=expected_result),
    ) as mock_tool:
        tool_result = await call_tool_async(
            "plan_advance_phase",
            {
                "initiative": "test-initiative",
                "from_phase": "phase-1",
                "to_phase": "phase-2",
            },
        )

    mock_tool.assert_called_once_with("test-initiative", "phase-1", "phase-2")
    assert tool_result["isError"] is False
    assert len(tool_result["content"]) == 1
    import json
    payload = json.loads(tool_result["content"][0]["text"])
    assert payload["advanced"] is True
    assert payload["unlocked_count"] == 3


@pytest.mark.anyio
async def test_call_tool_async_plan_advance_phase_missing_args_returns_error() -> None:
    """call_tool_async returns isError=True when required arguments are missing."""
    tool_result = await call_tool_async(
        "plan_advance_phase",
        {"initiative": "test-initiative"},  # from_phase and to_phase missing
    )

    assert tool_result["isError"] is True
    import json
    payload = json.loads(tool_result["content"][0]["text"])
    assert "error" in payload


@pytest.mark.anyio
async def test_call_tool_async_plan_advance_phase_open_issues_is_error() -> None:
    """When the tool returns advanced=False (gate blocked), isError is True."""
    blocked_result: dict[str, JsonValue] = {
        "advanced": False,
        "error": "2 open issues remain in phase 'phase-1'",
        "open_issues": [11, 12],
    }

    with patch(
        "agentception.mcp.server.plan_advance_phase",
        new=AsyncMock(return_value=blocked_result),
    ):
        tool_result = await call_tool_async(
            "plan_advance_phase",
            {
                "initiative": "test-initiative",
                "from_phase": "phase-1",
                "to_phase": "phase-2",
            },
        )

    assert tool_result["isError"] is True
    import json
    payload = json.loads(tool_result["content"][0]["text"])
    assert payload["advanced"] is False
