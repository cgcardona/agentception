from __future__ import annotations

"""Tests for GET /cognitive-arch (issue #823).

Verifies that the cognitive architecture browser route:
- Returns HTTP 200
- Renders figure and skill-domain data into the page
- Degrades gracefully when the YAML directories are absent
"""
from __future__ import annotations

import textwrap
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.routes.ui.cognitive_arch import (
    FigureEntry,
    SkillDomainEntry,
    _load_figures,
    _load_skill_domains,
)

# Subpath where cognitive archetypes live inside a repo root.
_ARCH_PATH = Path("scripts") / "gen_prompts" / "cognitive_archetypes"


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


# ── Route tests ───────────────────────────────────────────────────────────────


def test_cognitive_arch_returns_200(client: TestClient) -> None:
    """GET /cognitive-arch must return HTTP 200 regardless of YAML availability."""
    response = client.get("/cognitive-arch")
    assert response.status_code == 200


def test_cognitive_arch_html_content_type(client: TestClient) -> None:
    """GET /cognitive-arch must return HTML content."""
    response = client.get("/cognitive-arch")
    assert "text/html" in response.headers.get("content-type", "")


def test_cognitive_arch_renders_page_title(client: TestClient) -> None:
    """Response must include the cognitive architecture page title."""
    response = client.get("/cognitive-arch")
    assert "Cognitive Architecture" in response.text


# ── Unit tests for _load_figures ──────────────────────────────────────────────


def test_load_figures_returns_entries(tmp_path: Path) -> None:
    """_load_figures returns a FigureEntry for each valid YAML under <root>/scripts/.../figures/."""
    figures_dir = tmp_path / _ARCH_PATH / "figures"
    figures_dir.mkdir(parents=True)
    (figures_dir / "alan_turing.yaml").write_text(
        textwrap.dedent(
            """\
            id: alan_turing
            display_name: "Alan Turing"
            layer: figure
            description: Father of computer science and AI.
            skill_domains:
              primary: [python, algorithms]
              secondary: [cryptography]
            """
        ),
        encoding="utf-8",
    )

    entries = _load_figures(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, FigureEntry)
    assert entry.id == "alan_turing"
    assert entry.display_name == "Alan Turing"
    assert "python" in entry.compatible_skill_domains
    assert "cryptography" in entry.compatible_skill_domains


def test_load_figures_missing_dir_returns_empty(tmp_path: Path) -> None:
    """_load_figures returns [] when the figures directory does not exist."""
    entries = _load_figures(tmp_path)
    assert entries == []


def test_load_figures_skips_entries_without_display_name(tmp_path: Path) -> None:
    """_load_figures skips YAML files that lack a display_name field."""
    figures_dir = tmp_path / _ARCH_PATH / "figures"
    figures_dir.mkdir(parents=True)
    (figures_dir / "no_name.yaml").write_text("id: no_name\n", encoding="utf-8")

    entries = _load_figures(tmp_path)
    assert entries == []


def test_load_figures_tolerates_malformed_yaml(tmp_path: Path) -> None:
    """_load_figures skips YAML files that cannot be parsed."""
    figures_dir = tmp_path / _ARCH_PATH / "figures"
    figures_dir.mkdir(parents=True)
    (figures_dir / "bad.yaml").write_text(":\t\tinvalid: [broken", encoding="utf-8")
    (figures_dir / "good.yaml").write_text(
        'id: good\ndisplay_name: "Good Entry"\n', encoding="utf-8"
    )

    entries = _load_figures(tmp_path)
    assert len(entries) == 1
    assert entries[0].id == "good"


# ── Unit tests for _load_skill_domains ────────────────────────────────────────


def test_load_skill_domains_returns_entries(tmp_path: Path) -> None:
    """_load_skill_domains returns a SkillDomainEntry for each valid YAML."""
    domains_dir = tmp_path / _ARCH_PATH / "skill_domains"
    domains_dir.mkdir(parents=True)
    (domains_dir / "python.yaml").write_text(
        textwrap.dedent(
            """\
            id: python
            display_name: "Python"
            description: General-purpose scripting and backend language.
            """
        ),
        encoding="utf-8",
    )

    entries = _load_skill_domains(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, SkillDomainEntry)
    assert entry.id == "python"
    assert entry.display_name == "Python"
    assert "backend" in entry.description.lower()


def test_load_skill_domains_missing_dir_returns_empty(tmp_path: Path) -> None:
    """_load_skill_domains returns [] when the skill_domains directory does not exist."""
    entries = _load_skill_domains(tmp_path)
    assert entries == []


def test_load_skill_domains_skips_entries_without_display_name(tmp_path: Path) -> None:
    """_load_skill_domains skips YAML files that lack a display_name field."""
    domains_dir = tmp_path / _ARCH_PATH / "skill_domains"
    domains_dir.mkdir(parents=True)
    (domains_dir / "no_name.yaml").write_text("id: no_name\n", encoding="utf-8")

    entries = _load_skill_domains(tmp_path)
    assert entries == []


# ── Integration: page renders data from YAML files ────────────────────────────


def test_cognitive_arch_page_shows_figure_data(client: TestClient, tmp_path: Path) -> None:
    """When figures are present on disk, their display_names appear in the rendered HTML."""
    figures_dir = tmp_path / _ARCH_PATH / "figures"
    figures_dir.mkdir(parents=True)
    (figures_dir / "grace_hopper.yaml").write_text(
        'id: grace_hopper\ndisplay_name: "Grace Hopper"\ndescription: "COBOL pioneer."\n',
        encoding="utf-8",
    )

    from agentception.config import AgentCeptionSettings

    patched_settings = AgentCeptionSettings()
    patched_settings.repo_dir = tmp_path

    with patch("agentception.routes.ui.cognitive_arch._settings", patched_settings):
        response = client.get("/cognitive-arch")

    assert response.status_code == 200
    assert "Grace Hopper" in response.text


def test_cognitive_arch_page_shows_skill_domain_data(
    client: TestClient, tmp_path: Path
) -> None:
    """When skill_domains are present on disk, their display_names appear in the rendered HTML."""
    domains_dir = tmp_path / _ARCH_PATH / "skill_domains"
    domains_dir.mkdir(parents=True)
    (domains_dir / "fastapi.yaml").write_text(
        'id: fastapi\ndisplay_name: "FastAPI"\ndescription: "Async Python web framework."\n',
        encoding="utf-8",
    )

    from agentception.config import AgentCeptionSettings

    patched_settings = AgentCeptionSettings()
    patched_settings.repo_dir = tmp_path

    with patch("agentception.routes.ui.cognitive_arch._settings", patched_settings):
        response = client.get("/cognitive-arch")

    assert response.status_code == 200
    assert "FastAPI" in response.text
