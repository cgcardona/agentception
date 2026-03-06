from __future__ import annotations

"""Tests for agentception/readers/worktrees.py (AC-002, AC-048).

Verifies that the worktree reader correctly discovers active agent worktrees
and parses their TOML v2 .agent-task files into TaskFile models.

Run targeted:
    pytest agentception/tests/test_agentception_worktrees.py -v
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from agentception.models import IssueSub, TaskFile
from agentception.readers.worktrees import (
    list_active_worktrees,
    parse_agent_task,
    worktree_last_commit_time,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def issue_task_content() -> str:
    """Minimal TOML v2 .agent-task content for an issue-to-pr workflow."""
    return """\
[task]
version = "2.0"
workflow = "issue-to-pr"
attempt_n = 0
required_output = "pr_url"
on_block = "stop"

[agent]
role = "python-developer"
tier = "engineer"
org_domain = "engineering"
cognitive_arch = "turing:python"

[repo]
gh_repo = "cgcardona/agentception"
base = "dev"

[pipeline]
batch_id = "eng-20260301T214203Z-057d"

[spawn]
mode = "single"
sub_agents = false

[target]
issue_number = 610
depends_on = []
closes = [610]
file_ownership = []

[worktree]
path = "/home/user/.agentception/worktrees/agentception/issue-610"
branch = "feat/issue-610"
linked_pr = 0
"""


@pytest.fixture()
def pr_review_task_content() -> str:
    """Minimal TOML v2 .agent-task content for a pr-review workflow."""
    return """\
[task]
version = "2.0"
workflow = "pr-review"

[agent]
role = "pr-reviewer"

[repo]
gh_repo = "cgcardona/agentception"
base = "dev"

[pipeline]
batch_id = "eng-20260301T211956Z-741f"

[spawn]
mode = "chain"
sub_agents = false

[target]
pr_number = 642

[worktree]
path = "/home/user/.agentception/worktrees/agentception/pr-642"
branch = "feat/issue-609"
linked_pr = 0
"""


@pytest.fixture()
def worktree_with_issue_task(tmp_path: Path, issue_task_content: str) -> Path:
    """Temporary worktree directory with a valid issue-to-pr .agent-task file."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text(issue_task_content)
    return tmp_path


@pytest.fixture()
def worktree_with_pr_review_task(tmp_path: Path, pr_review_task_content: str) -> Path:
    """Temporary worktree directory with a valid pr-review .agent-task file."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text(pr_review_task_content)
    return tmp_path


# ── parse_agent_task — TOML v2 ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_parse_agent_task_issue(worktree_with_issue_task: Path) -> None:
    """parse_agent_task correctly extracts all fields from an issue-to-pr TOML task file."""
    result = await parse_agent_task(worktree_with_issue_task)

    assert result is not None
    assert result.task == "issue-to-pr"
    assert result.gh_repo == "cgcardona/agentception"
    assert result.issue_number == 610
    assert result.branch == "feat/issue-610"
    assert result.role == "python-developer"
    assert result.base == "dev"
    assert result.batch_id == "eng-20260301T214203Z-057d"
    assert result.closes_issues == [610]
    assert result.spawn_sub_agents is False
    assert result.attempt_n == 0
    assert result.required_output == "pr_url"
    assert result.on_block == "stop"
    assert result.pr_number is None
    assert result.tier == "engineer"
    assert result.org_domain == "engineering"
    assert result.cognitive_arch == "turing:python"


@pytest.mark.anyio
async def test_parse_agent_task_pr_review(worktree_with_pr_review_task: Path) -> None:
    """parse_agent_task correctly extracts all fields from a pr-review TOML task file."""
    result = await parse_agent_task(worktree_with_pr_review_task)

    assert result is not None
    assert result.task == "pr-review"
    assert result.pr_number == 642
    assert result.branch == "feat/issue-609"
    assert result.role == "pr-reviewer"
    assert result.gh_repo == "cgcardona/agentception"
    assert result.batch_id == "eng-20260301T211956Z-741f"
    assert result.spawn_mode == "chain"
    assert result.issue_number is None
    assert result.closes_issues == []


@pytest.mark.anyio
async def test_parse_agent_task_missing_returns_none(tmp_path: Path) -> None:
    """parse_agent_task returns None when the .agent-task file does not exist."""
    result = await parse_agent_task(tmp_path)
    assert result is None


@pytest.mark.anyio
async def test_parse_agent_task_malformed_toml_returns_none(tmp_path: Path) -> None:
    """parse_agent_task returns None when the file contains invalid TOML."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text("this is not valid toml [\nbad content")
    result = await parse_agent_task(tmp_path)
    assert result is None


@pytest.mark.anyio
async def test_parse_agent_task_depends_on_as_int_list(tmp_path: Path) -> None:
    """parse_agent_task populates depends_on as list[int] from TOML array."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text(
        "[task]\nworkflow = \"issue-to-pr\"\n\n"
        "[target]\nissue_number = 872\ndepends_on = [870, 871]\ncloses = []\nfile_ownership = []\n\n"
        "[worktree]\npath = \"/tmp/wt\"\n"
    )
    result = await parse_agent_task(tmp_path)
    assert result is not None
    assert result.depends_on == [870, 871]


@pytest.mark.anyio
async def test_parse_agent_task_issue_queue_populated(tmp_path: Path) -> None:
    """parse_agent_task populates issue_queue as list[IssueSub] from [[issue_queue]]."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text(
        '[task]\nworkflow = "coordinator"\n\n'
        "[spawn]\nmode = \"coordinator\"\nsub_agents = true\n\n"
        "[[issue_queue]]\n"
        "number = 870\n"
        'title = "MCP layer + schema tools"\n'
        'role = "python-developer"\n'
        'cognitive_arch = "turing:python"\n'
        "depends_on = []\n"
        'file_ownership = ["agentception/mcp/"]\n\n'
        "[[issue_queue]]\n"
        "number = 871\n"
        'title = "Plan tools"\n'
        'role = "python-developer"\n'
        'cognitive_arch = "turing:python"\n'
        "depends_on = [870]\n"
        "file_ownership = []\n"
    )
    result = await parse_agent_task(tmp_path)
    assert result is not None
    assert len(result.issue_queue) == 2
    first = result.issue_queue[0]
    assert isinstance(first, IssueSub)
    assert first.number == 870
    assert first.depends_on == []
    second = result.issue_queue[1]
    assert second.number == 871
    assert second.depends_on == [870]


@pytest.mark.anyio
async def test_parse_agent_task_output_section(tmp_path: Path) -> None:
    """parse_agent_task extracts output.draft_id and output.path from [output] section."""
    output_file = tmp_path / ".plan-output.yaml"
    task_file = tmp_path / ".agent-task"
    task_file.write_text(
        '[task]\nworkflow = "plan-spec"\n\n'
        "[spawn]\nmode = \"single\"\nsub_agents = false\n\n"
        "[output]\n"
        f'path = "{output_file}"\n'
        'draft_id = "abc-123-def"\n'
        'format = "yaml"\n'
    )
    result = await parse_agent_task(tmp_path)
    assert result is not None
    assert result.draft_id == "abc-123-def"
    assert result.output_path == str(output_file)


@pytest.mark.anyio
async def test_parse_agent_task_closes_issues_multi(tmp_path: Path) -> None:
    """parse_agent_task parses TOML array closes into closes_issues as list[int]."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text(
        '[task]\nworkflow = "issue-to-pr"\n\n'
        "[target]\nissue_number = 610\ncloses = [610, 611, 612]\ndepends_on = []\nfile_ownership = []\n"
    )
    result = await parse_agent_task(tmp_path)
    assert result is not None
    assert result.closes_issues == [610, 611, 612]


@pytest.mark.anyio
async def test_parse_agent_task_empty_sections_graceful(tmp_path: Path) -> None:
    """parse_agent_task handles missing optional sections gracefully."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text('[task]\nworkflow = "issue-to-pr"\n')
    result = await parse_agent_task(tmp_path)
    assert result is not None
    assert result.task == "issue-to-pr"
    assert result.role is None
    assert result.gh_repo is None
    assert result.batch_id is None
    assert result.depends_on == []
    assert result.closes_issues == []
    assert result.issue_queue == []
    assert result.pr_queue == []


# ── list_active_worktrees ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_active_worktrees_empty(tmp_path: Path) -> None:
    """list_active_worktrees returns an empty list when no worktrees have task files."""
    with patch("agentception.readers.worktrees.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        result = await list_active_worktrees()
    assert result == []


@pytest.mark.anyio
async def test_list_active_worktrees_nonexistent_dir() -> None:
    """list_active_worktrees returns an empty list when the worktrees directory is absent."""
    with patch("agentception.readers.worktrees.settings") as mock_settings:
        mock_settings.worktrees_dir = Path("/nonexistent/worktrees/dir")
        result = await list_active_worktrees()
    assert result == []


@pytest.mark.anyio
async def test_list_active_worktrees_one_active(tmp_path: Path, issue_task_content: str) -> None:
    """list_active_worktrees returns one TaskFile for a single worktree with a task file."""
    wt_dir = tmp_path / "issue-610"
    wt_dir.mkdir()
    (wt_dir / ".agent-task").write_text(issue_task_content)

    with patch("agentception.readers.worktrees.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        result = await list_active_worktrees()

    assert len(result) == 1
    assert result[0].issue_number == 610
    assert result[0].task == "issue-to-pr"


@pytest.mark.anyio
async def test_list_active_worktrees_skips_dirs_without_task(tmp_path: Path) -> None:
    """list_active_worktrees silently skips subdirectories that lack .agent-task."""
    (tmp_path / "stale-worktree").mkdir()
    (tmp_path / "other-dir").mkdir()

    with patch("agentception.readers.worktrees.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        result = await list_active_worktrees()

    assert result == []


@pytest.mark.anyio
async def test_list_active_worktrees_multiple(
    tmp_path: Path, issue_task_content: str, pr_review_task_content: str
) -> None:
    """list_active_worktrees returns one entry per valid worktree."""
    wt1 = tmp_path / "issue-610"
    wt1.mkdir()
    (wt1 / ".agent-task").write_text(issue_task_content)

    wt2 = tmp_path / "pr-642"
    wt2.mkdir()
    (wt2 / ".agent-task").write_text(pr_review_task_content)

    with patch("agentception.readers.worktrees.settings") as mock_settings:
        mock_settings.worktrees_dir = tmp_path
        result = await list_active_worktrees()

    assert len(result) == 2
    issue_numbers = {tf.issue_number for tf in result}
    pr_numbers = {tf.pr_number for tf in result}
    assert 610 in issue_numbers
    assert 642 in pr_numbers


# ── worktree_last_commit_time ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_worktree_last_commit_time_no_git_returns_zero(tmp_path: Path) -> None:
    """worktree_last_commit_time returns 0.0 for a non-git directory."""
    result = await worktree_last_commit_time(tmp_path)
    assert result == 0.0


@pytest.mark.anyio
async def test_worktree_last_commit_time_git_output_parsed(tmp_path: Path) -> None:
    """worktree_last_commit_time parses git log --format=%ct output into a float."""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"1740000000\n", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await worktree_last_commit_time(tmp_path)

    assert isinstance(result, float)
    assert result == 1_740_000_000.0


# ── TaskFile model ─────────────────────────────────────────────────────────────


def test_task_file_closes_issues_defaults_to_empty_list() -> None:
    """TaskFile.closes_issues defaults to [] when not provided."""
    tf = TaskFile(task="issue-to-pr")
    assert tf.closes_issues == []


def test_task_file_pr_number_field() -> None:
    """TaskFile.pr_number is available and optional."""
    tf = TaskFile(task="pr-review", pr_number=642)
    assert tf.pr_number == 642


def test_task_file_spawn_mode_field() -> None:
    """TaskFile.spawn_mode is available and optional."""
    tf = TaskFile(task="pr-review", spawn_mode="chain")
    assert tf.spawn_mode == "chain"


def test_task_file_merge_after_field() -> None:
    """TaskFile.merge_after is available and optional."""
    tf = TaskFile(task="issue-to-pr", merge_after="other-branch")
    assert tf.merge_after == "other-branch"
