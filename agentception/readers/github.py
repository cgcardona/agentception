from __future__ import annotations

"""GitHub data reader for AgentCeption.

All GitHub data flows through this module via ``gh`` CLI subprocess calls.
Results are cached for ``settings.github_cache_seconds`` (default 10 s) to
avoid hitting the GitHub API rate-limit and keep the dashboard UI snappy.

Write operations (``close_pr``, ``clear_wip_label``) always invalidate the
entire cache so subsequent reads reflect the new state without waiting for TTL
expiry.

Usage::

    from agentception.readers.github import get_open_issues, get_active_label

    issues = await get_open_issues(label="agentception/0-scaffold")
    label  = await get_active_label()
"""

import asyncio
import json
import logging
import time

from agentception.config import settings

logger = logging.getLogger(__name__)

# JSON-compatible value union — the true return type of json.loads().
# Using an explicit union avoids both bare `object` and `Any` while remaining
# honest about what the gh CLI can produce.
JsonValue = str | int | float | bool | list[object] | dict[str, object] | None

# ---------------------------------------------------------------------------
# Internal TTL cache
# ---------------------------------------------------------------------------
# Format: {cache_key: (result, expires_at_unix)}
_cache: dict[str, tuple[JsonValue, float]] = {}


def _cache_get(key: str) -> JsonValue:
    """Return cached value if it exists and has not expired, else None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    result, expires_at = entry
    if time.monotonic() > expires_at:
        del _cache[key]
        return None
    return result


def _cache_set(key: str, value: JsonValue) -> None:
    """Store *value* in the cache with a TTL of ``github_cache_seconds``."""
    expires_at = time.monotonic() + settings.github_cache_seconds
    _cache[key] = (value, expires_at)


def _cache_invalidate() -> None:
    """Clear the entire cache.

    Called after any write operation so the next read reflects current state
    rather than serving a stale response that was cached before the mutation.
    """
    _cache.clear()
    logger.debug("⚠️  GitHub cache invalidated after write operation")


# ---------------------------------------------------------------------------
# Low-level subprocess helper
# ---------------------------------------------------------------------------

async def gh_json(args: list[str], jq: str, cache_key: str) -> JsonValue:
    """Run ``gh`` with ``--json`` + ``--jq`` and cache the result.

    Parameters
    ----------
    args:
        Additional ``gh`` sub-command arguments (e.g. ``["issue", "list",
        "--repo", "cgcardona/agentception"]``).  Do **not** include ``--json`` or
        ``--jq`` — those are appended automatically.
    jq:
        A ``jq`` filter string passed verbatim to ``--jq``.  The ``gh`` CLI
        uses its own bundled ``jq`` — no host installation required.
    cache_key:
        Opaque string that identifies this particular query.  Use a value that
        captures all arguments that affect the result so distinct queries never
        share a cache entry.

    Returns
    -------
    JsonValue
        Parsed JSON (list, dict, str, int, …) — shape depends on the ``jq``
        filter.  Callers must narrow the type with ``isinstance`` checks.

    Raises
    ------
    RuntimeError
        When ``gh`` exits with a non-zero status.
    """
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("✅ GitHub cache hit: %s", cache_key)
        return cached

    cmd = ["gh"] + args + ["--jq", jq]
    logger.debug("⏱️  gh subprocess: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh command failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}  cmd={cmd}"
        )

    raw = stdout.decode().strip()
    result: JsonValue = [] if not raw else json.loads(raw)

    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

async def get_closed_issues(limit: int = 100) -> list[dict[str, object]]:
    """List recently closed issues (most recent first, capped at *limit*).

    Used by the poller to sync closed issues into ``ac_issues`` so the DB
    retains a complete history rather than only tracking open work.

    Parameters
    ----------
    limit:
        Maximum number of closed issues to fetch per tick.  Keeps the GitHub
        API cost proportional — closed issues change rarely so a small window
        captures all recent transitions.
    """
    repo = settings.gh_repo
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "closed",
        "--json", "number,title,labels,body,state,closedAt",
        "--limit", str(limit),
    ]
    cache_key = f"get_closed_issues:limit={limit}"
    result = await gh_json(args, ".", cache_key)
    if not isinstance(result, list):
        raise RuntimeError(f"get_closed_issues: expected list from gh, got {type(result).__name__}")
    return [item for item in result if isinstance(item, dict)]


async def get_open_issues(label: str | None = None) -> list[dict[str, object]]:
    """List open issues, optionally filtered by a single label.

    Returns each issue as a dict with at minimum: ``number``, ``title``,
    ``labels`` (list of label objects), ``body``, and ``state``.

    Parameters
    ----------
    label:
        When provided, only issues carrying this label are returned.
    """
    repo = settings.gh_repo
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,labels,body,state",
    ]
    if label:
        args += ["--label", label]

    cache_key = f"get_open_issues:label={label}"
    result = await gh_json(args, ".", cache_key)
    if not isinstance(result, list):
        raise RuntimeError(f"get_open_issues: expected list from gh, got {type(result).__name__}")
    return [item for item in result if isinstance(item, dict)]


async def get_open_prs() -> list[dict[str, object]]:
    """List open pull requests targeting the ``dev`` branch.

    Returns each PR as a dict with at minimum: ``number``, ``title``,
    ``headRefName``, ``labels``, and ``state``.
    """
    repo = settings.gh_repo
    args = [
        "pr", "list",
        "--repo", repo,
        "--base", "dev",
        "--state", "open",
        "--json", "number,title,headRefName,labels,state",
    ]
    result = await gh_json(args, ".", "get_open_prs")
    if not isinstance(result, list):
        raise RuntimeError(f"get_open_prs: expected list from gh, got {type(result).__name__}")
    return [item for item in result if isinstance(item, dict)]


async def get_open_prs_with_body() -> list[dict[str, object]]:
    """List open PRs targeting ``dev`` including the body text.

    Like ``get_open_prs()`` but also fetches the PR body so callers can parse
    ``Closes #NNN`` references to identify linked issues.  Used by the
    out-of-order PR guard (``agentception.intelligence.guards``).
    """
    repo = settings.gh_repo
    args = [
        "pr", "list",
        "--repo", repo,
        "--base", "dev",
        "--state", "open",
        "--json", "number,title,headRefName,labels,body",
    ]
    result = await gh_json(args, ".", "get_open_prs_with_body")
    if not isinstance(result, list):
        raise RuntimeError(
            f"get_open_prs_with_body: expected list from gh, got {type(result).__name__}"
        )
    return [item for item in result if isinstance(item, dict)]


async def get_merged_prs() -> list[dict[str, object]]:
    """List merged pull requests targeting the ``dev`` branch.

    Returns each PR as a dict with at minimum: ``number``, ``headRefName``,
    ``body``, and ``mergedAt``.  Used by the A/B results dashboard to
    correlate PR outcomes (merge status, reviewer grade) with agent batches.
    """
    repo = settings.gh_repo
    args = [
        "pr", "list",
        "--repo", repo,
        "--base", "dev",
        "--state", "merged",
        "--json", "number,headRefName,body,mergedAt",
    ]
    result = await gh_json(args, ".", "get_merged_prs")
    if not isinstance(result, list):
        raise RuntimeError(f"get_merged_prs: expected list from gh, got {type(result).__name__}")
    return [item for item in result if isinstance(item, dict)]


async def get_merged_prs_full(limit: int = 100) -> list[dict[str, object]]:
    """List recently merged PRs with full metadata including labels and title.

    Like ``get_merged_prs`` but adds ``title`` and ``labels`` so the results
    can be persisted into ``ac_pull_requests`` with complete information.
    The ``limit`` cap keeps the per-tick API cost bounded — merged PRs are
    immutable so a small recent window is sufficient for the DB to stay current.

    Parameters
    ----------
    limit:
        Maximum number of merged PRs to fetch per tick.
    """
    repo = settings.gh_repo
    args = [
        "pr", "list",
        "--repo", repo,
        "--base", "dev",
        "--state", "merged",
        "--json", "number,title,headRefName,labels,mergedAt,state",
        "--limit", str(limit),
    ]
    cache_key = f"get_merged_prs_full:limit={limit}"
    result = await gh_json(args, ".", cache_key)
    if not isinstance(result, list):
        raise RuntimeError(
            f"get_merged_prs_full: expected list from gh, got {type(result).__name__}"
        )
    return [item for item in result if isinstance(item, dict)]


async def get_pr_comments(pr_number: int) -> list[str]:
    """Return the body text of all comments posted on a pull request.

    Fetches issue-timeline comments (which includes entries created via
    ``gh pr comment``) using the GitHub REST API.  Returns an empty list
    when the PR has no comments or when the API call fails so callers can
    treat a missing grade as ``None`` without special-casing.

    Parameters
    ----------
    pr_number:
        GitHub pull request number.
    """
    repo = settings.gh_repo
    cache_key = f"get_pr_comments:{pr_number}"
    result = await gh_json(
        ["api", f"repos/{repo}/issues/{pr_number}/comments"],
        "[.[].body]",
        cache_key,
    )
    if not isinstance(result, list):
        return []
    return [str(c) for c in result if isinstance(c, str)]


async def get_issue_comments(issue_number: int) -> list[dict[str, object]]:
    """Return comments posted on a GitHub issue.

    Fetches via the GitHub REST API.  Each comment dict has: ``id``,
    ``author`` (login), ``body``, ``created_at``.

    Parameters
    ----------
    issue_number:
        GitHub issue number.
    """
    repo = settings.gh_repo
    cache_key = f"get_issue_comments:{issue_number}"
    result = await gh_json(
        ["api", f"repos/{repo}/issues/{issue_number}/comments"],
        '[.[] | {id: .id, author: .user.login, body: .body, created_at: .created_at}]',
        cache_key,
    )
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


async def get_pr_checks(pr_number: int) -> list[dict[str, object]]:
    """Return CI check statuses for a pull request.

    Uses ``gh pr checks`` which surfaces GitHub Actions, required status
    checks, and third-party CI integrations.  Each check dict has:
    ``name``, ``state``, ``conclusion``, ``url``.

    Returns an empty list on any error (e.g. no checks configured).

    Parameters
    ----------
    pr_number:
        GitHub pull request number.
    """
    repo = settings.gh_repo
    cache_key = f"get_pr_checks:{pr_number}"
    # gh pr checks returns tab-delimited output — use gh api instead for JSON
    result = await gh_json(
        ["api", f"repos/{repo}/commits/refs/pull/{pr_number}/head/check-runs"],
        "[.check_runs[] | {name: .name, state: .status, conclusion: .conclusion, url: .html_url}]",
        cache_key,
    )
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


async def get_pr_reviews(pr_number: int) -> list[dict[str, object]]:
    """Return review decisions for a pull request.

    Each review dict has: ``author``, ``state``, ``body``, ``submitted_at``.
    States are GitHub values: ``APPROVED``, ``CHANGES_REQUESTED``,
    ``COMMENTED``, ``DISMISSED``.

    Parameters
    ----------
    pr_number:
        GitHub pull request number.
    """
    repo = settings.gh_repo
    cache_key = f"get_pr_reviews:{pr_number}"
    result = await gh_json(
        ["api", f"repos/{repo}/pulls/{pr_number}/reviews"],
        "[.[] | {author: .user.login, state: .state, body: .body, submitted_at: .submitted_at}]",
        cache_key,
    )
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


async def get_wip_issues() -> list[dict[str, object]]:
    """Return issues currently labelled ``agent:wip``.

    An ``agent:wip`` label signals that a pipeline agent has claimed the
    issue.  The dashboard uses this to detect in-flight work.
    """
    return await get_open_issues(label="agent:wip")


async def get_active_label() -> str | None:
    """Return the currently active pipeline phase label.

    Resolution order:
    1. If an operator has manually pinned a label via the UI (see
       :mod:`agentception.readers.active_label_override`), return that pin
       immediately without touching GitHub.  This lets operators override the
       automatic phase selection — e.g. to target a later phase regardless of
       whether earlier phases are fully closed.
    2. Otherwise, scan open GitHub issues for the first label in
       ``pipeline-config.json`` ``active_labels_order`` that has at least one
       open issue (auto-advance behaviour).

    Returns ``None`` when no pin is set and no configured label has open issues.
    """
    from agentception.readers.active_label_override import get_pin
    from agentception.readers.pipeline_config import read_pipeline_config  # local import to avoid circular

    pin = get_pin()
    if pin is not None:
        return pin

    try:
        config = await read_pipeline_config()
        labels_order: list[str] = config.active_labels_order
    except Exception as exc:
        logger.warning("⚠️  Could not read pipeline config for active label: %s", exc)
        labels_order = []

    if not labels_order:
        return None

    # Fetch all labels present on any open issue (one gh call, cached).
    repo = settings.gh_repo
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "labels",
    ]
    result = await gh_json(args, "[.[].labels[].name]", "get_active_label")
    if not isinstance(result, list):
        raise RuntimeError(f"get_active_label: expected list from gh, got {type(result).__name__}")

    open_labels: set[str] = {name for name in result if isinstance(name, str)}

    # Return the first configured label that actually has open issues.
    for label in labels_order:
        if label in open_labels:
            return label

    return None


async def get_issue(number: int) -> dict[str, object]:
    """Fetch state, title, and labels for a single issue.

    Returns a dict with at minimum: ``number``, ``state``, ``title``,
    and ``labels`` (list of label-name strings).

    Parameters
    ----------
    number:
        GitHub issue number.

    Raises
    ------
    RuntimeError
        When ``gh`` exits with a non-zero status (e.g. issue not found).
    """
    repo = settings.gh_repo
    args = [
        "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,state,title,labels",
    ]
    result = await gh_json(
        args,
        "{number: .number, state: .state, title: .title, labels: [.labels[].name]}",
        f"get_issue:{number}",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"get_issue: expected dict from gh, got {type(result).__name__}")
    return result


async def get_issue_body(number: int) -> str:
    """Fetch the markdown body of a single issue.

    Used by the ticket analyser and DAG builder to parse dependency
    declarations (``Depends on #N``) and extract structured metadata.

    Parameters
    ----------
    number:
        GitHub issue number.

    Note
    ----
    The ``gh`` CLI outputs string-valued jq results in **raw mode** (no JSON
    quotes), so using ``--jq .body`` directly produces text that
    ``json.loads`` cannot parse.  We fetch the full ``{"body": "..."}``
    object with ``--jq "."`` and extract the field from the dict instead.
    """
    repo = settings.gh_repo
    args = [
        "issue", "view", str(number),
        "--repo", repo,
        "--json", "body",
    ]
    result = await gh_json(args, ".", f"get_issue_body:{number}")
    if not isinstance(result, dict):
        raise RuntimeError(f"get_issue_body: expected dict from gh, got {type(result).__name__}")
    body = result.get("body")
    return str(body) if body is not None else ""


# ---------------------------------------------------------------------------
# Write operations (always invalidate cache)
# ---------------------------------------------------------------------------

async def close_pr(number: int, comment: str) -> None:
    """Close a pull request and post a comment explaining the closure.

    Invalidates the cache so subsequent reads reflect the updated PR state.

    Parameters
    ----------
    number:
        GitHub PR number.
    comment:
        Comment body to post before closing (appears in the PR timeline).
    """
    repo = settings.gh_repo

    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "close", str(number),
        "--repo", repo,
        "--comment", comment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh pr close failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )

    logger.info("✅ PR #%d closed with comment", number)
    _cache_invalidate()


async def add_wip_label(issue_number: int) -> None:
    """Add the ``agent:wip`` label to an issue to claim it for a pipeline agent.

    Invalidates the cache so subsequent ``get_wip_issues()`` calls immediately
    reflect the new label without waiting for TTL expiry.

    Parameters
    ----------
    issue_number:
        GitHub issue number to label.

    Raises
    ------
    RuntimeError
        When ``gh`` exits with a non-zero status.
    """
    repo = settings.gh_repo

    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
        "--add-label", "agent:wip",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue edit (add label) failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )

    logger.info("✅ Added agent:wip to issue #%d", issue_number)
    _cache_invalidate()


async def add_label_to_issue(issue_number: int, label: str) -> None:
    """Add *label* to an issue.

    Follows the same pattern as :func:`add_wip_label` — runs ``gh issue edit
    --add-label`` as a subprocess and invalidates the cache on success so
    subsequent reads reflect the new label without waiting for TTL expiry.

    Parameters
    ----------
    issue_number:
        GitHub issue number to label.
    label:
        Label name to add (e.g. ``"approved"``).

    Raises
    ------
    RuntimeError
        When ``gh`` exits with a non-zero status.
    """
    repo = settings.gh_repo

    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
        "--add-label", label,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue edit (add label) failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )

    logger.info("✅ Added %r to issue #%d", label, issue_number)
    _cache_invalidate()


async def ensure_label_exists(name: str, color: str, description: str) -> None:
    """Create a GitHub label if it does not already exist.

    Uses ``gh label create --force`` which is idempotent: creates the label
    when absent and updates colour/description when present.  Safe to call
    on every approve request without checking first.

    Parameters
    ----------
    name:
        Label name (e.g. ``"approved"``).
    color:
        Six-digit hex colour without the leading ``#`` (e.g. ``"2ea44f"``).
    description:
        Short human-readable label description.

    Raises
    ------
    RuntimeError
        When ``gh`` exits with a non-zero status.
    """
    repo = settings.gh_repo

    proc = await asyncio.create_subprocess_exec(
        "gh", "label", "create", name,
        "--repo", repo,
        "--color", color,
        "--description", description,
        "--force",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh label create failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )

    logger.info("✅ Label %r ensured on %s", name, repo)


async def remove_label_from_issue(issue_number: int, label: str) -> None:
    """Remove *label* from an issue.

    Runs ``gh issue edit --remove-label`` as a subprocess and invalidates the
    cache on success.  If the label is not present on the issue, ``gh`` exits
    cleanly — this function is idempotent with respect to absent labels.

    Parameters
    ----------
    issue_number:
        GitHub issue number to modify.
    label:
        Label name to remove (e.g. ``"blocked"``).

    Raises
    ------
    RuntimeError
        When ``gh`` exits with a non-zero status for any reason other than the
        label not being present on the issue.
    """
    repo = settings.gh_repo

    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
        "--remove-label", label,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode().strip()
        # gh returns non-zero when the label does not exist on the issue;
        # treat that as a no-op rather than a hard failure.
        if "not found" in err_msg.lower() or "could not find" in err_msg.lower():
            logger.debug(
                "⚠️ remove_label_from_issue: label %r not on issue #%d (no-op)",
                label,
                issue_number,
            )
            return
        raise RuntimeError(
            f"gh issue edit (remove label) failed (exit {proc.returncode}): "
            f"{err_msg!r}"
        )

    logger.info("✅ Removed %r from issue #%d", label, issue_number)
    _cache_invalidate()


async def clear_wip_label(issue_number: int) -> None:
    """Remove the ``agent:wip`` label from an issue.

    Called by the control plane after an agent completes its task so the
    issue no longer shows up in ``get_wip_issues()``.

    Invalidates the cache so subsequent reads see the updated label set.

    Parameters
    ----------
    issue_number:
        GitHub issue number to remove ``agent:wip`` from.
    """
    repo = settings.gh_repo

    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
        "--remove-label", "agent:wip",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue edit failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()!r}"
        )

    logger.info("✅ Removed agent:wip from issue #%d", issue_number)
    _cache_invalidate()
