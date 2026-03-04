"""UI routes: Org Chart page with preset org picker, interactive builder, and D3 tree.

Provides:
- ``GET /org-chart`` — full-page render with left panel preset picker and
  right panel interactive builder.
- ``POST /api/org/select-preset`` — HTMX endpoint that persists the chosen
  preset to ``pipeline-config.json`` and refreshes the left panel.
- ``GET /api/org/tree`` — returns the active preset as a hierarchical JSON
  tree consumed by the D3 tree visualization in ``org_chart_tree.js``.
- ``GET /api/org/taxonomy`` — returns the role taxonomy grouped by tier (org-chart view)
  (c_suite / vp / worker) for the Add Role dropdown.
- ``POST /api/org/roles/add`` — adds a role to the active builder org and
  returns a refreshed role list partial.
- ``DELETE /api/org/roles/{slug}`` — removes a role from the builder org and
  returns a refreshed role list partial.
- ``POST /api/org/roles/{slug}/phases`` — updates assigned phases for a role
  card and returns the refreshed role list partial.
- ``POST /api/org/templates`` — saves the current builder org as a new preset
  in ``org-presets.yaml`` and returns the refreshed preset list partial.

Preset definitions are loaded from ``org-presets.yaml`` at the repo root.
Role list and phase assignments are persisted to ``pipeline-config.json``
under the ``active_org_roles`` key so they survive page reloads.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, TypedDict

import yaml  # type: ignore[import-untyped]
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from agentception.config import settings
from agentception.models import OrgTreeNode, OrgTreeRole
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

_PRESETS_PATH = Path(__file__).parent.parent.parent.parent / "org-presets.yaml"

# Path to role-taxonomy.yaml — used by /api/org/tree to enrich role nodes.
_TAXONOMY_PATH = (
    Path(__file__).parent.parent.parent.parent / "scripts" / "gen_prompts" / "role-taxonomy.yaml"
)


def _pipeline_config_path() -> Path:
    """Return the canonical path to pipeline-config.json for the active repo."""
    return settings.ac_dir / "pipeline-config.json"


# ---------------------------------------------------------------------------
# Role taxonomy — tiers used for the Add Role grouped dropdown.
# Roles not in any tier fall into "worker" implicitly (via the _OTHER sentinel).
# ---------------------------------------------------------------------------

#: Display labels for each tier (builder dropdown keys).
TIER_LABELS: dict[str, str] = {
    "c_suite": "C-Suite",
    "coordinator": "Coordinator",
    "worker": "Worker",
}

#: Tier label mapping for the D3 tree endpoint — keyed by taxonomy YAML level ids.
_TREE_TIER_LABELS: dict[str, str] = {
    "c_suite": "C-Suite",
    "coordinator": "Coordinator",
    "workers": "Worker",
}

#: Canonical role taxonomy, ordered within each tier for display.
ROLE_TAXONOMY: dict[str, list[str]] = {
    "c_suite": [
        "cto", "ceo", "coo", "cfo", "cpo", "cmo", "cdo", "ciso",
    ],
    "coordinator": [
        "engineering-coordinator", "qa-coordinator", "vp-product", "vp-data", "vp-design",
        "vp-infrastructure", "vp-ml", "vp-mobile", "vp-platform",
        "vp-security", "coordinator",
    ],
    "worker": [
        "python-developer", "api-developer", "database-architect",
        "frontend-developer", "typescript-developer", "react-developer",
        "ios-developer", "android-developer", "mobile-developer",
        "full-stack-developer", "go-developer", "rails-developer",
        "rust-developer", "test-engineer", "pr-reviewer", "devops-engineer",
        "security-engineer", "site-reliability-engineer", "ml-engineer",
        "ml-researcher", "data-engineer", "data-scientist",
        "systems-programmer", "technical-writer", "architect",
        "muse-specialist",
    ],
}

#: Tiers that get the phase-assignment multiselect on their role card.
_PHASE_ASSIGNABLE_TIERS: frozenset[str] = frozenset({"c_suite", "vp"})


def _tier_for_slug(slug: str) -> str:
    """Return the tier key for *slug*, defaulting to ``'worker'``."""
    for tier, slugs in ROLE_TAXONOMY.items():
        if slug in slugs:
            return tier
    return "worker"


# ---------------------------------------------------------------------------
# Typed role index entry for the D3 tree endpoint
# ---------------------------------------------------------------------------


class _RoleIndexEntry(TypedDict):
    """Enriched metadata for a single role slug from role-taxonomy.yaml."""

    tier: str
    compatible_figures: list[str]
    label: str
    title: str


# ---------------------------------------------------------------------------
# Typed role entry stored in pipeline-config.json
# ---------------------------------------------------------------------------


def _make_role_entry(slug: str) -> dict[str, Any]:
    """Create a fresh role entry dict with an empty assigned_phases list."""
    return {"slug": slug, "assigned_phases": []}


# ---------------------------------------------------------------------------
# pipeline-config.json I/O
# ---------------------------------------------------------------------------


def _read_pipeline_config() -> dict[str, Any]:
    """Read pipeline-config.json, returning an empty dict on any error."""
    path = _pipeline_config_path()
    if not path.exists():
        return {}
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.warning("⚠️ Could not read pipeline-config.json: %s", exc)
        return {}


def _write_pipeline_config(data: dict[str, Any]) -> None:
    """Persist *data* back to pipeline-config.json atomically via a rename."""
    path = _pipeline_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.rename(path)


def _read_active_org_roles(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and validate the ``active_org_roles`` list from *config*.

    Returns an empty list when the key is missing or malformed.  Each entry
    is coerced to have at least ``slug`` (str) and ``assigned_phases`` (list).
    """
    raw: object = config.get("active_org_roles", [])
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        phases: object = item.get("assigned_phases", [])
        if not isinstance(phases, list):
            phases = []
        result.append({"slug": slug, "assigned_phases": list(phases)})
    return result


def _read_phases(config: dict[str, Any]) -> list[str]:
    """Return phase label strings from *config*'s ``active_labels_order`` list."""
    raw: object = config.get("active_labels_order", [])
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str)]


# ---------------------------------------------------------------------------
# org-presets.yaml I/O
# ---------------------------------------------------------------------------


def _load_presets() -> list[dict[str, Any]]:
    """Read and parse org-presets.yaml, returning the ``presets`` list.

    Returns an empty list if the file is missing or malformed so the UI
    degrades gracefully rather than raising a 500.
    """
    try:
        raw: object = yaml.safe_load(_PRESETS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return []
        presets: object = raw.get("presets", [])
        if not isinstance(presets, list):
            return []
        return [p for p in presets if isinstance(p, dict)]
    except Exception as exc:
        logger.warning("⚠️ Could not load org-presets.yaml: %s", exc)
        return []


def _save_preset(name: str, roles: list[dict[str, Any]]) -> str:
    """Append a new preset to org-presets.yaml and return its generated slug.

    The slug is derived from the name by lowercasing and replacing spaces with
    hyphens.  If a preset with the same slug already exists it is overwritten.
    """
    slug = name.lower().replace(" ", "-")
    try:
        raw_text = _PRESETS_PATH.read_text(encoding="utf-8")
        raw: object = yaml.safe_load(raw_text)
        if not isinstance(raw, dict):
            raw = {}
        raw_dict: dict[str, Any] = raw  # type: ignore[assignment]
    except Exception:
        raw_dict = {}

    presets: object = raw_dict.get("presets", [])
    if not isinstance(presets, list):
        presets = []
    preset_list: list[dict[str, Any]] = [
        p for p in presets if isinstance(p, dict) and p.get("id") != slug
    ]

    # Build tiers from the roles list.
    leadership: list[str] = []
    workers: list[str] = []
    for entry in roles:
        role_slug: object = entry.get("slug")
        if not isinstance(role_slug, str):
            continue
        tier = _tier_for_slug(role_slug)
        if tier in ("c_suite", "vp"):
            leadership.append(role_slug)
        else:
            workers.append(role_slug)

    new_preset: dict[str, Any] = {
        "id": slug,
        "name": name,
        "description": f"Custom org template with {len(roles)} role(s).",
        "tiers": {"leadership": leadership, "workers": workers},
    }
    preset_list.append(new_preset)
    raw_dict["presets"] = preset_list

    tmp = _PRESETS_PATH.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(raw_dict, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.rename(_PRESETS_PATH)
    logger.info("✅ Saved preset %r to org-presets.yaml", slug)
    return slug


# ---------------------------------------------------------------------------
# Helper: build taxonomy role index for /api/org/tree
# ---------------------------------------------------------------------------


def _load_taxonomy_role_index() -> dict[str, _RoleIndexEntry]:
    """Build a flat slug → role-metadata dict from role-taxonomy.yaml.

    Returns an empty dict if the taxonomy file is missing or malformed so the
    tree endpoint degrades gracefully.  Each value includes ``tier`` (display
    string) and ``compatible_figures`` (list of strings).
    """
    try:
        raw: object = yaml.safe_load(_TAXONOMY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        index: dict[str, _RoleIndexEntry] = {}
        for level in raw.get("levels", []):
            if not isinstance(level, dict):
                continue
            level_id = str(level.get("id", ""))
            tier_label = _TREE_TIER_LABELS.get(level_id, level_id)
            for role in level.get("roles", []):
                if not isinstance(role, dict):
                    continue
                slug = str(role.get("slug", ""))
                if slug:
                    figures: object = role.get("compatible_figures", [])
                    index[slug] = _RoleIndexEntry(
                        tier=tier_label,
                        compatible_figures=(
                            [str(f) for f in figures] if isinstance(figures, list) else []
                        ),
                        label=str(role.get("label", slug)),
                        title=str(role.get("title", slug)),
                    )
        return index
    except Exception as exc:
        logger.warning("⚠️ Could not load role-taxonomy.yaml: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Helper: build template context for the builder right panel
# ---------------------------------------------------------------------------


def _builder_context(
    config: dict[str, Any],
    *,
    active_org_id: str | None = None,
) -> dict[str, Any]:
    """Return the shared context dict for the builder panel partials."""
    active_org_roles = _read_active_org_roles(config)
    phases = _read_phases(config)

    # Annotate each role entry with its tier so templates can branch.
    annotated: list[dict[str, Any]] = []
    for entry in active_org_roles:
        tier = _tier_for_slug(entry["slug"])
        annotated.append(
            {
                "slug": entry["slug"],
                "assigned_phases": entry["assigned_phases"],
                "tier": tier,
                "phase_assignable": tier in _PHASE_ASSIGNABLE_TIERS,
            }
        )

    return {
        "active_org_roles": annotated,
        "phases": phases,
        "taxonomy": ROLE_TAXONOMY,
        "tier_labels": TIER_LABELS,
        "active_org_id": active_org_id,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/org-chart", response_class=HTMLResponse)
async def org_chart_page(request: Request) -> HTMLResponse:
    """Org Chart page — preset picker (left) + interactive builder (right).

    The right panel contains the role builder (#829) with a D3 tree panel
    shell (#830).
    """
    presets = _load_presets()
    config = _read_pipeline_config()
    active_org: object = config.get("active_org")
    active_org_id: str | None = active_org if isinstance(active_org, str) else None

    return _TEMPLATES.TemplateResponse(
        request,
        "org_chart.html",
        {
            "presets": presets,
            **_builder_context(config, active_org_id=active_org_id),
        },
    )


@router.get("/api/org/tree", response_class=JSONResponse)
async def org_tree() -> JSONResponse:
    """Return the active preset org as a hierarchical JSON tree for the D3 visualization.

    Reads the active preset from pipeline-config.json, enriches each role with
    tier and compatible-figure data from role-taxonomy.yaml, and returns a nested
    ``OrgTreeNode`` tree.  The root node is the preset itself; children are the
    tier groups (leadership / workers); each tier's ``roles`` list holds the role
    cards rendered by ``org_chart_tree.js``.

    Returns HTTP 404 when no preset is selected or the active preset id is unknown.
    """
    config = _read_pipeline_config()
    active_org: object = config.get("active_org")
    if not isinstance(active_org, str) or not active_org:
        raise HTTPException(status_code=404, detail="No active org selected. Choose a preset first.")

    presets = _load_presets()
    preset = next((p for p in presets if p.get("id") == active_org), None)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Active preset {active_org!r} not found in org-presets.yaml")

    role_index = _load_taxonomy_role_index()

    def _make_role(slug: str) -> OrgTreeRole:
        meta: _RoleIndexEntry = role_index.get(
            slug,
            _RoleIndexEntry(tier="Worker", compatible_figures=[], label=slug, title=slug),
        )
        figures: list[str] = meta.get("compatible_figures", [])
        return OrgTreeRole(
            slug=slug,
            name=meta.get("label", slug),
            tier=meta.get("tier", "Worker"),
            assigned_phases=[],
            figures=figures[:2],
        )

    tiers_raw: object = preset.get("tiers", {})
    if not isinstance(tiers_raw, dict):
        tiers_raw = {}

    tier_children: list[OrgTreeNode] = []
    tier_display = {"leadership": "Leadership", "workers": "Workers"}
    for tier_key in ("leadership", "workers"):
        slugs: object = tiers_raw.get(tier_key, [])
        if not isinstance(slugs, list):
            continue
        roles = [_make_role(str(s)) for s in slugs]
        tier_children.append(
            OrgTreeNode(
                name=tier_display.get(tier_key, tier_key.title()),
                id=tier_key,
                tier=tier_key,
                roles=roles,
                children=[],
            )
        )

    root = OrgTreeNode(
        name=str(preset.get("name", active_org)),
        id=active_org,
        tier="org",
        roles=[],
        children=tier_children,
    )

    logger.info("✅ /api/org/tree built for preset %r (%d tiers)", active_org, len(tier_children))
    return JSONResponse(content=root.model_dump())


@router.post("/api/org/select-preset", response_class=HTMLResponse)
async def select_preset(
    request: Request,
    preset_id: str = Form(...),
) -> HTMLResponse:
    """Persist the chosen preset and return a refreshed left-panel partial.

    Writes ``active_org`` to ``pipeline-config.json`` and swaps the left panel
    so the selected card gets the ``active`` CSS class without a full-page reload.

    Returns HTTP 422 when *preset_id* does not match any known preset.
    """
    presets = _load_presets()
    preset_ids = {p["id"] for p in presets if "id" in p}

    if preset_id not in preset_ids:
        logger.warning("⚠️ Unknown preset_id requested: %s", preset_id)
        raise HTTPException(status_code=422, detail=f"Unknown preset: {preset_id!r}")

    config = _read_pipeline_config()
    config["active_org"] = preset_id
    try:
        _write_pipeline_config(config)
        logger.info("✅ Active org set to %r", preset_id)
    except Exception as exc:
        logger.error("❌ Failed to write pipeline-config.json: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to persist preset selection") from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "_org_preset_list.html",
        {
            "presets": presets,
            "active_org_id": preset_id,
        },
    )


@router.get("/api/org/taxonomy")
async def roles_taxonomy() -> JSONResponse:
    """Return the role taxonomy grouped by tier for the Add Role dropdown.

    Response shape::

        {
          "tiers": {
            "c_suite":  {"label": "C-Suite",         "roles": ["cto", ...]},
            "vp":       {"label": "VP / Coordinator", "roles": ["vp-engineering", ...]},
            "worker":   {"label": "Worker",           "roles": ["python-developer", ...]}
          }
        }
    """
    tiers: dict[str, dict[str, Any]] = {
        tier: {"label": TIER_LABELS[tier], "roles": roles}
        for tier, roles in ROLE_TAXONOMY.items()
    }
    return JSONResponse({"tiers": tiers})


@router.post("/api/org/roles/add", response_class=HTMLResponse)
async def add_role(
    request: Request,
    slug: Annotated[str, Form()],
) -> HTMLResponse:
    """Append *slug* to ``active_org_roles`` in pipeline-config.json.

    Silently ignores duplicates so double-submits are idempotent.  Returns the
    refreshed ``_org_role_list.html`` partial which HTMX swaps into the builder.

    Raises HTTP 422 when *slug* is not in the taxonomy.
    """
    all_slugs: set[str] = {
        s for slugs in ROLE_TAXONOMY.values() for s in slugs
    }
    if slug not in all_slugs:
        raise HTTPException(status_code=422, detail=f"Unknown role slug: {slug!r}")

    config = _read_pipeline_config()
    roles = _read_active_org_roles(config)

    if not any(r["slug"] == slug for r in roles):
        roles.append(_make_role_entry(slug))
        config["active_org_roles"] = roles
        try:
            _write_pipeline_config(config)
            logger.info("✅ Added role %r to active org", slug)
        except Exception as exc:
            logger.error("❌ Failed to write pipeline-config.json: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to add role") from exc
    else:
        logger.info("ℹ️ Role %r already present — skipping duplicate", slug)

    return _TEMPLATES.TemplateResponse(
        request,
        "_org_role_list.html",
        _builder_context(config),
    )


@router.delete("/api/org/roles/{slug}", response_class=HTMLResponse)
async def remove_role(
    request: Request,
    slug: str,
) -> HTMLResponse:
    """Remove the role identified by *slug* from ``active_org_roles``.

    Returns the refreshed ``_org_role_list.html`` partial.  Silently succeeds
    even if *slug* is not currently in the list (idempotent).
    """
    config = _read_pipeline_config()
    roles = _read_active_org_roles(config)
    updated = [r for r in roles if r["slug"] != slug]

    if len(updated) != len(roles):
        config["active_org_roles"] = updated
        try:
            _write_pipeline_config(config)
            logger.info("✅ Removed role %r from active org", slug)
        except Exception as exc:
            logger.error("❌ Failed to write pipeline-config.json: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to remove role") from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "_org_role_list.html",
        _builder_context(config),
    )


@router.post("/api/org/roles/{slug}/phases", response_class=HTMLResponse)
async def update_role_phases(
    request: Request,
    slug: str,
) -> HTMLResponse:
    """Persist the phase assignment for *slug* from the multiselect form data.

    Reads ``phases`` from the raw request form (list of strings — one per
    selected phase label).  Returns the refreshed ``_org_role_list.html``
    partial.

    Raises HTTP 404 when *slug* is not in the current role list.
    """
    form = await request.form()
    # ``phases`` may appear as multiple values from a multi-select.
    # form.getlist returns list[UploadFile | str]; filter to strings only.
    selected_phases: list[str] = [
        v for v in form.getlist("phases") if isinstance(v, str)
    ]

    config = _read_pipeline_config()
    roles = _read_active_org_roles(config)

    target_idx: int | None = None
    for i, r in enumerate(roles):
        if r["slug"] == slug:
            target_idx = i
            break

    if target_idx is None:
        raise HTTPException(status_code=404, detail=f"Role {slug!r} not in active org")

    roles[target_idx]["assigned_phases"] = selected_phases
    config["active_org_roles"] = roles
    try:
        _write_pipeline_config(config)
        logger.info("✅ Updated phases for role %r: %s", slug, selected_phases)
    except Exception as exc:
        logger.error("❌ Failed to write pipeline-config.json: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to update phases") from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "_org_role_list.html",
        _builder_context(config),
    )


@router.post("/api/org/templates", response_class=HTMLResponse)
async def save_template(
    request: Request,
    name: Annotated[str, Form()],
) -> HTMLResponse:
    """Save the current builder org as a named preset in org-presets.yaml.

    After saving, returns the refreshed ``_org_preset_list.html`` partial so
    HTMX swaps it into the left panel — the new preset appears immediately.

    Raises HTTP 422 when *name* is blank or the current role list is empty.
    """
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Template name must not be blank")

    config = _read_pipeline_config()
    roles = _read_active_org_roles(config)

    if not roles:
        raise HTTPException(
            status_code=422,
            detail="Cannot save a template with no roles. Add at least one role first.",
        )

    try:
        slug = _save_preset(name, roles)
        logger.info("✅ Saved org template %r (id=%r)", name, slug)
    except Exception as exc:
        logger.error("❌ Failed to save template: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save template") from exc

    presets = _load_presets()
    active_org: object = config.get("active_org")
    active_org_id: str | None = active_org if isinstance(active_org, str) else None

    return _TEMPLATES.TemplateResponse(
        request,
        "_org_preset_list.html",
        {
            "presets": presets,
            "active_org_id": active_org_id,
        },
    )
