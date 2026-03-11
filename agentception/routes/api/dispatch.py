from __future__ import annotations

"""Dispatch API routes — launch agents from the Ship UI.

Three endpoints drive the Ship page launch modal:

1. ``GET /api/dispatch/context`` — return phases and open issues for a
   label so the modal can populate its pickers.
2. ``POST /api/dispatch/issue`` — create a worktree, fire the agent loop
   immediately, and return once the run is ``implementing``.  No Dispatcher
   or Cursor session is required; the agent calls Anthropic directly via the
   server-side asyncio loop.  This mirrors ``create_and_launch_run`` for
   issue-scoped leaf workers.
3. ``POST /api/dispatch/label`` — same but scoped to an initiative label or
   phase sub-label (spawns a coordinator or leaf depending on *scope*).
4. ``GET /api/dispatch/prompt`` — serve the Dispatcher prompt so the UI
   can offer a one-click copy.

**Lifecycle for ``POST /api/dispatch/issue``:**

    1. Create git worktree at ``{worktrees_dir}/issue-{N}`` branching from ``origin/dev``
   (implementers) or from the PR branch (``reviewer`` role — remote branch
   is fetched first; branch name is ``pr_branch`` if provided, otherwise
   ``feat/issue-{N}``; reviewer starts on the implementer's code immediately).
2. Configure worktree remote to embed ``GITHUB_TOKEN`` (enables ``git push``
   without a separate credential helper).
3. Pre-inject semantically relevant code chunks into ``task_description``
   (Qdrant search at dispatch time — not agent-turn time).
4. Reset ``memory.json`` in the worktree — clears any stale memory from a
   prior run sharing the same ``run_id``, seeds ``plan`` from ``task_description``
   so the agent has its briefing from turn 1 without calling ``issue_read``.
5. Persist DB row as ``pending_launch``.
6. Acknowledge (``pending_launch`` → ``implementing``).
7. Fire ``run_agent_loop`` as an asyncio background task.
8. Fire worktree Qdrant indexing as a background task.
9. Return immediately — the agent is now running.

See ``docs/agent-tree-protocol.md`` for the full tier spec.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class OrgNodeSpec(BaseModel):
    """One node in a user-designed agent org tree.

    Persisted to the DB and included in the agent's run context so the
    launched agent knows the exact hierarchy it was designed to spawn rather
    than inferring structure from the ticket list.

    Self-referential via ``children`` — ``model_rebuild()`` is required after
    the class definition.
    """

    id: str
    role: str
    figure: str = ""
    scope: Literal["full_initiative", "phase"] = "full_initiative"
    scope_label: str = ""
    children: list["OrgNodeSpec"] = []


OrgNodeSpec.model_rebuild()

import ast as _ast

from agentception.config import settings
from agentception.db.persist import acknowledge_agent_run, persist_agent_run_dispatch, persist_execution_plan
from agentception.db.queries import get_label_context
from agentception.models import ExecutionPlan
from agentception.services.agent_loop import run_agent_loop
from agentception.services.code_indexer import SearchMatch, search_codebase
from agentception.services.cognitive_arch import _resolve_cognitive_arch
from agentception.services.planner import generate_execution_plan
from agentception.services.run_factory import _configure_worktree_auth, _index_worktree
from agentception.services.working_memory import WorkingMemory, write_memory
from agentception.services.spawn_child import (
    SpawnChildError,
    ScopeType,
    Tier,
    spawn_child,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


async def _resolve_dev_sha() -> str:
    """Return the current SHA of origin/dev.

    Pinning the worktree start point to a concrete SHA rather than the
    symbolic HEAD of the main repo prevents agents from inheriting local
    commits that are not yet on origin/dev and keeps each worktree
    reproducibly anchored to the same commit regardless of the main
    repo's checked-out branch.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse origin/dev failed: {stderr.decode().strip()}"
        )
    return stdout.decode().strip()

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Matches a Markdown checkbox bullet: "- [ ] text" or "- [x] text"
_AC_CHECKBOX_RE = re.compile(r"^-\s+\[[ xX]\]\s+(.+)$")
# Matches a Markdown section header at level 1–3
_SECTION_HEADER_RE = re.compile(r"^#{1,3}\s+")
# Matches the acceptance criteria section header (case-insensitive)
_AC_HEADER_RE = re.compile(r"^#{1,3}\s+acceptance criteria", re.IGNORECASE)
# Matches backtick-quoted file paths, e.g. `agentception/db/models.py`
_AC_FILE_PATH_RE = re.compile(r"`([a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+)`")
# Matches plain (unquoted) relative file paths, e.g. agentception/mcp/__init__.py
# Requires at least one slash so bare words like "foo.py" are not matched.
_PLAIN_FILE_PATH_RE = re.compile(r"\b([a-zA-Z0-9_.][a-zA-Z0-9_./]*(?:/[a-zA-Z0-9_.]+)+\.[a-zA-Z0-9]+)\b")

# Max lines to inject per file.  Beyond this, the tail is truncated with a
# notice — the agent can always read_file_lines for deeper context if needed.
_AC_FILE_MAX_LINES: int = 250


def _extract_ac_items(issue_body: str) -> list[str]:
    """Return AC checkbox bullets from *issue_body* as verbatim ``next_steps`` strings.

    Scans for a ``## Acceptance criteria`` section (any level 1–3 header,
    case-insensitive) and extracts every ``- [ ] item`` checkbox bullet
    verbatim.  Stops at the next section header.

    Each item is prefixed with ``"AC: "`` so the agent knows it came directly
    from the spec and must not be paraphrased or skipped.

    Returns an empty list when no AC section or no checkboxes are found.
    """
    ac_items: list[str] = []
    in_ac_section = False

    for line in issue_body.splitlines():
        stripped = line.strip()
        if _AC_HEADER_RE.match(stripped):
            in_ac_section = True
            continue
        if in_ac_section and _SECTION_HEADER_RE.match(stripped):
            break
        if in_ac_section:
            m = _AC_CHECKBOX_RE.match(stripped)
            if m:
                ac_items.append(f"AC: {m.group(1).strip()}")

    return ac_items


def _extract_ac_file_paths(issue_body: str) -> list[str]:
    """Return unique file paths (with at least one slash) mentioned in the issue body.

    Scans both backtick-quoted tokens and plain unquoted text for tokens that
    look like relative file paths (contain ``/`` and have a file extension).
    Deduplicates and sorts so the order is deterministic across runs.

    Examples of matches (backtick-quoted or plain):
        ``agentception/db/models.py``
        agentception/mcp/__init__.py
        ``.cursor/mcp.json``
    """
    paths: set[str] = set()
    for match in _AC_FILE_PATH_RE.finditer(issue_body):
        candidate = match.group(1)
        if "/" in candidate:
            paths.add(candidate)
    for match in _PLAIN_FILE_PATH_RE.finditer(issue_body):
        candidate = match.group(1)
        # Exclude URLs and other non-path patterns.
        if not candidate.startswith(("http", "www")):
            paths.add(candidate)
    return sorted(paths)


def _build_ac_file_sections(worktree_path: Path, file_paths: list[str]) -> list[str]:
    """Read each AC-mentioned file from *worktree_path* and return Markdown sections.

    For files that exist: inject up to ``_AC_FILE_MAX_LINES`` lines with a
    truncation notice when the file is longer.

    For files that do not yet exist (e.g. a new Alembic migration): list the
    most-recent sibling files in the same directory so the agent knows the
    naming convention and can write the new file without any discovery reads.
    """
    sections: list[str] = []
    for rel_path in file_paths:
        full_path = worktree_path / rel_path
        if full_path.exists() and full_path.is_file():
            try:
                raw_lines = full_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            total = len(raw_lines)
            head = raw_lines[:_AC_FILE_MAX_LINES]
            suffix = (
                f"\n... ({total - _AC_FILE_MAX_LINES} more lines — use read_file_lines for the rest)"
                if total > _AC_FILE_MAX_LINES
                else ""
            )
            lang = "python" if rel_path.endswith(".py") else ""
            sections.append(
                f"### `{rel_path}` ({total} lines)\n"
                f"```{lang}\n"
                + "\n".join(head)
                + suffix
                + "\n```"
            )
        else:
            # File doesn't exist — show the parent directory listing so the
            # agent knows the naming convention (e.g. Alembic migration numbers).
            parent = full_path.parent
            if parent.exists():
                siblings = sorted(p.name for p in parent.iterdir() if p.is_file())
                if siblings:
                    listing = "\n".join(siblings[-8:])
                    sections.append(
                        f"### `{rel_path}` (file does not exist yet)\n"
                        f"Existing files in `{parent.relative_to(worktree_path)}`:\n"
                        f"```\n{listing}\n```"
                    )
    return sections


# ---------------------------------------------------------------------------
# POST /api/dispatch/regenerate — re-run generate.py and refresh prompt files
# ---------------------------------------------------------------------------


class RegenerateResponse(BaseModel):
    """Response shape for ``POST /api/dispatch/regenerate``."""

    ok: bool
    files: list[str] = []
    error: str | None = None


@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate_prompts() -> RegenerateResponse:
    """Re-run generate.py and return the list of regenerated .md files."""
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "/app/scripts/gen_prompts/generate.py",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace")[:500]
        return RegenerateResponse(ok=False, error=err_text)
    agentception_dir = Path(settings.repo_dir) / ".agentception"
    files = sorted(str(p) for p in agentception_dir.rglob("*.md"))
    return RegenerateResponse(ok=True, files=files)


# ---------------------------------------------------------------------------
# POST /api/dispatch/launch — return dispatcher prompt as JSON
# ---------------------------------------------------------------------------


class LaunchPromptResponse(BaseModel):
    """Response shape for ``POST /api/dispatch/launch``."""

    ok: bool
    prompt: str
    error: str | None = None


@router.post("/launch", response_model=LaunchPromptResponse)
async def launch_dispatcher_prompt() -> LaunchPromptResponse:
    """Read .agentception/agent-conductor.md and return its contents as JSON."""
    conductor_path = Path(settings.repo_dir) / ".agentception" / "agent-conductor.md"
    try:
        contents = conductor_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LaunchPromptResponse(ok=False, prompt="", error="agent-conductor.md not found")
    return LaunchPromptResponse(ok=True, prompt=contents)


# ---------------------------------------------------------------------------
# GET /api/dispatch/context — label context for the launch modal
# ---------------------------------------------------------------------------



class PhaseSummaryItem(BaseModel):
    """One phase sub-label and its open-issue count, for the launch modal picker."""

    label: str
    count: int


class IssueSummaryItem(BaseModel):
    """A minimal open-issue descriptor for the launch modal single-ticket picker."""

    number: int
    title: str


class LabelContextResponse(BaseModel):
    """Response shape for ``GET /api/dispatch/context``."""

    phases: list[PhaseSummaryItem]
    issues: list[IssueSummaryItem]


@router.get("/context", response_model=LabelContextResponse)
async def get_label_context_route(
    label: str,
    repo: str,
) -> LabelContextResponse:
    """Return phases and open issues for *label* so the Launch modal can populate pickers.

    Response shape::

        {
          "phases": [{"label": "ac-workflow/5-plan-step-v2", "count": 3}, ...],
          "issues": [{"number": 108, "title": "..."}, ...]
        }

    Falls back to empty lists when the initiative has no recorded data yet.
    """
    ctx = await get_label_context(repo=repo, initiative_label=label)
    return LabelContextResponse(
        phases=[PhaseSummaryItem(label=p["label"], count=p["count"]) for p in ctx["phases"]],
        issues=[IssueSummaryItem(number=i["number"], title=i["title"]) for i in ctx["issues"]],
    )


# ---------------------------------------------------------------------------
# POST /api/dispatch/issue — single-issue leaf dispatch
# ---------------------------------------------------------------------------


class DispatchRequest(BaseModel):
    """Request body for ``POST /api/dispatch/issue``."""

    issue_number: int
    issue_title: str
    issue_body: str = ""
    """Issue body text used to derive skill domains for the cognitive arch."""
    role: str
    """Role slug from ``.agentception/roles/`` (e.g. ``developer``, ``reviewer``)."""
    repo: str
    """``owner/repo`` string (e.g. ``cgcardona/agentception``)."""
    pr_number: int | None = None
    """PR number to associate with this run.

    Required for ``reviewer`` dispatches — the worktree is anchored to the
    PR branch instead of ``origin/dev``, so the reviewer starts on the
    implementer's code without any manual branch-switching.
    Optional for implementer dispatches; when omitted, the DB field is left
    ``NULL`` until the agent self-reports the PR via ``build_complete_run``.
    """
    pr_branch: str | None = None
    """Exact remote branch name for the PR being reviewed.

    For ``reviewer`` dispatches only.  When omitted the endpoint derives
    the branch as ``feat/issue-{issue_number}``, which is the standard naming
    convention for AgentCeption agent branches.

    Provide this field when the PR branch does not follow the standard naming
    convention — e.g. ``feat/reviewer-branch-orientation`` (not tied to a
    single issue number).

    The ``run_id`` and worktree slug are always derived from ``issue_number``
    regardless of what ``pr_branch`` is set to.
    """


class DispatchResponse(BaseModel):
    """Successful dispatch response.

    ``status`` is always ``"implementing"`` — the agent loop is already running
    by the time this response is returned.
    """

    run_id: str
    worktree: str
    host_worktree: str
    branch: str
    batch_id: str
    status: str = "implementing"


def _make_batch_id(issue_number: int) -> str:
    """Generate a deterministic-but-unique batch id for this dispatch."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:4]
    return f"issue-{issue_number}-{stamp}-{short}"


# ---------------------------------------------------------------------------
# Dispatch-time context extractors — run after ensure_worktree so the
# worktree is on disk.  Both are off the hot path (run in a thread via
# asyncio.to_thread) and fail silently so a broken file never blocks dispatch.
# ---------------------------------------------------------------------------

_SIG_MAX_CHARS = 160   # max chars per signature line kept in the summary
_SIG_MAX_SYMBOLS = 30  # max symbols extracted per file


def _ast_signatures_from_file(path: Path) -> str:
    """Return a compact string of class/function signatures from *path*.

    Uses Python ``ast`` so only the declaration lines are read — no function
    bodies, no docstrings.  Each line is at most ``_SIG_MAX_CHARS`` chars.
    Skips files that fail to parse (e.g. syntax errors, non-UTF-8).
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return ""

    lines = source.splitlines()
    sigs: list[str] = []

    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            continue
        if len(sigs) >= _SIG_MAX_SYMBOLS:
            break
        lineno = node.lineno - 1
        if lineno < 0 or lineno >= len(lines):
            continue
        # Grab up to 3 lines to capture multi-line signatures.
        raw = " ".join(
            lines[lineno + i].strip()
            for i in range(min(3, len(lines) - lineno))
            if lines[lineno + i].strip()
        )
        sig = raw[:_SIG_MAX_CHARS] + ("…" if len(raw) > _SIG_MAX_CHARS else "")
        sigs.append(sig)

    return "\n".join(sigs)


async def _extract_type_signatures(
    worktree_path: Path,
    relevant_files: list[str],
) -> dict[str, str]:
    """Extract class/function type signatures from *relevant_files* in the worktree.

    Returns a ``dict[file_path, signature_summary]`` injected into
    ``WorkingMemory.findings`` at dispatch time so the agent can write
    correctly-typed code without reading each file first.

    Runs ``_ast_signatures_from_file`` in a thread pool to avoid blocking the
    event loop.  Returns an empty dict if nothing can be extracted.
    """
    result: dict[str, str] = {}
    for rel_path in relevant_files:
        abs_path = worktree_path / rel_path
        if not abs_path.exists() or not rel_path.endswith(".py"):
            continue
        try:
            sigs = await asyncio.to_thread(_ast_signatures_from_file, abs_path)
        except Exception:  # noqa: BLE001
            continue
        if sigs:
            result[rel_path] = f"[Type signatures]\n{sigs}"
    return result


def _test_names_from_file(path: Path) -> list[str]:
    """Return all ``def test_*`` function names found in *path* via AST."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return []
    return [
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


async def _extract_test_coverage(
    worktree_path: Path,
    relevant_files: list[str],
) -> dict[str, str]:
    """Map relevant test files to their existing ``test_*`` function names.

    Derives candidate test filenames from *relevant_files*: for each source
    module (e.g. ``agentception/poller.py``) it checks for common test file
    naming patterns in ``agentception/tests/``.  The result is injected into
    ``WorkingMemory.findings`` so the agent knows which scenarios are already
    covered and writes only the missing tests.
    """
    tests_dir = worktree_path / "agentception" / "tests"
    if not tests_dir.exists():
        return {}

    # Derive candidate test filenames for each source file.
    candidates: set[str] = set()
    for rel_path in relevant_files:
        stem = Path(rel_path).stem  # e.g. "poller", "__init__" → "models"
        if stem == "__init__":
            stem = Path(rel_path).parent.name  # agentception/models → "models"
        for prefix in ("test_agentception_", "test_", ""):
            candidates.add(f"{prefix}{stem}.py")

    result: dict[str, str] = {}
    for test_file in sorted(tests_dir.iterdir()):
        if test_file.name not in candidates:
            continue
        try:
            names = await asyncio.to_thread(_test_names_from_file, test_file)
        except Exception:  # noqa: BLE001
            continue
        if names:
            rel = f"agentception/tests/{test_file.name}"
            result[rel] = "[Existing tests — do not duplicate]\n" + "\n".join(names)
    return result


def _format_execution_plan(plan: ExecutionPlan) -> str:
    """Render an ExecutionPlan as a Markdown section for the executor's task briefing.

    The executor reads this section to know exactly which tool to call with
    exactly which parameters — no file reads required.
    """
    lines: list[str] = [
        "## EXECUTION PLAN",
        "",
        "Apply each operation below in order using the exact tool and parameters "
        "specified. Do not read files. Do not add operations. Do not deviate.",
        "",
    ]
    for i, op in enumerate(plan.operations, 1):
        lines.append(f"### Operation {i}: {op.tool}")
        lines.append(f"File: `{op.file}`")
        lines.append("")
        if op.tool == "replace_in_file":
            lines.append("**old_string** (replace this exact text):")
            lines.append(f"```\n{op.old_string}\n```")
            lines.append("")
            lines.append("**new_string** (replace with this exact text):")
            lines.append(f"```\n{op.new_string}\n```")
        elif op.tool == "insert_after_in_file":
            lines.append("**after** (insert after this exact line):")
            lines.append(f"```\n{op.after}\n```")
            lines.append("")
            lines.append("**text** (insert this text):")
            lines.append(f"```\n{op.text}\n```")
        elif op.tool == "write_file":
            lines.append("**content** (write this as the complete file):")
            lines.append(f"```\n{op.content}\n```")
        lines.append("")
    return "\n".join(lines)


@router.post("/issue", response_model=DispatchResponse)
async def dispatch_agent(req: DispatchRequest) -> DispatchResponse:
    """Create a worktree and immediately fire the agent loop via Anthropic.

    Full lifecycle (all steps complete before the response is returned except
    the agent loop and indexing, which run as asyncio background tasks):

    1. Create git worktree — anchored to ``origin/dev`` for implementers;
       anchored to ``origin/feat/issue-{N}`` for ``reviewer`` dispatches
       (after fetching the remote branch so the reviewer starts on the
       implementer's code from turn 1 with no manual branch-switching needed).
    2. Configure worktree remote to embed ``GITHUB_TOKEN`` for push access.
    3. Pre-inject semantically relevant code chunks into ``task_description``.
    4. Persist DB row as ``pending_launch`` (``pr_number`` written if provided).
    5. Acknowledge → ``implementing``.
    6. Fire ``run_agent_loop`` as an asyncio background task (calls Anthropic).
    7. Fire worktree Qdrant indexing as an asyncio background task.

    No Cursor session or external Dispatcher is required.

    Raises:
        HTTPException 422: PR branch not found on remote (already deleted after merge,
            or non-standard name not passed via ``pr_branch``).
        HTTPException 500: git worktree add or git auth configuration failed.
    """
    is_reviewer = req.role == "reviewer"

    # Reviewers get their own run_id and worktree slug so they don't clobber the
    # implementer's DB row (token counts, status history, etc.).
    # slug: review-{pr_number}  →  worktree at /worktrees/review-{pr_number}
    # Implementers always use the issue-{N} slug.
    if is_reviewer and req.pr_number is not None:
        slug = f"review-{req.pr_number}"
        run_id = f"review-{req.pr_number}"
    else:
        slug = f"issue-{req.issue_number}"
        run_id = f"issue-{req.issue_number}"

    # For reviewer dispatches the PR branch may not follow feat/issue-{N} naming
    # (e.g. feat/reviewer-branch-orientation).  pr_branch overrides the default.
    branch = req.pr_branch if req.pr_branch else f"feat/issue-{req.issue_number}"
    batch_id = _make_batch_id(req.issue_number)
    worktree_path = str(Path(settings.worktrees_dir) / slug)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / slug)

    from agentception.readers.git import ensure_worktree  # noqa: PLC0415

    if is_reviewer:
        # For reviewers the relevant code lives on the implementer's branch, not
        # dev.  Fetch the remote branch so `origin/<branch>` is up to date, then
        # create the worktree from that ref.  The agent is on the correct branch
        # from its very first turn — no wasted turns detecting the wrong branch.
        fetch_proc = await asyncio.create_subprocess_exec(
            "git", "fetch", "origin", branch,
            cwd=str(settings.repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _fetch_out, _fetch_err = await fetch_proc.communicate()
        if fetch_proc.returncode != 0:
            err_msg = _fetch_err.decode(errors="replace").strip()
            logger.error("❌ dispatch: fetch of %s failed — %s", branch, err_msg)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Remote branch '{branch}' not found. "
                    "If the PR was already merged and the branch deleted, reviewers "
                    "must be dispatched before merge. "
                    "If the branch uses a non-standard name, pass it in 'pr_branch'. "
                    f"git error: {err_msg}"
                ),
            )
        worktree_base = f"origin/{branch}"
        logger.info("✅ dispatch: fetched %s for reviewer worktree", branch)
    else:
        worktree_base = "origin/dev"

    try:
        # reset=True for implementers: if a stale worktree/branch exists from a
        # prior run, tear it down first so the executor always starts from a
        # clean origin/dev.  Reviewers use reset=False — they reuse the branch
        # the implementer already pushed.
        await ensure_worktree(Path(worktree_path), branch, worktree_base, reset=not is_reviewer)
        logger.info("✅ dispatch: worktree created at %s (base=%s)", worktree_path, worktree_base)
    except RuntimeError as exc:
        logger.error("❌ dispatch: worktree creation failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Embed GITHUB_TOKEN in the worktree remote so git push works without a
    # separate credential helper.  The adhoc path always did this; the issue
    # dispatch path was missing it, causing push failures inside the container.
    await _configure_worktree_auth(Path(worktree_path), run_id)

    cognitive_arch = _resolve_cognitive_arch(req.issue_body, req.role)

    # Build task_description from the issue title + body so the agent briefing
    # includes the full issue text and never needs to call issue_read for context.
    task_description: str | None = None
    if req.issue_title or req.issue_body:
        parts = []
        if req.issue_title:
            parts.append(f"# {req.issue_title}")
        if req.issue_body:
            parts.append(req.issue_body.strip())
        task_description = "\n\n".join(parts)

    # Pre-inject semantically relevant code context from the main Qdrant index.
    # Searching at dispatch time (not agent-turn time) amortises the embedding
    # cost once and gives the agent oriented code context from turn 1.
    # Cap: top 3 chunks, 800 chars each, total ≤ 3 000 chars added.
    # code_matches is also reused below for type-signature extraction.
    code_matches: list[SearchMatch] = []
    if task_description:
        search_query = f"{req.issue_title} {req.issue_body}"[:800]
        try:
            code_matches = await search_codebase(search_query, n_results=3)
            if code_matches:
                ctx_blocks: list[str] = []
                remaining = 3_000
                for m in code_matches:
                    block = (
                        f"**{m['file']}** (lines {m['start_line']}–{m['end_line']})\n"
                        f"```\n{m['chunk'][:800]}\n```"
                    )
                    if remaining <= 0:
                        break
                    ctx_blocks.append(block)
                    remaining -= len(block)
                if ctx_blocks:
                    task_description += (
                        "\n\n---\n\n## Pre-injected Code Context\n\n"
                        + "\n\n".join(ctx_blocks)
                    )
                    logger.info(
                        "✅ dispatch: pre-injected %d code chunks for run_id=%s",
                        len(ctx_blocks),
                        run_id,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ dispatch: context pre-injection failed — %s", exc)

    # Pre-load files explicitly named in the AC items.
    # The agent spends 40–60% of its iteration budget on discovery reads.
    # Injecting the full content of every AC-referenced file into the task
    # briefing eliminates that phase entirely — the agent starts iteration 1
    # with all the context it needs and can go straight to IMPLEMENT.
    if req.issue_body and task_description:
        ac_file_paths = _extract_ac_file_paths(req.issue_body)
        if ac_file_paths:
            ac_file_sections = _build_ac_file_sections(Path(worktree_path), ac_file_paths)
            if ac_file_sections:
                task_description += (
                    "\n\n---\n\n## Pre-loaded Files\n\n"
                    "_These files are injected verbatim at dispatch time. "
                    "You do not need to read them — start implementing immediately._\n\n"
                    + "\n\n".join(ac_file_sections)
                )
                logger.info(
                    "✅ dispatch: pre-loaded %d AC file(s) for run_id=%s: %s",
                    len(ac_file_sections),
                    run_id,
                    ", ".join(ac_file_paths),
                )

    # ---------------------------------------------------------------------------
    # Planner / executor pipeline — developer role only
    # ---------------------------------------------------------------------------
    # For developer dispatches: call the planner LLM once to produce an
    # ExecutionPlan, then switch to the executor role so the agent applies the
    # plan mechanically without reading the codebase or adding extras.
    #
    # On planner failure: fall back to the developer role (original behaviour).
    # ---------------------------------------------------------------------------
    effective_role = req.role
    if req.role == "developer" and task_description and req.issue_body and settings.planner_enabled:
        ac_file_paths_for_planner = _extract_ac_file_paths(req.issue_body)
        execution_plan = await generate_execution_plan(
            run_id=run_id,
            issue_number=req.issue_number,
            issue_title=req.issue_title or "",
            issue_body=req.issue_body,
            worktree_path=Path(worktree_path),
            file_paths=ac_file_paths_for_planner,
        )
        if execution_plan is not None:
            # Store the plan in the DB before the executor loop starts.
            await persist_execution_plan(
                run_id=run_id,
                plan_json=execution_plan.model_dump_json(),
                issue_number=req.issue_number,
            )
            # Replace the task description with the formatted plan so the
            # executor sees the exact operations to apply.
            task_description = (
                f"# {req.issue_title or 'Task'}\n\n"
                + _format_execution_plan(execution_plan)
            )
            effective_role = "executor"
            logger.info(
                "✅ dispatch: planner generated %d-operation plan for run_id=%s — switching to executor role",
                len(execution_plan.operations),
                run_id,
            )
        else:
            logger.warning(
                "⚠️ dispatch: planner failed for run_id=%s — falling back to developer role",
                run_id,
            )

    # Build dispatch-time findings: type signatures + existing test names.
    # These are extracted from the worktree (which already exists on disk) and
    # injected into WorkingMemory.findings so the agent can write correctly-typed
    # code and non-duplicate tests from turn 1 — eliminating the mypy-fix loop
    # and test-collision discovery turns that previously cost 5-8 turns each.
    relevant_files = [m["file"] for m in code_matches]
    dispatch_findings: dict[str, str] = {}
    try:
        type_findings = await _extract_type_signatures(Path(worktree_path), relevant_files)
        dispatch_findings.update(type_findings)
        logger.info(
            "✅ dispatch: extracted type signatures from %d files for run_id=%s",
            len(type_findings),
            run_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ dispatch: type signature extraction failed — %s", exc)
    try:
        test_findings = await _extract_test_coverage(Path(worktree_path), relevant_files)
        dispatch_findings.update(test_findings)
        logger.info(
            "✅ dispatch: extracted test coverage from %d files for run_id=%s",
            len(test_findings),
            run_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ dispatch: test coverage extraction failed — %s", exc)

    # Reset working memory so re-dispatches always start with a clean slate.
    # For reviewers: seed plan with the review task (PR number + branch),
    # not the implementation issue body — otherwise the agent reads the issue
    # text, assumes it is a developer picking up unfinished implementation work,
    # and tries to implement instead of review.
    if is_reviewer and req.pr_number:
        review_plan = (
            f"# Review PR #{req.pr_number}: {req.issue_title}\n\n"
            f"Your task is to **review** the existing pull request #{req.pr_number} "
            f"on branch `{branch}` against `dev`.\n\n"
            f"The implementation is already committed. Do NOT implement anything. "
            f"Read `task/briefing` for PR_NUMBER, BRANCH, GH_REPO, and ISSUE_NUMBER, "
            f"then follow the Review Protocol in your role file."
        )
        memory_plan = review_plan
        ac_next_steps: list[str] = []
    else:
        memory_plan = task_description or ""
        # Mechanically extract AC checkbox bullets from the issue body and
        # pre-populate next_steps verbatim.  This bypasses the agent's lossy
        # reading of the AC — items are injected before iteration 1 so the
        # agent cannot paraphrase, collapse, or drop any requirement.
        ac_next_steps = _extract_ac_items(req.issue_body) if req.issue_body else []
        if ac_next_steps:
            logger.info(
                "✅ dispatch: pre-seeded %d AC items into next_steps for run_id=%s",
                len(ac_next_steps),
                run_id,
            )

    initial_memory = WorkingMemory(plan=memory_plan, findings=dispatch_findings)
    if ac_next_steps:
        initial_memory["next_steps"] = ac_next_steps
    write_memory(Path(worktree_path), initial_memory)
    logger.info("✅ dispatch: working memory reset for run_id=%s", run_id)

    # Persist all task context to DB; agents read via ac://runs/{run_id}/context.
    # effective_role is "executor" when the planner succeeded, "developer" as fallback.
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=req.issue_number,
        role=effective_role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=cognitive_arch,
        gh_repo=settings.gh_repo,
        task_description=task_description,
        pr_number=req.pr_number,
    )

    # Transition pending_launch → implementing and fire the agent loop.
    # This mirrors create_and_launch_run — no external Dispatcher required.
    await acknowledge_agent_run(run_id)
    asyncio.create_task(run_agent_loop(run_id), name=f"agent-loop-{run_id}")

    # Index the worktree in the background so agents can search it via
    # search_codebase.  Every role gets a per-run worktree collection so that
    # search_codebase scoped to "worktree-<run_id>" reflects the agent's own
    # edits, not just origin/dev.  The incremental indexer only re-hashes
    # changed files, so the cost after the initial build is minimal.
    asyncio.create_task(
        _index_worktree(Path(worktree_path), run_id),
        name=f"index-worktree-{run_id}",
    )

    logger.info("✅ dispatch: agent loop fired for run_id=%s", run_id)

    return DispatchResponse(
        run_id=run_id,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        branch=branch,
        batch_id=batch_id,
        status="implementing",
    )


# ---------------------------------------------------------------------------
# Node type helpers shared by dispatch-label
# ---------------------------------------------------------------------------

#: Role slugs known to be coordinators (spawn children rather than working directly).
_COORDINATOR_ROLES: frozenset[str] = frozenset({
    # C-suite
    "cto", "csto", "ceo", "cpo", "coo", "cdo", "cfo", "ciso", "cmo",
    # Domain coordinators
    "engineering-coordinator", "qa-coordinator", "coordinator", "conductor",
    "platform-coordinator", "infrastructure-coordinator", "data-coordinator",
    "ml-coordinator", "design-coordinator", "mobile-coordinator",
    "security-coordinator", "product-coordinator",
})


def _tier_for_role(role: str) -> Tier:
    """Return the behavioral tier for a role slug.

    All coordinator roles (C-suite and sub-coordinators alike) survey their
    scope and spawn children → ``coordinator``.  Every other role is a
    ``worker`` — it claims one unit of work and executes it, whether that
    work is implementing an issue or reviewing a PR.
    """
    if role in _COORDINATOR_ROLES:
        return "coordinator"
    return "worker"


#: Map role slug prefixes/exact slugs to their org domain (UI hierarchy slot).
_ROLE_DOMAIN: dict[str, str] = {
    "cto": "c-suite",
    "ceo": "c-suite",
    "cpo": "c-suite",
    "coo": "c-suite",
    "cdo": "c-suite",
    "cfo": "c-suite",
    "ciso": "c-suite",
    "cmo": "c-suite",
    "csto": "c-suite",
    "engineering-coordinator": "engineering",
    "qa-coordinator": "qa",
    "platform-coordinator": "engineering",
    "infrastructure-coordinator": "engineering",
    "data-coordinator": "engineering",
    "ml-coordinator": "engineering",
    "design-coordinator": "engineering",
    "mobile-coordinator": "engineering",
    "security-coordinator": "engineering",
    "product-coordinator": "engineering",
    "reviewer": "qa",
}

_ROLE_DOMAIN_PREFIXES: list[tuple[str, str]] = [
    ("python-", "engineering"),
    ("js-", "engineering"),
    ("frontend-", "engineering"),
    ("backend-", "engineering"),
    ("infra-", "engineering"),
    ("data-", "engineering"),
    ("ml-", "engineering"),
    ("security-", "engineering"),
    ("mobile-", "engineering"),
    ("design-", "engineering"),
]


def _org_domain_for_role(role: str) -> str | None:
    """Return the org domain (UI hierarchy slot) for a role slug, or ``None`` when unknown."""
    if role in _ROLE_DOMAIN:
        return _ROLE_DOMAIN[role]
    for prefix, domain in _ROLE_DOMAIN_PREFIXES:
        if role.startswith(prefix):
            return domain
    return None


# ---------------------------------------------------------------------------
# POST /api/dispatch/label — launch a manager or root agent scoped to a label
# ---------------------------------------------------------------------------


class LabelDispatchRequest(BaseModel):
    """Request body for ``POST /api/dispatch/label``.

    *scope* is the primary selector:

    - ``"full_initiative"`` — a root coordinator (e.g. CTO) surveys every
      open ticket under *label* and assembles its own child team.  ``tier``
      is ``"coordinator"``.
    - ``"phase"`` — a coordinator handles just one phase sub-label; supply
      *scope_label* with the sub-label string.  ``tier`` is
      ``"coordinator"``.
    - ``"issue"`` — a single worker agent implements one issue; supply
      *scope_issue_number*.  ``tier`` is ``"worker"``.

    *role* is optional.  When omitted the server derives a sensible default
    (``cto`` for ``full_initiative``, ``engineering-coordinator`` for
    ``phase``, and ``developer`` for ``issue``).
    """

    label: str
    """Initiative label string, e.g. ``ac-workflow``."""
    scope: Literal["full_initiative", "phase", "issue"] = "full_initiative"
    """Determines the tier and scope for this dispatch."""
    scope_label: str | None = None
    """Phase sub-label when *scope* is ``"phase"``."""
    scope_issue_number: int | None = None
    """Issue number when *scope* is ``"issue"``."""
    role: str | None = None
    """Entry role override.  Derived from *scope* when omitted."""
    repo: str
    """``owner/repo`` string."""
    parent_run_id: str | None = None
    """Run ID of the agent that is spawning this one (spawn-lineage tracking)."""
    cognitive_arch_override: str | None = None
    """Figure slug chosen in the Org Designer (e.g. ``"steve_jobs"``).

    When set, this figure is injected into the agent's COGNITIVE_ARCH string,
    bypassing the role-default mapping while still deriving skills from context.
    Corresponds to ``figure_override`` in ``_resolve_cognitive_arch``.
    """
    org_tree: OrgNodeSpec | None = None
    """Full org tree designed in the Org Designer.

    Persisted to the DB row as ``org_tree_json`` (compact JSON string) so
    the launched agent knows the exact hierarchy it is expected to spawn.
    When absent the agent infers its own team structure from the ticket list.
    """
    cascade_enabled: bool = True
    """When False the launched agent must not spawn any child agents.

    Used for incremental smoke-testing: prove one tier works before wiring it
    to the next.  The agent reads this flag from ``[spawn].cascade_enabled``
    via its context, and, if False, outputs its self-introduction,
    calls ``log_run_step`` + ``build_complete_run`` via MCP, and exits without
    querying GitHub or dispatching children.
    """


class LabelDispatchResponse(BaseModel):
    """Successful label-dispatch response."""

    run_id: str
    tier: str
    role: str
    label: str
    worktree: str
    host_worktree: str
    batch_id: str
    status: str = "pending_launch"


def _label_slug(label: str) -> str:
    """Turn a GitHub label into a filesystem-safe slug."""
    return _SLUG_RE.sub("-", label.lower()).strip("-")[:48]


def _make_label_batch_id(label: str) -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:4]
    slug = _label_slug(label)
    return f"label-{slug}-{stamp}-{short}"


def _role_and_tier_for_scope(
    scope: Literal["full_initiative", "phase", "issue"],
    role_override: str | None,
) -> tuple[str, Tier]:
    """Derive the effective role and behavioral tier from the launch scope."""
    default_role = "developer" if scope == "issue" else (
        "cto" if scope == "full_initiative" else "engineering-coordinator"
    )
    role = role_override.strip() if role_override and role_override.strip() else default_role
    return role, _tier_for_role(role)


@router.post("/label", response_model=LabelDispatchResponse)
async def dispatch_label_agent(req: LabelDispatchRequest) -> LabelDispatchResponse:
    """Launch an agent scoped to a GitHub label (initiative or phase) or a single issue.

    *scope* drives the structural classification:

    - ``"full_initiative"`` → coordinator, surveys all tickets, spawns child team.
    - ``"phase"`` → coordinator, owns one phase sub-label only.
    - ``"issue"`` → leaf, works on a single ticket.

    A worktree is always created so the agent runs in an isolated checkout.
    All task context is persisted to the DB row.

    Raises:
        HTTPException 409: Worktree already exists.
        HTTPException 500: git worktree add failed.
    """
    role, tier = _role_and_tier_for_scope(req.scope, req.role)
    org_domain = _org_domain_for_role(role)

    if req.scope == "phase" and req.scope_label:
        scope_value = req.scope_label
        scope_type: ScopeType = "label"
    elif req.scope == "issue" and req.scope_issue_number is not None:
        scope_value = str(req.scope_issue_number)
        scope_type = "issue"
    else:
        scope_value = req.label
        scope_type = "label"

    logger.warning(
        "🚀 dispatch-label: scope=%r role=%r tier=%r scope_value=%r repo=%r",
        req.scope, role, tier, scope_value, req.repo,
    )

    label_slug = _label_slug(req.label)
    batch_id = _make_label_batch_id(req.label)
    run_id = f"label-{label_slug}-{uuid.uuid4().hex[:6]}"
    branch = f"agent/{label_slug}-{uuid.uuid4().hex[:4]}"

    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)
    logger.warning(
        "🚀 dispatch-label: run_id=%r tier=%r org_domain=%r",
        run_id, tier, org_domain,
    )

    if Path(worktree_path).exists():
        raise HTTPException(
            status_code=409,
            detail=f"Worktree already exists at {worktree_path}.",
        )

    try:
        dev_sha = await _resolve_dev_sha()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch, dev_sha,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.error("❌ dispatch-label: git worktree add failed — %s", err)
        raise HTTPException(status_code=500, detail=f"git worktree add failed: {err}")

    logger.info("✅ dispatch-label: worktree %s for label %r tier=%s", worktree_path, req.label, tier)

    label_cognitive_arch = _resolve_cognitive_arch(
        "", role, figure_override=req.cognitive_arch_override
    )

    # Persist all task context to DB.
    logger.warning(
        "🚀 dispatch-label: calling persist_agent_run_dispatch run_id=%r host_worktree_path=%r",
        run_id, host_worktree_path,
    )
    issue_number = req.scope_issue_number if (req.scope == "issue" and req.scope_issue_number is not None) else 0
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=issue_number,
        role=role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=label_cognitive_arch,
        tier=tier,
        org_domain=org_domain,
        parent_run_id=req.parent_run_id,
        gh_repo=req.repo,
    )
    logger.warning("✅ dispatch-label: persist complete — run_id=%r is now pending_launch", run_id)

    return LabelDispatchResponse(
        run_id=run_id,
        tier=tier,
        role=role,
        label=req.label,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        batch_id=batch_id,
        status="pending_launch",
    )

