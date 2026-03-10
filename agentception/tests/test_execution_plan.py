from __future__ import annotations

"""Tests for the ExecutionPlan / PlanOperation models and the planner service.

Covers:
- PlanOperation validation for each tool type.
- ExecutionPlan construction and validation.
- Planner prompt building.
- Planner JSON parsing (happy path, malformed JSON, missing fields).
- Planner fallback on LLM failure.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.models import ExecutionPlan, PlanOperation
from agentception.services.planner import (
    _build_planner_prompt,
    _parse_plan_json,
    generate_execution_plan,
)


# ---------------------------------------------------------------------------
# PlanOperation
# ---------------------------------------------------------------------------


def test_plan_operation_replace_in_file_valid() -> None:
    """replace_in_file with old_string and new_string is valid."""
    op = PlanOperation(
        tool="replace_in_file",
        file="agentception/config.py",
        old_string="    poll_interval_seconds: int = 30",
        new_string="    poll_interval_seconds: int = 30\n    agent_max_iterations: int = 100",
    )
    assert op.tool == "replace_in_file"
    assert op.file == "agentception/config.py"


def test_plan_operation_replace_in_file_missing_old_string_raises() -> None:
    """replace_in_file without old_string raises ValidationError."""
    with pytest.raises(Exception, match="old_string"):
        PlanOperation(
            tool="replace_in_file",
            file="agentception/config.py",
            new_string="something",
        )


def test_plan_operation_replace_in_file_missing_new_string_raises() -> None:
    """replace_in_file without new_string raises ValidationError."""
    with pytest.raises(Exception, match="new_string"):
        PlanOperation(
            tool="replace_in_file",
            file="agentception/config.py",
            old_string="something",
        )


def test_plan_operation_insert_after_in_file_valid() -> None:
    """insert_after_in_file with after and text is valid."""
    op = PlanOperation(
        tool="insert_after_in_file",
        file="agentception/tests/test_config.py",
        after="assert s.poll_interval_seconds == 30",
        text="def test_new() -> None:\n    pass",
    )
    assert op.tool == "insert_after_in_file"


def test_plan_operation_insert_after_in_file_missing_after_raises() -> None:
    """insert_after_in_file without after raises ValidationError."""
    with pytest.raises(Exception, match="after"):
        PlanOperation(
            tool="insert_after_in_file",
            file="agentception/tests/test_config.py",
            text="some text",
        )


def test_plan_operation_write_file_valid() -> None:
    """write_file with content is valid."""
    op = PlanOperation(
        tool="write_file",
        file="agentception/routes/api/ping.py",
        content='"""Ping route."""\nfrom fastapi import APIRouter\nrouter = APIRouter()\n',
    )
    assert op.tool == "write_file"


def test_plan_operation_write_file_missing_content_raises() -> None:
    """write_file without content raises ValidationError."""
    with pytest.raises(Exception, match="content"):
        PlanOperation(tool="write_file", file="agentception/new.py")


# ---------------------------------------------------------------------------
# ExecutionPlan
# ---------------------------------------------------------------------------


def _make_replace_op(file: str = "agentception/config.py") -> PlanOperation:
    return PlanOperation(
        tool="replace_in_file",
        file=file,
        old_string="old",
        new_string="new",
    )


def test_execution_plan_valid() -> None:
    """ExecutionPlan with one valid operation is valid."""
    plan = ExecutionPlan(
        run_id="issue-501",
        issue_number=501,
        operations=[_make_replace_op()],
    )
    assert plan.run_id == "issue-501"
    assert len(plan.operations) == 1


def test_execution_plan_empty_operations_raises() -> None:
    """ExecutionPlan with no operations raises ValidationError."""
    with pytest.raises(Exception, match="at least one operation"):
        ExecutionPlan(run_id="issue-501", issue_number=501, operations=[])


def test_execution_plan_serialise_round_trip() -> None:
    """ExecutionPlan serialises to JSON and back without data loss."""
    plan = ExecutionPlan(
        run_id="issue-501",
        issue_number=501,
        operations=[
            PlanOperation(
                tool="replace_in_file",
                file="agentception/config.py",
                old_string="    poll_interval_seconds: int = 30",
                new_string="    poll_interval_seconds: int = 30\n    agent_max_iterations: int = 100",
            ),
        ],
    )
    raw = plan.model_dump_json()
    restored = ExecutionPlan.model_validate_json(raw)
    assert restored.run_id == plan.run_id
    assert len(restored.operations) == 1
    assert restored.operations[0].old_string == plan.operations[0].old_string


# ---------------------------------------------------------------------------
# Planner prompt building
# ---------------------------------------------------------------------------


def test_build_planner_prompt_includes_issue_body() -> None:
    """The planner prompt includes the issue title and body."""
    prompt = _build_planner_prompt(
        issue_title="Add a field",
        issue_body="Add `agent_max_iterations: int = 100`",
        file_contents={},
    )
    assert "Add a field" in prompt
    assert "agent_max_iterations" in prompt


def test_build_planner_prompt_includes_file_contents() -> None:
    """The planner prompt includes pre-loaded file contents."""
    prompt = _build_planner_prompt(
        issue_title="Test",
        issue_body="body",
        file_contents={"agentception/config.py": "class AgentCeptionSettings:\n    pass"},
    )
    assert "agentception/config.py" in prompt
    assert "AgentCeptionSettings" in prompt


# ---------------------------------------------------------------------------
# Planner JSON parsing
# ---------------------------------------------------------------------------


def test_parse_plan_json_valid() -> None:
    """Valid JSON with one replace_in_file operation produces an ExecutionPlan."""
    raw = json.dumps(
        {
            "operations": [
                {
                    "tool": "replace_in_file",
                    "file": "agentception/config.py",
                    "old_string": "    poll_interval_seconds: int = 30",
                    "new_string": "    poll_interval_seconds: int = 30\n    agent_max_iterations: int = 100",
                }
            ]
        }
    )
    plan = _parse_plan_json(raw, "issue-501", 501)
    assert plan is not None
    assert len(plan.operations) == 1
    assert plan.operations[0].tool == "replace_in_file"


def test_parse_plan_json_strips_markdown_fences() -> None:
    """JSON wrapped in markdown fences is parsed correctly."""
    raw = (
        "```json\n"
        + json.dumps(
            {
                "operations": [
                    {
                        "tool": "replace_in_file",
                        "file": "a.py",
                        "old_string": "old",
                        "new_string": "new",
                    }
                ]
            }
        )
        + "\n```"
    )
    plan = _parse_plan_json(raw, "issue-1", 1)
    assert plan is not None


def test_parse_plan_json_empty_string_returns_none() -> None:
    """Empty response returns None."""
    assert _parse_plan_json("", "issue-1", 1) is None


def test_parse_plan_json_invalid_json_returns_none() -> None:
    """Malformed JSON returns None."""
    assert _parse_plan_json("{not valid json", "issue-1", 1) is None


def test_parse_plan_json_empty_operations_returns_none() -> None:
    """An operations list with no valid entries returns None."""
    raw = json.dumps({"operations": []})
    assert _parse_plan_json(raw, "issue-1", 1) is None


def test_parse_plan_json_skips_invalid_operations() -> None:
    """Invalid operations are skipped; valid ones are kept."""
    raw = json.dumps(
        {
            "operations": [
                # Missing required fields for replace_in_file — will be skipped
                {"tool": "replace_in_file", "file": "a.py"},
                # Valid
                {
                    "tool": "replace_in_file",
                    "file": "b.py",
                    "old_string": "x",
                    "new_string": "y",
                },
            ]
        }
    )
    plan = _parse_plan_json(raw, "issue-1", 1)
    assert plan is not None
    assert len(plan.operations) == 1
    assert plan.operations[0].file == "b.py"


# ---------------------------------------------------------------------------
# generate_execution_plan — integration (LLM mocked)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_execution_plan_returns_plan_on_success(
    tmp_path: Path,
) -> None:
    """generate_execution_plan returns an ExecutionPlan when the LLM responds correctly."""
    # Create a dummy file in the temp worktree
    config_file = tmp_path / "agentception" / "config.py"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("    poll_interval_seconds: int = 30\n")

    llm_response = json.dumps(
        {
            "operations": [
                {
                    "tool": "replace_in_file",
                    "file": "agentception/config.py",
                    "old_string": "    poll_interval_seconds: int = 30",
                    "new_string": "    poll_interval_seconds: int = 30\n    agent_max_iterations: int = 100",
                }
            ]
        }
    )

    with patch(
        "agentception.services.planner.call_anthropic",
        new=AsyncMock(return_value=llm_response),
    ):
        plan = await generate_execution_plan(
            run_id="issue-501",
            issue_number=501,
            issue_title="Add field",
            issue_body="Add `agent_max_iterations: int = 100`",
            worktree_path=tmp_path,
            file_paths=["agentception/config.py"],
        )

    assert plan is not None
    assert plan.run_id == "issue-501"
    assert len(plan.operations) == 1


@pytest.mark.anyio
async def test_generate_execution_plan_returns_none_on_llm_failure(
    tmp_path: Path,
) -> None:
    """generate_execution_plan returns None when the LLM call raises."""
    with patch(
        "agentception.services.planner.call_anthropic",
        new=AsyncMock(side_effect=RuntimeError("API unavailable")),
    ):
        plan = await generate_execution_plan(
            run_id="issue-501",
            issue_number=501,
            issue_title="Add field",
            issue_body="body",
            worktree_path=tmp_path,
            file_paths=[],
        )

    assert plan is None


@pytest.mark.anyio
async def test_generate_execution_plan_returns_none_on_bad_json(
    tmp_path: Path,
) -> None:
    """generate_execution_plan returns None when the LLM returns unparseable JSON."""
    with patch(
        "agentception.services.planner.call_anthropic",
        new=AsyncMock(return_value="not json at all"),
    ):
        plan = await generate_execution_plan(
            run_id="issue-501",
            issue_number=501,
            issue_title="Add field",
            issue_body="body",
            worktree_path=tmp_path,
            file_paths=[],
        )

    assert plan is None
