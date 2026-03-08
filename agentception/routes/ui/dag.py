"""UI route: dependency DAG visualisation page."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.intelligence.dag import DependencyDAG, IssueNode, build_dag
from agentception.readers.pipeline_config import read_pipeline_config
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/dag", response_class=HTMLResponse)
async def dag_page(request: Request) -> HTMLResponse:
    """Dependency DAG visualisation — D3.js force-directed graph of issue dependencies.

    Fetches all open issues, parses their dependency declarations, and renders
    an interactive SVG graph using D3.js (loaded from CDN).  Nodes are coloured
    by ``agentception/*`` phase label; the ``agent/wip`` issues are highlighted
    with a green stroke; closed nodes are rendered at 50% opacity.

    Enriches each node with:
    - ``blocking_count``: how many other issues depend on this one (fan-in).
    - ``depth``: topological level (0 = no dependencies, N = deepest blocker).

    Callers who need the raw DAG data should use ``GET /api/dag`` instead.
    """
    dag: DependencyDAG = await build_dag()
    phase_labels: list[str] = []
    try:
        pipeline_cfg = await read_pipeline_config()
        phase_labels = pipeline_cfg.active_labels_order
    except Exception:
        pass

    # --- Enrich nodes with blocking_count and depth -------------------------
    issue_nodes: list[IssueNode] = dag.nodes
    edges: list[tuple[int, int]] = dag.edges

    # blocking_count: for each node, count how many edges target it
    blocking: dict[int, int] = {}
    for src, tgt in edges:
        blocking[tgt] = blocking.get(tgt, 0) + 1

    # depth: BFS/topological level — "how far from a leaf are you?"
    # depth 0 = no deps (ready to start), higher = deeper chain
    deps_map: dict[int, list[int]] = {n.number: n.deps for n in issue_nodes}
    all_nums: set[int] = {n.number for n in issue_nodes}

    depth_cache: dict[int, int] = {}

    def _depth(num: int, visiting: set[int]) -> int:
        if num in depth_cache:
            return depth_cache[num]
        if num in visiting:
            return 0  # cycle guard
        visiting = visiting | {num}
        deps = [d for d in deps_map.get(num, []) if d in all_nums]
        if not deps:
            depth_cache[num] = 0
            return 0
        result = 1 + max(_depth(d, visiting) for d in deps)
        depth_cache[num] = result
        return result

    # Build enriched dicts for the Jinja2 template (adds blocking_count + depth).
    nodes: list[dict[str, object]] = []
    for node in issue_nodes:
        nd: dict[str, object] = node.model_dump()
        nd["blocking_count"] = blocking.get(node.number, 0)
        nd["depth"] = _depth(node.number, set())
        nodes.append(nd)

    # --- Summary stats for the page header ---------------------------------
    wip_count = sum(1 for n in issue_nodes if n.has_wip)
    open_count = sum(1 for n in issue_nodes if n.state.upper() == "OPEN")
    # depth_cache is dict[int, int] — populated by the _depth calls above.
    max_depth: int = max(depth_cache.values(), default=0)

    return _TEMPLATES.TemplateResponse(
        request,
        "dag.html",
        {
            "dag": {"nodes": nodes, "edges": edges},
            "phase_labels": phase_labels,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "wip_count": wip_count,
                "open_count": open_count,
                "max_depth": max_depth,
            },
        },
    )
