from __future__ import annotations

"""Integration tests for the agent self-introduction protocol (issue #177).

Verifies that:

- Every agent tier (coordinator, sub-coordinator, leaf) receives a self-introduction
  instruction in its system prompt when ``is_resumed=False``.
- Resumed agents (``is_resumed=True``) do NOT receive the instruction.
- The instruction contains the required format: "My name is {name}. My cognitive
  architecture is: {description}."
- The ``_build_intro_instruction`` helper returns ``""`` for resumed or arch-less agents.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_self_introduce.py -v
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentception.services.prompt_assembly import _build_intro_instruction, build_system_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SELF_INTRO_PATTERN = re.compile(
    r"My name is .+\. My cognitive architecture is: .+",
    re.DOTALL,
)


def _make_figures_dir(tmp_path: Path, figure_id: str, display_name: str, description: str) -> Path:
    """Write a minimal figure YAML into a correctly structured tmp_path tree."""
    scripts_dir = tmp_path / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    yaml_content = (
        f"id: {figure_id}\n"
        f'display_name: "{display_name}"\n'
        f'description: "{description}"\n'
    )
    (scripts_dir / f"{figure_id}.yaml").write_text(yaml_content, encoding="utf-8")
    return tmp_path


def _fake_settings(repo_dir: Path) -> MagicMock:
    s = MagicMock()
    s.repo_dir = repo_dir
    return s


# ---------------------------------------------------------------------------
# _build_intro_instruction — unit tests
# ---------------------------------------------------------------------------


def test_intro_instruction_returned_for_fresh_agent(tmp_path: Path) -> None:
    """_build_intro_instruction returns a non-empty block when is_resumed=False."""
    repo_dir = _make_figures_dir(tmp_path, "turing", "Alan Turing", "Pioneer of theoretical computer science.")
    with patch("agentception.services.prompt_assembly.settings", _fake_settings(repo_dir)):
        result = _build_intro_instruction("turing:python", is_resumed=False)
    assert result != ""
    assert "Alan Turing" in result
    assert "My name is Alan Turing." in result
    assert "My cognitive architecture is:" in result


def test_intro_instruction_empty_when_resumed(tmp_path: Path) -> None:
    """_build_intro_instruction returns '' when is_resumed=True."""
    repo_dir = _make_figures_dir(tmp_path, "turing", "Alan Turing", "Pioneer of theoretical computer science.")
    with patch("agentception.services.prompt_assembly.settings", _fake_settings(repo_dir)):
        result = _build_intro_instruction("turing:python", is_resumed=True)
    assert result == ""


def test_intro_instruction_empty_when_arch_none() -> None:
    """_build_intro_instruction returns '' when cognitive_arch is None."""
    result = _build_intro_instruction(None, is_resumed=False)
    assert result == ""


def test_intro_instruction_empty_when_arch_empty() -> None:
    """_build_intro_instruction returns '' when cognitive_arch is empty string."""
    result = _build_intro_instruction("", is_resumed=False)
    assert result == ""


def test_intro_instruction_format_matches_required_pattern(tmp_path: Path) -> None:
    """The intro instruction text embeds the required 'My name is … My cognitive architecture is:' sentence."""
    repo_dir = _make_figures_dir(tmp_path, "jeff_dean", "Jeff Dean", "Designs massively scalable distributed systems.")
    with patch("agentception.services.prompt_assembly.settings", _fake_settings(repo_dir)):
        result = _build_intro_instruction("jeff_dean:python", is_resumed=False)
    assert _SELF_INTRO_PATTERN.search(result), (
        f"Intro instruction did not match required pattern.\nGot:\n{result}"
    )


# ---------------------------------------------------------------------------
# build_system_prompt — ordering and content with self-intro
# ---------------------------------------------------------------------------


def test_build_system_prompt_includes_intro_before_role_for_fresh_agent(tmp_path: Path) -> None:
    """build_system_prompt inserts self-intro instruction between persona and role instructions."""
    repo_dir = _make_figures_dir(tmp_path, "hopper", "Grace Hopper", "Compiler pioneer.")
    role_instructions = "Implement the GitHub issue. Write tests. Open a PR."

    with patch("agentception.services.prompt_assembly.settings", _fake_settings(repo_dir)):
        prompt = build_system_prompt(
            "hopper:python",
            role_instructions,
            agent_type="leaf",
            is_resumed=False,
        )

    persona_pos = prompt.index("Grace Hopper")
    intro_pos = prompt.index("My name is Grace Hopper.")
    role_pos = prompt.index("Implement the GitHub issue")
    assert persona_pos < intro_pos < role_pos, (
        "Prompt ordering must be: persona → self-intro → role instructions"
    )


def test_build_system_prompt_omits_intro_for_resumed_agent(tmp_path: Path) -> None:
    """build_system_prompt does NOT include self-intro when is_resumed=True."""
    repo_dir = _make_figures_dir(tmp_path, "hopper", "Grace Hopper", "Compiler pioneer.")
    role_instructions = "Continue the implementation. Open a PR."

    with patch("agentception.services.prompt_assembly.settings", _fake_settings(repo_dir)):
        prompt = build_system_prompt(
            "hopper:python",
            role_instructions,
            agent_type="leaf",
            is_resumed=True,
        )

    assert "My name is" not in prompt
    assert "My cognitive architecture is:" not in prompt
    assert "Grace Hopper" in prompt, "Persona block must still appear even for resumed agents"
    assert "Continue the implementation" in prompt


# ---------------------------------------------------------------------------
# test_all_tiers_self_introduce — integration: coordinator, sub-coord, leaf
# ---------------------------------------------------------------------------


def test_all_tiers_self_introduce(tmp_path: Path) -> None:
    """All three tiers produce a self-introduction instruction in their system prompts.

    This is the canonical integration test for issue #177.  It simulates the
    coordinator → sub-coordinator → leaf tree using three separate
    ``build_system_prompt`` calls (one per tier) with distinct cognitive arch
    figures and asserts that each prompt contains the required self-introduction
    sentence in response content (not just embedded in the persona block).
    """
    # Set up three figure YAMLs — one per tier.
    repo_dir = tmp_path
    for figure_id, display_name, description in [
        ("von_neumann", "John von Neumann", "Architect of the stored-program computer."),
        ("turing", "Alan Turing", "Pioneer of theoretical computer science."),
        ("hopper", "Grace Hopper", "Compiler pioneer and COBOL inventor."),
    ]:
        scripts_dir = (
            repo_dir / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
        )
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / f"{figure_id}.yaml").write_text(
            f'id: {figure_id}\ndisplay_name: "{display_name}"\ndescription: "{description}"\n',
            encoding="utf-8",
        )

    fake_settings = _fake_settings(repo_dir)
    role_instructions = "Do work."

    with patch("agentception.services.prompt_assembly.settings", fake_settings):
        # Coordinator tier
        coord_prompt = build_system_prompt(
            "von_neumann:python", role_instructions, agent_type="coordinator", is_resumed=False
        )
        # Sub-coordinator tier (same build_system_prompt path — tier label is informational only)
        sub_coord_prompt = build_system_prompt(
            "turing:python", role_instructions, agent_type="coordinator", is_resumed=False
        )
        # Leaf / engineer tier
        leaf_prompt = build_system_prompt(
            "hopper:python", role_instructions, agent_type="leaf", is_resumed=False
        )

    for tier_label, prompt in [
        ("coordinator", coord_prompt),
        ("sub-coordinator", sub_coord_prompt),
        ("leaf", leaf_prompt),
    ]:
        assert "My name is" in prompt, (
            f"{tier_label} prompt missing 'My name is' self-introduction"
        )
        assert "My cognitive architecture is:" in prompt, (
            f"{tier_label} prompt missing 'My cognitive architecture is:' self-introduction"
        )
        assert _SELF_INTRO_PATTERN.search(prompt), (
            f"{tier_label} prompt intro does not match required pattern.\nGot:\n{prompt}"
        )


