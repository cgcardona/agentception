"""Template export and import logic for AC-602.

A *template* is a versioned ``.tar.gz`` archive of the pipeline configuration
files that live under ``.agentception/`` in a Maestro repo.  Exporting
packages the current repo's managed files; importing extracts them into any
target repo's ``.agentception/`` directory.

Managed files that are always included when they exist:

- ``.agentception/roles/*.md``
- ``.agentception/prompts/*.md``
- ``.agentception/pipeline-config.json``
- ``.agentception/agent-command-policy.md``
- ``.agentception/dispatcher.md``

The archive also contains a ``template-manifest.json`` at the top level that
records provenance (name, version, created_at, gh_repo, file list).

Exported archives are stored under ``~/.agentception/templates/`` so they
persist across service restarts and are accessible from the UI.
"""
from __future__ import annotations

import io
import json
import logging
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from agentception.config import settings
from agentception.models import (
    TemplateConflict,
    TemplateImportResult,
    TemplateListEntry,
    TemplateManifest,
)

logger = logging.getLogger(__name__)

#: Directory where exported templates are stored persistently.
TEMPLATES_STORE: Path = Path.home() / ".agentception" / "templates"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _gather_managed_files(repo_dir: Path) -> list[Path]:
    """Return the list of managed pipeline files that exist in *repo_dir*.

    Files are relative to *repo_dir* — callers use them both as archive member
    names and as on-disk paths by joining with *repo_dir*.
    """
    ac_dir = repo_dir / ".agentception"
    candidates: list[Path] = []

    # Role files
    roles_dir = ac_dir / "roles"
    if roles_dir.is_dir():
        candidates.extend(sorted(roles_dir.glob("*.md")))

    # Prompt templates
    prompts_dir = ac_dir / "prompts"
    if prompts_dir.is_dir():
        candidates.extend(sorted(prompts_dir.glob("*.md")))

    # pipeline-config.json
    pc = ac_dir / "pipeline-config.json"
    if pc.exists():
        candidates.append(pc)

    # Top-level markdown files (dispatcher, policy, task spec, etc.)
    for name in (
        "dispatcher.md",
        "agent-command-policy.md",
        "agent-task-spec.md",
        "conflict-rules.md",
    ):
        p = ac_dir / name
        if p.exists():
            candidates.append(p)

    return [p for p in candidates if p.is_file()]


def _relative_str(path: Path, base: Path) -> str:
    """Return a POSIX-style relative path string of *path* under *base*."""
    return path.relative_to(base).as_posix()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_template(name: str, version: str) -> tuple[bytes, str]:
    """Build a ``.tar.gz`` archive of the current repo's pipeline config files.

    Parameters
    ----------
    name:
        Human-readable template name embedded in ``template-manifest.json``.
    version:
        Semver-style version string (e.g. ``"1.0.0"``).

    Returns
    -------
    tuple[bytes, str]
        ``(archive_bytes, filename)`` — the raw bytes of the ``.tar.gz``
        archive and the suggested filename for download/storage.
    """
    repo_dir = settings.repo_dir
    managed = _gather_managed_files(repo_dir)

    created_at = datetime.now(timezone.utc).isoformat()
    file_list = [_relative_str(p, repo_dir) for p in managed]

    manifest = TemplateManifest(
        name=name,
        version=version,
        created_at=created_at,
        gh_repo=settings.gh_repo,
        files=file_list,
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode()

    # Build the archive in memory so we can stream or persist without tmp files.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # manifest first so unpackers can inspect it cheaply
        manifest_info = tarfile.TarInfo(name="template-manifest.json")
        manifest_info.size = len(manifest_bytes)
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))

        for abs_path in managed:
            arc_name = _relative_str(abs_path, repo_dir)
            tar.add(str(abs_path), arcname=arc_name)

    archive_bytes = buf.getvalue()

    safe_name = name.replace(" ", "_").replace("/", "-")
    filename = f"{safe_name}-{version}.tar.gz"

    # Persist to the templates store so the UI can list past exports.
    TEMPLATES_STORE.mkdir(parents=True, exist_ok=True)
    dest = TEMPLATES_STORE / filename
    dest.write_bytes(archive_bytes)
    logger.info("✅ Exported template %s v%s → %s", name, version, dest)

    return archive_bytes, filename


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_template(archive_bytes: bytes, target_repo: str) -> TemplateImportResult:
    """Extract a template archive into *target_repo*'s ``.cursor/`` directory.

    Parameters
    ----------
    archive_bytes:
        Raw bytes of the ``.tar.gz`` archive to import.
    target_repo:
        Absolute filesystem path to the target repository root.

    Returns
    -------
    TemplateImportResult
        Lists all extracted paths and any conflicts (files that already existed
        before the import).

    Raises
    ------
    ValueError
        When the archive does not contain a valid ``template-manifest.json``.
    """
    target_path = Path(target_repo)
    if not target_path.is_dir():
        raise ValueError(f"Target repo directory does not exist: {target_repo!r}")

    buf = io.BytesIO(archive_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        members = tar.getmembers()

        # Extract and validate manifest first.
        manifest_member = next(
            (m for m in members if m.name == "template-manifest.json"), None
        )
        if manifest_member is None:
            raise ValueError("Archive is missing template-manifest.json")

        manifest_file = tar.extractfile(manifest_member)
        if manifest_file is None:
            raise ValueError("Cannot read template-manifest.json from archive")
        manifest_data: object = json.loads(manifest_file.read())
        if not isinstance(manifest_data, dict):
            raise ValueError("template-manifest.json is not a JSON object")
        manifest = TemplateManifest.model_validate(manifest_data)

        # Detect conflicts before writing anything.
        conflicts: list[TemplateConflict] = []
        for member in members:
            if member.name == "template-manifest.json":
                continue
            dest_file = target_path / member.name
            conflicts.append(
                TemplateConflict(path=member.name, exists=dest_file.exists())
            )

        # Extract all non-manifest members, creating parent dirs as needed.
        extracted: list[str] = []
        for member in members:
            if member.name == "template-manifest.json":
                continue
            dest_file = target_path / member.name
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            file_obj = tar.extractfile(member)
            if file_obj is None:
                continue
            dest_file.write_bytes(file_obj.read())
            extracted.append(member.name)

    logger.info(
        "✅ Imported template %s v%s into %s (%d files, %d conflicts)",
        manifest.name,
        manifest.version,
        target_repo,
        len(extracted),
        sum(1 for c in conflicts if c.exists),
    )
    return TemplateImportResult(
        name=manifest.name,
        version=manifest.version,
        extracted=extracted,
        conflicts=conflicts,
    )


# ---------------------------------------------------------------------------
# List stored templates
# ---------------------------------------------------------------------------


def list_stored_templates() -> list[TemplateListEntry]:
    """Return metadata for all previously exported templates in the store directory.

    Reads ``template-manifest.json`` from inside each ``.tar.gz`` file in
    ``TEMPLATES_STORE``.  Archives that cannot be parsed are silently skipped.

    Returns entries sorted most-recent-first by ``created_at``.
    """
    if not TEMPLATES_STORE.is_dir():
        return []

    entries: list[TemplateListEntry] = []
    for archive in TEMPLATES_STORE.glob("*.tar.gz"):
        try:
            with tarfile.open(archive, mode="r:gz") as tar:
                mf = tar.extractfile("template-manifest.json")
                if mf is None:
                    continue
                raw: object = json.loads(mf.read())
            if not isinstance(raw, dict):
                continue
            manifest = TemplateManifest.model_validate(raw)
            entries.append(
                TemplateListEntry(
                    filename=archive.name,
                    name=manifest.name,
                    version=manifest.version,
                    created_at=manifest.created_at,
                    gh_repo=manifest.gh_repo,
                    size_bytes=archive.stat().st_size,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Skipping unreadable template archive %s: %s", archive, exc)

    return sorted(entries, key=lambda e: e.created_at, reverse=True)
