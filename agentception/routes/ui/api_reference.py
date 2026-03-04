"""UI route: native API reference page."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

#: Human-readable labels and display order for OpenAPI tags.
_API_TAG_META: dict[str, str] = {
    "ui":           "UI Pages",
    "api":          "REST API",
    "control":      "Control Plane",
    "intelligence": "Intelligence",
    "roles":        "Roles",
    "config":       "Configuration",
    "telemetry":    "Telemetry",
    "templates":    "Templates",
    "sse":          "Server-Sent Events",
    "health":       "Health",
}


def _resolve_ref(schema_root: dict[str, object], ref: str) -> dict[str, object]:
    """Walk a JSON Pointer like '#/components/schemas/Foo' and return the node."""
    parts = ref.lstrip("#/").split("/")
    node: object = schema_root
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part, {})
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _resolve_schema(
    schema_root: dict[str, object],
    schema: dict[str, object],
    depth: int = 0,
) -> dict[str, object]:
    """Recursively resolve $ref and allOf, capped to avoid circular loops."""
    if depth > 5:
        return schema
    if "$ref" in schema:
        resolved = _resolve_ref(schema_root, str(schema["$ref"]))
        return _resolve_schema(schema_root, resolved, depth + 1)
    if "allOf" in schema:
        merged: dict[str, object] = {}
        all_of = schema.get("allOf", [])
        for sub in (all_of if isinstance(all_of, list) else []):
            if isinstance(sub, dict):
                merged.update(_resolve_schema(schema_root, sub, depth + 1))
        return merged
    return schema


def _schema_to_fields(
    schema_root: dict[str, object],
    schema: dict[str, object],
    depth: int = 0,
) -> list[dict[str, object]]:
    """Flatten a JSON Schema object into a list of field descriptors."""
    resolved = _resolve_schema(schema_root, schema, depth)
    props: dict[str, object] = {}
    if isinstance(resolved.get("properties"), dict):
        props = resolved["properties"]  # type: ignore[assignment]
    required_raw = resolved.get("required", [])
    required_set: set[str] = set(required_raw) if isinstance(required_raw, list) else set()
    fields: list[dict[str, object]] = []
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        prop = _resolve_schema(schema_root, prop, depth + 1)
        if prop.get("type") == "array":
            items = prop.get("items", {})
            if isinstance(items, dict):
                items = _resolve_schema(schema_root, items, depth + 2)
                type_str: str = f"array[{items.get('type', 'object')}]"
            else:
                type_str = "array"
        elif "anyOf" in prop:
            any_of = prop.get("anyOf", [])
            parts_list = [
                _resolve_schema(schema_root, t, depth + 1).get("type", "")
                for t in (any_of if isinstance(any_of, list) else [])
                if isinstance(t, dict)
            ]
            type_str = " | ".join(str(p) for p in parts_list if p and p != "null") or "any"
        else:
            type_str = str(prop.get("type", "any"))
        fields.append({
            "name": name,
            "type": type_str,
            "required": name in required_set,
            "description": str(prop.get("description", "")),
            "default": prop.get("default"),
        })
    return fields


def _build_api_groups(
    schema_root: dict[str, object],
) -> list[dict[str, object]]:
    """Group endpoints by their first tag and return ordered groups.

    Every endpoint dict carries the full set of fields Swagger UI exposes:
    deprecated, operationId, per-response content-type and schema fields.
    """
    paths: dict[str, object] = schema_root.get("paths", {})  # type: ignore[assignment]

    tag_order: list[str] = list(_API_TAG_META)
    buckets: dict[str, list[dict[str, object]]] = {t: [] for t in tag_order}

    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            tags: list[str] = op.get("tags", ["other"])
            tag = tags[0] if tags else "other"
            if tag not in buckets:
                buckets[tag] = []

            # ── Request body ───────────────────────────────────────────────
            request_body: dict[str, object] | None = None
            rb = op.get("requestBody")
            if isinstance(rb, dict):
                rb_content = rb.get("content", {})
                if isinstance(rb_content, dict):
                    # Prefer JSON; fall back to first available content type.
                    rb_ct = "application/json" if "application/json" in rb_content else (
                        next(iter(rb_content), "")
                    )
                    rb_ct_data: dict[str, object] = rb_content.get(rb_ct, {})
                    raw_schema = rb_ct_data.get("schema", {}) if isinstance(rb_ct_data, dict) else {}
                    if isinstance(raw_schema, dict):
                        ref_name = str(raw_schema.get("$ref", "")).split("/")[-1]
                        request_body = {
                            "required": bool(rb.get("required", False)),
                            "schema_name": ref_name,
                            "content_type": rb_ct,
                            "fields": _schema_to_fields(schema_root, raw_schema),
                        }

            # ── Responses ──────────────────────────────────────────────────
            responses: list[dict[str, object]] = []
            for code, resp_data in (op.get("responses") or {}).items():
                if not isinstance(resp_data, dict):
                    continue
                resp_content = resp_data.get("content") or {}
                # Pick primary content type (JSON preferred, then html, then first)
                resp_ct = ""
                resp_schema_raw: dict[str, object] = {}
                if isinstance(resp_content, dict) and resp_content:
                    for ct_pref in ("application/json", "text/html", "text/plain"):
                        if ct_pref in resp_content:
                            resp_ct = ct_pref
                            ct_data = resp_content[ct_pref]
                            if isinstance(ct_data, dict):
                                s = ct_data.get("schema", {})
                                resp_schema_raw = s if isinstance(s, dict) else {}
                            break
                    if not resp_ct:
                        resp_ct = next(iter(resp_content))

                ref_name = str(resp_schema_raw.get("$ref", "")).split("/")[-1]
                # Expand schema fields for non-trivial schemas (skip error types)
                skip_expand = {"HTTPValidationError", "ValidationError", ""}
                resp_fields = (
                    _schema_to_fields(schema_root, resp_schema_raw)
                    if ref_name not in skip_expand
                    else []
                )
                responses.append({
                    "code": str(code),
                    "description": str(resp_data.get("description", "")),
                    "schema_name": ref_name,
                    "content_type": resp_ct,
                    "fields": resp_fields,
                })

            buckets[tag].append({
                "method": method.upper(),
                "path": str(path),
                "summary": str(op.get("summary", "")),
                "description": str(op.get("description", "")),
                "operation_id": str(op.get("operationId", "")),
                "deprecated": bool(op.get("deprecated", False)),
                "parameters": op.get("parameters", []),
                "request_body": request_body,
                "responses": responses,
            })

    ordered = [t for t in tag_order if buckets.get(t)]
    extra = [t for t in buckets if t not in tag_order and buckets[t]]
    return [
        {
            "tag": tag,
            "label": _API_TAG_META.get(tag, tag.replace("-", " ").title()),
            "endpoints": buckets[tag],
        }
        for tag in ordered + extra
    ]


def _build_schema_models(schema_root: dict[str, object]) -> list[dict[str, object]]:
    """Return a sorted list of all component schemas with their resolved fields.

    Excludes low-signal FastAPI-generated error types.
    """
    components = schema_root.get("components", {})
    raw: dict[str, object] = (
        components.get("schemas", {}) if isinstance(components, dict) else {}
    )
    skip = {"HTTPValidationError", "ValidationError"}
    models: list[dict[str, object]] = []
    for name, s in raw.items():
        if name in skip or not isinstance(s, dict):
            continue
        resolved = _resolve_schema(schema_root, s)
        models.append({
            "name": name,
            "description": str(resolved.get("description", "")),
            "fields": _schema_to_fields(schema_root, resolved),
        })
    return sorted(models, key=lambda m: str(m["name"]))


@router.get("/api", response_class=HTMLResponse)
async def api_reference(request: Request) -> HTMLResponse:
    """Native API reference — renders the OpenAPI schema as a first-party branded page.

    Replaces FastAPI's built-in Swagger UI (which is disabled in app.py).
    The schema is pre-processed in Python ($refs resolved, endpoints grouped by
    tag, response schemas expanded, schema models collected) so the Jinja
    template receives clean structured data with no schema logic inside.
    """
    schema: dict[str, object] = request.app.openapi()
    info: dict[str, object] = schema.get("info", {})  # type: ignore[assignment]
    return _TEMPLATES.TemplateResponse(
        request,
        "api_reference.html",
        {
            "info": info,
            "groups": _build_api_groups(schema),
            "schema_models": _build_schema_models(schema),
        },
    )
