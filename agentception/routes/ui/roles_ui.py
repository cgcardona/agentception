"""UI routes: Cognitive Architecture Studio and role detail pages."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.config import settings as _settings
from agentception.routes.roles import get_atoms, get_personas, get_taxonomy
from agentception.types import JsonValue
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

_ARCH_SUBPATH = Path("scripts") / "gen_prompts" / "cognitive_archetypes"


def _load_atom_descs(root: Path) -> dict[str, str]:
    """Return a flat {dimension:value → description} map from all atom YAML files.

    Keys are ``"dimension:value_id"`` strings (e.g. ``"epistemic_style:empirical"``)
    so Jinja2 can look them up with a single dict access.
    """
    atoms_dir = root / _ARCH_SUBPATH / "atoms"
    result: dict[str, str] = {}
    for path in sorted(atoms_dir.glob("*.yaml")):
        try:
            raw: JsonValue = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            dimension = str(raw.get("dimension", path.stem))
            values_raw: JsonValue = raw.get("values", {})
            if not isinstance(values_raw, dict):
                continue
            for val_id, val_data in values_raw.items():
                if isinstance(val_data, dict):
                    result[f"{dimension}:{val_id}"] = str(
                        val_data.get("description", "")
                    )
        except Exception:
            pass
    return result


def _load_archetype_desc(root: Path, archetype_id: str) -> str:
    """Return the description string for a named archetype, or empty string."""
    arch_file = root / _ARCH_SUBPATH / "archetypes" / f"{archetype_id}.yaml"
    if not arch_file.exists():
        return ""
    try:
        raw: JsonValue = yaml.safe_load(arch_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return str(raw.get("description", "")).strip()
    except Exception:
        pass
    return ""


def _load_skill_descs(root: Path) -> dict[str, str]:
    """Return a {skill_id → description} map from all skill-domain YAML files."""
    skill_dir = root / _ARCH_SUBPATH / "skill_domains"
    result: dict[str, str] = {}
    for path in sorted(skill_dir.glob("*.yaml")):
        try:
            raw: JsonValue = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("display_name"):
                result[str(raw.get("id", path.stem))] = str(
                    raw.get("description", "")
                ).strip()
        except Exception:
            pass
    return result


@router.get("/roles", response_class=HTMLResponse)
async def roles_page(request: Request) -> HTMLResponse:
    """Cognitive Architecture Studio — server-side rendered org tree with HTMX role selection."""
    try:
        taxonomy = await get_taxonomy()
        personas_resp = await get_personas()
        atoms_resp = await get_atoms()
    except Exception as exc:
        return _TEMPLATES.TemplateResponse(
            request, "roles.html",
            {"taxonomy": None, "personas_by_id": {}, "atoms": [], "error": str(exc)},
        )

    personas_by_id = {p.id: p for p in personas_resp.personas}
    return _TEMPLATES.TemplateResponse(
        request, "roles.html",
        {
            "taxonomy": taxonomy,
            "personas_by_id": personas_by_id,
            "atoms": atoms_resp.atoms,
            "error": None,
        },
    )


@router.get("/roles/{slug}/detail", response_class=HTMLResponse)
async def role_detail_partial(request: Request, slug: str) -> HTMLResponse:
    """HTMX partial — rendered when a role is selected in the org tree.

    Returns the center panel: persona cards for this role + the composer form.
    The editor content is loaded separately via the Monaco init in app.js.
    """
    try:
        taxonomy = await get_taxonomy()
        personas_resp = await get_personas()
        atoms_resp = await get_atoms()
    except Exception as exc:
        return HTMLResponse(f'<p class="text-muted" style="padding:1rem">Error: {exc}</p>')

    # Find the role in the taxonomy
    selected_role = None
    for level in taxonomy.levels:
        for role in level.roles:
            if role.slug == slug:
                selected_role = role
                break

    if selected_role is None:
        return HTMLResponse('<p class="text-muted" style="padding:1rem">Role not found.</p>')

    # Filter personas compatible with this role
    compatible_personas = [
        p for p in personas_resp.personas
        if p.id in selected_role.compatible_figures
    ]

    # Collect all unique skill domains across the full taxonomy (used by composer)
    all_skill_domains: list[str] = []
    seen_skills: set[str] = set()
    for level in taxonomy.levels:
        for role in level.roles:
            for s in role.compatible_skill_domains:
                if s not in seen_skills:
                    seen_skills.add(s)
                    all_skill_domains.append(s)
    all_skill_domains.sort()

    # Serialize Pydantic models to plain dicts so Jinja2 tojson works correctly.
    # `personas_json` is embedded as JSON in the Alpine component for client-side
    # "Apply to Composer" logic; `personas` and `all_personas` are for Jinja2 loops.
    personas_json = [p.model_dump() for p in compatible_personas]

    return _TEMPLATES.TemplateResponse(
        request, "_role_detail.html",
        {
            "role": selected_role,
            "personas": compatible_personas,
            "personas_json": personas_json,
            "all_personas": personas_resp.personas,
            "atoms": atoms_resp.atoms,
            "skill_domains": all_skill_domains,
            "slug": slug,
        },
    )


@router.get("/cognitive-arch/{arch_id}", response_class=HTMLResponse)
async def cognitive_arch_detail(request: Request, arch_id: str) -> HTMLResponse:
    """Cognitive architecture detail page — full visualisation of a figure or composed arch.

    ``arch_id`` is a URL-safe version of the COGNITIVE_ARCH string, with colons
    replaced by hyphens (e.g. ``steve_jobs-python-fastapi`` for
    ``steve_jobs:python:fastapi``).  The route normalises both forms so links
    from agent cards work regardless of encoding.
    """
    from agentception.routes.roles import resolve_cognitive_arch

    # Accept both colon-separated (raw) and hyphen-separated (URL-safe) forms.
    arch_str = arch_id.replace("-", ":") if ":" not in arch_id else arch_id
    persona = resolve_cognitive_arch(arch_str)

    root = Path(_settings.repo_dir)
    archetype_id = str(persona.get("archetype", ""))
    archetype_desc = _load_archetype_desc(root, archetype_id) if archetype_id else ""
    atom_descs = _load_atom_descs(root)
    skill_descs = _load_skill_descs(root)

    return _TEMPLATES.TemplateResponse(
        request,
        "cognitive_arch.html",
        {
            "arch_id": arch_id,
            "arch_str": arch_str,
            "persona": persona,
            "archetype_desc": archetype_desc,
            "atom_descs": atom_descs,
            "skill_descs": skill_descs,
        },
    )
