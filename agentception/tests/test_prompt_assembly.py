from __future__ import annotations

"""Unit tests for agentception.services.prompt_assembly.

Covers:
- build_system_prompt returns persona block before role instructions (coordinator)
- build_system_prompt returns persona block before role instructions (leaf)
- build_system_prompt logs a warning when cognitive_arch is None
- build_system_prompt logs a warning when cognitive_arch is an empty string
- _build_persona_block returns the display name for a known figure
- _build_persona_block returns non-empty string for valid arch with known figure
- _build_persona_block returns bare figure_id when YAML is missing
- _format_persona produces the correct single-line block with and without description
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentception.services.prompt_assembly import (
    _build_persona_block,
    _format_persona,
    build_system_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_figure_yaml(tmp_path: Path, figure_id: str, display_name: str, description: str) -> Path:
    """Write a minimal figure YAML file and return its parent directory (figures_dir)."""
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir(parents=True)
    yaml_content = (
        f"id: {figure_id}\n"
        f'display_name: "{display_name}"\n'
        f"description: |\n"
        + "".join(f"  {line}\n" for line in description.splitlines())
    )
    (figures_dir / f"{figure_id}.yaml").write_text(yaml_content, encoding="utf-8")
    return figures_dir


def _patch_figures_dir(monkeypatch: pytest.MonkeyPatch, figures_dir: Path) -> None:
    """Patch settings.repo_dir so _load_figure_identity resolves to tmp figures."""
    repo_root = figures_dir.parent.parent.parent  # figures_dir = <repo>/scripts/gen_prompts/cognitive_archetypes/figures
    fake_settings = MagicMock()
    fake_settings.repo_dir = figures_dir.parent.parent.parent
    monkeypatch.setattr(
        "agentception.services.prompt_assembly.settings",
        fake_settings,
    )


# ---------------------------------------------------------------------------
# _format_persona
# ---------------------------------------------------------------------------


def test_format_persona_with_description() -> None:
    """_format_persona includes name and description when description is non-empty."""
    result = _format_persona("Guido van Rossum", "Creator of Python.")
    assert result == "You are Guido van Rossum. Your cognitive architecture: Creator of Python."


def test_format_persona_without_description() -> None:
    """_format_persona returns 'You are {name}.' when description is empty."""
    result = _format_persona("Guido van Rossum", "")
    assert result == "You are Guido van Rossum."


# ---------------------------------------------------------------------------
# _build_persona_block — None / empty cognitive_arch
# ---------------------------------------------------------------------------


def test_build_persona_block_none_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """_build_persona_block returns '' and logs a warning when cognitive_arch is None."""
    with caplog.at_level(logging.WARNING, logger="agentception.services.prompt_assembly"):
        result = _build_persona_block(None, "leaf")
    assert result == ""
    assert any("cognitive_arch is absent" in r.message for r in caplog.records)


def test_build_persona_block_empty_str_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """_build_persona_block returns '' and logs a warning when cognitive_arch is ''."""
    with caplog.at_level(logging.WARNING, logger="agentception.services.prompt_assembly"):
        result = _build_persona_block("", "coordinator")
    assert result == ""
    assert any("cognitive_arch is absent" in r.message for r in caplog.records)


def test_build_persona_block_missing_yaml_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_build_persona_block logs a warning when figure YAML does not exist."""
    fake_settings = MagicMock()
    fake_settings.repo_dir = tmp_path
    with (
        patch("agentception.services.prompt_assembly.settings", fake_settings),
        caplog.at_level(logging.WARNING, logger="agentception.services.prompt_assembly"),
    ):
        result = _build_persona_block("nonexistent_figure:python", "leaf")
    # Falls back to bare figure_id without raising
    assert "nonexistent_figure" in result
    assert any("figure YAML not found" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _build_persona_block — valid figure
# ---------------------------------------------------------------------------


def test_build_persona_block_known_figure(tmp_path: Path) -> None:
    """_build_persona_block returns a persona block containing display_name."""
    figures_dir = _make_figure_yaml(
        tmp_path,
        figure_id="test_figure",
        display_name="Test Figure",
        description="A test figure for unit testing.",
    )
    # Restructure tmp_path so settings.repo_dir resolves correctly:
    # settings.repo_dir / scripts / gen_prompts / cognitive_archetypes / figures / test_figure.yaml
    scripts_dir = tmp_path / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "test_figure.yaml").write_text(
        'id: test_figure\ndisplay_name: "Test Figure"\ndescription: "A test figure for unit testing."\n',
        encoding="utf-8",
    )
    fake_settings = MagicMock()
    fake_settings.repo_dir = tmp_path

    with patch("agentception.services.prompt_assembly.settings", fake_settings):
        result = _build_persona_block("test_figure:python", "leaf")

    assert "Test Figure" in result
    assert result.startswith("You are Test Figure.")


# ---------------------------------------------------------------------------
# build_system_prompt — coordinator
# ---------------------------------------------------------------------------


def test_coordinator_prompt_includes_arch_block(tmp_path: Path) -> None:
    """build_system_prompt for a coordinator contains the persona block before role instructions."""
    scripts_dir = tmp_path / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "von_neumann.yaml").write_text(
        'id: von_neumann\ndisplay_name: "John von Neumann"\ndescription: "Mathematician and architect of the stored-program computer."\n',
        encoding="utf-8",
    )
    fake_settings = MagicMock()
    fake_settings.repo_dir = tmp_path

    role_instructions = "Survey the label scope. Spawn child agents for each unclaimed issue."

    with patch("agentception.services.prompt_assembly.settings", fake_settings):
        prompt = build_system_prompt(
            "von_neumann:python",
            role_instructions,
            agent_type="coordinator",
        )

    # Persona block must appear before role instructions
    persona_pos = prompt.index("John von Neumann")
    role_pos = prompt.index("Survey the label scope")
    assert persona_pos < role_pos, "Persona block must appear before role instructions"

    # Persona block contains name and mental-model description
    assert "You are John von Neumann." in prompt
    assert "Mathematician" in prompt


# ---------------------------------------------------------------------------
# build_system_prompt — leaf
# ---------------------------------------------------------------------------


def test_leaf_prompt_includes_arch_block(tmp_path: Path) -> None:
    """build_system_prompt for a leaf agent contains the persona block before role instructions."""
    scripts_dir = tmp_path / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "guido_van_rossum.yaml").write_text(
        'id: guido_van_rossum\ndisplay_name: "Guido van Rossum"\ndescription: "Creator of Python."\n',
        encoding="utf-8",
    )
    fake_settings = MagicMock()
    fake_settings.repo_dir = tmp_path

    role_instructions = "Implement the GitHub issue. Create a PR. Run tests."

    with patch("agentception.services.prompt_assembly.settings", fake_settings):
        prompt = build_system_prompt(
            "guido_van_rossum:python",
            role_instructions,
            agent_type="leaf",
        )

    # Persona block must appear before role instructions
    persona_pos = prompt.index("Guido van Rossum")
    role_pos = prompt.index("Implement the GitHub issue")
    assert persona_pos < role_pos, "Persona block must appear before role instructions"

    # Persona block contains name and mental-model description
    assert "You are Guido van Rossum." in prompt
    assert "Creator of Python" in prompt


# ---------------------------------------------------------------------------
# build_system_prompt — missing arch logs warning
# ---------------------------------------------------------------------------


def test_missing_arch_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """build_system_prompt emits a WARNING when cognitive_arch is None."""
    with caplog.at_level(logging.WARNING, logger="agentception.services.prompt_assembly"):
        prompt = build_system_prompt(
            None,
            "Do the work.",
            agent_type="leaf",
        )
    # Warning must be logged
    assert any("cognitive_arch is absent" in r.message for r in caplog.records)
    # Prompt still contains role instructions even without persona block
    assert "Do the work." in prompt


def test_missing_arch_empty_string_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """build_system_prompt emits a WARNING when cognitive_arch is an empty string."""
    with caplog.at_level(logging.WARNING, logger="agentception.services.prompt_assembly"):
        prompt = build_system_prompt(
            "",
            "Coordinate the wave.",
            agent_type="coordinator",
        )
    assert any("cognitive_arch is absent" in r.message for r in caplog.records)
    assert "Coordinate the wave." in prompt


# ---------------------------------------------------------------------------
# build_system_prompt — ordering invariant
# ---------------------------------------------------------------------------


def test_persona_block_always_first(tmp_path: Path) -> None:
    """build_system_prompt always places persona block at position 0 in the output."""
    scripts_dir = tmp_path / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "turing.yaml").write_text(
        'id: turing\ndisplay_name: "Alan Turing"\ndescription: "Pioneer of theoretical computer science."\n',
        encoding="utf-8",
    )
    fake_settings = MagicMock()
    fake_settings.repo_dir = tmp_path

    with patch("agentception.services.prompt_assembly.settings", fake_settings):
        prompt = build_system_prompt("turing:python", "Role instructions here.", agent_type="leaf")

    assert prompt.startswith("You are Alan Turing.")


# ---------------------------------------------------------------------------
# build_system_prompt — role instructions always present
# ---------------------------------------------------------------------------


def test_role_instructions_always_present_even_without_arch() -> None:
    """build_system_prompt always includes role_instructions even without arch."""
    prompt = build_system_prompt(None, "Always include this.", agent_type="leaf")
    assert "Always include this." in prompt
