"""Tests for agentception/routes/api/presets.py and agentception/data/org_presets.py.

HTTP routes:
    GET  /api/org-presets              → list of OrgPresetSummary (no tree)
    GET  /api/org-presets/{preset_id}  → OrgPresetDetail (includes template tree)

Data layer (pure functions):
    list_presets()   — returns all summaries
    get_preset(id)   — returns OrgPresetDetail | None
    _count(tmpl)     — recursive node counter
    _t(role, ...)    — concise node constructor
    _mk(...)         — catalog entry factory

Run targeted:
    pytest agentception/tests/test_presets_api.py -v
"""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.data.org_presets import (
    OrgPresetDetail,
    OrgPresetSummary,
    PresetNodeTemplate,
    _count,
    _mk,
    _t,
    get_preset,
    list_presets,
)


def _all_ids() -> list[str]:
    """Return the IDs of every preset in the catalog via the public API."""
    return [s.id for s in list_presets()]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


# ── GET /api/org-presets ──────────────────────────────────────────────────────


def test_list_presets_returns_200(client: TestClient) -> None:
    """GET /api/org-presets must respond HTTP 200."""
    response = client.get("/api/org-presets")
    assert response.status_code == 200


def test_list_presets_returns_a_list(client: TestClient) -> None:
    """GET /api/org-presets must return a JSON array."""
    body = client.get("/api/org-presets").json()
    assert isinstance(body, list)


def test_list_presets_is_non_empty(client: TestClient) -> None:
    """GET /api/org-presets must include at least one preset."""
    body = client.get("/api/org-presets").json()
    assert len(body) > 0


def test_list_presets_items_have_summary_fields(client: TestClient) -> None:
    """Every item in GET /api/org-presets must expose the OrgPresetSummary fields."""
    body = client.get("/api/org-presets").json()
    required = {"id", "name", "description", "icon", "accent", "node_count", "group"}
    for item in body:
        assert required.issubset(item.keys()), f"Missing fields in {item}"


def test_list_presets_items_omit_template(client: TestClient) -> None:
    """GET /api/org-presets must NOT include the tree template (summary only)."""
    body = client.get("/api/org-presets").json()
    for item in body:
        assert "template" not in item, f"Unexpected template in summary for {item['id']}"


def test_list_presets_groups_are_valid(client: TestClient) -> None:
    """Every preset's 'group' must be one of the seven declared PresetGroup literals."""
    valid_groups = {
        "engineering", "data", "executive", "product",
        "marketing", "security", "operations",
    }
    body = client.get("/api/org-presets").json()
    for item in body:
        assert item["group"] in valid_groups, (
            f"Preset '{item['id']}' has invalid group '{item['group']}'"
        )


def test_list_presets_node_counts_are_positive(client: TestClient) -> None:
    """Every preset must have node_count >= 1."""
    body = client.get("/api/org-presets").json()
    for item in body:
        assert item["node_count"] >= 1, f"Preset '{item['id']}' has node_count={item['node_count']}"


def test_list_presets_ids_are_unique(client: TestClient) -> None:
    """All preset IDs returned by the list endpoint must be unique."""
    body = client.get("/api/org-presets").json()
    ids = [item["id"] for item in body]
    assert len(ids) == len(set(ids)), "Duplicate preset IDs in catalog"


# ── GET /api/org-presets/{preset_id} ─────────────────────────────────────────


def test_get_preset_returns_200_for_known_id(client: TestClient) -> None:
    """GET /api/org-presets/{id} returns HTTP 200 for any catalog preset."""
    # Use the first preset from the list to avoid hard-coding a specific ID.
    first_id = client.get("/api/org-presets").json()[0]["id"]
    response = client.get(f"/api/org-presets/{first_id}")
    assert response.status_code == 200


def test_get_preset_returns_404_for_unknown_id(client: TestClient) -> None:
    """GET /api/org-presets/{id} returns HTTP 404 for an unrecognised preset ID."""
    response = client.get("/api/org-presets/does-not-exist")
    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


def test_get_preset_includes_template(client: TestClient) -> None:
    """GET /api/org-presets/{id} response must include a 'template' field with a 'role'."""
    first_id = client.get("/api/org-presets").json()[0]["id"]
    body = client.get(f"/api/org-presets/{first_id}").json()
    assert "template" in body
    assert "role" in body["template"]
    assert isinstance(body["template"]["role"], str)
    assert body["template"]["role"]  # non-empty


def test_get_preset_node_count_matches_template_depth(client: TestClient) -> None:
    """The node_count in the detail response must match the actual tree depth."""
    first_id = client.get("/api/org-presets").json()[0]["id"]
    body = client.get(f"/api/org-presets/{first_id}").json()

    def _count_json(node: dict[str, object]) -> int:
        children = node.get("children", [])
        assert isinstance(children, list)
        return 1 + sum(_count_json(c) for c in children)

    assert body["node_count"] == _count_json(body["template"])


def test_get_specific_builtin_cto_full(client: TestClient) -> None:
    """GET /api/org-presets/builtin-cto-full returns the expected root role."""
    response = client.get("/api/org-presets/builtin-cto-full")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "builtin-cto-full"
    assert body["template"]["role"] == "cto"
    assert body["group"] == "engineering"


def test_all_catalog_presets_are_individually_reachable(client: TestClient) -> None:
    """Every preset listed by the catalog must be reachable via its own detail endpoint."""
    ids = [item["id"] for item in client.get("/api/org-presets").json()]
    for preset_id in ids:
        response = client.get(f"/api/org-presets/{preset_id}")
        assert response.status_code == 200, f"Preset '{preset_id}' returned {response.status_code}"


# ── Data-layer unit tests ─────────────────────────────────────────────────────


def test_list_presets_count_is_stable() -> None:
    """`list_presets()` must return the same count on repeated calls (no mutation)."""
    first = len(list_presets())
    second = len(list_presets())
    assert first == second
    assert first > 0


def test_list_presets_returns_summary_not_detail() -> None:
    """`list_presets()` returns OrgPresetSummary objects, not OrgPresetDetail."""
    for item in list_presets():
        assert isinstance(item, OrgPresetSummary)
        assert not isinstance(item, OrgPresetDetail)


def test_get_preset_returns_none_for_unknown_id() -> None:
    """`get_preset` returns None for an unrecognised ID."""
    assert get_preset("nonexistent-id") is None


def test_get_preset_returns_detail_for_known_id() -> None:
    """`get_preset` returns an OrgPresetDetail for a known catalog ID."""
    first_id = _all_ids()[0]
    result = get_preset(first_id)
    assert isinstance(result, OrgPresetDetail)
    assert result.id == first_id


def test_get_preset_detail_includes_template() -> None:
    """`get_preset` detail includes the full PresetNodeTemplate tree."""
    first_id = _all_ids()[0]
    detail = get_preset(first_id)
    assert detail is not None
    assert isinstance(detail.template, PresetNodeTemplate)
    assert detail.template.role


def test_count_single_node() -> None:
    """`_count` returns 1 for a leaf node with no children."""
    assert _count(_t("developer")) == 1


def test_count_flat_children() -> None:
    """`_count` returns 1 + n for a root with n leaf children."""
    root = _t("engineering-coordinator", _t("developer"), _t("developer"))
    assert _count(root) == 3


def test_count_nested_children() -> None:
    """`_count` correctly sums a two-level nested tree."""
    tree = _t("cto",
              _t("engineering-coordinator",
                 _t("developer"),
                 _t("developer")),
              _t("qa-coordinator",
                 _t("reviewer")))
    # cto(1) + eng-coord(1) + python-dev(1) + go-dev(1) + qa-coord(1) + reviewer(1) = 6
    assert _count(tree) == 6


def test_t_sets_role_and_children() -> None:
    """`_t` correctly populates role and children on the returned node."""
    child_a = _t("developer")
    child_b = _t("developer")
    parent = _t("engineering-coordinator", child_a, child_b)
    assert parent.role == "engineering-coordinator"
    assert len(parent.children) == 2
    assert parent.children[0].role == "developer"
    assert parent.children[1].role == "developer"


def test_t_with_figure() -> None:
    """`_t` propagates the optional figure argument."""
    node = _t("cto", figure="feynman")
    assert node.figure == "feynman"


def test_t_leaf_has_empty_children() -> None:
    """`_t` with no children produces an empty children list."""
    node = _t("reviewer")
    assert node.children == []


def test_mk_node_count_matches_tree() -> None:
    """`_mk` computes node_count from the template automatically."""
    tmpl = _t("cto", _t("developer"), _t("developer"))
    detail = _mk("test-preset", "Test", "Desc", "⬡", "blue", "engineering", tmpl)
    assert detail.node_count == _count(tmpl)  # == 3


def test_catalog_ids_are_all_builtin_prefixed() -> None:
    """All catalog preset IDs must start with 'builtin-'."""
    for preset_id in _all_ids():
        assert preset_id.startswith("builtin-"), f"Non-builtin ID in catalog: {preset_id}"


def test_all_presets_have_non_empty_name_and_description() -> None:
    """Every catalog preset must have a non-empty name and description."""
    for preset_id in _all_ids():
        detail = get_preset(preset_id)
        assert detail is not None
        assert detail.name.strip(), f"Preset '{preset_id}' has blank name"
        assert detail.description.strip(), f"Preset '{preset_id}' has blank description"
