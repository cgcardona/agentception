from __future__ import annotations

"""Unit tests for agentception.readers.llm_phase_planner helpers.

Covers:
- _strip_fences: removes markdown code fences in every permutation
- _build_skill_ids: reads skill_domains directory; falls back gracefully
- _build_yaml_system_prompt: injects skill IDs and appends arch section

All filesystem reads are either performed on real assets (if present) or
mocked via tmp_path so the tests run cleanly in CI without the full scripts/
directory mounted.

Run targeted:
    pytest agentception/tests/test_llm_phase_planner.py -v
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from agentception.readers.llm_phase_planner import (
    _build_skill_ids,
    _build_yaml_system_prompt,
    _strip_fences,
    get_fallback_plan_spec,
)


# ---------------------------------------------------------------------------
# _strip_fences
# ---------------------------------------------------------------------------


def test_strip_fences_plain_yaml_unchanged() -> None:
    """YAML with no fences is returned as-is (modulo surrounding whitespace)."""
    raw = "initiative: auth\nphases: []\n"
    assert _strip_fences(raw) == raw.strip()


def test_strip_fences_removes_plain_backtick_fence() -> None:
    """``` fences (no language tag) are stripped."""
    raw = "```\ninitiative: auth\nphases: []\n```"
    result = _strip_fences(raw)
    assert "```" not in result
    assert "initiative: auth" in result


def test_strip_fences_removes_yaml_language_fence() -> None:
    """```yaml fences are stripped."""
    raw = "```yaml\ninitiative: auth\nphases: []\n```"
    result = _strip_fences(raw)
    assert "```" not in result
    assert "initiative: auth" in result


def test_strip_fences_removes_yml_language_fence() -> None:
    """```yml fences are stripped."""
    raw = "```yml\ninitiative: auth\nphases: []\n```"
    result = _strip_fences(raw)
    assert "```" not in result
    assert "initiative: auth" in result


def test_strip_fences_handles_missing_closing_fence() -> None:
    """Unclosed fence (no closing ```) returns the inner content without crash."""
    raw = "```yaml\ninitiative: auth\nphases: []\n"
    result = _strip_fences(raw)
    # Should not raise; inner content must be present.
    assert "initiative: auth" in result


def test_strip_fences_trims_surrounding_whitespace() -> None:
    """Leading and trailing whitespace is removed from the result."""
    raw = "  \n```\ninitiative: auth\n```\n  "
    result = _strip_fences(raw)
    assert not result.startswith(" ")
    assert not result.endswith(" ")


def test_strip_fences_empty_string_returns_empty() -> None:
    """Empty input → empty output."""
    assert _strip_fences("") == ""


def test_strip_fences_fenced_content_preserves_inner_yaml() -> None:
    """The inner YAML content is preserved byte-for-byte (modulo whitespace)."""
    inner = "initiative: auth\nphases:\n  - label: 0-foundation\n"
    raw = f"```yaml\n{inner}```"
    result = _strip_fences(raw)
    assert "initiative: auth" in result
    assert "0-foundation" in result


# ---------------------------------------------------------------------------
# _build_skill_ids
# ---------------------------------------------------------------------------


def test_build_skill_ids_returns_python_when_dir_absent(tmp_path: Path) -> None:
    """Falls back to 'python' when the skill_domains directory does not exist."""
    missing = tmp_path / "skill_domains"
    with patch(
        "agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", missing
    ):
        result = _build_skill_ids()
    assert result == "python"


def test_build_skill_ids_returns_python_when_dir_empty(tmp_path: Path) -> None:
    """Falls back to 'python' when the skill_domains directory contains no YAML files."""
    skill_dir = tmp_path / "skill_domains"
    skill_dir.mkdir()
    with patch(
        "agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", skill_dir
    ):
        result = _build_skill_ids()
    assert result == "python"


def test_build_skill_ids_reads_yaml_stems(tmp_path: Path) -> None:
    """Returns comma-separated, sorted stem names from *.yaml files in the directory."""
    skill_dir = tmp_path / "skill_domains"
    skill_dir.mkdir()
    (skill_dir / "typescript.yaml").write_text("id: typescript", encoding="utf-8")
    (skill_dir / "fastapi.yaml").write_text("id: fastapi", encoding="utf-8")
    (skill_dir / "python.yaml").write_text("id: python", encoding="utf-8")

    with patch(
        "agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", skill_dir
    ):
        result = _build_skill_ids()

    assert result == "fastapi, python, typescript"


def test_build_skill_ids_ignores_non_yaml_files(tmp_path: Path) -> None:
    """Non-YAML files (e.g. .txt, .json) are ignored."""
    skill_dir = tmp_path / "skill_domains"
    skill_dir.mkdir()
    (skill_dir / "python.yaml").write_text("id: python", encoding="utf-8")
    (skill_dir / "readme.txt").write_text("ignore me", encoding="utf-8")
    (skill_dir / "config.json").write_text("{}", encoding="utf-8")

    with patch(
        "agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", skill_dir
    ):
        result = _build_skill_ids()

    assert result == "python"


def test_build_skill_ids_is_sorted() -> None:
    """The returned IDs are always in alphabetical order regardless of filesystem ordering."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        skill_dir = Path(td) / "skill_domains"
        skill_dir.mkdir()
        for name in ("zig", "ada", "python", "rust"):
            (skill_dir / f"{name}.yaml").write_text(f"id: {name}", encoding="utf-8")

        with patch(
            "agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", skill_dir
        ):
            result = _build_skill_ids()

    ids = result.split(", ")
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# _build_yaml_system_prompt
# ---------------------------------------------------------------------------


def test_build_yaml_system_prompt_injects_skill_ids(tmp_path: Path) -> None:
    """__SKILL_IDS__ sentinel in the raw prompt is replaced with real skill IDs."""
    skill_dir = tmp_path / "skill_domains"
    skill_dir.mkdir()
    (skill_dir / "fastapi.yaml").write_text("id: fastapi", encoding="utf-8")
    (skill_dir / "python.yaml").write_text("id: python", encoding="utf-8")

    # Patch both the skill dir and the cognitive arch cache so the test is self-contained.
    with (
        patch("agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", skill_dir),
        patch(
            "agentception.readers.llm_phase_planner._COGNITIVE_ARCH_SECTION", ""
        ),
    ):
        prompt = _build_yaml_system_prompt()

    assert "__SKILL_IDS__" not in prompt, "Sentinel must be replaced"
    assert "fastapi" in prompt
    assert "python" in prompt


def test_build_yaml_system_prompt_no_sentinel_remains(tmp_path: Path) -> None:
    """After construction, the prompt must never contain the literal __SKILL_IDS__ string."""
    skill_dir = tmp_path / "skill_domains"
    skill_dir.mkdir()
    (skill_dir / "python.yaml").write_text("id: python", encoding="utf-8")

    with (
        patch("agentception.readers.llm_phase_planner._SKILL_DOMAINS_DIR", skill_dir),
        patch("agentception.readers.llm_phase_planner._COGNITIVE_ARCH_SECTION", ""),
    ):
        prompt = _build_yaml_system_prompt()

    assert "__SKILL_IDS__" not in prompt


def test_build_yaml_system_prompt_includes_identity_block() -> None:
    """The built prompt includes the Identity block (decisive planning engine, parallelism rules)."""
    with patch(
        "agentception.readers.llm_phase_planner._COGNITIVE_ARCH_SECTION", ""
    ):
        prompt = _build_yaml_system_prompt()

    assert "## Identity" in prompt
    assert "parallel" in prompt


def test_build_yaml_system_prompt_is_non_empty() -> None:
    """The built prompt is never empty — it must contain the system instructions."""
    with patch(
        "agentception.readers.llm_phase_planner._COGNITIVE_ARCH_SECTION", ""
    ):
        prompt = _build_yaml_system_prompt()

    assert len(prompt) > 500, "System prompt must be substantive"


def test_get_fallback_plan_spec_returns_clarify_and_scope() -> None:
    """Fallback plan is valid PlanSpec with initiative clarify-and-scope, one phase, one issue."""
    spec = get_fallback_plan_spec()
    assert spec.initiative == "clarify-and-scope"
    assert len(spec.phases) == 1
    assert spec.phases[0].label == "0-scope"
    assert len(spec.phases[0].issues) == 1
    assert spec.phases[0].issues[0].id == "clarify-and-scope-p0-001"
    assert "too vague" in spec.phases[0].issues[0].body
