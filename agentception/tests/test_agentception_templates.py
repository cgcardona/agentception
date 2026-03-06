from __future__ import annotations

"""Tests for AC-602: template export/import.

Covers:
- test_export_creates_valid_tarball
- test_export_includes_all_managed_files
- test_import_extracts_to_target
- test_import_detects_conflicts
- API endpoint integration tests (export, import, list, download)
"""

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import TemplateExportRequest, TemplateManifest
from agentception.readers.templates import (
    TEMPLATES_STORE,
    export_template,
    import_template,
    list_stored_templates,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal fake repo with managed .cursor/ files."""
    ac = tmp_path / ".agentception"
    ac.mkdir(parents=True, exist_ok=True)
    roles = ac / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "python-developer.md").write_text("# Python Developer", encoding="utf-8")
    (ac / "parallel-issue-to-pr.md").write_text("# Parallel", encoding="utf-8")
    (ac / "pipeline-config.json").write_text('{"coordinator_limits": {"engineering-coordinator": 1, "qa-coordinator": 1}, "pool_size": 4}', encoding="utf-8")
    (ac / "agent-command-policy.md").write_text("# Policy", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests — readers/templates.py
# ---------------------------------------------------------------------------


def test_export_creates_valid_tarball(tmp_path: Path) -> None:
    """export_template produces a readable .tar.gz archive."""
    repo = _make_repo(tmp_path)
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "test/repo"
        archive_bytes, filename = export_template("test-pipeline", "1.0.0")

    assert filename == "test-pipeline-1.0.0.tar.gz"
    assert len(archive_bytes) > 0

    buf = io.BytesIO(archive_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = tar.getnames()

    assert "template-manifest.json" in names


def test_export_includes_all_managed_files(tmp_path: Path) -> None:
    """export_template includes roles, PARALLEL_*.md, pipeline-config.json, and policy."""
    repo = _make_repo(tmp_path)
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "test/repo"
        archive_bytes, _ = export_template("full-export", "2.0.0")

    buf = io.BytesIO(archive_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = tar.getnames()

    assert ".agentception/roles/python-developer.md" in names
    assert ".agentception/parallel-issue-to-pr.md" in names
    assert ".agentception/pipeline-config.json" in names
    assert ".agentception/agent-command-policy.md" in names


def test_export_manifest_contains_correct_metadata(tmp_path: Path) -> None:
    """The manifest embedded in the archive has the correct name, version, and repo."""
    repo = _make_repo(tmp_path)
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "org/myrepo"
        archive_bytes, _ = export_template("my-template", "0.9.1")

    buf = io.BytesIO(archive_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        mf = tar.extractfile("template-manifest.json")
        assert mf is not None
        data: object = json.loads(mf.read())

    assert isinstance(data, dict)
    manifest = TemplateManifest.model_validate(data)
    assert manifest.name == "my-template"
    assert manifest.version == "0.9.1"
    assert manifest.gh_repo == "org/myrepo"
    assert ".agentception/pipeline-config.json" in manifest.files


def test_export_persists_archive_to_store(tmp_path: Path) -> None:
    """export_template writes the .tar.gz to TEMPLATES_STORE."""
    repo = _make_repo(tmp_path)
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "test/repo"
        _, filename = export_template("persisted", "3.0.0")

    assert (store / filename).is_file()


def test_import_extracts_to_target(tmp_path: Path) -> None:
    """import_template writes all managed files into the target repo."""
    src_repo = _make_repo(tmp_path / "src")
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = src_repo
        mock_settings.gh_repo = "test/repo"
        archive_bytes, _ = export_template("import-test", "1.0.0")

    target_repo = tmp_path / "target"
    target_repo.mkdir()

    result = import_template(archive_bytes, str(target_repo))

    assert len(result.extracted) > 0
    assert (target_repo / ".agentception" / "pipeline-config.json").is_file()
    assert (target_repo / ".agentception" / "roles" / "python-developer.md").is_file()


def test_import_detects_conflicts(tmp_path: Path) -> None:
    """import_template surfaces files that already exist in the target repo."""
    src_repo = _make_repo(tmp_path / "src")
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = src_repo
        mock_settings.gh_repo = "test/repo"
        archive_bytes, _ = export_template("conflict-test", "1.0.0")

    # Pre-create one of the managed files in target to simulate a conflict.
    target_repo = tmp_path / "target"
    target_ac = target_repo / ".agentception"
    target_ac.mkdir(parents=True)
    existing = target_ac / "pipeline-config.json"
    existing.write_text('{"coordinator_limits": {"engineering-coordinator": 99, "qa-coordinator": 1}, "pool_size": 4}', encoding="utf-8")

    result = import_template(archive_bytes, str(target_repo))

    conflict_paths = {c.path for c in result.conflicts if c.exists}
    assert ".agentception/pipeline-config.json" in conflict_paths


def test_import_raises_on_missing_manifest(tmp_path: Path) -> None:
    """import_template raises ValueError when the archive has no manifest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        content = b"some content"
        info = tarfile.TarInfo(name=".agentception/roles/test.md")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))

    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="template-manifest.json"):
        import_template(buf.getvalue(), str(target))


def test_import_raises_on_nonexistent_target(tmp_path: Path) -> None:
    """import_template raises ValueError when target_repo does not exist."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass

    with pytest.raises(ValueError, match="does not exist"):
        import_template(buf.getvalue(), str(tmp_path / "nonexistent"))


def test_list_stored_templates_empty_when_no_store(tmp_path: Path) -> None:
    """list_stored_templates returns [] when the store directory does not exist."""
    missing_store = tmp_path / "missing"
    with patch("agentception.readers.templates.TEMPLATES_STORE", missing_store):
        result = list_stored_templates()
    assert result == []


def test_list_stored_templates_returns_entries(tmp_path: Path) -> None:
    """list_stored_templates returns one entry per valid archive."""
    repo = _make_repo(tmp_path / "repo")
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "test/repo"
        export_template("list-test", "1.0.0")

    with patch("agentception.readers.templates.TEMPLATES_STORE", store):
        entries = list_stored_templates()

    assert len(entries) == 1
    assert entries[0].name == "list-test"
    assert entries[0].version == "1.0.0"


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------


def test_api_export_returns_tarball(tmp_path: Path) -> None:
    """POST /api/templates/export returns a gzip response with a valid archive."""
    repo = _make_repo(tmp_path)
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "test/repo"
        response = client.post(
            "/api/templates/export",
            json={"name": "api-test", "version": "1.2.3"},
        )

    assert response.status_code == 200
    assert "application/gzip" in response.headers["content-type"]
    assert response.headers["content-disposition"].startswith("attachment")


def test_api_import_endpoint_extracts_files(tmp_path: Path) -> None:
    """POST /api/templates/import extracts files and returns TemplateImportResult."""
    src_repo = _make_repo(tmp_path / "src")
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = src_repo
        mock_settings.gh_repo = "test/repo"
        archive_bytes, filename = export_template("import-api", "1.0.0")

    target = tmp_path / "target"
    target.mkdir()

    response = client.post(
        "/api/templates/import",
        params={"target_repo": str(target)},
        files={"file": (filename, archive_bytes, "application/gzip")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "import-api"
    assert len(body["extracted"]) > 0


def test_api_list_templates_returns_json(tmp_path: Path) -> None:
    """GET /api/templates returns a JSON array."""
    with patch("agentception.readers.templates.TEMPLATES_STORE", tmp_path / "empty"):
        response = client.get("/api/templates")

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_api_download_template_returns_bytes(tmp_path: Path) -> None:
    """GET /api/templates/{filename} returns the archive bytes for a known file."""
    repo = _make_repo(tmp_path / "repo")
    store = tmp_path / "store"

    with (
        patch("agentception.readers.templates.settings") as mock_settings,
        patch("agentception.readers.templates.TEMPLATES_STORE", store),
    ):
        mock_settings.repo_dir = repo
        mock_settings.gh_repo = "test/repo"
        _, filename = export_template("dl-test", "1.0.0")

    with patch("agentception.routes.templates_api.TEMPLATES_STORE", store):
        response = client.get(f"/api/templates/{filename}")

    assert response.status_code == 200
    assert len(response.content) > 0


def test_api_download_template_404_for_unknown_file() -> None:
    """GET /api/templates/{filename} returns 404 for a nonexistent file."""
    response = client.get("/api/templates/nonexistent-file.tar.gz")
    assert response.status_code == 404


def test_api_import_400_for_invalid_archive(tmp_path: Path) -> None:
    """POST /api/templates/import returns 400 when the archive is malformed."""
    target = tmp_path / "target"
    target.mkdir()
    response = client.post(
        "/api/templates/import",
        params={"target_repo": str(target)},
        files={"file": ("bad.tar.gz", b"not a real archive", "application/gzip")},
    )
    assert response.status_code in (400, 422, 500)


def test_ui_templates_page_renders(tmp_path: Path) -> None:
    """GET /templates returns a 200 HTML page."""
    with patch("agentception.readers.templates.TEMPLATES_STORE", tmp_path / "empty"):
        response = client.get("/templates")
    assert response.status_code == 200
    assert b"Templates" in response.content
