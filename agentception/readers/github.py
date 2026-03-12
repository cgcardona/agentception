from __future__ import annotations

"""GitHub REST API client for AgentCeption infrastructure.

All GitHub data flows through this module via authenticated ``httpx`` calls to
``https://api.github.com``.  Results are cached for
``settings.github_cache_seconds`` (default 10 s) to avoid hitting rate limits
and keep the dashboard UI snappy.

Write operations always invalidate the entire cache so subsequent reads reflect
the new state without waiting for TTL expiry.

Usage::

    from agentception.readers.github import get_open_issues, get_active_label

    issues = await get_open_issues(label="agentception/0-scaffold")
    label  = await get_active_label()
"""

import asyncio
import logging
import time
import urllib.parse

import httpx

from agentception.config import settings

logger = logging.getLogger(__name__)

# JSON-compatible value union — the true return type of json.loads().
# Using an explicit union avoids both bare `object` and `Any` while remaining
# honest about what the GitHub REST API can produce.
JsonValue = str | int | float | bool | list[object] | dict[str, object] | None

# ---------------------------------------------------------------------------
# Internal TTL cache
# ---------------------------------------------------------------------------
# Format: {cache_key: (result, expires_at_unix)}
_cache: dict[str, tuple[JsonValue, float]] = {}

_BASE_URL = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# 429 retry / backoff constants
# ---------------------------------------------------------------------------
# GitHub's rate-limit window is 60 s.  We wait at least this long before
# retrying so we don't burn the remaining quota on a burst of retries.
_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF_SECS: float = 60.0


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
    """Store *value* in the cache with a TTL of ``github_cache_seconds``.

    Invariant: TTL < poll_interval_seconds so every poller tick receives fresh
    data from GitHub.  Default TTL is min(poll_interval_seconds / 2, 30) = 15 s
    against the default 30 s poll interval.  If you raise the poll interval,
    keep GITHUB_CACHE_SECONDS strictly below it.
    """
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
# 429 backoff helper
# ---------------------------------------------------------------------------

async def _rate_limit_sleep(response: httpx.Response, attempt: int) -> None:
    """Sleep the appropriate amount after a 429 response from GitHub.

    Reads the ``Retry-After`` header when present; otherwise uses an
    exponentially growing backoff starting at ``_RATE_LIMIT_BACKOFF_SECS``.

    Parameters
    ----------
    response:
        The 429 response from GitHub.
    attempt:
        Zero-based retry attempt index (0 = first retry).
    """
    retry_after_raw = response.headers.get("retry-after", "")
    try:
        wait = max(float(retry_after_raw), _RATE_LIMIT_BACKOFF_SECS)
    except (ValueError, TypeError):
        wait = _RATE_LIMIT_BACKOFF_SECS * (2.0 ** attempt)
    logger.warning(
        "⚠️  GitHub rate-limited (429) retry %d/%d — sleeping %.0fs (Retry-After=%r)",
        attempt + 1,
        _MAX_RETRIES,
        wait,
        retry_after_raw or "not set",
    )
    await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    """Return authenticated headers for the GitHub REST API.

    Raises ``RuntimeError`` when ``GITHUB_TOKEN`` is not configured so callers
    get a clear error instead of a 401 from the API.
    """
    token = settings.github_token
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set — GitHub REST API calls are unavailable. "
            "Set the GITHUB_TOKEN env var and restart the service."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
    }


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

async def _api_get(
    path: str,
    params: dict[str, str | int],
    cache_key: str,
) -> JsonValue:
    """Authenticated GET against the GitHub REST API, with TTL caching.

    Parameters
    ----------
    path:
        Path relative to ``https://api.github.com/`` (no leading slash),
        e.g. ``"repos/org/repo/issues/42"``.
    params:
        Query-string parameters (merged with the request).
    cache_key:
        Opaque string identifying this query.  Distinct queries must use
        distinct keys so they never share a cache entry.

    Returns
    -------
    JsonValue
        Parsed JSON — callers must narrow with ``isinstance`` checks.

    Raises
    ------
    RuntimeError
        On any non-2xx HTTP status or when ``GITHUB_TOKEN`` is unset.
    """
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("✅ GitHub cache hit: %s", cache_key)
        return cached

    logger.debug("⏱️  GitHub REST GET: %s params=%s", path, params)
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{_BASE_URL}/{path}",
                params=params,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API GET /{path} failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc
        result: JsonValue = r.json()
        _cache_set(cache_key, result)
        return result

    # Exhausted retries on 429 — raise from the last response.
    raise RuntimeError(
        f"GitHub API GET /{path} failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


async def _api_get_all(
    path: str,
    params: dict[str, str | int],
    cache_key: str,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Paginated GET — fetches up to *limit* items across pages (max 100/page).

    Uses the GitHub REST API Link header pagination.  Stops when a page
    returns fewer items than requested or when *limit* is reached.
    """
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("✅ GitHub cache hit: %s", cache_key)
        if isinstance(cached, list):
            return [i for i in cached if isinstance(i, dict)]
        return []

    per_page = min(limit, 100)
    all_items: list[dict[str, object]] = []
    page = 1

    while len(all_items) < limit:
        page_params: dict[str, str | int] = {
            **params,
            "per_page": per_page,
            "page": page,
        }
        logger.debug("⏱️  GitHub REST GET page %d: %s", page, path)
        r: httpx.Response | None = None
        for attempt in range(_MAX_RETRIES + 1):
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{_BASE_URL}/{path}",
                    params=page_params,
                    headers=_headers(),
                    timeout=_TIMEOUT,
                )
            if r.status_code == 429 and attempt < _MAX_RETRIES:
                await _rate_limit_sleep(r, attempt)
                continue
            break
        assert r is not None
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API GET /{path} page {page} failed "
                f"({exc.response.status_code}): {exc.response.text[:400]}"
            ) from exc

        page_data: object = r.json()
        if not isinstance(page_data, list) or not page_data:
            break

        for item in page_data:
            if isinstance(item, dict):
                all_items.append(item)
            if len(all_items) >= limit:
                break

        if len(page_data) < per_page:
            break  # last page — no point requesting further
        page += 1

    # Store as list[object] (the JsonValue-compatible supertype).
    _cache_set(cache_key, list(all_items))
    return all_items


async def _api_post(path: str, payload: dict[str, object]) -> dict[str, object]:
    """Authenticated POST. Always invalidates the cache on success."""
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{_BASE_URL}/{path}",
                json=payload,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API POST /{path} failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc
        _cache_invalidate()
        result: object = r.json()
        return result if isinstance(result, dict) else {}

    raise RuntimeError(
        f"GitHub API POST /{path} failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


async def _api_patch(path: str, payload: dict[str, object]) -> dict[str, object]:
    """Authenticated PATCH. Always invalidates the cache on success."""
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.patch(
                f"{_BASE_URL}/{path}",
                json=payload,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API PATCH /{path} failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc
        _cache_invalidate()
        result: object = r.json()
        return result if isinstance(result, dict) else {}

    raise RuntimeError(
        f"GitHub API PATCH /{path} failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


async def _api_put(path: str, payload: dict[str, object]) -> dict[str, object]:
    """Authenticated PUT. Always invalidates the cache on success."""
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.put(
                f"{_BASE_URL}/{path}",
                json=payload,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API PUT /{path} failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc
        _cache_invalidate()
        result: object = r.json()
        return result if isinstance(result, dict) else {}

    raise RuntimeError(
        f"GitHub API PUT /{path} failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


async def _api_delete(path: str) -> None:
    """Authenticated DELETE. Always invalidates the cache on success."""
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"{_BASE_URL}/{path}",
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API DELETE /{path} failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc
        _cache_invalidate()
        return

    raise RuntimeError(
        f"GitHub API DELETE /{path} failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


# ---------------------------------------------------------------------------
# Field-name normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_pr(raw: dict[str, object]) -> dict[str, object]:
    """Map GitHub REST PR fields to the camelCase names our codebase expects.

    The GitHub REST API uses ``head.ref`` / ``base.ref`` / ``draft``; the rest
    of AgentCeption uses ``headRefName`` / ``baseRefName`` / ``isDraft`` (the
    names that the ``gh`` CLI's ``--json`` output used).  Normalising here
    keeps every caller unchanged.
    """
    head: object = raw.get("head")
    base: object = raw.get("base")
    return {
        **raw,
        "headRefName": (head.get("ref") if isinstance(head, dict) else None),
        "baseRefName": (base.get("ref") if isinstance(base, dict) else None),
        "isDraft": bool(raw.get("draft", False)),
        "mergedAt": raw.get("merged_at"),
    }


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

async def get_closed_issues(limit: int = 100) -> list[dict[str, object]]:
    """List recently closed issues (most recent first, capped at *limit*).

    Used by the poller to sync closed issues into the DB so it retains a
    complete history rather than only tracking open work.

    Parameters
    ----------
    limit:
        Maximum number of closed issues to fetch.  Keeps API cost proportional
        — closed issues change rarely so a small window captures all recent
        transitions.
    """
    repo = settings.gh_repo
    cache_key = f"get_closed_issues:limit={limit}"
    items = await _api_get_all(
        f"repos/{repo}/issues",
        {"state": "closed"},
        cache_key,
        limit=limit,
    )
    # The REST issues endpoint includes pull requests — filter them out.
    return [i for i in items if "pull_request" not in i]


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
    params: dict[str, str | int] = {"state": "open"}
    if label:
        params["labels"] = label

    cache_key = f"get_open_issues:label={label}"
    items = await _api_get_all(f"repos/{repo}/issues", params, cache_key)
    # Filter out pull requests (GitHub issues endpoint includes them).
    return [i for i in items if "pull_request" not in i]


async def get_open_prs() -> list[dict[str, object]]:
    """List open pull requests targeting the ``dev`` branch.

    Returns each PR as a dict with: ``number``, ``title``, ``headRefName``,
    ``baseRefName``, ``labels``, ``state``, ``body``, ``isDraft``.

    The ``body`` and ``baseRefName`` fields are required for correct PR↔Issue
    linkage and base-mismatch detection in the workflow state machine.
    """
    repo = settings.gh_repo
    items = await _api_get_all(
        f"repos/{repo}/pulls",
        {"state": "open", "base": "dev"},
        "get_open_prs",
    )
    return [_normalize_pr(i) for i in items]


async def get_open_prs_any_base() -> list[dict[str, object]]:
    """List ALL open pull requests regardless of target branch.

    Ensures PRs opened against ``main``, ``staging``, or any other branch
    are not lost.  The workflow state machine uses ``baseRefName`` to detect
    base-mismatch and issue a warning, but the card still moves.
    """
    repo = settings.gh_repo
    items = await _api_get_all(
        f"repos/{repo}/pulls",
        {"state": "open"},
        "get_open_prs_any_base",
    )
    return [_normalize_pr(i) for i in items]


async def get_open_prs_with_body() -> list[dict[str, object]]:
    """List open PRs targeting ``dev`` including the body text.

    Delegates to ``get_open_prs()`` which always includes body.
    """
    return await get_open_prs()


async def get_merged_prs() -> list[dict[str, object]]:
    """List merged pull requests targeting the ``dev`` branch.

    Returns each PR as a dict with at minimum: ``number``, ``headRefName``,
    ``body``, and ``mergedAt``.  Used by the A/B results dashboard to
    correlate PR outcomes (merge status, reviewer grade) with agent batches.
    """
    repo = settings.gh_repo
    items = await _api_get_all(
        f"repos/{repo}/pulls",
        {"state": "closed", "base": "dev"},
        "get_merged_prs",
    )
    # The closed pulls endpoint includes both merged and simply-closed PRs.
    merged = [i for i in items if i.get("merged_at") is not None]
    return [_normalize_pr(i) for i in merged]


async def get_merged_prs_full(limit: int = 100) -> list[dict[str, object]]:
    """List recently merged PRs with full metadata including labels and title.

    Like ``get_merged_prs`` but adds ``title`` and ``labels`` so results can
    be persisted into ``pull_requests`` with complete information.

    Parameters
    ----------
    limit:
        Maximum number of merged PRs to fetch per tick.
    """
    repo = settings.gh_repo
    cache_key = f"get_merged_prs_full:limit={limit}"
    items = await _api_get_all(
        f"repos/{repo}/pulls",
        {"state": "closed", "base": "dev"},
        cache_key,
        limit=limit,
    )
    merged = [i for i in items if i.get("merged_at") is not None]
    return [_normalize_pr(i) for i in merged]


async def get_pr_comments(pr_number: int) -> list[str]:
    """Return the body text of all comments posted on a pull request.

    Returns an empty list when the PR has no comments or when the API call
    fails so callers can treat a missing grade as ``None`` without
    special-casing.

    Parameters
    ----------
    pr_number:
        GitHub pull request number.
    """
    repo = settings.gh_repo
    cache_key = f"get_pr_comments:{pr_number}"
    result = await _api_get(
        f"repos/{repo}/issues/{pr_number}/comments",
        {},
        cache_key,
    )
    if not isinstance(result, list):
        return []
    return [
        str(c.get("body", ""))
        for c in result
        if isinstance(c, dict) and isinstance(c.get("body"), str)
    ]


async def get_issue_comments(issue_number: int) -> list[dict[str, object]]:
    """Return comments posted on a GitHub issue.

    Each comment dict has: ``id``, ``author`` (login), ``body``,
    ``created_at``.

    Parameters
    ----------
    issue_number:
        GitHub issue number.
    """
    repo = settings.gh_repo
    cache_key = f"get_issue_comments:{issue_number}"
    result = await _api_get(
        f"repos/{repo}/issues/{issue_number}/comments",
        {},
        cache_key,
    )
    if not isinstance(result, list):
        return []
    out: list[dict[str, object]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        user: object = item.get("user")
        login = user.get("login") if isinstance(user, dict) else ""
        out.append(
            {
                "id": item.get("id"),
                "author": login,
                "body": item.get("body", ""),
                "created_at": item.get("created_at"),
            }
        )
    return out


async def get_pr_checks(pr_number: int) -> list[dict[str, object]]:
    """Return CI check statuses for a pull request.

    Each check dict has: ``name``, ``state``, ``conclusion``, ``url``.
    Returns an empty list on any error (e.g. no checks configured).

    Parameters
    ----------
    pr_number:
        GitHub pull request number.
    """
    repo = settings.gh_repo
    cache_key = f"get_pr_checks:{pr_number}"
    try:
        result = await _api_get(
            f"repos/{repo}/commits/refs/pull/{pr_number}/head/check-runs",
            {},
            cache_key,
        )
    except RuntimeError:
        return []

    if not isinstance(result, dict):
        return []
    check_runs: object = result.get("check_runs", [])
    if not isinstance(check_runs, list):
        return []
    out: list[dict[str, object]] = []
    for run in check_runs:
        if isinstance(run, dict):
            out.append(
                {
                    "name": run.get("name"),
                    "state": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "url": run.get("html_url"),
                }
            )
    return out


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
    result = await _api_get(
        f"repos/{repo}/pulls/{pr_number}/reviews",
        {},
        cache_key,
    )
    if not isinstance(result, list):
        return []
    out: list[dict[str, object]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        user: object = item.get("user")
        login = user.get("login") if isinstance(user, dict) else ""
        out.append(
            {
                "author": login,
                "state": item.get("state"),
                "body": item.get("body", ""),
                "submitted_at": item.get("submitted_at"),
            }
        )
    return out


async def get_wip_issues() -> list[dict[str, object]]:
    """Return issues currently labelled ``agent/wip``.

    An ``agent/wip`` label signals that a pipeline agent has claimed the
    issue.  The dashboard uses this to detect in-flight work.
    """
    return await get_open_issues(label="agent/wip")


async def get_active_label() -> str | None:
    """Return the currently active pipeline phase label.

    Resolution order:
    1. If an operator has manually pinned a label via the UI, return that pin
       immediately without touching GitHub.  This lets operators override the
       automatic phase selection.
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

    repo = settings.gh_repo
    result = await _api_get(
        f"repos/{repo}/issues",
        {"state": "open", "per_page": 100},
        "get_active_label",
    )
    if not isinstance(result, list):
        raise RuntimeError(
            f"get_active_label: expected list from GitHub API, got {type(result).__name__}"
        )

    open_labels: set[str] = set()
    for issue in result:
        if not isinstance(issue, dict):
            continue
        # Skip pull requests — GitHub issues endpoint includes them.
        if "pull_request" in issue:
            continue
        for lbl in issue.get("labels", []):
            if isinstance(lbl, dict):
                name: object = lbl.get("name")
                if isinstance(name, str):
                    open_labels.add(name)

    for label in labels_order:
        if label in open_labels:
            return label

    return None


async def get_issue(number: int) -> dict[str, object]:
    """Fetch state, title, and labels for a single issue.

    Returns a dict with at minimum: ``number``, ``state``, ``title``,
    ``body``, and ``labels`` (list of label-name strings).

    Parameters
    ----------
    number:
        GitHub issue number.

    Raises
    ------
    RuntimeError
        When the API returns a non-2xx status (e.g. issue not found).
    """
    repo = settings.gh_repo
    result = await _api_get(
        f"repos/{repo}/issues/{number}",
        {},
        f"get_issue:{number}",
    )
    if not isinstance(result, dict):
        raise RuntimeError(
            f"get_issue: expected dict from GitHub API, got {type(result).__name__}"
        )
    # Normalise labels to a list of name strings (same shape as before).
    raw_labels: object = result.get("labels", [])
    label_names: list[str] = []
    if isinstance(raw_labels, list):
        for lbl in raw_labels:
            if isinstance(lbl, dict):
                name: object = lbl.get("name")
                if isinstance(name, str):
                    label_names.append(name)
    return {
        "number": result.get("number"),
        "state": result.get("state"),
        "title": result.get("title"),
        "body": result.get("body", ""),
        "labels": label_names,
    }


async def get_repo_labels(limit: int = 100) -> list[dict[str, object]]:
    """Return all labels defined in the repository.

    Each label dict has at minimum ``name``, ``color``, and ``description``
    (GitHub REST shape).  Used by ``plan_get_labels`` and the context packer to
    surface available labels as LLM context.

    Parameters
    ----------
    limit:
        Maximum number of labels to fetch (default 100).
    """
    repo = settings.gh_repo
    cache_key = f"get_repo_labels:limit={limit}"
    return await _api_get_all(
        f"repos/{repo}/labels",
        {},
        cache_key,
        limit=limit,
    )


async def get_issues_with_all_labels(
    labels: list[str],
    state: str = "all",
    limit: int = 200,
) -> list[dict[str, object]]:
    """Fetch issues that carry **every** label in *labels* (AND semantics).

    The GitHub REST API accepts a comma-separated ``labels`` query parameter
    and returns only issues that have all specified labels — matching the
    AND behaviour of ``gh issue list --label A --label B``.

    State values are normalised to uppercase (``"OPEN"`` / ``"CLOSED"``)
    in the returned dicts to preserve backward-compatibility with callers
    that were written against the ``gh`` CLI output format.

    Parameters
    ----------
    labels:
        Label names that must all be present on matching issues.
    state:
        One of ``"open"``, ``"closed"``, or ``"all"`` (default).
    limit:
        Maximum number of issues to fetch.
    """
    repo = settings.gh_repo
    params: dict[str, str | int] = {
        "state": state,
        "labels": ",".join(labels),
    }
    cache_key = f"get_issues_with_all_labels:labels={'|'.join(sorted(labels))}:state={state}"
    items = await _api_get_all(f"repos/{repo}/issues", params, cache_key, limit=limit)
    # Normalise state to uppercase to match the legacy gh CLI output shape.
    normalised: list[dict[str, object]] = []
    for item in items:
        if "pull_request" in item:
            continue
        raw_state: object = item.get("state", "")
        normalised.append(
            {
                **item,
                "state": str(raw_state).upper() if isinstance(raw_state, str) else raw_state,
            }
        )
    return normalised


async def get_issue_body(number: int) -> str:
    """Fetch the markdown body of a single issue.

    Used by the ticket analyser and DAG builder to parse dependency
    declarations (``Depends on #N``) and extract structured metadata.

    Parameters
    ----------
    number:
        GitHub issue number.
    """
    repo = settings.gh_repo
    result = await _api_get(
        f"repos/{repo}/issues/{number}",
        {},
        f"get_issue_body:{number}",
    )
    if not isinstance(result, dict):
        raise RuntimeError(
            f"get_issue_body: expected dict from GitHub API, got {type(result).__name__}"
        )
    body = result.get("body")
    return str(body) if body is not None else ""


# ---------------------------------------------------------------------------
# Write operations (always invalidate cache)
# ---------------------------------------------------------------------------

async def close_pr(number: int, comment: str) -> None:
    """Close a pull request and post a comment explaining the closure.

    Parameters
    ----------
    number:
        GitHub PR number.
    comment:
        Comment body to post before closing (appears in the PR timeline).
    """
    repo = settings.gh_repo
    # Post the comment first, then close.
    await _api_post(
        f"repos/{repo}/issues/{number}/comments",
        {"body": comment},
    )
    await _api_patch(
        f"repos/{repo}/pulls/{number}",
        {"state": "closed"},
    )
    logger.info("✅ PR #%d closed with comment", number)


async def add_wip_label(issue_number: int) -> None:
    """Add the ``agent/wip`` label to an issue to claim it for a pipeline agent.

    Invalidates the cache so subsequent ``get_wip_issues()`` calls immediately
    reflect the new label without waiting for TTL expiry.

    Parameters
    ----------
    issue_number:
        GitHub issue number to label.

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response.
    """
    repo = settings.gh_repo
    await _api_post(
        f"repos/{repo}/issues/{issue_number}/labels",
        {"labels": ["agent/wip"]},
    )
    logger.info("✅ Added agent/wip to issue #%d", issue_number)


async def add_label_to_issue(issue_number: int, label: str) -> None:
    """Add *label* to an issue.

    Parameters
    ----------
    issue_number:
        GitHub issue number to label.
    label:
        Label name to add (e.g. ``"approved"``).

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response.
    """
    repo = settings.gh_repo
    await _api_post(
        f"repos/{repo}/issues/{issue_number}/labels",
        {"labels": [label]},
    )
    logger.info("✅ Added %r to issue #%d", label, issue_number)


async def ensure_label_exists(name: str, color: str, description: str) -> None:
    """Create a GitHub label if it does not already exist.

    Uses a try-create / update-on-conflict pattern that is idempotent:
    creates the label when absent and updates colour/description when present.
    Safe to call on every approve request without checking first.

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
        On any non-2xx GitHub API response other than 422 (already exists).
    """
    repo = settings.gh_repo
    payload: dict[str, object] = {
        "name": name,
        "color": color,
        "description": description,
    }
    # Attempt to create; if 422 (already exists) update in place instead.
    # We cannot use _api_post directly because we need to inspect the 422
    # status before raising, so we call the underlying client once and branch.
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{_BASE_URL}/repos/{repo}/labels",
                json=payload,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        if r.status_code == 422:
            # Label already exists — update it in place via the _api_patch helper
            # which itself handles 429 retries.
            encoded = urllib.parse.quote(name, safe="")
            await _api_patch(f"repos/{repo}/labels/{encoded}", payload)
            logger.info("✅ Label %r ensured on %s", name, repo)
            return
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API POST /repos/{repo}/labels failed "
                f"({exc.response.status_code}): {exc.response.text[:400]}"
            ) from exc
        _cache_invalidate()
        logger.info("✅ Label %r ensured on %s", name, repo)
        return

    raise RuntimeError(
        f"GitHub API POST /repos/{repo}/labels failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


async def remove_label_from_issue(issue_number: int, label: str) -> None:
    """Remove *label* from an issue.

    Idempotent: if the label is not present on the issue the GitHub API
    returns 404, which is treated as a no-op rather than a hard failure.

    Parameters
    ----------
    issue_number:
        GitHub issue number to modify.
    label:
        Label name to remove (e.g. ``"blocked"``).

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response other than 404.
    """
    repo = settings.gh_repo
    encoded = urllib.parse.quote(label, safe="")
    # We cannot use _api_delete directly because we need to treat 404 as a
    # no-op rather than an error.  Handle 429 retries manually here.
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"{_BASE_URL}/repos/{repo}/issues/{issue_number}/labels/{encoded}",
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        if r.status_code == 429 and attempt < _MAX_RETRIES:
            await _rate_limit_sleep(r, attempt)
            continue
        if r.status_code == 404:
            logger.debug(
                "⚠️ remove_label_from_issue: label %r not on issue #%d (no-op)",
                label,
                issue_number,
            )
            return
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GitHub API DELETE label failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc
        _cache_invalidate()
        logger.info("✅ Removed %r from issue #%d", label, issue_number)
        return

    raise RuntimeError(
        f"GitHub API DELETE label failed after {_MAX_RETRIES} retries (429 rate limit)"
    )


async def clear_wip_label(issue_number: int) -> None:
    """Remove the ``agent/wip`` label from an issue.

    Called by the control plane after an agent completes its task so the
    issue no longer shows up in ``get_wip_issues()``.

    Parameters
    ----------
    issue_number:
        GitHub issue number to remove ``agent/wip`` from.
    """
    await remove_label_from_issue(issue_number, "agent/wip")
    logger.info("✅ Removed agent/wip from issue #%d", issue_number)


async def add_comment_to_issue(issue_number: int, body: str) -> str:
    """Post a Markdown comment on a GitHub issue and return the comment URL.

    Parameters
    ----------
    issue_number:
        GitHub issue number to comment on.
    body:
        Markdown text for the comment body.

    Returns
    -------
    str
        The URL of the newly created comment
        (e.g. ``"https://github.com/org/repo/issues/42#issuecomment-123456"``).

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response.
    """
    repo = settings.gh_repo
    result = await _api_post(
        f"repos/{repo}/issues/{issue_number}/comments",
        {"body": body},
    )
    comment_url = str(result.get("html_url", ""))
    logger.info("✅ Added comment to issue #%d: %s", issue_number, comment_url)
    return comment_url


async def approve_pr(pr_number: int) -> None:
    """Submit an approving review on a pull request.

    Parameters
    ----------
    pr_number:
        GitHub PR number to approve.

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response (e.g. cannot review your own PR,
        draft PR, or insufficient permissions).
    """
    repo = settings.gh_repo
    await _api_post(
        f"repos/{repo}/pulls/{pr_number}/reviews",
        {"event": "APPROVE", "body": ""},
    )
    logger.info("✅ Approved PR #%d", pr_number)


async def merge_pr(pr_number: int, delete_branch: bool = True) -> None:
    """Squash-merge a pull request and optionally delete the head branch.

    Parameters
    ----------
    pr_number:
        GitHub PR number to merge.
    delete_branch:
        When ``True`` (default), deletes the head branch after a successful
        merge.

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response (e.g. merge conflicts,
        branch-protection rules, or missing approvals).
    """
    repo = settings.gh_repo

    # Capture head branch name before the merge (needed for deletion).
    head_ref: str | None = None
    if delete_branch:
        pr_data = await _api_get(
            f"repos/{repo}/pulls/{pr_number}",
            {},
            f"_pre_merge_pr:{pr_number}",
        )
        if isinstance(pr_data, dict):
            head: object = pr_data.get("head")
            if isinstance(head, dict):
                ref: object = head.get("ref")
                head_ref = str(ref) if isinstance(ref, str) else None

    await _api_put(
        f"repos/{repo}/pulls/{pr_number}/merge",
        {"merge_method": "squash"},
    )
    logger.info("✅ Merged PR #%d (delete_branch=%s)", pr_number, delete_branch)

    if delete_branch and head_ref:
        encoded = urllib.parse.quote(head_ref, safe="")
        try:
            await _api_delete(f"repos/{repo}/git/refs/heads/{encoded}")
            logger.info("✅ Deleted branch %r after merging PR #%d", head_ref, pr_number)
        except RuntimeError as exc:
            logger.warning(
                "⚠️ merge_pr: branch deletion for %r failed (non-fatal) — %s",
                head_ref,
                exc,
            )


async def ensure_pull_request(
    head: str,
    base: str,
    title: str,
    body: str,
) -> tuple[int, bool]:
    """Create a pull request only if one does not already exist for the head branch.

    Idempotent: checks for an existing open PR with the same head branch before
    creating. If found, returns the existing PR number with ``created=False``.
    If not found, creates a new PR and returns its number with ``created=True``.

    Parameters
    ----------
    head:
        Head branch name (e.g. ``"feat/issue-123"``).
    base:
        Base branch to merge into (e.g. ``"dev"``).
    title:
        PR title.
    body:
        PR body (Markdown).

    Returns
    -------
    tuple[int, bool]
        ``(pr_number, created)`` where ``created`` is ``True`` if a new PR was
        created, ``False`` if an existing PR was found.

    Raises
    ------
    RuntimeError
        If the GitHub API call fails.

    Notes
    -----
    This function only checks for open PRs. Closed PRs with the same head branch
    are ignored, and a new PR will be created.
    """
    repo = settings.gh_repo
    owner = repo.split("/")[0]

    # Check for existing open PR with the same head branch via _api_get helper
    # (which handles 429 retries and caching).
    existing = await _api_get(
        f"repos/{repo}/pulls",
        {"state": "open", "head": f"{owner}:{head}"},
        f"ensure_pull_request:check:{head}",
    )
    if isinstance(existing, list) and existing:
        first = existing[0]
        pr_number = int(first["number"]) if isinstance(first, dict) else 0
        logger.info("✅ Found existing PR #%d for branch %r — skipping creation", pr_number, head)
        return (pr_number, False)

    # No existing PR — create one via _api_post (handles 429 retries).
    payload: dict[str, object] = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }
    pr_data = await _api_post(f"repos/{repo}/pulls", payload)
    _pr_num = pr_data.get("number")
    pr_number = _pr_num if isinstance(_pr_num, int) else 0
    logger.info("✅ Created PR #%d: %s → %s", pr_number, head, base)
    return (pr_number, True)


async def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> dict[str, object]:
    """Create a new GitHub issue and return the API response dict.

    Parameters
    ----------
    title:
        Issue title.
    body:
        Issue body (Markdown).
    labels:
        Optional list of label names to apply immediately.
    assignees:
        Optional list of GitHub login names to assign.

    Returns
    -------
    dict[str, object]
        The GitHub API response for the created issue, including ``number``,
        ``html_url``, ``state``, ``title``, and ``body``.

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response.
    """
    repo = settings.gh_repo
    payload: dict[str, object] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    result = await _api_post(f"repos/{repo}/issues", payload)
    logger.info("✅ Created issue #%s: %s", result.get("number"), title)
    return result


async def update_issue(
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> dict[str, object]:
    """Update fields on an existing GitHub issue.

    Only the keyword arguments that are not ``None`` are sent to the API,
    so callers can update a single field without touching the others.

    Parameters
    ----------
    issue_number:
        GitHub issue number to update.
    title:
        New title, or ``None`` to leave unchanged.
    body:
        New body (Markdown), or ``None`` to leave unchanged.
    state:
        ``"open"`` or ``"closed"``, or ``None`` to leave unchanged.
    labels:
        Replacement label list, or ``None`` to leave unchanged.
    assignees:
        Replacement assignee list, or ``None`` to leave unchanged.

    Returns
    -------
    dict[str, object]
        The GitHub API response for the updated issue.

    Raises
    ------
    RuntimeError
        On any non-2xx GitHub API response.
    """
    repo = settings.gh_repo
    payload: dict[str, object] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state
    if labels is not None:
        payload["labels"] = labels
    if assignees is not None:
        payload["assignees"] = assignees
    result = await _api_patch(f"repos/{repo}/issues/{issue_number}", payload)
    logger.info("✅ Updated issue #%d", issue_number)
    return result

