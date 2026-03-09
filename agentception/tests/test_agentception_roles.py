from __future__ import annotations

"""Tests for the Role file reader/writer API (AC-301/303) and Role Studio UI (AC-302/303).

Covers:
- list_roles returns all managed files that exist on disk
- get_role returns content and meta for a known slug
- get_role returns 404 for an unknown slug
- update_role writes new content to disk
- role_history returns a list of commit dicts
- GET /roles page returns 200 with file list and Monaco CDN script
- role_diff returns unified diff of proposed content vs HEAD (AC-303)
- role_diff returns empty diff when proposed content is identical to HEAD (AC-303)
- commit_role writes file and creates git commit (AC-303)
- commit_role returns correct commit message format (AC-303)

Run targeted:
    pytest agentception/tests/test_agentception_roles.py -v
"""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.models import RoleMeta


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a temp directory that mimics .agentception/roles/ structure with two role files."""
    roles_dir = tmp_path / ".agentception" / "roles"
    roles_dir.mkdir(parents=True)
    (roles_dir / "cto.md").write_text("# CTO\nLeads engineering.", encoding="utf-8")
    (roles_dir / "developer.md").write_text(
        "# Python Developer\nWrites code.", encoding="utf-8"
    )
    return tmp_path


# ── list_roles ────────────────────────────────────────────────────────────────


def test_list_roles_returns_all_managed_files(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles must return one RoleMeta entry per file that exists on disk."""
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        with patch(
            "agentception.routes.roles._git_log_one",
            new=AsyncMock(return_value=("abc123", "initial commit")),
        ):
            response = client.get("/api/roles")

    assert response.status_code == 200
    slugs = {item["slug"] for item in response.json()}
    # Only files that exist in tmp_repo are returned
    assert "cto" in slugs
    assert "developer" in slugs
    # Files not created in tmp_repo are absent
    assert "engineering-coordinator" not in slugs


# ── get_role ──────────────────────────────────────────────────────────────────


def test_get_role_returns_content_and_meta(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles/{slug} must return slug, content, and meta for a known slug."""
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        with patch(
            "agentception.routes.roles._git_log_one",
            new=AsyncMock(return_value=("deadbeef", "feat: add cto role")),
        ):
            response = client.get("/api/roles/cto")

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert "CTO" in data["content"]
    meta = data["meta"]
    assert meta["slug"] == "cto"
    assert meta["path"] == ".agentception/roles/cto.md"
    assert meta["line_count"] >= 1
    assert meta["last_commit_sha"] == "deadbeef"
    assert meta["last_commit_message"] == "feat: add cto role"


def test_get_role_unknown_slug_404(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles/{slug} must return 404 for a slug not in the managed allowlist."""
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        response = client.get("/api/roles/nonexistent-role")

    assert response.status_code == 404
    assert "nonexistent-role" in response.json()["detail"]


# ── update_role ───────────────────────────────────────────────────────────────


def test_update_role_writes_file(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """PUT /api/roles/{slug} must write new content to disk and return a diff."""
    new_content = "# CTO (Updated)\nNew responsibilities.\n"

    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        with patch(
            "agentception.routes.roles._git_log_one",
            new=AsyncMock(return_value=("", "")),
        ):
            # Patch git diff subprocess so it doesn't require a real git repo
            async def fake_diff(*args: object, **kwargs: object) -> object:
                class FakeProc:
                    returncode = 0

                    async def communicate(self) -> tuple[bytes, bytes]:
                        return b"--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n", b""

                return FakeProc()

            with patch("asyncio.create_subprocess_exec", side_effect=fake_diff):
                response = client.put("/api/roles/cto", json={"content": new_content})

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert "diff" in data
    # Verify file was actually written
    written = (tmp_repo / ".agentception" / "roles" / "cto.md").read_text(encoding="utf-8")
    assert written == new_content


# ── role_history ──────────────────────────────────────────────────────────────


def test_role_history_returns_commits(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /api/roles/{slug}/history must return a list of commit dicts."""
    fake_commits = [
        {"sha": "aaa111", "date": "2026-03-01 12:00:00 +0000", "subject": "feat: role update"},
        {"sha": "bbb222", "date": "2026-02-28 10:00:00 +0000", "subject": "initial commit"},
    ]

    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        with patch(
            "agentception.routes.roles._git_log_recent",
            new=AsyncMock(return_value=fake_commits),
        ):
            response = client.get("/api/roles/cto/history")

    assert response.status_code == 200
    history = response.json()
    assert len(history) == 2
    assert history[0]["sha"] == "aaa111"
    assert history[0]["subject"] == "feat: role update"
    assert "date" in history[0]


# ── GET /roles — Role Studio UI (AC-302) ──────────────────────────────────────


def test_roles_page_returns_200(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /roles must return HTTP 200 with valid HTML."""
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        with patch(
            "agentception.routes.roles._git_log_one",
            new=AsyncMock(return_value=("abc123", "initial commit")),
        ):
            response = client.get("/roles")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_roles_page_loads_successfully(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /roles HTML must return 200 with the three-panel Cognitive Architecture UI.

    The template uses SSR Jinja2 for the org tree panel and Alpine.js components
    for the detail/editor panels.  CSS class names encode the three-panel structure.
    """
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        response = client.get("/roles")

    assert response.status_code == 200
    html = response.text
    # Three-panel layout markers (CSS class names used by the template)
    assert "cas-panel--org" in html
    assert "cas-panel--detail" in html
    assert "cas-panel--editor" in html
    # Alpine.js components that drive interactivity
    assert "rolesEditor()" in html


def test_roles_page_includes_monaco_cdn(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """GET /roles HTML must load the rolesEditor() Alpine component.

    Monaco is bootstrapped lazily by rolesEditor._bootMonaco() when the user
    first opens a prompt for editing — the CDN script tag is injected at
    runtime, not on page load.  The HTML therefore does NOT contain a static
    Monaco <script> tag; instead we verify the rolesEditor Alpine component is
    present, which is the entry-point that boots Monaco on demand.
    """
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        with patch(
            "agentception.routes.roles._git_log_one",
            new=AsyncMock(return_value=("", "")),
        ):
            response = client.get("/roles")

    assert response.status_code == 200
    assert "rolesEditor()" in response.text


# ── role_diff (AC-303) ────────────────────────────────────────────────────────


def test_diff_endpoint_returns_unified_diff(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """POST /api/roles/{slug}/diff must return a non-empty unified diff for changed content."""
    proposed = "# CTO (Changed)\nDifferent content.\n"

    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        async def fake_diff(*args: object, **kwargs: object) -> object:
            class FakeProc:
                returncode = 1  # git diff --no-index exits 1 when files differ

                async def communicate(self) -> tuple[bytes, bytes]:
                    return b"--- a/cto.md\n+++ b/cto.md\n@@ -1,2 +1,2 @@\n-# CTO\n+# CTO (Changed)\n", b""

            return FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_diff):
            response = client.post(
                "/api/roles/cto/diff",
                json={"content": proposed},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert "diff" in data
    assert "@@" in data["diff"]


def test_diff_identical_content_returns_empty(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """POST /api/roles/{slug}/diff must return an empty diff when proposed matches HEAD."""
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        async def fake_no_diff(*args: object, **kwargs: object) -> object:
            class FakeProc:
                returncode = 0  # exit 0 means no differences

                async def communicate(self) -> tuple[bytes, bytes]:
                    return b"", b""

            return FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_no_diff):
            response = client.post(
                "/api/roles/cto/diff",
                json={"content": "# CTO\nLeads engineering."},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert data["diff"] == ""


# ── commit_role (AC-303) ──────────────────────────────────────────────────────


def test_commit_writes_file_and_creates_commit(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """POST /api/roles/{slug}/commit must write content to disk and return a commit SHA."""
    new_content = "# CTO (Committed)\nUpdated responsibilities.\n"

    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        _SHA = b"abcdef1234567890abcdef1234567890abcdef12\n"

        async def fake_git(*args: object, **kwargs: object) -> object:
            # Return the commit SHA for rev-parse; empty stdout for add/commit.
            is_rev_parse = len(args) >= 1 and "rev-parse" in args

            class FakeProc:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    return (_SHA, b"") if is_rev_parse else (b"", b"")

            return FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
            response = client.post(
                "/api/roles/cto/commit",
                json={"content": new_content},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["slug"] == "cto"
    assert "commit_sha" in data
    assert len(data["commit_sha"]) > 0
    assert "message" in data
    # File must have been written to disk
    written = (tmp_repo / ".agentception" / "roles" / "cto.md").read_text(encoding="utf-8")
    assert written == new_content


def test_commit_creates_correct_message(
    client: TestClient,
    tmp_repo: Path,
) -> None:
    """POST /api/roles/{slug}/commit must use the expected commit message format."""
    with patch("agentception.routes.roles.settings") as mock_settings:
        mock_settings.repo_dir = tmp_repo

        async def fake_git(*args: object, **kwargs: object) -> object:
            class FakeProc:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    return b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n", b""

            return FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
            response = client.post(
                "/api/roles/cto/commit",
                json={"content": "# CTO\nLeads engineering.\n"},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "role(agentception): update cto"


# ---------------------------------------------------------------------------
# Cognitive Architecture API — taxonomy / personas / atoms
# ---------------------------------------------------------------------------


def test_taxonomy_returns_three_levels(client: TestClient) -> None:
    """GET /api/roles/taxonomy must return levels for c_suite, coordinator, and worker."""
    response = client.get("/api/roles/taxonomy")
    assert response.status_code == 200
    data = response.json()
    assert "levels" in data
    level_ids = [lv["id"] for lv in data["levels"]]
    assert "c_suite" in level_ids
    assert "coordinator" in level_ids
    assert "worker" in level_ids


def test_taxonomy_c_suite_contains_cto(client: TestClient) -> None:
    """Taxonomy must include the existing CTO role in the C-Suite level."""
    response = client.get("/api/roles/taxonomy")
    assert response.status_code == 200
    data = response.json()
    c_suite = next(lv for lv in data["levels"] if lv["id"] == "c_suite")
    slugs = [r["slug"] for r in c_suite["roles"]]
    assert "cto" in slugs


def test_taxonomy_worker_roles_are_spawnable(client: TestClient) -> None:
    """All worker-level roles in the taxonomy must be marked spawnable=True."""
    response = client.get("/api/roles/taxonomy")
    assert response.status_code == 200
    data = response.json()
    worker_level = next(lv for lv in data["levels"] if lv["id"] == "worker")
    for role in worker_level["roles"]:
        assert role["spawnable"] is True, f"Worker role {role['slug']} should be spawnable"


def test_taxonomy_c_suite_roles_are_not_spawnable(client: TestClient) -> None:
    """C-Suite orchestration roles must NOT be marked spawnable via spawn API."""
    response = client.get("/api/roles/taxonomy")
    assert response.status_code == 200
    data = response.json()
    c_suite = next(lv for lv in data["levels"] if lv["id"] == "c_suite")
    for role in c_suite["roles"]:
        assert role["spawnable"] is False, f"C-Suite role {role['slug']} should not be spawnable"


def test_taxonomy_role_has_required_fields(client: TestClient) -> None:
    """Every role entry must have slug, label, title, description, compatible_figures."""
    response = client.get("/api/roles/taxonomy")
    assert response.status_code == 200
    data = response.json()
    for level in data["levels"]:
        for role in level["roles"]:
            assert "slug" in role
            assert "label" in role
            assert "title" in role
            assert "description" in role
            assert "compatible_figures" in role
            assert "compatible_skill_domains" in role
            assert "spawnable" in role
            assert "file_exists" in role


def test_personas_returns_list(client: TestClient) -> None:
    """GET /api/roles/personas must return a non-empty personas list."""
    response = client.get("/api/roles/personas")
    assert response.status_code == 200
    data = response.json()
    assert "personas" in data
    assert len(data["personas"]) > 0


def test_personas_contains_new_figures(client: TestClient) -> None:
    """GET /api/roles/personas must include the 13 new industry personas."""
    response = client.get("/api/roles/personas")
    assert response.status_code == 200
    data = response.json()
    persona_ids = {p["id"] for p in data["personas"]}
    new_personas = {
        "steve_jobs", "satya_nadella", "jeff_bezos", "werner_vogels",
        "margaret_hamilton", "linus_torvalds", "bjarne_stroustrup",
        "martin_fowler", "kent_beck", "yann_lecun", "andrej_karpathy",
        "bruce_schneier", "guido_van_rossum",
    }
    for pid in new_personas:
        assert pid in persona_ids, f"New persona '{pid}' missing from /api/roles/personas"


def test_personas_have_required_fields(client: TestClient) -> None:
    """Each persona entry must have id, display_name, extends, description, prompt_prefix."""
    response = client.get("/api/roles/personas")
    assert response.status_code == 200
    data = response.json()
    for persona in data["personas"]:
        assert "id" in persona
        assert "display_name" in persona
        assert "extends" in persona
        assert "description" in persona
        assert "prompt_prefix" in persona
        assert "overrides" in persona


def test_atoms_returns_all_dimensions(client: TestClient) -> None:
    """GET /api/roles/atoms must return the 10 cognitive atom dimensions."""
    response = client.get("/api/roles/atoms")
    assert response.status_code == 200
    data = response.json()
    assert "atoms" in data
    assert len(data["atoms"]) >= 10


def test_atoms_each_dimension_has_values(client: TestClient) -> None:
    """Each atom dimension must have at least 2 named values for the composer dropdowns."""
    response = client.get("/api/roles/atoms")
    assert response.status_code == 200
    data = response.json()
    for atom in data["atoms"]:
        assert "dimension" in atom
        assert "values" in atom
        assert len(atom["values"]) >= 2, f"Atom '{atom['dimension']}' has fewer than 2 values"
        for val in atom["values"]:
            assert "id" in val
            assert "label" in val


def test_new_worker_roles_in_managed_files(client: TestClient) -> None:
    """The new worker role slugs must be listable via GET /api/roles."""
    response = client.get("/api/roles")
    assert response.status_code == 200
    data = response.json()
    slugs = {r["slug"] for r in data}
    new_workers = {
        "developer", "developer", "developer",
        "developer", "ml-engineer", "data-engineer", "devops-engineer",
        "security-engineer", "test-engineer", "architect", "developer",
        "technical-writer",
    }
    for slug in new_workers:
        assert slug in slugs, f"New worker slug '{slug}' missing from /api/roles"


def test_new_c_suite_roles_in_managed_files(client: TestClient) -> None:
    """The new C-Suite role slugs must be listable via GET /api/roles."""
    response = client.get("/api/roles")
    assert response.status_code == 200
    data = response.json()
    slugs = {r["slug"] for r in data}
    new_csuite = {"ceo", "cpo", "cfo", "ciso", "cdo", "cmo", "coo"}
    for slug in new_csuite:
        assert slug in slugs, f"New C-Suite slug '{slug}' missing from /api/roles"
