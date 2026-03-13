from __future__ import annotations

"""Tests for pipeline-config.json reader/writer and API endpoints.

Covers:
- read_pipeline_config returns defaults when file is absent
- write_pipeline_config persists values and returns them
- GET /api/config returns current config
- PUT /api/config validates schema and persists changes
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import PipelineConfig
from agentception.readers.pipeline_config import (
    _DEFAULTS,
    read_pipeline_config,
    switch_project,
    write_pipeline_config,
)

client = TestClient(app)

# Convenience: a minimal valid coordinator_limits dict
_COORD_LIMITS: dict[str, int] = {"engineering-coordinator": 1, "qa-coordinator": 1}


# ---------------------------------------------------------------------------
# Unit tests for read_pipeline_config
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_pipeline_config_returns_defaults_when_file_absent(
    tmp_path: Path,
) -> None:
    """read_pipeline_config returns the built-in defaults when the config file does not exist."""
    missing = tmp_path / "nonexistent" / "pipeline-config.json"
    with patch("agentception.readers.pipeline_config._config_path", return_value=missing):
        result = await read_pipeline_config()

    assert result.coordinator_limits == _DEFAULTS["coordinator_limits"]
    assert result.pool_size == _DEFAULTS["pool_size"]
    assert result.active_labels_order == []


@pytest.mark.anyio
async def test_read_pipeline_config_reads_file_when_present(tmp_path: Path) -> None:
    """read_pipeline_config parses the config file and returns a validated PipelineConfig."""
    config_file = tmp_path / "pipeline-config.json"
    custom = {
        "coordinator_limits": {"engineering-coordinator": 2, "qa-coordinator": 3},
        "pool_size": 6,
        "active_labels_order": ["agentception/0-scaffold", "agentception/1-controls"],
    }
    config_file.write_text(json.dumps(custom), encoding="utf-8")

    with patch("agentception.readers.pipeline_config._config_path", return_value=config_file):
        result = await read_pipeline_config()

    assert result.coordinator_limits == {"engineering-coordinator": 2, "qa-coordinator": 3}
    assert result.pool_size == 6
    assert result.active_labels_order == ["agentception/0-scaffold", "agentception/1-controls"]


# ---------------------------------------------------------------------------
# Unit tests for write_pipeline_config
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_write_pipeline_config_persists(tmp_path: Path) -> None:
    """write_pipeline_config writes the config to disk and returns it."""
    config_file = tmp_path / ".cursor" / "pipeline-config.json"
    config = PipelineConfig(
        coordinator_limits=_COORD_LIMITS,
        pool_size=4,
        active_labels_order=["agentception/0-scaffold"],
    )

    with patch("agentception.readers.pipeline_config._config_path", return_value=config_file):
        returned = await write_pipeline_config(config)

    assert returned == config
    assert config_file.exists()
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk["coordinator_limits"] == _COORD_LIMITS
    assert on_disk["active_labels_order"] == ["agentception/0-scaffold"]


@pytest.mark.anyio
async def test_write_pipeline_config_creates_parent_dirs(tmp_path: Path) -> None:
    """write_pipeline_config creates intermediate directories automatically."""
    nested = tmp_path / "deep" / "nested" / "pipeline-config.json"
    config = PipelineConfig(
        coordinator_limits=_COORD_LIMITS,
        pool_size=4,
        active_labels_order=[],
    )
    with patch("agentception.readers.pipeline_config._config_path", return_value=nested):
        await write_pipeline_config(config)

    assert nested.exists()


# ---------------------------------------------------------------------------
# API integration tests — GET /api/config
# ---------------------------------------------------------------------------


def test_config_api_get_returns_defaults() -> None:
    """GET /api/config returns built-in defaults when config file is absent."""
    default_config = PipelineConfig.model_validate(_DEFAULTS)
    with patch(
        "agentception.routes.api.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=default_config,
    ):
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["coordinator_limits"] == _DEFAULTS["coordinator_limits"]
    assert body["pool_size"] == _DEFAULTS["pool_size"]
    # active_labels_order defaults to [] when not specified in the config file.
    assert body["active_labels_order"] == []


def test_config_api_get_returns_custom_values() -> None:
    """GET /api/config returns the current values from the config file."""
    custom_config = PipelineConfig(
        coordinator_limits={"engineering-coordinator": 2, "qa-coordinator": 2},
        pool_size=8,
        active_labels_order=["agentception/0-scaffold"],
    )
    with patch(
        "agentception.routes.api.config.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=custom_config,
    ):
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["coordinator_limits"] == {"engineering-coordinator": 2, "qa-coordinator": 2}
    assert body["pool_size"] == 8
    assert body["active_labels_order"] == ["agentception/0-scaffold"]


# ---------------------------------------------------------------------------
# API integration tests — PUT /api/config
# ---------------------------------------------------------------------------


def test_config_api_put_validates_schema_and_persists() -> None:
    """PUT /api/config validates the body and returns the saved config."""
    payload = {
        "coordinator_limits": {"engineering-coordinator": 1, "qa-coordinator": 1},
        "pool_size": 4,
        "active_labels_order": [
            "agentception/0-scaffold",
            "agentception/1-controls",
        ],
    }
    saved_config = PipelineConfig.model_validate(payload)
    with patch(
        "agentception.routes.api.config.write_pipeline_config",
        new_callable=AsyncMock,
        return_value=saved_config,
    ):
        response = client.put("/api/config", json=payload)

    assert response.status_code == 200
    assert response.json() == saved_config.model_dump()


def test_pipeline_config_rejects_zero_pool_size() -> None:
    """PUT /api/config with pool_size=0 must return 422."""
    payload = {
        "coordinator_limits": _COORD_LIMITS,
        "pool_size": 0,
        "active_labels_order": [],
    }
    response = client.put("/api/config", json=payload)
    assert response.status_code == 422


def test_pipeline_config_rejects_negative_pool_size() -> None:
    """PUT /api/config with pool_size=-1 must return 422."""
    payload = {
        "coordinator_limits": _COORD_LIMITS,
        "pool_size": -1,
        "active_labels_order": [],
    }
    response = client.put("/api/config", json=payload)
    assert response.status_code == 422


def test_config_api_put_rejects_missing_fields() -> None:
    """PUT /api/config returns 422 when required fields are absent."""
    # pool_size is required (must be > 0); omitting coordinator_limits is fine
    # (it has a default). But sending an invalid pool_size type triggers 422.
    incomplete = {"coordinator_limits": _COORD_LIMITS, "pool_size": "bad"}
    response = client.put("/api/config", json=incomplete)
    assert response.status_code == 422


def test_config_api_put_rejects_wrong_types() -> None:
    """PUT /api/config returns 422 when field types are wrong."""
    bad = {
        "coordinator_limits": "not-a-dict",  # should be dict[str, int]
        "pool_size": 4,
        "active_labels_order": [],
    }
    response = client.put("/api/config", json=bad)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# AC-601: Multi-repo config schema + project switcher — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_settings_reads_active_project(tmp_path: Path) -> None:
    """AgentCeptionSettings applies the active project's paths over env-var defaults."""
    from agentception.config import AgentCeptionSettings

    cursor_dir = tmp_path / ".agentception"
    cursor_dir.mkdir(parents=True)
    config_data = {
        "coordinator_limits": _COORD_LIMITS,
        "pool_size": 4,
        "active_labels_order": [],
        "active_project": "Other Repo",
        "projects": [
            {
                "name": "Example Project",
                "gh_repo": "cgcardona/agentception",
                "repo_dir": str(tmp_path),
                "worktrees_dir": "~/.agentception/worktrees/agentception",
                "cursor_project_id": "Users-example-dev-cgcardona-example-project",
                "active_labels_order": [],
            },
            {
                "name": "Other Repo",
                "gh_repo": "acme/other",
                "repo_dir": str(tmp_path / "other"),
                "worktrees_dir": str(tmp_path / "other-worktrees"),
                "cursor_project_id": "other-project-id",
                "active_labels_order": [],
            },
        ],
    }
    (cursor_dir / "pipeline-config.json").write_text(
        json.dumps(config_data), encoding="utf-8"
    )

    s = AgentCeptionSettings(repo_dir=tmp_path)
    assert s.gh_repo == "acme/other"
    assert s.worktrees_dir == tmp_path / "other-worktrees"


@pytest.mark.anyio
async def test_switch_project_updates_config(tmp_path: Path) -> None:
    """switch_project() sets active_project and persists the updated config."""
    config_file = tmp_path / "pipeline-config.json"
    initial = {
        "coordinator_limits": _COORD_LIMITS,
        "pool_size": 4,
        "active_labels_order": [],
        "active_project": "Example Project",
        "projects": [
            {
                "name": "Example Project",
                "gh_repo": "cgcardona/agentception",
                "repo_dir": "/dev/example-project",
                "worktrees_dir": "~/.agentception/worktrees/agentception",
                "cursor_project_id": "example-project-id",
                "active_labels_order": [],
            },
            {
                "name": "Other Repo",
                "gh_repo": "acme/other",
                "repo_dir": "/dev/other",
                "worktrees_dir": "~/.agentception/worktrees/other",
                "cursor_project_id": "other-id",
                "active_labels_order": [],
            },
        ],
    }
    config_file.write_text(json.dumps(initial), encoding="utf-8")

    with patch("agentception.readers.pipeline_config._config_path", return_value=config_file):
        result = await switch_project("Other Repo")

    assert result.active_project == "Other Repo"
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk["active_project"] == "Other Repo"


@pytest.mark.anyio
async def test_switch_project_rejects_unknown_name(tmp_path: Path) -> None:
    """switch_project() raises ValueError for a project name not in projects list."""
    config_file = tmp_path / "pipeline-config.json"
    config = {
        "coordinator_limits": _COORD_LIMITS,
        "pool_size": 4,
        "active_labels_order": [],
        "active_project": "Example Project",
        "projects": [
            {
                "name": "Example Project",
                "gh_repo": "cgcardona/agentception",
                "repo_dir": "/dev/example-project",
                "worktrees_dir": "~/.agentception/worktrees/agentception",
                "cursor_project_id": "example-project-id",
                "active_labels_order": [],
            },
        ],
    }
    config_file.write_text(json.dumps(config), encoding="utf-8")

    with patch("agentception.readers.pipeline_config._config_path", return_value=config_file):
        with pytest.raises(ValueError, match="Unknown project"):
            await switch_project("Nonexistent Project")


def test_switch_project_api_returns_404_for_unknown_project() -> None:
    """POST /api/config/switch-project returns 404 when project_name is not in projects."""
    with patch(
        "agentception.routes.api.config.switch_project",
        new_callable=AsyncMock,
        side_effect=ValueError("Unknown project 'Nonexistent'. Available: []"),
    ):
        response = client.post(
            "/api/config/switch-project", json={"project_name": "Nonexistent"}
        )

    assert response.status_code == 404
    assert "Unknown project" in response.json()["detail"]


def test_switch_project_api_returns_updated_config() -> None:
    """POST /api/config/switch-project returns the updated PipelineConfig on success."""
    updated_config = PipelineConfig(
        coordinator_limits=_COORD_LIMITS,
        pool_size=4,
        active_labels_order=[],
        active_project="Other Repo",
        projects=[],
    )
    with patch(
        "agentception.routes.api.config.switch_project",
        new_callable=AsyncMock,
        return_value=updated_config,
    ):
        response = client.post(
            "/api/config/switch-project", json={"project_name": "Other Repo"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["active_project"] == "Other Repo"
