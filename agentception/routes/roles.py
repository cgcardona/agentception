from __future__ import annotations

"""Role file reader/writer API for the Role Studio editor (AC-301/303).

Exposes all managed ``.agentception/roles/*.md`` files
through a REST API so the Role Studio UI (AC-302/303) can list, read, update,
diff, commit, and inspect git history for each file without direct filesystem access.

Managed files are defined in ``_MANAGED_FILES`` — a hardcoded allowlist that
prevents arbitrary writes to the repository. Slugs are the dict keys; paths
are relative to ``settings.repo_dir``.

Also exposes the cognitive architecture meta-API:
- ``GET /api/roles/taxonomy`` — full three-tier org hierarchy from role-taxonomy.yaml
- ``GET /api/roles/personas`` — all figure YAMLs as structured JSON for the GUI
- ``GET /api/roles/atoms`` — all atom dimension YAMLs for the primitive composer
"""

import asyncio
import logging
import tempfile
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

from agentception.config import settings
from agentception.types import JsonValue
from agentception.intelligence.role_versions import (
    read_role_versions,
    record_version_bump,
)
from agentception.models import (
    AtomDimension,
    AtomValue,
    AtomsResponse,
    PersonaEntry,
    PersonasResponse,
    RoleCommitRequest,
    RoleCommitResponse,
    RoleContent,
    RoleDiffRequest,
    RoleDiffResponse,
    RoleMeta,
    RoleUpdateRequest,
    RoleUpdateResponse,
    RoleVersionEntry,
    RoleVersionInfo,
    RoleVersionsResponse,
    TaxonomyLevel,
    TaxonomyResponse,
    TaxonomyRole,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/roles", tags=["roles"])

# Allowlist of managed files. Slug → relative path from repo root.
# Only files in this dict can be read or written through the API.
_MANAGED_FILES: dict[str, str] = {
    # ── C-Suite ───────────────────────────────────────────────────────────
    "ceo": ".agentception/roles/ceo.md",
    "cto": ".agentception/roles/cto.md",
    "cpo": ".agentception/roles/cpo.md",
    "cfo": ".agentception/roles/cfo.md",
    "ciso": ".agentception/roles/ciso.md",
    "cdo": ".agentception/roles/cdo.md",
    "cmo": ".agentception/roles/cmo.md",
    "coo": ".agentception/roles/coo.md",
    # ── Coordinators (tree nodes — delegate to sub-coordinators or workers) ──
    "coordinator": ".agentception/roles/coordinator.md",
    "engineering-coordinator": ".agentception/roles/engineering-coordinator.md",
    "qa-coordinator": ".agentception/roles/qa-coordinator.md",
    "product-coordinator": ".agentception/roles/product-coordinator.md",
    "design-coordinator": ".agentception/roles/design-coordinator.md",
    "data-coordinator": ".agentception/roles/data-coordinator.md",
    "security-coordinator": ".agentception/roles/security-coordinator.md",
    "infrastructure-coordinator": ".agentception/roles/infrastructure-coordinator.md",
    "mobile-coordinator": ".agentception/roles/mobile-coordinator.md",
    "platform-coordinator": ".agentception/roles/platform-coordinator.md",
    "ml-coordinator": ".agentception/roles/ml-coordinator.md",
    # ── Workers (leaf agents — produce artifacts directly) ─────────────────
    # All developer variants collapsed into a single role; language/framework
    # specialisation is supplied by cognitive architecture at dispatch time.
    "developer": ".agentception/roles/developer.md",
    "database-architect": ".agentception/roles/database-architect.md",
    "reviewer": ".agentception/roles/reviewer.md",
    "ml-engineer": ".agentception/roles/ml-engineer.md",
    "ml-researcher": ".agentception/roles/ml-researcher.md",
    "data-engineer": ".agentception/roles/data-engineer.md",
    "data-scientist": ".agentception/roles/data-scientist.md",
    "devops-engineer": ".agentception/roles/devops-engineer.md",
    "security-engineer": ".agentception/roles/security-engineer.md",
    "site-reliability-engineer": ".agentception/roles/site-reliability-engineer.md",
    "test-engineer": ".agentception/roles/test-engineer.md",
    "architect": ".agentception/roles/architect.md",
    "technical-writer": ".agentception/roles/technical-writer.md",
    "content-writer": ".agentception/roles/content-writer.md",
    "legal-analyst": ".agentception/roles/legal-analyst.md",
    "ops-analyst": ".agentception/roles/ops-analyst.md",
}


async def _git_log_one(repo_dir: Path, rel_path: str) -> tuple[str, str]:
    """Return (sha, subject) of the most recent commit touching ``rel_path``.

    Returns empty strings when the file has never been committed or the git
    command fails — callers must tolerate missing history gracefully.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo_dir),
        "log", "-1", "--format=%H\t%s", "--", rel_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    line = stdout.decode().strip()
    if not line or "\t" not in line:
        return "", ""
    sha, _, subject = line.partition("\t")
    return sha.strip(), subject.strip()


async def _git_log_recent(repo_dir: Path, rel_path: str, n: int = 20) -> list[dict[str, str]]:
    """Return the last ``n`` commits touching ``rel_path`` as a list of dicts.

    Each dict has ``sha``, ``subject``, and ``date`` (ISO-8601 format).
    Returns an empty list when there are no commits for the file.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo_dir),
        "log", f"-{n}", "--format=%H\t%ai\t%s", "--", rel_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    entries: list[dict[str, str]] = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        entries.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return entries


async def _build_meta(slug: str, rel_path: str) -> RoleMeta:
    """Build a ``RoleMeta`` for the given slug and relative path.

    Reads the file from ``settings.repo_dir`` and runs a scoped ``git log``
    to populate the last-commit fields. Raises ``HTTPException(404)`` when
    the file does not exist on disk.
    """
    abs_path: Path = settings.repo_dir / rel_path
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail=f"Managed file not found on disk: {rel_path}")

    content = abs_path.read_text(encoding="utf-8")
    line_count = len(content.splitlines())
    mtime = abs_path.stat().st_mtime

    sha, message = await _git_log_one(settings.repo_dir, rel_path)

    return RoleMeta(
        slug=slug,
        path=rel_path,
        line_count=line_count,
        mtime=mtime,
        last_commit_sha=sha,
        last_commit_message=message,
    )


def _resolve_slug(slug: str) -> str:
    """Return the relative path for a slug, or raise HTTP 404.

    Centralises the allowlist lookup so callers don't repeat the guard.
    """
    rel_path = _MANAGED_FILES.get(slug)
    if rel_path is None:
        raise HTTPException(status_code=404, detail=f"Unknown role slug: {slug!r}")
    return rel_path


@router.get("", summary="List all managed role and cursor files")
async def list_roles() -> list[RoleMeta]:
    """Return metadata for every file in the managed allowlist.

    Files that exist in the allowlist but are missing from disk are silently
    omitted so a missing optional file does not break the entire listing.
    Returns slugs in the order they appear in ``_MANAGED_FILES``.
    """
    results: list[RoleMeta] = []
    for slug, rel_path in _MANAGED_FILES.items():
        abs_path = settings.repo_dir / rel_path
        if not abs_path.exists():
            logger.warning("⚠️  Managed file missing from disk: %s", rel_path)
            continue
        try:
            meta = await _build_meta(slug, rel_path)
            results.append(meta)
        except HTTPException:
            pass
    return results


_ARCHETYPES_DIR = settings.repo_dir / "scripts" / "gen_prompts" / "cognitive_archetypes"
_TAXONOMY_FILE = settings.repo_dir / "scripts" / "gen_prompts" / "role-taxonomy.yaml"


@router.get("/taxonomy", summary="Full three-tier org hierarchy")
async def get_taxonomy() -> TaxonomyResponse:
    """Return the complete role hierarchy from ``role-taxonomy.yaml``.

    The GUI uses this to render the hierarchy browser (C-Suite → VP → Workers)
    and to filter compatible figures/skills when composing a cognitive architecture.
    Each role includes a live ``file_exists`` flag indicating whether the
    corresponding ``.agentception/roles/<slug>.md`` file has been authored.
    """
    if not _TAXONOMY_FILE.exists():
        raise HTTPException(status_code=503, detail="role-taxonomy.yaml not found")

    raw = yaml.safe_load(_TAXONOMY_FILE.read_text(encoding="utf-8"))
    levels: list[TaxonomyLevel] = []

    for raw_level in raw.get("levels", []):
        roles: list[TaxonomyRole] = []
        for raw_role in raw_level.get("roles", []):
            slug = str(raw_role.get("slug", ""))
            rel_path = _MANAGED_FILES.get(slug, f".agentception/roles/{slug}.md")
            file_exists = (settings.repo_dir / rel_path).exists()
            roles.append(
                TaxonomyRole(
                    slug=slug,
                    label=str(raw_role.get("label", slug)),
                    title=str(raw_role.get("title", slug)),
                    category=str(raw_role.get("category", "")),
                    description=str(raw_role.get("description", "")),
                    spawnable=bool(raw_role.get("spawnable", False)),
                    compatible_figures=[str(f) for f in raw_role.get("compatible_figures", [])],
                    compatible_skill_domains=[str(s) for s in raw_role.get("compatible_skill_domains", [])],
                    file_exists=file_exists,
                )
            )
        levels.append(
            TaxonomyLevel(
                id=str(raw_level.get("id", "")),
                label=str(raw_level.get("label", "")),
                description=str(raw_level.get("description", "")),
                roles=roles,
            )
        )

    return TaxonomyResponse(levels=levels)


@router.get("/personas", summary="All cognitive architecture personas / figures")
async def get_personas() -> PersonasResponse:
    """Return all figure YAMLs from the cognitive architecture library.

    Each entry corresponds to one ``.yaml`` file in
    ``scripts/gen_prompts/cognitive_archetypes/figures/``.  The GUI uses
    this list to populate persona cards in the hierarchy browser and the
    primitive composer's figure dropdown.
    """
    figures_dir = _ARCHETYPES_DIR / "figures"
    if not figures_dir.exists():
        raise HTTPException(status_code=503, detail="figures directory not found")

    personas: list[PersonaEntry] = []
    for yaml_file in sorted(figures_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            overrides_raw = raw.get("overrides", {})
            overrides = {str(k): str(v) for k, v in overrides_raw.items()} if isinstance(overrides_raw, dict) else {}
            injection = raw.get("prompt_injection", {})
            prefix = str(injection.get("prefix", "")) if isinstance(injection, dict) else ""
            personas.append(
                PersonaEntry(
                    id=str(raw.get("id", yaml_file.stem)),
                    display_name=str(raw.get("display_name", yaml_file.stem)),
                    layer=str(raw.get("layer", "figure")),
                    extends=str(raw.get("extends", "")),
                    description=str(raw.get("description", "")).strip(),
                    prompt_prefix=prefix.strip(),
                    overrides=overrides,
                )
            )
        except Exception:
            logger.warning("⚠️ Failed to parse figure YAML: %s", yaml_file)
            continue

    return PersonasResponse(personas=personas)


@router.get("/atoms", summary="Cognitive atom dimensions for the primitive composer")
async def get_atoms() -> AtomsResponse:
    """Return all atom dimension YAMLs from the cognitive architecture library.

    Each entry corresponds to one ``.yaml`` file in
    ``scripts/gen_prompts/cognitive_archetypes/atoms/``.  The GUI uses this
    to render atom dropdowns in the primitive composer, allowing users to
    override individual cognitive dimensions when designing a custom role.
    """
    atoms_dir = _ARCHETYPES_DIR / "atoms"
    if not atoms_dir.exists():
        raise HTTPException(status_code=503, detail="atoms directory not found")

    atoms: list[AtomDimension] = []
    for yaml_file in sorted(atoms_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            raw_values = raw.get("values", {})
            values: list[AtomValue] = []
            if isinstance(raw_values, dict):
                for val_id, val_data in raw_values.items():
                    if isinstance(val_data, dict):
                        values.append(
                            AtomValue(
                                id=str(val_id),
                                label=str(val_data.get("label", val_id)),
                                description=str(val_data.get("description", "")),
                            )
                        )
            atoms.append(
                AtomDimension(
                    dimension=str(raw.get("dimension", yaml_file.stem)),
                    description=str(raw.get("description", "")).strip(),
                    values=values,
                )
            )
        except Exception:
            logger.warning("⚠️ Failed to parse atom YAML: %s", yaml_file)
            continue

    return AtomsResponse(atoms=atoms)


# ---------------------------------------------------------------------------
# Cognitive architecture resolver — used by agent detail pages
# ---------------------------------------------------------------------------

# Archetype-to-emoji mapping for visual personality display.
_ARCHETYPE_EMOJI: dict[str, str] = {
    "the_visionary": "🔮",
    "the_architect": "🏛️",
    "the_hacker": "⚡",
    "the_guardian": "🛡️",
    "the_scholar": "📚",
    "the_mentor": "🧑‍🏫",
    "the_operator": "⚙️",
    "the_pragmatist": "🔧",
}

# Atom dimension labels for display.
_ATOM_LABELS: dict[str, str] = {
    "epistemic_style": "Epistemic Style",
    "creativity_level": "Creativity",
    "quality_bar": "Quality Bar",
    "scope_instinct": "Scope Instinct",
    "collaboration_posture": "Collaboration",
    "communication_style": "Communication",
    "cognitive_rhythm": "Cognitive Rhythm",
    "mental_model": "Mental Model",
    "uncertainty_handling": "Uncertainty",
    "error_posture": "Error Posture",
}


def resolve_cognitive_arch(cognitive_arch_str: str | None) -> dict[str, JsonValue]:
    """Parse a COGNITIVE_ARCH string and return a rich display dict.

    The string format is ``figure_id:skill1:skill2`` where the first token
    is an optional figure ID and subsequent tokens are skill domain IDs.

    Returns a dict with:
    - ``raw``: the original string
    - ``figure_id``: first token (or None if only skills)
    - ``skill_domains``: list of skill domain IDs
    - ``display_name``: human-readable name from the figure YAML (or figure_id)
    - ``archetype``: the archetype the figure extends (or None)
    - ``archetype_emoji``: emoji for the archetype
    - ``description``: full description from the figure YAML (or "")
    - ``overrides``: dict of atom dimension overrides
    - ``atom_labels``: dict mapping dimension key → human label
    - ``prompt_prefix``: first 300 chars of the prompt injection prefix
    - ``is_named_figure``: True when a figure YAML was resolved
    """
    if not cognitive_arch_str:
        return _empty_arch()

    parts = [p.strip() for p in cognitive_arch_str.split(":") if p.strip()]
    if not parts:
        return _empty_arch()

    # First part might be a figure ID or a skill domain.  We disambiguate by
    # checking whether a figure YAML exists for it.
    figures_dir = _ARCHETYPES_DIR / "figures"
    first = parts[0]
    figure_yaml = figures_dir / f"{first}.yaml"
    skill_parts = parts[1:] if figure_yaml.exists() else parts
    figure_id = first if figure_yaml.exists() else None

    skills_jv: list[JsonValue] = []
    skills_jv.extend(skill_parts)
    atoms_jv: dict[str, JsonValue] = {k: v for k, v in _ATOM_LABELS.items()}
    result: dict[str, JsonValue] = {
        "raw": cognitive_arch_str,
        "figure_id": figure_id,
        "skill_domains": skills_jv,
        "display_name": figure_id or "Custom",
        "archetype": None,
        "archetype_emoji": "🤖",
        "description": "",
        "overrides": {},
        "atom_labels": atoms_jv,
        "prompt_prefix": "",
        "is_named_figure": False,
    }

    if figure_id and figure_yaml.exists():
        try:
            raw = yaml.safe_load(figure_yaml.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                archetype = str(raw.get("extends", ""))
                injection = raw.get("prompt_injection", {})
                prefix = str(injection.get("prefix", "")) if isinstance(injection, dict) else ""
                overrides_raw = raw.get("overrides", {})
                overrides = {str(k): str(v) for k, v in overrides_raw.items()} if isinstance(overrides_raw, dict) else {}

                result["display_name"] = str(raw.get("display_name", figure_id))
                result["archetype"] = archetype
                result["archetype_emoji"] = _ARCHETYPE_EMOJI.get(archetype, "🤖")
                result["description"] = str(raw.get("description", "")).strip()
                ov_jv: dict[str, JsonValue] = {k: v for k, v in overrides.items()}
                result["overrides"] = ov_jv
                result["prompt_prefix"] = prefix.strip()
                result["is_named_figure"] = True
        except Exception:
            logger.warning("⚠️ Failed to load figure YAML for: %s", figure_id)

    return result


def _empty_arch() -> dict[str, JsonValue]:
    atoms_jv: dict[str, JsonValue] = {k: v for k, v in _ATOM_LABELS.items()}
    return {
        "raw": None,
        "figure_id": None,
        "skill_domains": [],
        "display_name": "Unknown",
        "archetype": None,
        "archetype_emoji": "🤖",
        "description": "",
        "overrides": {},
        "atom_labels": atoms_jv,
        "prompt_prefix": "",
        "is_named_figure": False,
    }


@router.get("/{slug}", summary="Get content and metadata for a single role file")
async def get_role(slug: str) -> RoleContent:
    """Return the full file content and metadata for a managed slug.

    Raises HTTP 404 when the slug is not in the managed allowlist or the file
    does not exist on disk.
    """
    rel_path = _resolve_slug(slug)
    meta = await _build_meta(slug, rel_path)
    abs_path = settings.repo_dir / rel_path
    content = abs_path.read_text(encoding="utf-8")
    return RoleContent(slug=slug, content=content, meta=meta)


@router.put("/{slug}", summary="Write new content to a managed role file")
async def update_role(slug: str, body: RoleUpdateRequest) -> RoleUpdateResponse:
    """Overwrite a managed file with new content and return a diff vs HEAD.

    Does NOT auto-commit — the caller is responsible for committing the change
    (or discarding it with ``git restore``). The returned ``diff`` is the output
    of ``git diff HEAD -- <path>`` immediately after writing; an empty string
    means the content was identical to the committed version.

    Raises HTTP 404 when the slug is not in the managed allowlist.
    """
    rel_path = _resolve_slug(slug)
    abs_path = settings.repo_dir / rel_path
    abs_path.write_text(body.content, encoding="utf-8")
    logger.info("✅ Wrote %d bytes to %s", len(body.content), rel_path)

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(settings.repo_dir),
        "diff", "HEAD", "--", rel_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    diff = stdout.decode()

    meta = await _build_meta(slug, rel_path)
    return RoleUpdateResponse(slug=slug, diff=diff, meta=meta)


@router.get("/{slug}/history", summary="Return git commit history for a managed role file")
async def role_history(slug: str) -> list[dict[str, str]]:
    """Return the last 20 git commits that touched the managed file.

    Each entry has ``sha``, ``date`` (ISO-8601), and ``subject``.
    Returns an empty list when the file has no commits (e.g. brand-new file).
    Raises HTTP 404 when the slug is not in the managed allowlist.
    """
    rel_path = _resolve_slug(slug)
    return await _git_log_recent(settings.repo_dir, rel_path)


@router.post("/{slug}/diff", summary="Preview a unified diff of proposed content vs HEAD")
async def role_diff(slug: str, body: RoleDiffRequest) -> RoleDiffResponse:
    """Return a unified diff comparing ``body.content`` against HEAD without writing the file.

    Accepts a POST body so that large managed files (e.g. PARALLEL_PR_REVIEW.md)
    do not exceed Nginx's URI length limit.  Writes ``body.content`` to a temp
    file, then runs ``git diff --no-index`` between the committed file and the
    temp file so the user can review changes before saving.  An empty ``diff``
    string means the proposed content is identical to the committed version.
    Raises HTTP 404 for unknown slugs.
    """
    rel_path = _resolve_slug(slug)
    abs_path = settings.repo_dir / rel_path

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail=f"Managed file not found on disk: {rel_path}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(body.content)
        tmp_path = tmp.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(settings.repo_dir),
            "diff", "--no-index", "--unified=3",
            str(abs_path), tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        diff = stdout.decode()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return RoleDiffResponse(slug=slug, diff=diff)


@router.post("/{slug}/commit", summary="Write content and create a git commit for a managed role file")
async def commit_role(slug: str, body: RoleCommitRequest) -> RoleCommitResponse:
    """Write ``body.content`` to the managed file, stage it, and create a git commit.

    The commit message is ``role(agentception): update {slug}``.  Returns the
    resulting commit SHA so the UI can confirm the commit was created.
    Raises HTTP 404 for unknown slugs or when the file does not exist on disk.
    Raises HTTP 500 when ``git commit`` fails (e.g. nothing to commit because
    the content is identical to HEAD).
    """
    rel_path = _resolve_slug(slug)
    abs_path = settings.repo_dir / rel_path

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail=f"Managed file not found on disk: {rel_path}")

    await asyncio.to_thread(abs_path.write_text, body.content, encoding="utf-8")
    logger.info("✅ Wrote %d bytes to %s for commit", len(body.content), rel_path)

    add_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(settings.repo_dir),
        "add", rel_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, add_err = await add_proc.communicate()
    if add_proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git add failed: {add_err.decode().strip()}",
        )

    commit_message = f"role(agentception): update {slug}"
    commit_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(settings.repo_dir),
        "commit", "-m", commit_message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    commit_out, commit_err = await commit_proc.communicate()
    if commit_proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git commit failed: {commit_err.decode().strip() or commit_out.decode().strip()}",
        )

    sha_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(settings.repo_dir),
        "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    sha_out, _ = await sha_proc.communicate()
    commit_sha = sha_out.decode().strip()

    logger.info("✅ Committed %s → %s", rel_path, commit_sha[:8])

    # Record the new commit SHA in role-versions.json so callers can correlate
    # which role version governed agents in any given batch (AC-503).
    await record_version_bump(slug, commit_sha)

    return RoleCommitResponse(slug=slug, commit_sha=commit_sha, message=commit_message)


@router.get("/{slug}/versions", summary="Return role version history for a managed slug (AC-503)")
async def role_versions_api(slug: str) -> RoleVersionsResponse:
    """Return structured version history for ``slug`` from role-versions.json.

    The history is chronologically ordered (oldest first).  Each entry records
    the git SHA, version label (v1, v2, …), and UNIX timestamp of the commit.
    Returns an empty history list when no commits have been recorded yet —
    this is not an error; it simply means the role has not been committed via
    the Role Studio commit endpoint.

    Raises HTTP 404 when ``slug`` is not in the managed allowlist.
    """
    _resolve_slug(slug)  # raises 404 for unknown slugs

    data = await read_role_versions()
    versions_map_raw: JsonValue = data.get("versions", {})
    versions_map: dict[str, JsonValue] = versions_map_raw if isinstance(versions_map_raw, dict) else {}

    raw_entry = versions_map.get(slug)
    if isinstance(raw_entry, dict):
        current = str(raw_entry.get("current", "v1"))
        raw_history_raw: JsonValue = raw_entry.get("history", [])
        raw_history: list[JsonValue] = raw_history_raw if isinstance(raw_history_raw, list) else []
        history = []
        for h in raw_history:
            if not isinstance(h, dict):
                continue
            ts_raw = h.get("timestamp")
            ts = int(ts_raw) if isinstance(ts_raw, (int, float)) else 0
            history.append(
                RoleVersionEntry(
                    sha=str(h.get("sha", "")),
                    label=str(h.get("label", "")),
                    timestamp=ts,
                )
            )
    else:
        current = "v1"
        history = []

    return RoleVersionsResponse(
        slug=slug,
        versions=RoleVersionInfo(current=current, history=history),
    )
