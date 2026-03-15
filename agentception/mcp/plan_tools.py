"""AgentCeption MCP plan tools — schema inspection and validation.

Provides four MCP-exposed functions:

``plan_get_schema()``
    Returns the JSON Schema for :class:`~agentception.models.PlanSpec`.
    The schema is generated from the Pydantic model at call time and cached
    for the process lifetime so repeated ``tools/call`` invocations are fast.

``plan_validate_spec(spec_json)``
    Parses a JSON string and validates it against :class:`~agentception.models.PlanSpec`.
    Returns a structured result dict indicating success or failure with
    human-readable error messages.

``plan_get_labels()``
    Async.  Fetches the full GitHub label list for the configured repository
    via :func:`agentception.readers.github.gh_json`.  Returns a list of
    ``{"name": str, "description": str}`` dicts for use as LLM context.

``plan_validate_manifest(json_text)``
    Parses a JSON string and validates it against
    :class:`~agentception.models.EnrichedManifest`.  Returns computed
    ``total_issues`` and ``estimated_waves`` invariants alongside the validated
    manifest dict.

Boundary constraint: zero imports from external packages.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import NotRequired, TypedDict

import yaml

from pydantic import ValidationError

from agentception.types import JsonSchemaObj, JsonValue

from agentception.models import EnrichedManifest, PlanSpec
from agentception.readers.github import get_repo_labels

logger = logging.getLogger(__name__)

# Path to the cognitive archetypes directory (repo root / scripts / gen_prompts / ...)
_ARCHETYPES_DIR: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "cognitive_archetypes" / "figures"
)
_TAXONOMY_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "scripts" / "gen_prompts" / "role-taxonomy.yaml"
)


class CognitiveFigureEntry(TypedDict):
    """A single cognitive figure returned by ``plan_get_cognitive_figures``."""

    id: str
    display_name: str
    description: str


class CognitiveFiguresResult(TypedDict):
    """Return shape of ``plan_get_cognitive_figures``."""

    role: str
    figures: list[CognitiveFigureEntry]
    error: NotRequired[str]


class ValidateSpecSuccessResult(TypedDict):
    """Successful PlanSpec validation result."""

    valid: bool
    spec: dict[str, JsonValue]


class ValidationErrorResult(TypedDict):
    """Failed validation result (shared by spec and manifest)."""

    valid: bool
    errors: list[str]


class RepoLabelEntry(TypedDict):
    """A single label from the GitHub label catalogue."""

    name: str
    description: str


class LabelsResult(TypedDict):
    """Return shape of ``plan_get_labels``."""

    labels: list[RepoLabelEntry]


class ValidateManifestSuccessResult(TypedDict):
    """Successful EnrichedManifest validation result."""

    valid: bool
    manifest: dict[str, JsonValue]
    total_issues: int
    estimated_waves: int


def _load_compatible_figures(role: str) -> list[str] | None:
    """Return the ``compatible_figures`` list for *role* from the taxonomy.

    Returns ``None`` when the taxonomy file is absent or the role slug is not
    found in any level.
    """
    if not _TAXONOMY_PATH.exists():
        logger.warning("⚠️ role-taxonomy.yaml not found at %s", _TAXONOMY_PATH)
        return None
    raw: JsonValue = yaml.safe_load(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    raw_levels: JsonValue = raw.get("levels", [])
    if not isinstance(raw_levels, list):
        return None
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        raw_roles: JsonValue = level.get("roles", [])
        if not isinstance(raw_roles, list):
            continue
        for role_entry in raw_roles:
            if not isinstance(role_entry, dict):
                continue
            if role_entry.get("slug") == role:
                figures = role_entry.get("compatible_figures", [])
                if isinstance(figures, list):
                    return [str(f) for f in figures]
    return None


def _figure_entry(figure_id: str) -> CognitiveFigureEntry | None:
    """Read a single figure YAML and return its entry dict.

    Returns ``None`` when the file does not exist or is malformed.
    """
    path = _ARCHETYPES_DIR / f"{figure_id}.yaml"
    if not path.exists():
        return None
    try:
        raw: JsonValue = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    display_name = str(raw.get("display_name", figure_id))
    desc_raw = raw.get("description", "")
    desc = str(desc_raw).strip()
    # Trim to first sentence for concise LLM context.
    first_sentence = desc.split("\n")[0].split(". ")[0].rstrip(".")
    return {"id": figure_id, "display_name": display_name, "description": first_sentence}


def plan_get_cognitive_figures(role: str) -> CognitiveFiguresResult:
    """Return the catalog of cognitive figures compatible with *role*.

    Reads ``role-taxonomy.yaml`` to obtain the ``compatible_figures`` list for
    the given role slug, then reads each figure's YAML from
    ``scripts/gen_prompts/cognitive_archetypes/figures/`` to produce a concise
    catalog entry (id, display name, one-line description).

    This tool is designed for use by the Phase 1A LLM planner and by any
    orchestration agent that needs to make an informed cognitive-architecture
    assignment.  The returned list is already filtered to the role, so the
    caller receives only the figures that are semantically appropriate for that
    position in the org hierarchy.

    Args:
        role: A role slug from ``role-taxonomy.yaml`` — e.g. ``"cto"``,
              ``"engineering-coordinator"``, ``"qa-coordinator"``,
              ``"developer"``.

    Returns:
        On success:
        ``{"role": str, "figures": [{"id": str, "display_name": str,
          "description": str}, ...]}``

        When the role slug is unknown:
        ``{"role": str, "figures": [], "error": "Role not found in taxonomy"}``

        When no figures are configured for the role:
        ``{"role": str, "figures": [], "error": "No compatible figures for role"}``
    """
    compatible = _load_compatible_figures(role)
    if compatible is None:
        logger.warning("⚠️ plan_get_cognitive_figures: role %r not found in taxonomy", role)
        return {"role": role, "figures": [], "error": "Role not found in taxonomy"}
    if not compatible:
        logger.warning("⚠️ plan_get_cognitive_figures: no compatible figures for role %r", role)
        return {"role": role, "figures": [], "error": "No compatible figures for role"}

    entries: list[CognitiveFigureEntry] = []
    for fig_id in compatible:
        entry = _figure_entry(fig_id)
        if entry is not None:
            entries.append(entry)

    logger.info(
        "✅ plan_get_cognitive_figures: role=%r → %d figure(s)", role, len(entries)
    )
    return {"role": role, "figures": entries}

# Module-level cache: populated on the first call to plan_get_schema().
_schema_cache: JsonSchemaObj | None = None


def plan_get_schema() -> JsonSchemaObj:
    """Return the JSON Schema for PlanSpec.

    The schema is generated once from the Pydantic model and cached for the
    process lifetime.  Callers receive a reference to the cached dict — do
    not mutate it.

    Returns:
        A ``dict[str, JsonValue]`` containing the full JSON Schema for
        :class:`~agentception.models.PlanSpec`, including all nested
        definitions for ``PlanPhase`` and ``PlanIssue``.
    """
    global _schema_cache
    if _schema_cache is None:
        raw: JsonSchemaObj = PlanSpec.model_json_schema()
        _schema_cache = raw
        logger.debug("✅ PlanSpec JSON schema generated and cached")
    return _schema_cache


def plan_validate_spec(spec_json: str) -> ValidateSpecSuccessResult | ValidationErrorResult:
    """Validate a JSON string against the PlanSpec schema.

    Parses ``spec_json`` as JSON and attempts to construct a
    :class:`~agentception.models.PlanSpec` from the parsed data.
    Pydantic's full validation stack runs — including the phase DAG
    invariant checker — so any structural or semantic error is reported.

    Args:
        spec_json: A UTF-8 JSON string expected to represent a PlanSpec.

    Returns:
        On success: ``{"valid": True, "spec": <serialised PlanSpec dict>}``
        On JSON parse failure: ``{"valid": False, "errors": ["JSON parse error: ..."]}``
        On Pydantic validation failure: ``{"valid": False, "errors": [<list of error strings>]}``

    Never raises — all errors are captured and returned in the result dict
    so that the MCP caller receives a well-formed tool result in every case.
    """
    try:
        raw: JsonValue = json.loads(spec_json)
    except json.JSONDecodeError as exc:
        logger.warning("⚠️ plan_validate_spec: JSON parse error — %s", exc)
        return {"valid": False, "errors": [f"JSON parse error: {exc}"]}

    try:
        spec = PlanSpec.model_validate(raw)
    except ValidationError as exc:
        errors: list[str] = [
            f"{' -> '.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        logger.info("ℹ️ plan_validate_spec: validation failed — %d error(s)", len(errors))
        return {"valid": False, "errors": errors}
    except Exception as exc:
        logger.warning("⚠️ plan_validate_spec: unexpected error — %s", exc)
        return {"valid": False, "errors": [f"Validation error: {exc}"]}

    logger.debug("✅ plan_validate_spec: spec is valid")
    spec_dict: dict[str, JsonValue] = json.loads(spec.model_dump_json())
    return {"valid": True, "spec": spec_dict}


# ---------------------------------------------------------------------------
# Issue #871 additions — label context, manifest validation, coordinator spawn
# ---------------------------------------------------------------------------


async def plan_get_labels() -> LabelsResult:
    """Fetch the full GitHub label list for the configured repository.

    Uses :func:`agentception.readers.github.get_repo_labels` to call the
    GitHub REST API and returns the result in a shape suitable for use as LLM
    context when assigning labels to enriched issues.

    Returns:
        ``{"labels": [{"name": str, "description": str}, ...]}``
        Returns an empty list when the API call fails or returns no labels.
    """
    from agentception.config import settings

    repo = settings.gh_repo
    raw = await get_repo_labels(limit=100)

    labels: list[RepoLabelEntry] = []
    for item in raw:
        name = item.get("name", "")
        description = item.get("description", "")
        labels.append(RepoLabelEntry(
            name=str(name),
            description=str(description) if description else "",
        ))

    logger.info("✅ plan_get_labels: fetched %d labels from %s", len(labels), repo)
    return {"labels": labels}


def plan_validate_manifest(json_text: str) -> ValidateManifestSuccessResult | ValidationErrorResult:
    """Validate a JSON string against the EnrichedManifest schema.

    Parses ``json_text`` as JSON and validates it against
    :class:`~agentception.models.EnrichedManifest`.  Both ``total_issues`` and
    ``estimated_waves`` are computed invariants derived by the model validator
    so the returned values are always authoritative regardless of what the
    caller supplied.

    Args:
        json_text: A JSON-encoded string representing an ``EnrichedManifest``.

    Returns:
        On success:
        ``{"valid": True, "manifest": {...}, "total_issues": int,
        "estimated_waves": int}``

        On failure:
        ``{"valid": False, "errors": [str, ...]}``

    Never raises — all errors are captured in the result dict.
    """
    try:
        raw: JsonValue = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.warning("⚠️ plan_validate_manifest: JSON parse error — %s", exc)
        return {"valid": False, "errors": [f"JSON parse error: {exc}"]}

    try:
        manifest = EnrichedManifest.model_validate(raw)
    except ValidationError as exc:
        errors: list[str] = [
            f"{' -> '.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        logger.info(
            "ℹ️ plan_validate_manifest: validation failed — %d error(s)", len(errors)
        )
        return {"valid": False, "errors": errors}
    except Exception as exc:
        logger.warning("⚠️ plan_validate_manifest: unexpected error — %s", exc)
        return {"valid": False, "errors": [f"Validation error: {exc}"]}

    manifest_dict: dict[str, JsonValue] = json.loads(manifest.model_dump_json())
    logger.info(
        "✅ plan_validate_manifest: valid — %d issues, %d waves",
        manifest.total_issues,
        manifest.estimated_waves,
    )
    return {
        "valid": True,
        "manifest": manifest_dict,
        "total_issues": manifest.total_issues,
        "estimated_waves": manifest.estimated_waves,
    }


