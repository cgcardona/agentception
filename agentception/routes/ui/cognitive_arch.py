from __future__ import annotations

"""UI routes: cognitive architecture catalog (GET /cognitive-arch).

Reads all figure and skill-domain YAML files from the
``scripts/gen_prompts/cognitive_archetypes/`` tree and renders a read-only
browser at ``/cognitive-arch``.  The page requires no interactivity — it is
a static catalog that surfaces the full library of figures and skill domains
available when composing a COGNITIVE_ARCH string.

This route is intentionally decoupled from the persona-resolution logic in
``routes/roles.py``.  It reads raw YAML rather than constructing a resolved
:class:`~agentception.routes.roles.Persona` because the catalog must show
*all* entries including ones that would fail persona resolution (e.g. draft
figures without a complete ``prompt_injection`` block).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.config import settings as _settings
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

_ARCH_SUBPATH = Path("scripts") / "gen_prompts" / "cognitive_archetypes"


@dataclass
class FigureEntry:
    """A single figure loaded from a YAML file under cognitive_archetypes/figures/.

    ``compatible_skill_domains`` merges both the ``primary`` and ``secondary``
    skill domain lists from the YAML so callers need not inspect nested keys.
    """

    id: str
    display_name: str
    description: str
    compatible_skill_domains: list[str] = field(default_factory=list)


@dataclass
class SkillDomainEntry:
    """A single skill domain loaded from a YAML file under cognitive_archetypes/skill_domains/."""

    id: str
    display_name: str
    description: str


def _load_figures(root_dir: Path) -> list[FigureEntry]:
    """Load all ``*.yaml`` files from ``<root_dir>/scripts/.../figures/``.

    Skips files that:
    - cannot be parsed as YAML
    - do not contain a ``display_name`` key (draft/incomplete entries)
    - are not top-level dicts

    Returns entries sorted by ``display_name`` for stable rendering.

    Parameters
    ----------
    root_dir:
        Repo root directory.  The function constructs the full path to the
        figures directory internally so callers do not need to know the layout.
    """
    figures_dir = root_dir / _ARCH_SUBPATH / "figures"
    entries: list[FigureEntry] = []
    if not figures_dir.is_dir():
        logger.warning("⚠️ Figures directory not found: %s", figures_dir)
        return entries

    for path in sorted(figures_dir.glob("*.yaml")):
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("⚠️ Skipping %s — unexpected YAML shape", path.name)
                continue

            display_name_raw: object = raw.get("display_name")
            if not display_name_raw:
                logger.warning("⚠️ Skipping %s — missing display_name", path.name)
                continue

            skill_domains_raw: object = raw.get("skill_domains", {})
            compatible: list[str] = []
            if isinstance(skill_domains_raw, dict):
                for key in ("primary", "secondary"):
                    bucket: object = skill_domains_raw.get(key, [])
                    if isinstance(bucket, list):
                        compatible.extend(str(x) for x in bucket)

            entries.append(
                FigureEntry(
                    id=str(raw.get("id", path.stem)),
                    display_name=str(display_name_raw),
                    description=str(raw.get("description", "")).strip(),
                    compatible_skill_domains=compatible,
                )
            )
        except Exception as exc:
            logger.warning("⚠️ Failed to load figure %s: %s", path.name, exc)

    return entries


def _load_skill_domains(root_dir: Path) -> list[SkillDomainEntry]:
    """Load all ``*.yaml`` files from ``<root_dir>/scripts/.../skill_domains/``.

    Skips files that cannot be parsed or lack a ``display_name`` key.

    Returns entries sorted by ``display_name`` for stable rendering.

    Parameters
    ----------
    root_dir:
        Repo root directory.  The function constructs the full path internally.
    """
    skill_domains_dir = root_dir / _ARCH_SUBPATH / "skill_domains"
    entries: list[SkillDomainEntry] = []
    if not skill_domains_dir.is_dir():
        logger.warning("⚠️ Skill domains directory not found: %s", skill_domains_dir)
        return entries

    for path in sorted(skill_domains_dir.glob("*.yaml")):
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("⚠️ Skipping %s — unexpected YAML shape", path.name)
                continue

            display_name_raw: object = raw.get("display_name")
            if not display_name_raw:
                logger.warning("⚠️ Skipping %s — missing display_name", path.name)
                continue

            entries.append(
                SkillDomainEntry(
                    id=str(raw.get("id", path.stem)),
                    display_name=str(display_name_raw),
                    description=str(raw.get("description", "")).strip(),
                )
            )
        except Exception as exc:
            logger.warning("⚠️ Failed to load skill domain %s: %s", path.name, exc)

    return entries


@router.get("/cognitive-arch", response_class=HTMLResponse)
async def cognitive_arch_index(request: Request) -> HTMLResponse:
    """Cognitive Architecture catalog — all figures and skill domains.

    Reads ``scripts/gen_prompts/cognitive_archetypes/figures/`` and
    ``scripts/gen_prompts/cognitive_archetypes/skill_domains/`` from the repo
    root (``settings.repo_dir``) and renders a read-only browser.

    Template context
    ----------------
    ``figures``
        List of :class:`FigureEntry` instances sorted by display_name.
    ``skill_domains``
        List of :class:`SkillDomainEntry` instances sorted by display_name.
    """
    root = Path(_settings.repo_dir)
    figures = _load_figures(root)
    skill_domains = _load_skill_domains(root)

    logger.info(
        "✅ Cognitive arch catalog: %d figures, %d skill domains",
        len(figures),
        len(skill_domains),
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "cognitive_arch.html",
        {
            "figures": figures,
            "skill_domains": skill_domains,
        },
    )
