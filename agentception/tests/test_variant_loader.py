"""Tests for variant-aware _load_role_prompt resolution."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_load_role_prompt_uses_variant_file(tmp_path: Path) -> None:
    """When the variant file exists, _load_role_prompt returns its content."""
    roles_dir = tmp_path / ".agentception" / "roles"
    roles_dir.mkdir(parents=True)

    base_file = roles_dir / "developer.md"
    base_file.write_text("base content", encoding="utf-8")

    variant_file = roles_dir / "developer-streamlined.md"
    variant_file.write_text("streamlined content", encoding="utf-8")

    with patch("agentception.services.agent_loop.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        from agentception.services.agent_loop import _load_role_prompt

        result = _load_role_prompt("developer", "streamlined")

    assert result == "streamlined content"


def test_load_role_prompt_falls_back_when_variant_missing(tmp_path: Path) -> None:
    """When the variant file does not exist, _load_role_prompt falls back to the base file."""
    roles_dir = tmp_path / ".agentception" / "roles"
    roles_dir.mkdir(parents=True)

    base_file = roles_dir / "developer.md"
    base_file.write_text("base content", encoding="utf-8")

    # No developer-streamlined.md — only the base file exists.

    with patch("agentception.services.agent_loop.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        from agentception.services.agent_loop import _load_role_prompt

        result = _load_role_prompt("developer", "streamlined")

    assert result == "base content"


def test_load_role_prompt_falls_back_when_variant_none(tmp_path: Path) -> None:
    """When variant is None, _load_role_prompt uses the base role file unchanged."""
    roles_dir = tmp_path / ".agentception" / "roles"
    roles_dir.mkdir(parents=True)

    base_file = roles_dir / "developer.md"
    base_file.write_text("base content", encoding="utf-8")

    with patch("agentception.services.agent_loop.settings") as mock_settings:
        mock_settings.repo_dir = tmp_path
        from agentception.services.agent_loop import _load_role_prompt

        result = _load_role_prompt("developer", None)

    assert result == "base content"
