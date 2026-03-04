"""Tests for role version tracking (AC-503).

Covers:
- record_version_bump appends a new entry to history
- get_version_for_batch returns the correct SHA for a given batch timestamp
- role-versions.json is created on first write when it does not exist
- GET /api/roles/{slug}/versions returns structured version history
- GET /api/roles/{slug}/versions returns empty history for unrecorded slug

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_agentception_role_versions.py -v
"""
from __future__ import annotations

import json
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.intelligence.role_versions import (
    get_version_for_batch,
    read_role_versions,
    record_version_bump,
    write_role_versions,
)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Temp directory mimicking a minimal .cursor/ structure for version tracking tests."""
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir(parents=True)
    roles_dir = cursor_dir / "roles"
    roles_dir.mkdir()
    (roles_dir / "cto.md").write_text("# CTO\nLeads engineering.", encoding="utf-8")
    return tmp_path


# ── record_version_bump ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_record_version_bump_appends_to_history(tmp_path: Path) -> None:
    """record_version_bump must append a new entry to the role's history list."""
    with patch("agentception.intelligence.role_versions.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        # Bootstrap an empty role-versions.json
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(exist_ok=True)
        (cursor_dir / "role-versions.json").write_text(
            json.dumps({"versions": {}, "ab_mode": {"enabled": False}}),
            encoding="utf-8",
        )

        sha_1 = "aabbccdd" * 5  # 40 chars
        await record_version_bump("cto", sha_1)

        data = await read_role_versions()

    versions = data["versions"]
    assert isinstance(versions, dict)
    cto_entry = versions.get("cto")
    assert isinstance(cto_entry, dict)
    history: list[dict[str, object]] = cto_entry.get("history", [])
    assert len(history) == 1
    assert history[0]["sha"] == sha_1
    assert history[0]["label"] == "v1"
    assert isinstance(history[0]["timestamp"], int)
    assert cto_entry["current"] == "v1"


@pytest.mark.anyio
async def test_record_version_bump_idempotent_on_same_sha(tmp_path: Path) -> None:
    """record_version_bump must not add a duplicate entry when called twice with the same SHA."""
    with patch("agentception.intelligence.role_versions.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(exist_ok=True)
        (cursor_dir / "role-versions.json").write_text(
            json.dumps({"versions": {}, "ab_mode": {"enabled": False}}),
            encoding="utf-8",
        )

        sha = "deadbeef" * 5
        await record_version_bump("cto", sha)
        await record_version_bump("cto", sha)  # duplicate — must be skipped

        data = await read_role_versions()

    versions = data["versions"]
    assert isinstance(versions, dict)
    cto = versions.get("cto")
    assert isinstance(cto, dict)
    cto_history: list[object] = cto.get("history", [])
    assert len(cto_history) == 1, "Duplicate SHA must not produce two history entries"


# ── get_version_for_batch ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_version_for_batch_returns_correct_sha(tmp_path: Path) -> None:
    """get_version_for_batch must return the version label active at the batch timestamp."""
    now = int(time.time())
    # v1 was committed 2 hours ago, v2 was committed 1 hour ago.
    history = [
        {"sha": "sha111" * 6 + "1111", "label": "v1", "timestamp": now - 7200},
        {"sha": "sha222" * 6 + "2222", "label": "v2", "timestamp": now - 3600},
    ]
    scaffold = {
        "versions": {"cto": {"current": "v2", "history": history}},
        "ab_mode": {"enabled": False},
    }

    with patch("agentception.intelligence.role_versions.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(exist_ok=True)
        (cursor_dir / "role-versions.json").write_text(
            json.dumps(scaffold), encoding="utf-8"
        )

        # A batch that started 90 minutes ago — between v1 and v2 → should get v1.
        batch_ts = now - 5400
        # Pass as integer string (accepted by _parse_batch_timestamp fallback).
        result = await get_version_for_batch("cto", str(batch_ts))

    assert result == "v1", f"Expected v1 (pre-v2 batch), got {result!r}"


@pytest.mark.anyio
async def test_get_version_for_batch_returns_none_for_unknown_slug(tmp_path: Path) -> None:
    """get_version_for_batch must return None when the slug has no recorded history."""
    with patch("agentception.intelligence.role_versions.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(exist_ok=True)
        (cursor_dir / "role-versions.json").write_text(
            json.dumps({"versions": {}, "ab_mode": {"enabled": False}}),
            encoding="utf-8",
        )

        result = await get_version_for_batch("nonexistent-slug", str(int(time.time())))

    assert result is None


# ── write creates file on first write ─────────────────────────────────────────


@pytest.mark.anyio
async def test_role_versions_file_created_on_first_write(tmp_path: Path) -> None:
    """write_role_versions must create role-versions.json when it does not yet exist."""
    with patch("agentception.intelligence.role_versions.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        # Ensure the .cursor dir does not exist yet.
        target = tmp_path / ".cursor" / "role-versions.json"
        assert not target.exists(), "Pre-condition: file must not exist before first write"

        scaffold: dict[str, object] = {"versions": {}, "ab_mode": {"enabled": False}}
        await write_role_versions(scaffold)

    assert target.exists(), "role-versions.json must be created on first write"
    content = json.loads(target.read_text(encoding="utf-8"))
    assert content == scaffold


# ── GET /api/roles/{slug}/versions ────────────────────────────────────────────


def test_role_versions_api_returns_history(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles/{slug}/versions must return structured version history."""
    history = [
        {"sha": "abc123" * 6 + "abcd", "label": "v1", "timestamp": 1700000000},
    ]
    versions_data = {
        "versions": {"cto": {"current": "v1", "history": history}},
        "ab_mode": {"enabled": False},
    }

    with patch("agentception.routes.roles.settings") as mock_route_settings:
        mock_route_settings.repo_dir = tmp_repo
        with patch(
            "agentception.intelligence.role_versions.settings"
        ) as mock_intel_settings:
            mock_intel_settings.repo_dir = tmp_repo
            (tmp_repo / ".cursor" / "role-versions.json").write_text(
                json.dumps(versions_data), encoding="utf-8"
            )
            response = client.get("/api/roles/cto/versions")

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert data["versions"]["current"] == "v1"
    assert len(data["versions"]["history"]) == 1
    assert data["versions"]["history"][0]["label"] == "v1"


def test_role_versions_api_returns_empty_for_unrecorded_slug(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles/{slug}/versions returns empty history for a slug with no commits."""
    versions_data = {"versions": {}, "ab_mode": {"enabled": False}}

    with patch("agentception.routes.roles.settings") as mock_route_settings:
        mock_route_settings.repo_dir = tmp_repo
        with patch(
            "agentception.intelligence.role_versions.settings"
        ) as mock_intel_settings:
            mock_intel_settings.repo_dir = tmp_repo
            (tmp_repo / ".cursor" / "role-versions.json").write_text(
                json.dumps(versions_data), encoding="utf-8"
            )
            response = client.get("/api/roles/cto/versions")

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert data["versions"]["history"] == []


def test_role_versions_api_unknown_slug_404(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles/{slug}/versions must return 404 for an unknown slug."""
    response = client.get("/api/roles/nonexistent-role/versions")
    assert response.status_code == 404
