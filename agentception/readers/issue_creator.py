from __future__ import annotations

"""Create GitHub issues directly from a PlanSpec using the gh CLI.

This is the Step 1.B execution layer.  It takes the validated PlanSpec YAML
that the user reviewed in the CodeMirror editor and creates real GitHub issues
— no agents, no LLM calls, no worktrees.

Execution order
---------------
1. Ensure all required labels exist (initiative label + scoped phase labels).
2. Iterate phases in order.  Within each phase, create issues concurrently
   (they have no inter-phase dependency at creation time).
3. After all issues are created and GitHub numbers are known, edit any issue
   whose ``depends_on`` list is non-empty to append a "Blocked by: #X" line.

Labels applied to every issue
------------------------------
- ``{spec.initiative}``             e.g. ``ac-build``
- ``{spec.initiative}/{phase.label}`` e.g. ``ac-build/phase-0``

The scoped phase label acts as the execution gate.  Using the initiative as a
prefix keeps GitHub labels namespaced per initiative — no global ``phase-N``
labels are created or expected.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import TypedDict

from agentception.config import settings as _cfg
from agentception.db.persist import persist_initiative_phases, persist_issue_depends_on
from agentception.models import PlanIssue, PlanSpec
from agentception.readers.github import ensure_label_exists

logger = logging.getLogger(__name__)

# ── Phase metadata (colour, description) ──────────────────────────────────
# Keyed by the internal phase identifier (phase-0 … phase-3).
# GitHub labels are namespaced as ``{initiative}/{phase}`` — no global labels.
# Phase label colors cycled by position (idx % len) so any number of phases
# and any slug convention receives a distinct, deterministic GitHub label color.
_PHASE_PALETTE: list[str] = ["B60205", "E4E669", "0075CA", "CFD3D7"]
_INITIATIVE_COLOR = "7057FF"


# ── Event types streamed to the browser ───────────────────────────────────


class StartEvent(TypedDict):
    t: str  # "start"
    total: int
    initiative: str


class LabelEvent(TypedDict):
    t: str  # "label"
    text: str


class IssueEvent(TypedDict):
    t: str  # "issue"
    index: int
    total: int
    number: int
    url: str
    title: str
    phase: str


class BlockedEvent(TypedDict):
    t: str  # "blocked"
    number: int
    blocked_by: list[int]


class CreatedIssue(TypedDict):
    """A single issue returned in the ``done`` SSE event."""

    issue_id: str
    number: int
    url: str
    title: str
    phase: str


class DoneEvent(TypedDict):
    t: str  # "done"
    total: int
    initiative: str
    batch_id: str
    issues: list[CreatedIssue]


class FilingErrorEvent(TypedDict):
    t: str  # "error"
    detail: str


IssueFileEvent = (
    StartEvent | LabelEvent | IssueEvent | BlockedEvent | DoneEvent | FilingErrorEvent
)


# ── gh CLI helpers ────────────────────────────────────────────────────────


async def _gh_create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
) -> tuple[int, str]:
    """Create a GitHub issue. Returns (number, html_url).

    ``gh issue create`` prints the issue URL as plain text on stdout —
    it does not accept ``--json``.  We parse the number from the URL.
    """
    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ]
    for label in labels:
        cmd += ["--label", label]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue create failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )

    url = stdout.decode().strip()
    if not url:
        raise RuntimeError("gh issue create returned empty output")

    # URL shape: https://github.com/owner/repo/issues/123
    try:
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError as exc:
        raise RuntimeError(f"Could not parse issue number from URL {url!r}") from exc

    return number, url


async def _gh_edit_body(repo: str, number: int, new_body: str) -> None:
    """Replace an issue's body text."""
    cmd = [
        "gh", "issue", "edit", str(number),
        "--repo", repo,
        "--body", new_body,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue edit failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )


# ── Label bootstrap ───────────────────────────────────────────────────────


async def _bootstrap_labels(spec: PlanSpec) -> None:
    """Ensure the initiative label and all scoped phase labels exist in the repo.

    Creates ``{initiative}`` and ``{initiative}/{phase.label}`` for each phase.
    Labels are namespaced per initiative — no bare global labels are created.
    Phase colors are assigned by position (``_PHASE_PALETTE[idx % len]``) so
    any slug convention and any number of phases receives a distinct color.
    """
    coros = [
        ensure_label_exists(
            spec.initiative,
            _INITIATIVE_COLOR,
            f"Initiative: {spec.initiative}",
        )
    ]
    for idx, phase in enumerate(spec.phases):
        color = _PHASE_PALETTE[idx % len(_PHASE_PALETTE)]
        scoped_label = f"{spec.initiative}/{phase.label}"
        coros.append(ensure_label_exists(scoped_label, color, phase.description))
    await asyncio.gather(*coros)


# ── per-issue creation helper (free function avoids closure capture bugs) ──


def _embed_skills(body: str, skills: list[str]) -> str:
    """Append an HTML comment with skill domain IDs to the issue body.

    The comment is machine-readable and invisible to humans in the GitHub UI.
    It is parsed back at agent spawn time by ``_extract_skills_from_body`` in
    ``agentception/routes/api/_shared.py`` to pass as ``skills_hint`` to
    ``_resolve_cognitive_arch``, replacing fallback keyword extraction.
    """
    if not skills:
        return body
    skills_str = ", ".join(skills)
    return f"{body}\n\n<!-- ac:skills: {skills_str} -->"


async def _create_one(
    repo: str, issue: PlanIssue, labels: list[str]
) -> tuple[str, int, str]:
    """Create a single issue; return (issue.id, github_number, html_url)."""
    body_with_skills = _embed_skills(issue.body, issue.skills)
    number, url = await _gh_create_issue(repo, issue.title, body_with_skills, labels)
    return issue.id, number, url


# ── Public async generator ────────────────────────────────────────────────


async def file_issues(spec: PlanSpec) -> AsyncGenerator[IssueFileEvent, None]:
    """Async generator — yields progress events as issues are created.

    Usage::

        async for event in file_issues(spec):
            # serialize and send over SSE
            ...

    The generator never raises; errors are yielded as ``FilingErrorEvent``
    and iteration stops.
    """
    repo = _cfg.gh_repo
    batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    total_issues = sum(len(p.issues) for p in spec.phases)
    id_to_number: dict[str, int] = {}
    id_to_body: dict[str, str] = {
        issue.id: issue.body
        for phase in spec.phases
        for issue in phase.issues
    }
    created: list[CreatedIssue] = []

    yield StartEvent(t="start", total=total_issues, initiative=spec.initiative)

    # ── 1. Labels ──────────────────────────────────────────────────────────
    yield LabelEvent(t="label", text="Ensuring labels exist in GitHub…")
    try:
        await _bootstrap_labels(spec)
    except Exception as exc:
        yield FilingErrorEvent(t="error", detail=f"Label setup failed: {exc}")
        return

    # ── 2. Issues (concurrent within each phase) ───────────────────────────
    index = 0
    for phase in spec.phases:
        labels = [spec.initiative, f"{spec.initiative}/{phase.label}"]
        phase_tasks: list[asyncio.Task[tuple[str, int, str]]] = [
            asyncio.create_task(_create_one(repo, issue, labels))
            for issue in phase.issues
        ]

        for task in asyncio.as_completed(phase_tasks):
            try:
                issue_id, number, url = await task
            except Exception as exc:
                yield FilingErrorEvent(t="error", detail=str(exc))
                # Cancel remaining tasks in this phase before aborting.
                for t in phase_tasks:
                    t.cancel()
                return

            index += 1
            id_to_number[issue_id] = number

            # Find the title for this issue_id.
            title = next(
                (i.title for p in spec.phases for i in p.issues if i.id == issue_id),
                issue_id,
            )
            created.append(
                CreatedIssue(
                    issue_id=issue_id,
                    number=number,
                    url=url,
                    title=title,
                    phase=phase.label,
                )
            )
            logger.info("✅ Created #%d — %s (%s)", number, title, phase.label)
            yield IssueEvent(
                t="issue",
                index=index,
                total=total_issues,
                number=number,
                url=url,
                title=title,
                phase=phase.label,
            )

    # ── 3. Resolve depends_on ──────────────────────────────────────────────
    # Build a map of issue_number → blocker_numbers for DB persistence.
    issue_deps: dict[int, list[int]] = {}
    for phase in spec.phases:
        for issue in phase.issues:
            if not issue.depends_on:
                continue

            blocker_numbers = [
                id_to_number[dep_id]
                for dep_id in issue.depends_on
                if dep_id in id_to_number
            ]
            if not blocker_numbers:
                continue

            our_number = id_to_number.get(issue.id)
            if our_number is None:
                continue

            issue_deps[our_number] = blocker_numbers

            original_body = id_to_body.get(issue.id, issue.body)
            blocked_line = (
                "\n\n---\n**Blocked by:** "
                + ", ".join(f"#{n}" for n in blocker_numbers)
            )
            try:
                await _gh_edit_body(repo, our_number, original_body.rstrip() + blocked_line)
                logger.info(
                    "✅ #%d blocked_by %s",
                    our_number,
                    [f"#{n}" for n in blocker_numbers],
                )
                yield BlockedEvent(
                    t="blocked",
                    number=our_number,
                    blocked_by=blocker_numbers,
                )
            except RuntimeError as exc:
                # Non-fatal — log and continue.
                logger.warning("⚠️ Could not edit #%d for depends_on: %s", our_number, exc)

    # Persist ticket-level deps to DB so the Build board can display them.
    await persist_issue_depends_on(repo, issue_deps)

    # ── 4. Persist phase DAG and display order ────────────────────────────
    # Writes phase_order (list index) alongside the dependency graph so the
    # Build board has a single, explicit source of truth for phase ordering.
    await persist_initiative_phases(
        initiative=spec.initiative,
        phases=[
            {
                "label": f"{spec.initiative}/{p.label}",
                "order": idx,
                "depends_on": [f"{spec.initiative}/{d}" for d in p.depends_on],
            }
            for idx, p in enumerate(spec.phases)
        ],
    )

    yield DoneEvent(
        t="done",
        total=total_issues,
        initiative=spec.initiative,
        batch_id=batch_id,
        issues=created,
    )
