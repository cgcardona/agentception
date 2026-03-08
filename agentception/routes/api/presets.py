from __future__ import annotations

"""Org preset catalog API.

Endpoints
---------
GET /api/org-presets
    Return all preset summaries — id, name, description, icon, accent,
    node_count, group.  No tree data; suitable for rendering the picker grid.

GET /api/org-presets/{preset_id}
    Return a single preset's full detail including the recursive
    ``PresetNodeTemplate`` tree.  The frontend calls ``buildTree(template)``
    to materialise live OrgNode objects with fresh IDs.
"""

from fastapi import APIRouter, HTTPException

from agentception.data.org_presets import (
    OrgPresetDetail,
    OrgPresetSummary,
    get_preset,
    list_presets,
)

router = APIRouter()


@router.get("/org-presets", response_model=list[OrgPresetSummary])
async def get_org_presets() -> list[OrgPresetSummary]:
    """List all org preset summaries (no tree data)."""
    return list_presets()


@router.get("/org-presets/{preset_id}", response_model=OrgPresetDetail)
async def get_org_preset(preset_id: str) -> OrgPresetDetail:
    """Return a single preset's full detail including the tree template."""
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Preset '{preset_id}' not found.")
    return preset
