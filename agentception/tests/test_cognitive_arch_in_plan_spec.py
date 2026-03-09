from __future__ import annotations

"""Tests for the cognitive-arch-in-plan-spec feature.

Covers every layer introduced in the feat/cognitive-arch-in-plan-spec branch:

1. Model layer  — PlanIssue.cognitive_arch, PlanSpec.coordinator_arch,
                  PlanSpec.to_yaml() round-trip.
2. plan_tools   — plan_get_cognitive_figures(role) function.
3. MCP resource — plan_get_cognitive_figures is exposed as ac://plan/figures/{role}
                  Resource (not a Tool) and routed correctly via read_resource().
4. Issue creator — _embed_cognitive_arch, _create_one embedding.
5. Cognitive arch service — _extract_cognitive_arch_from_body,
                            _resolve_cognitive_arch priority order.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agentception.mcp.plan_tools import plan_get_cognitive_figures
from agentception.mcp.server import call_tool
from agentception.models import PlanIssue, PlanPhase, PlanSpec
from agentception.readers.issue_creator import (
    _embed_cognitive_arch,
    _embed_skills,
)
from agentception.services.cognitive_arch import (
    _extract_cognitive_arch_from_body,
    _resolve_cognitive_arch,
)
# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(
    cognitive_arch: str = "",
    skills: list[str] | None = None,
    coordinator_arch: dict[str, str] | None = None,
) -> PlanSpec:
    """Build a minimal PlanSpec for testing."""
    issue = PlanIssue(
        id="test-p0-001",
        title="Test issue",
        body="## Context\nTest.",
        skills=skills or [],
        cognitive_arch=cognitive_arch,
    )
    phase = PlanPhase(label="0-foundation", description="Foundation phase", issues=[issue])
    return PlanSpec(
        initiative="test-init",
        phases=[phase],
        coordinator_arch=coordinator_arch or {},
    )


# ---------------------------------------------------------------------------
# 1. Model layer
# ---------------------------------------------------------------------------


class TestPlanIssueCognitiveArch:
    def test_cognitive_arch_defaults_to_empty(self) -> None:
        issue = PlanIssue(id="x", title="t", body="b")
        assert issue.cognitive_arch == ""

    def test_cognitive_arch_stored_correctly(self) -> None:
        issue = PlanIssue(
            id="x", title="t", body="b", cognitive_arch="jeff_dean:llm:python"
        )
        assert issue.cognitive_arch == "jeff_dean:llm:python"

    def test_cognitive_arch_accepts_figure_only(self) -> None:
        issue = PlanIssue(id="x", title="t", body="b", cognitive_arch="hopper")
        assert issue.cognitive_arch == "hopper"


class TestPlanSpecCoordinatorArch:
    def test_coordinator_arch_defaults_to_empty_dict(self) -> None:
        spec = _make_spec()
        assert spec.coordinator_arch == {}

    def test_coordinator_arch_stored_correctly(self) -> None:
        arches = {
            "cto": "jeff_dean:llm:python",
            "engineering-coordinator": "hamming:python",
            "qa-coordinator": "w_edwards_deming:testing",
        }
        spec = _make_spec(coordinator_arch=arches)
        assert spec.coordinator_arch == arches

    def test_coordinator_arch_is_open_ended(self) -> None:
        """Any role slug is a valid key — no schema constraint on keys."""
        arches = {"future-coordinator": "some_figure:some_skill"}
        spec = _make_spec(coordinator_arch=arches)
        assert spec.coordinator_arch["future-coordinator"] == "some_figure:some_skill"


class TestPlanSpecToYaml:
    def test_coordinator_arch_omitted_when_empty(self) -> None:
        spec = _make_spec()
        data: object = yaml.safe_load(spec.to_yaml())
        assert isinstance(data, dict)
        assert "coordinator_arch" not in data

    def test_coordinator_arch_present_when_set(self) -> None:
        spec = _make_spec(coordinator_arch={"cto": "jeff_dean:python"})
        data: object = yaml.safe_load(spec.to_yaml())
        assert isinstance(data, dict)
        assert data.get("coordinator_arch") == {"cto": "jeff_dean:python"}

    def test_issue_cognitive_arch_present_when_set(self) -> None:
        spec = _make_spec(cognitive_arch="barbara_liskov:fastapi:python")
        data: object = yaml.safe_load(spec.to_yaml())
        assert isinstance(data, dict)
        phases = data.get("phases", [])
        assert isinstance(phases, list)
        issue = phases[0]["issues"][0]
        assert issue["cognitive_arch"] == "barbara_liskov:fastapi:python"

    def test_issue_cognitive_arch_absent_when_empty(self) -> None:
        spec = _make_spec(cognitive_arch="")
        data: object = yaml.safe_load(spec.to_yaml())
        assert isinstance(data, dict)
        phases = data.get("phases", [])
        assert isinstance(phases, list)
        issue = phases[0]["issues"][0]
        assert "cognitive_arch" not in issue

    def test_skills_present_when_set(self) -> None:
        spec = _make_spec(skills=["fastapi", "python"])
        data: object = yaml.safe_load(spec.to_yaml())
        assert isinstance(data, dict)
        assert isinstance(data, dict)
        phases = data["phases"]
        issue = phases[0]["issues"][0]
        assert issue["skills"] == ["fastapi", "python"]

    def test_round_trip_preserves_coordinator_arch(self) -> None:
        arches = {"cto": "jeff_dean:llm", "engineering-coordinator": "hamming:python"}
        spec = _make_spec(coordinator_arch=arches)
        reloaded = PlanSpec.from_yaml(spec.to_yaml())
        assert reloaded.coordinator_arch == arches

    def test_round_trip_preserves_issue_cognitive_arch(self) -> None:
        spec = _make_spec(cognitive_arch="ken_thompson:python")
        reloaded = PlanSpec.from_yaml(spec.to_yaml())
        assert reloaded.phases[0].issues[0].cognitive_arch == "ken_thompson:python"

    def test_coordinator_arch_appears_before_phases_in_yaml(self) -> None:
        """coordinator_arch must precede phases so readers see it first."""
        spec = _make_spec(coordinator_arch={"cto": "jeff_dean:python"})
        raw = spec.to_yaml()
        assert raw.index("coordinator_arch") < raw.index("phases")


# ---------------------------------------------------------------------------
# 2. MCP tool — plan_get_cognitive_figures
# ---------------------------------------------------------------------------


def _fake_taxonomy(roles: dict[str, list[str]]) -> dict[str, object]:
    """Build a minimal taxonomy dict for test stubs."""
    level_roles: list[dict[str, object]] = [
        {"slug": slug, "compatible_figures": figs} for slug, figs in roles.items()
    ]
    return {"levels": [{"id": "test-level", "roles": level_roles}]}


def _fake_figure_yaml(fig_id: str, display_name: str, description: str) -> str:
    return yaml.dump(
        {"id": fig_id, "display_name": display_name, "description": description}
    )


class TestPlanGetCognitiveFigures:
    def test_returns_figures_for_known_role(self, tmp_path: Path) -> None:
        tax_path = tmp_path / "role-taxonomy.yaml"
        tax_path.write_text(
            yaml.dump(_fake_taxonomy({"cto": ["jeff_dean", "werner_vogels"]}))
        )
        figs_dir = tmp_path / "figures"
        figs_dir.mkdir()
        (figs_dir / "jeff_dean.yaml").write_text(
            _fake_figure_yaml("jeff_dean", "Jeff Dean", "Scale changes what's possible.")
        )
        (figs_dir / "werner_vogels.yaml").write_text(
            _fake_figure_yaml("werner_vogels", "Werner Vogels", "Operational ownership.")
        )

        with (
            patch(
                "agentception.mcp.plan_tools._TAXONOMY_PATH", tax_path
            ),
            patch(
                "agentception.mcp.plan_tools._ARCHETYPES_DIR", figs_dir
            ),
        ):
            result = plan_get_cognitive_figures("cto")

        assert result["role"] == "cto"
        figures = result["figures"]
        assert isinstance(figures, list)
        assert len(figures) == 2
        ids = [f["id"] for f in figures]
        assert "jeff_dean" in ids
        assert "werner_vogels" in ids

    def test_returns_error_for_unknown_role(self, tmp_path: Path) -> None:
        tax_path = tmp_path / "role-taxonomy.yaml"
        tax_path.write_text(yaml.dump(_fake_taxonomy({"cto": ["jeff_dean"]})))
        figs_dir = tmp_path / "figures"
        figs_dir.mkdir()

        with (
            patch("agentception.mcp.plan_tools._TAXONOMY_PATH", tax_path),
            patch("agentception.mcp.plan_tools._ARCHETYPES_DIR", figs_dir),
        ):
            result = plan_get_cognitive_figures("nonexistent-role")

        assert result["figures"] == []
        assert "error" in result

    def test_skips_missing_figure_yaml(self, tmp_path: Path) -> None:
        tax_path = tmp_path / "role-taxonomy.yaml"
        tax_path.write_text(
            yaml.dump(_fake_taxonomy({"cto": ["jeff_dean", "ghost_figure"]}))
        )
        figs_dir = tmp_path / "figures"
        figs_dir.mkdir()
        (figs_dir / "jeff_dean.yaml").write_text(
            _fake_figure_yaml("jeff_dean", "Jeff Dean", "Scale at planetary level.")
        )
        # ghost_figure.yaml intentionally absent

        with (
            patch("agentception.mcp.plan_tools._TAXONOMY_PATH", tax_path),
            patch("agentception.mcp.plan_tools._ARCHETYPES_DIR", figs_dir),
        ):
            result = plan_get_cognitive_figures("cto")

        assert isinstance(result["figures"], list)
        assert len(result["figures"]) == 1
        assert result["figures"][0]["id"] == "jeff_dean"

    def test_description_trimmed_to_first_sentence(self, tmp_path: Path) -> None:
        tax_path = tmp_path / "role-taxonomy.yaml"
        tax_path.write_text(yaml.dump(_fake_taxonomy({"cto": ["jeff_dean"]})))
        figs_dir = tmp_path / "figures"
        figs_dir.mkdir()
        (figs_dir / "jeff_dean.yaml").write_text(
            _fake_figure_yaml(
                "jeff_dean",
                "Jeff Dean",
                "First sentence here. Second sentence ignored.",
            )
        )

        with (
            patch("agentception.mcp.plan_tools._TAXONOMY_PATH", tax_path),
            patch("agentception.mcp.plan_tools._ARCHETYPES_DIR", figs_dir),
        ):
            result = plan_get_cognitive_figures("cto")

        figures = result["figures"]
        assert isinstance(figures, list)
        desc = figures[0]["description"]
        assert "Second sentence" not in desc
        assert "First sentence" in desc

    def test_returns_error_when_taxonomy_missing(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.yaml"
        with patch("agentception.mcp.plan_tools._TAXONOMY_PATH", missing_path):
            result = plan_get_cognitive_figures("cto")
        assert result["figures"] == []
        assert "error" in result


# ---------------------------------------------------------------------------
# 3. MCP server — registration and dispatch
# ---------------------------------------------------------------------------


class TestMcpServerCognitiveFigures:
    def test_is_resource_not_tool(self) -> None:
        """plan_get_cognitive_figures is a Resource (ac://plan/figures/{role}), not a Tool."""
        from agentception.mcp.resources import RESOURCE_TEMPLATES
        from agentception.mcp.server import list_tools

        tool_names = [t["name"] for t in list_tools()]
        assert "plan_get_cognitive_figures" not in tool_names

        template_uris = {t["uriTemplate"] for t in RESOURCE_TEMPLATES}
        assert "ac://plan/figures/{role}" in template_uris

    @pytest.mark.anyio
    async def test_read_resource_dispatches_correctly(self, tmp_path: Path) -> None:
        """ac://plan/figures/{role} resource routes to plan_get_cognitive_figures."""
        from agentception.mcp.resources import read_resource

        tax_path = tmp_path / "role-taxonomy.yaml"
        tax_path.write_text(yaml.dump(_fake_taxonomy({"cto": ["jeff_dean"]})))
        figs_dir = tmp_path / "figures"
        figs_dir.mkdir()
        (figs_dir / "jeff_dean.yaml").write_text(
            _fake_figure_yaml("jeff_dean", "Jeff Dean", "Scale.")
        )

        with (
            patch("agentception.mcp.plan_tools._TAXONOMY_PATH", tax_path),
            patch("agentception.mcp.plan_tools._ARCHETYPES_DIR", figs_dir),
        ):
            result = await read_resource("ac://plan/figures/cto")

        payload: object = json.loads(result["contents"][0]["text"])
        assert isinstance(payload, dict)
        assert payload.get("role") == "cto"

    @pytest.mark.anyio
    async def test_read_resource_call_tool_redirect_returns_error(self) -> None:
        """Calling the retired plan_get_cognitive_figures tool returns a redirect error."""
        result = call_tool("plan_get_cognitive_figures", {})
        assert result["isError"] is True
        payload: object = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict)
        assert "ac://plan/figures/{role}" in str(payload.get("error", ""))

    @pytest.mark.anyio
    async def test_read_resource_unknown_role_returns_error_in_payload(self, tmp_path: Path) -> None:
        """ac://plan/figures/{role} for an unknown role returns an error-keyed payload."""
        from agentception.mcp.resources import read_resource

        tax_path = tmp_path / "nonexistent.yaml"
        figs_dir = tmp_path / "figures"
        figs_dir.mkdir()
        with (
            patch("agentception.mcp.plan_tools._TAXONOMY_PATH", tax_path),
            patch("agentception.mcp.plan_tools._ARCHETYPES_DIR", figs_dir),
        ):
            result = await read_resource("ac://plan/figures/unknown-role-xyz")

        payload: object = json.loads(result["contents"][0]["text"])
        assert isinstance(payload, dict)
        assert "error" in payload or payload.get("figures") == []


# ---------------------------------------------------------------------------
# 4. Issue creator — _embed_cognitive_arch
# ---------------------------------------------------------------------------


class TestEmbedCognitiveArch:
    def test_appends_comment_when_arch_set(self) -> None:
        body = "## Context\nSome content."
        result = _embed_cognitive_arch(body, "jeff_dean:python")
        assert "<!-- ac:cognitive_arch: jeff_dean:python -->" in result

    def test_body_unchanged_when_arch_empty(self) -> None:
        body = "## Context\nSome content."
        result = _embed_cognitive_arch(body, "")
        assert result == body

    def test_comment_appended_after_skills_comment(self) -> None:
        body = "## Context\nContent."
        with_skills = _embed_skills(body, ["fastapi", "python"])
        with_arch = _embed_cognitive_arch(with_skills, "barbara_liskov:fastapi:python")
        assert with_skills in with_arch
        assert with_arch.index("ac:skills") < with_arch.index("ac:cognitive_arch")

    def test_arch_string_preserved_verbatim(self) -> None:
        arch = "margaret_hamilton:devops:python"
        body = _embed_cognitive_arch("body", arch)
        assert f"<!-- ac:cognitive_arch: {arch} -->" in body


# ---------------------------------------------------------------------------
# 5. Cognitive arch service — extraction and priority
# ---------------------------------------------------------------------------


class TestExtractCognitiveArchFromBody:
    def test_extracts_arch_when_comment_present(self) -> None:
        body = "Some body.\n<!-- ac:cognitive_arch: jeff_dean:llm:python -->"
        assert _extract_cognitive_arch_from_body(body) == "jeff_dean:llm:python"

    def test_returns_none_when_comment_absent(self) -> None:
        body = "Some body without the comment."
        assert _extract_cognitive_arch_from_body(body) is None

    def test_tolerates_extra_whitespace(self) -> None:
        body = "<!--  ac:cognitive_arch:   hamming:python  -->"
        assert _extract_cognitive_arch_from_body(body) == "hamming:python"

    def test_ignores_unrelated_comments(self) -> None:
        body = "<!-- ac:skills: python -->\nContent."
        assert _extract_cognitive_arch_from_body(body) is None


class TestResolveCognitiveArchPriority:
    def test_priority1_embedded_arch_wins_over_skills_hint(self) -> None:
        """Embedded ac:cognitive_arch comment takes priority over skills_hint."""
        body = "Content.\n<!-- ac:cognitive_arch: ken_thompson:python -->"
        result = _resolve_cognitive_arch(
            body, "python-developer", skills_hint=["fastapi"]
        )
        assert result == "ken_thompson:python"

    def test_priority1_embedded_arch_wins_over_ac_skills_comment(self) -> None:
        body = (
            "Content.\n<!-- ac:skills: fastapi, python -->"
            "\n<!-- ac:cognitive_arch: barbara_liskov:fastapi:python -->"
        )
        result = _resolve_cognitive_arch(body, "python-developer")
        assert result == "barbara_liskov:fastapi:python"

    def test_priority2_skills_hint_used_when_no_embedded_arch(self) -> None:
        body = "Content without arch comment."
        result = _resolve_cognitive_arch(
            body, "python-developer", skills_hint=["fastapi", "python"]
        )
        # Figure comes from ROLE_DEFAULT_FIGURE["python-developer"], skills from hint.
        assert result.endswith(":fastapi:python")

    def test_priority3_ac_skills_comment_used_when_no_arch_or_hint(self) -> None:
        body = "Content.\n<!-- ac:skills: htmx, jinja2 -->"
        result = _resolve_cognitive_arch(body, "python-developer")
        assert result.endswith(":htmx:jinja2")

    def test_priority4_keyword_fallback_when_nothing_else(self) -> None:
        body = "Implements a FastAPI router with Depends."
        result = _resolve_cognitive_arch(body, "python-developer")
        # Keyword scan picks up "fastapi" → "fastapi:python"
        assert "fastapi" in result

    def test_heuristic_fallback_for_legacy_issue_body(self) -> None:
        """Body with no comments at all still resolves via keyword scan."""
        body = "Add pytest fixtures for the database layer."
        result = _resolve_cognitive_arch(body, "test-engineer")
        assert isinstance(result, str)
        assert ":" in result  # figure:skills format


