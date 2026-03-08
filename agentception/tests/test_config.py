"""Tests for agentception/config.py — AgentCeptionSettings and _resolve_project.

Covers:
  - _resolve_project() applies gh_repo, repo_dir, and worktrees_dir from a
    matching project entry and is a no-op for every degenerate input.
  - _resolve_project() expands leading ``~/`` in worktrees_dir.
  - _resolve_project() leaves env-var defaults untouched for absent fields.
  - AgentCeptionSettings.ac_dir returns repo_dir / ".agentception".
  - AgentCeptionSettings._apply_active_project is a no-op when the config
    file is absent, has no active_project, or contains invalid JSON.
  - AgentCeptionSettings._apply_active_project applies the active project
    when the file is well-formed.
  - AgentCeptionSettings.reload() mirrors the validator's behaviour and
    handles every error path gracefully.

Run targeted:
    pytest agentception/tests/test_config.py -v
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from agentception.config import AgentCeptionSettings, _resolve_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> AgentCeptionSettings:
    """Return a fresh settings instance rooted at *tmp_path* with no config file."""
    return AgentCeptionSettings(repo_dir=tmp_path)


def _write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    """Write *data* as pipeline-config.json inside *tmp_path*/.agentception/."""
    ac = tmp_path / ".agentception"
    ac.mkdir(parents=True, exist_ok=True)
    path = ac / "pipeline-config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Unit tests — _resolve_project
# ---------------------------------------------------------------------------


def test_resolve_project_no_op_when_active_project_absent() -> None:
    """_resolve_project is a no-op when active_project key is missing."""
    s = AgentCeptionSettings(repo_dir=Path("/tmp"))
    original_repo = s.gh_repo
    _resolve_project({"projects": [{"name": "X", "gh_repo": "acme/x"}]}, s)
    assert s.gh_repo == original_repo


def test_resolve_project_no_op_when_projects_not_a_list() -> None:
    """_resolve_project is a no-op when projects is not a list."""
    s = AgentCeptionSettings(repo_dir=Path("/tmp"))
    original_repo = s.gh_repo
    _resolve_project({"active_project": "X", "projects": "bad"}, s)
    assert s.gh_repo == original_repo


def test_resolve_project_no_op_when_no_project_matches() -> None:
    """_resolve_project is a no-op when active_project name matches no entry."""
    s = AgentCeptionSettings(repo_dir=Path("/tmp"))
    original_repo = s.gh_repo
    _resolve_project(
        {
            "active_project": "Missing",
            "projects": [{"name": "Present", "gh_repo": "acme/present"}],
        },
        s,
    )
    assert s.gh_repo == original_repo


def test_resolve_project_applies_gh_repo(tmp_path: Path) -> None:
    """_resolve_project sets gh_repo from the matching project entry."""
    s = _make_settings(tmp_path)
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [{"name": "Mine", "gh_repo": "acme/mine"}],
        },
        s,
    )
    assert s.gh_repo == "acme/mine"


def test_resolve_project_applies_repo_dir_when_present(tmp_path: Path) -> None:
    """_resolve_project updates repo_dir when the project entry provides it."""
    s = _make_settings(tmp_path)
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [
                {"name": "Mine", "gh_repo": "acme/mine", "repo_dir": str(tmp_path / "sub")}
            ],
        },
        s,
    )
    assert s.repo_dir == tmp_path / "sub"


def test_resolve_project_leaves_repo_dir_when_absent(tmp_path: Path) -> None:
    """_resolve_project does not touch repo_dir when the project entry omits it."""
    s = _make_settings(tmp_path)
    original = s.repo_dir
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [{"name": "Mine", "gh_repo": "acme/mine"}],
        },
        s,
    )
    assert s.repo_dir == original


def test_resolve_project_applies_worktrees_dir_when_present(tmp_path: Path) -> None:
    """_resolve_project updates worktrees_dir when the project entry provides it."""
    s = _make_settings(tmp_path)
    wt = str(tmp_path / "worktrees")
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [
                {"name": "Mine", "gh_repo": "acme/mine", "worktrees_dir": wt}
            ],
        },
        s,
    )
    assert s.worktrees_dir == Path(wt)


def test_resolve_project_leaves_worktrees_dir_when_absent(tmp_path: Path) -> None:
    """_resolve_project does not touch worktrees_dir when the project entry omits it."""
    s = _make_settings(tmp_path)
    original = s.worktrees_dir
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [{"name": "Mine", "gh_repo": "acme/mine"}],
        },
        s,
    )
    assert s.worktrees_dir == original


def test_resolve_project_expands_tilde_in_worktrees_dir(tmp_path: Path) -> None:
    """_resolve_project expands a leading ~/ in worktrees_dir to the home directory."""
    s = _make_settings(tmp_path)
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [
                {"name": "Mine", "gh_repo": "acme/mine", "worktrees_dir": "~/.agentception/wt"}
            ],
        },
        s,
    )
    assert s.worktrees_dir == Path.home() / ".agentception/wt"


def test_resolve_project_only_first_matching_project_applied(tmp_path: Path) -> None:
    """_resolve_project stops after the first matching entry (break semantics)."""
    s = _make_settings(tmp_path)
    _resolve_project(
        {
            "active_project": "Mine",
            "projects": [
                {"name": "Mine", "gh_repo": "acme/first"},
                {"name": "Mine", "gh_repo": "acme/second"},
            ],
        },
        s,
    )
    assert s.gh_repo == "acme/first"


# ---------------------------------------------------------------------------
# Unit tests — AgentCeptionSettings.ac_dir
# ---------------------------------------------------------------------------


def test_ac_dir_is_repo_dir_dot_agentception(tmp_path: Path) -> None:
    """ac_dir property returns repo_dir / '.agentception'."""
    s = _make_settings(tmp_path)
    assert s.ac_dir == tmp_path / ".agentception"


def test_ac_dir_tracks_repo_dir(tmp_path: Path) -> None:
    """ac_dir reflects the current repo_dir even after mutation."""
    s = _make_settings(tmp_path)
    new_root = tmp_path / "other"
    s.repo_dir = new_root
    assert s.ac_dir == new_root / ".agentception"


# ---------------------------------------------------------------------------
# Unit tests — AgentCeptionSettings._apply_active_project (via constructor)
# ---------------------------------------------------------------------------


def test_apply_active_project_no_op_when_config_absent(tmp_path: Path) -> None:
    """Validator is a no-op when pipeline-config.json does not exist."""
    s = _make_settings(tmp_path)
    assert s.gh_repo == "cgcardona/agentception"


def test_apply_active_project_no_op_when_active_project_key_missing(
    tmp_path: Path,
) -> None:
    """Validator is a no-op when the config file has no active_project key."""
    _write_config(tmp_path, {"projects": [{"name": "X", "gh_repo": "acme/x"}]})
    s = _make_settings(tmp_path)
    assert s.gh_repo == "cgcardona/agentception"


def test_apply_active_project_no_op_when_config_is_json_array(tmp_path: Path) -> None:
    """Validator is a no-op when the config file contains a JSON array instead of object."""
    ac = tmp_path / ".agentception"
    ac.mkdir(parents=True)
    (ac / "pipeline-config.json").write_text("[1, 2, 3]", encoding="utf-8")
    s = _make_settings(tmp_path)
    assert s.gh_repo == "cgcardona/agentception"


def test_apply_active_project_applies_matching_project(tmp_path: Path) -> None:
    """Validator applies gh_repo and worktrees_dir from the active project."""
    wt = str(tmp_path / "my-worktrees")
    _write_config(
        tmp_path,
        {
            "active_project": "My Project",
            "projects": [
                {
                    "name": "My Project",
                    "gh_repo": "acme/myproject",
                    "repo_dir": str(tmp_path),
                    "worktrees_dir": wt,
                }
            ],
        },
    )
    s = _make_settings(tmp_path)
    assert s.gh_repo == "acme/myproject"
    assert s.worktrees_dir == Path(wt)


def test_apply_active_project_partial_entry_preserves_env_defaults(
    tmp_path: Path,
) -> None:
    """When the project entry has only gh_repo, repo_dir and worktrees_dir are untouched."""
    s_default = _make_settings(tmp_path)
    original_worktrees = s_default.worktrees_dir

    _write_config(
        tmp_path,
        {
            "active_project": "Slim",
            "projects": [{"name": "Slim", "gh_repo": "acme/slim"}],
        },
    )
    s = _make_settings(tmp_path)
    assert s.gh_repo == "acme/slim"
    assert s.worktrees_dir == original_worktrees


# ---------------------------------------------------------------------------
# Unit tests — AgentCeptionSettings.reload()
# ---------------------------------------------------------------------------


def test_reload_no_op_when_config_absent(tmp_path: Path) -> None:
    """reload() returns immediately when pipeline-config.json does not exist."""
    s = _make_settings(tmp_path)
    original = s.gh_repo
    s.reload()
    assert s.gh_repo == original


def test_reload_applies_new_active_project(tmp_path: Path) -> None:
    """reload() picks up a changed active_project from the config file."""
    s = _make_settings(tmp_path)
    _write_config(
        tmp_path,
        {
            "active_project": "New",
            "projects": [{"name": "New", "gh_repo": "acme/new"}],
        },
    )
    s.reload()
    assert s.gh_repo == "acme/new"


def test_reload_no_op_on_malformed_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """reload() logs a warning and returns when the config file is not valid JSON."""
    ac = tmp_path / ".agentception"
    ac.mkdir(parents=True)
    (ac / "pipeline-config.json").write_text("this is not json", encoding="utf-8")
    s = _make_settings(tmp_path)
    original = s.gh_repo
    with caplog.at_level(logging.WARNING, logger="agentception.config"):
        s.reload()
    assert s.gh_repo == original
    assert any("Could not read pipeline-config.json" in r.message for r in caplog.records)


def test_reload_no_op_when_json_not_a_dict(tmp_path: Path) -> None:
    """reload() is a no-op when the config file parses to a non-dict value."""
    ac = tmp_path / ".agentception"
    ac.mkdir(parents=True)
    (ac / "pipeline-config.json").write_text("[1, 2, 3]", encoding="utf-8")
    s = _make_settings(tmp_path)
    original = s.gh_repo
    s.reload()
    assert s.gh_repo == original


def test_reload_logs_debug_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """reload() emits a debug log line showing the new gh_repo and repo_dir."""
    _write_config(
        tmp_path,
        {
            "active_project": "New",
            "projects": [
                {"name": "New", "gh_repo": "acme/new", "repo_dir": str(tmp_path)}
            ],
        },
    )
    s = _make_settings(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="agentception.config"):
        s.reload()
    assert any("reloaded" in r.message for r in caplog.records)
