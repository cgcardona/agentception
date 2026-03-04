from __future__ import annotations

"""AgentCeption MCP plan tools — schema inspection, validation, and coordinator spawn.

Provides five MCP-exposed functions:

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

``plan_spawn_coordinator(manifest_json)``
    Async.  Validates the manifest, creates a git worktree, and writes a
    ``.agent-task`` file for the coordinator agent.

Boundary constraint: zero imports from maestro, muse, kly, or storpheus.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from agentception.models import EnrichedManifest, PlanSpec
from agentception.readers.github import gh_json

logger = logging.getLogger(__name__)

# Module-level cache: populated on the first call to plan_get_schema().
_schema_cache: dict[str, object] | None = None


def plan_get_schema() -> dict[str, object]:
    """Return the JSON Schema for PlanSpec.

    The schema is generated once from the Pydantic model and cached for the
    process lifetime.  Callers receive a reference to the cached dict — do
    not mutate it.

    Returns:
        A ``dict[str, object]`` containing the full JSON Schema for
        :class:`~agentception.models.PlanSpec`, including all nested
        definitions for ``PlanPhase`` and ``PlanIssue``.
    """
    global _schema_cache
    if _schema_cache is None:
        raw: dict[str, object] = PlanSpec.model_json_schema()
        _schema_cache = raw
        logger.debug("✅ PlanSpec JSON schema generated and cached")
    return _schema_cache


def plan_validate_spec(spec_json: str) -> dict[str, object]:
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
        raw: object = json.loads(spec_json)
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
    return {"valid": True, "spec": spec.model_dump()}


# ---------------------------------------------------------------------------
# Issue #871 additions — label context, manifest validation, coordinator spawn
# ---------------------------------------------------------------------------


async def plan_get_labels() -> dict[str, object]:
    """Fetch the full GitHub label list for the configured repository.

    Uses :func:`agentception.readers.github.gh_json` to call
    ``gh label list --json name,description`` and returns the result in a
    shape suitable for use as LLM context when assigning labels to enriched
    issues.

    Returns:
        ``{"labels": [{"name": str, "description": str}, ...]}``
        Returns an empty list if the gh CLI returns an unexpected type.
    """
    from agentception.config import settings

    repo = settings.gh_repo
    args = [
        "label", "list",
        "--repo", repo,
        "--json", "name,description",
        "--limit", "100",
    ]
    result = await gh_json(args, ".", "plan_get_labels")
    if not isinstance(result, list):
        logger.warning(
            "⚠️ plan_get_labels: unexpected gh output type %s", type(result).__name__
        )
        return {"labels": []}

    labels: list[dict[str, str]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        description = item.get("description", "")
        labels.append({
            "name": str(name),
            "description": str(description) if description else "",
        })

    logger.info("✅ plan_get_labels: fetched %d labels from %s", len(labels), repo)
    return {"labels": labels}


def plan_validate_manifest(json_text: str) -> dict[str, object]:
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
        raw: object = json.loads(json_text)
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

    manifest_dict: dict[str, object] = json.loads(manifest.model_dump_json())
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


async def plan_spawn_coordinator(manifest_json: str) -> dict[str, object]:
    """Validate a manifest and spawn a coordinator git worktree.

    Steps:
    1. Validate ``manifest_json`` via :func:`plan_validate_manifest`.
    2. Generate a timestamped slug (e.g. ``coordinator-20260303-142201``).
    3. Run ``git worktree add /tmp/worktrees/<slug> -b coordinator/<stamp>``
       via ``asyncio.create_subprocess_exec``.
    4. Write a ``.agent-task`` file with ``WORKFLOW=bugs-to-issues`` and
       the ``ENRICHED_MANIFEST:`` JSON block.
    5. Return ``{"worktree": str, "branch": str, "agent_task_path": str,
       "batch_id": str}``.

    Args:
        manifest_json: JSON-encoded ``EnrichedManifest`` string.

    Returns:
        On success: ``{"worktree", "branch", "agent_task_path", "batch_id"}``
        On invalid manifest: ``{"error": str}``

    Raises:
        RuntimeError: When ``git worktree add`` exits with a non-zero status.
    """
    validation = plan_validate_manifest(manifest_json)
    if not validation.get("valid"):
        errors = validation.get("errors", ["unknown validation error"])
        logger.warning("⚠️ plan_spawn_coordinator: manifest validation failed — %s", errors)
        return {"error": f"Invalid manifest: {errors}"}

    manifest_dict = validation.get("manifest", {})

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = f"coordinator-{stamp}"
    branch = f"coordinator/{stamp}"
    worktree_path = f"/tmp/worktrees/{slug}"

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode().strip()
        logger.error(
            "❌ plan_spawn_coordinator: git worktree add failed — %s", err_msg
        )
        raise RuntimeError(
            f"git worktree add failed (exit {proc.returncode}): {err_msg!r}"
        )

    logger.info("✅ plan_spawn_coordinator: worktree created at %s", worktree_path)

    manifest_json_pretty = json.dumps(manifest_dict, indent=2)
    agent_task_content = (
        f"WORKFLOW=bugs-to-issues\n"
        f"BATCH_ID={slug}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={worktree_path}\n"
        f"\n"
        f"ENRICHED_MANIFEST:\n"
        f"```json\n"
        f"{manifest_json_pretty}\n"
        f"```\n"
    )

    agent_task_path = str(Path(worktree_path) / ".agent-task")
    try:
        Path(agent_task_path).write_text(agent_task_content, encoding="utf-8")
    except Exception as exc:
        # Worktree was created; clean it up to avoid orphaned state.
        cleanup = await asyncio.create_subprocess_exec(
            "git", "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await cleanup.communicate()
        logger.error(
            "❌ plan_spawn_coordinator: .agent-task write failed, worktree removed — %s", exc
        )
        raise

    logger.info(
        "✅ plan_spawn_coordinator: .agent-task written to %s", agent_task_path
    )

    return {
        "worktree": worktree_path,
        "branch": branch,
        "agent_task_path": agent_task_path,
        "batch_id": slug,
    }
