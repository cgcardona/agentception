"""Org preset catalog — static library of agent hierarchy templates.

Served by ``agentception.routes.api.presets`` via:
  GET /api/org-presets            → list of summaries (no tree)
  GET /api/org-presets/{id}       → full detail with tree template

Templates encode the *shape* of an org tree using role slugs.  Slugs
must match the ``ROLE_GROUPS`` / ``CHILD_ROLE_RULES`` catalog in
``org_designer.ts``.  When the user picks a preset the frontend calls
``buildTree(template)`` to materialise fresh OrgNode IDs.

Groups (and their display order on the picker):
  engineering | data | executive | product | marketing | security | operations
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


# ── Domain types ──────────────────────────────────────────────────────────────

PresetGroup = Literal[
    "engineering", "data", "executive", "product", "marketing", "security", "operations"
]


class PresetNodeTemplate(BaseModel):
    """One node in an org tree template — role slug + optional children."""

    role: str
    figure: str = ""
    children: list["PresetNodeTemplate"] = []


PresetNodeTemplate.model_rebuild()


class OrgPresetSummary(BaseModel):
    """Lightweight preset descriptor returned by the list endpoint."""

    id: str
    name: str
    description: str
    icon: str
    accent: str
    node_count: int
    group: PresetGroup


class OrgPresetDetail(OrgPresetSummary):
    """Full preset including the recursive tree template."""

    template: PresetNodeTemplate


# ── Catalog helpers ───────────────────────────────────────────────────────────


def _count(tmpl: PresetNodeTemplate) -> int:
    return 1 + sum(_count(c) for c in tmpl.children)


def _t(role: str, *children: PresetNodeTemplate, figure: str = "") -> PresetNodeTemplate:
    """Concise node constructor for use inside the catalog literals."""
    return PresetNodeTemplate(role=role, figure=figure, children=list(children))


def _mk(
    preset_id: str,
    name: str,
    description: str,
    icon: str,
    accent: str,
    group: PresetGroup,
    template: PresetNodeTemplate,
) -> OrgPresetDetail:
    return OrgPresetDetail(
        id=preset_id,
        name=name,
        description=description,
        icon=icon,
        accent=accent,
        node_count=_count(template),
        group=group,
        template=template,
    )


# ── Preset catalog ────────────────────────────────────────────────────────────

_CATALOG: list[OrgPresetDetail] = [

    # ── Engineering ───────────────────────────────────────────────────────────

    _mk(
        "builtin-cto-full", "CTO + Full Team",
        "CTO surveys all tickets & PRs, spawns an Engineering Manager and QA Lead "
        "who each assemble their own workers.",
        "⬡", "violet", "engineering",
        _t("cto",
           _t("engineering-coordinator",
              _t("developer"),
              _t("developer")),
           _t("qa-coordinator",
              _t("reviewer"))),
    ),

    _mk(
        "builtin-eng-sprint", "Engineering Sprint",
        "Engineering Manager pulls a single phase, spawns dev workers and a PR Reviewer. "
        "Fast and focused.",
        "⚡", "blue", "engineering",
        _t("engineering-coordinator",
           _t("developer"),
           _t("developer"),
           _t("reviewer")),
    ),

    _mk(
        "builtin-qa-pass", "QA Review Pass",
        "QA Lead surveys all open PRs and dispatches a dedicated reviewer for each one.",
        "◎", "amber", "engineering",
        _t("qa-coordinator",
           _t("reviewer"),
           _t("test-engineer")),
    ),

    _mk(
        "builtin-quick-fix", "Quick Fix",
        "One engineer, one reviewer. The smallest possible team — ideal for a single focused ticket.",
        "⚑", "cyan", "engineering",
        _t("engineering-coordinator",
           _t("developer")),
    ),

    _mk(
        "builtin-api-focus", "API Sprint",
        "Engineering Manager drives a focused API sprint — developers work in parallel "
        "with language and stack set by cognitive architecture at dispatch.",
        "⟨⟩", "blue", "engineering",
        _t("engineering-coordinator",
           _t("developer"),
           _t("developer"),
           _t("developer"),
           _t("reviewer")),
    ),

    _mk(
        "builtin-full-stack-sprint", "Parallel Dev Sprint",
        "Eng Manager coordinates multiple developers working in parallel — cognitive "
        "architecture assigns each agent its language and framework specialisation.",
        "◈", "violet", "engineering",
        _t("engineering-coordinator",
           _t("developer"),
           _t("developer"),
           _t("developer"),
           _t("reviewer")),
    ),

    _mk(
        "builtin-platform-reliability", "Platform Reliability",
        "CTO delegates infra and platform concerns — DevOps and SRE keep the lights on.",
        "⊞", "teal", "engineering",
        _t("cto",
           _t("platform-coordinator",
              _t("devops-engineer"),
              _t("site-reliability-engineer")),
           _t("infrastructure-coordinator",
              _t("developer"),
              _t("devops-engineer"))),
    ),

    _mk(
        "builtin-multi-lang", "Polyglot Sprint",
        "Eng Manager fields multiple developers in parallel — each agent's language "
        "and framework is set by cognitive architecture at dispatch time.",
        "⬙", "blue", "engineering",
        _t("engineering-coordinator",
           _t("developer"),
           _t("developer"),
           _t("developer"),
           _t("developer"),
           _t("reviewer")),
    ),

    # ── ML / Data ─────────────────────────────────────────────────────────────

    _mk(
        "builtin-ml-team", "ML Team",
        "ML Coordinator assembles a research and engineering crew — great for data-heavy "
        "initiatives.",
        "◆", "teal", "data",
        _t("ml-coordinator",
           _t("ml-engineer"),
           _t("ml-researcher"),
           _t("data-scientist")),
    ),

    _mk(
        "builtin-ml-research", "ML Research Crew",
        "Deep research team: ML researchers explore new models while engineers prototype "
        "and data scientists validate.",
        "⬟", "cyan", "data",
        _t("ml-coordinator",
           _t("ml-researcher"),
           _t("ml-researcher"),
           _t("ml-engineer"),
           _t("data-scientist")),
    ),

    _mk(
        "builtin-data-pipeline", "Data Pipeline",
        "CDO delegates data infrastructure to a Data Coordinator who orchestrates engineers "
        "and a scientist for validation.",
        "⊳", "teal", "data",
        _t("cdo",
           _t("data-coordinator",
              _t("data-engineer"),
              _t("data-engineer"),
              _t("data-scientist"))),
    ),

    _mk(
        "builtin-data-full", "CDO + Full Data Org",
        "Chief Data Officer commands both data engineering and ML research tracks "
        "simultaneously.",
        "⬡", "violet", "data",
        _t("cdo",
           _t("data-coordinator",
              _t("data-engineer"),
              _t("data-scientist")),
           _t("ml-coordinator",
              _t("ml-engineer"),
              _t("ml-researcher"))),
    ),

    # ── Executive ─────────────────────────────────────────────────────────────

    _mk(
        "builtin-single-cto", "Single CTO",
        "One CTO agent working solo. Surveys the initiative and decides what to do next. "
        "Perfect for smoke tests.",
        "✦", "emerald", "executive",
        _t("cto"),
    ),

    _mk(
        "builtin-ceo-full", "CEO + Full Org",
        "CEO delegates to a CTO who assembles the full engineering and QA hierarchy "
        "beneath them — developers are specialised via cognitive architecture at dispatch.",
        "◇", "rose", "executive",
        _t("ceo",
           _t("cto",
              _t("engineering-coordinator",
                 _t("developer"),
                 _t("developer")),
              _t("qa-coordinator",
                 _t("reviewer")))),
    ),

    _mk(
        "builtin-engineering-security", "Engineering + Security",
        "CEO splits authority between CTO (build) and CISO (secure) — engineering and "
        "security sub-teams work in parallel.",
        "⊕", "amber", "executive",
        _t("ceo",
           _t("cto",
              _t("engineering-coordinator",
                 _t("developer"),
                 _t("developer"))),
           _t("ciso",
              _t("security-coordinator",
                 _t("security-engineer"),
                 _t("test-engineer")))),
    ),

    _mk(
        "builtin-product-engineering", "Product + Engineering",
        "CEO pairs CTO with CPO — engineering ships features while product and design define the experience.",
        "⊛", "blue", "executive",
        _t("ceo",
           _t("cto",
              _t("engineering-coordinator",
                 _t("developer"),
                 _t("developer")),
              _t("qa-coordinator",
                 _t("reviewer"))),
           _t("cpo",
              _t("design-coordinator",
                 _t("developer"),
                 _t("technical-writer")))),
    ),

    _mk(
        "builtin-cto-cdo", "Engineering + Data",
        "CEO commands CTO and CDO in parallel — build the product and build the intelligence "
        "that runs it.",
        "⬤", "teal", "executive",
        _t("ceo",
           _t("cto",
              _t("engineering-coordinator",
                 _t("developer"),
                 _t("developer"))),
           _t("cdo",
              _t("ml-coordinator",
                 _t("ml-engineer"),
                 _t("data-scientist")))),
    ),

    # ── Product / Design ──────────────────────────────────────────────────────

    _mk(
        "builtin-product-launch", "Product Launch",
        "CPO coordinates product managers, UX designers, and developers for a "
        "polished feature launch.",
        "◉", "emerald", "product",
        _t("cpo",
           _t("product-coordinator",
              _t("technical-writer"),
              _t("content-writer")),
           _t("design-coordinator",
              _t("developer"),
              _t("developer"))),
    ),

    _mk(
        "builtin-mobile-launch", "Mobile Launch",
        "CPO deploys a mobile-first team — developers ship cross-platform with mobile "
        "cognitive architecture assigned at dispatch.",
        "⬡", "blue", "product",
        _t("cpo",
           _t("mobile-coordinator",
              _t("developer"),
              _t("developer"),
              _t("developer"))),
    ),

    _mk(
        "builtin-design-sprint", "Design Sprint",
        "Design Lead drives a rapid UX iteration — developers ship alongside a technical writer.",
        "⬙", "violet", "product",
        _t("design-coordinator",
           _t("developer"),
           _t("developer"),
           _t("technical-writer")),
    ),

    # ── Marketing ─────────────────────────────────────────────────────────────

    _mk(
        "builtin-cmo-content", "Content Team",
        "CMO drives a content and design blitz — writers, developers, and a technical writer ship assets.",
        "✎", "amber", "marketing",
        _t("cmo",
           _t("design-coordinator",
              _t("content-writer"),
              _t("developer"),
              _t("technical-writer"))),
    ),

    # ── Security ──────────────────────────────────────────────────────────────

    _mk(
        "builtin-security-audit", "Security Audit",
        "CISO runs a focused security review — engineers probe for vulnerabilities and "
        "test engineers validate fixes.",
        "⏣", "rose", "security",
        _t("ciso",
           _t("security-coordinator",
              _t("security-engineer"),
              _t("security-engineer"),
              _t("test-engineer"))),
    ),

    # ── Operations ────────────────────────────────────────────────────────────

    _mk(
        "builtin-docs-sprint", "Docs Sprint",
        "A focused documentation push — technical writers and content writers collaborating "
        "under the Design Lead.",
        "⎗", "emerald", "operations",
        _t("design-coordinator",
           _t("technical-writer"),
           _t("technical-writer"),
           _t("content-writer")),
    ),

]

_INDEX: dict[str, OrgPresetDetail] = {p.id: p for p in _CATALOG}


# ── Public API ────────────────────────────────────────────────────────────────


def list_presets() -> list[OrgPresetSummary]:
    """Return all preset summaries (no template tree)."""
    return [
        OrgPresetSummary(
            id=p.id,
            name=p.name,
            description=p.description,
            icon=p.icon,
            accent=p.accent,
            node_count=p.node_count,
            group=p.group,
        )
        for p in _CATALOG
    ]


def get_preset(preset_id: str) -> OrgPresetDetail | None:
    """Return a full preset by id, or None if not found."""
    return _INDEX.get(preset_id)
