# Services Reference

## `agentception/readers/github.py`

The GitHub REST API client for AgentCeption infrastructure. All GitHub data
flows through this module via authenticated `httpx` calls to
`https://api.github.com`.

### Design principles

- **Single entry point.** Every GitHub API call goes through one of the
  low-level `_api_*` helpers (`_api_get`, `_api_get_all`, `_api_post`,
  `_api_patch`, `_api_put`, `_api_delete`). No other module opens an
  `httpx.AsyncClient` for GitHub calls.

- **TTL caching.** Read results are cached for `settings.github_cache_seconds`
  (default 10 s). Write operations always call `_cache_invalidate()` so the
  next read reflects the new state.

- **429 retry/backoff.** Every `_api_*` helper retries up to `_MAX_RETRIES`
  times on HTTP 429. The wait duration is read from the `Retry-After` response
  header when present; otherwise exponential backoff starting at
  `_RATE_LIMIT_BACKOFF_SECS` is used.

### Low-level HTTP helpers

| Helper | Method | Notes |
|---|---|---|
| `_api_get(path, params, cache_key)` | GET | Returns `JsonValue`; result is cached. |
| `_api_get_all(path, params, cache_key, limit)` | GET (paginated) | Fetches up to `limit` items across pages. |
| `_api_post(path, payload)` | POST | Invalidates cache on success. |
| `_api_patch(path, payload)` | PATCH | Invalidates cache on success. |
| `_api_put(path, payload)` | PUT | Invalidates cache on success. |
| `_api_delete(path)` | DELETE | Invalidates cache on success. |

### Public read API

| Function | Description |
|---|---|
| `get_open_issues(label)` | List open issues, optionally filtered by label. |
| `get_closed_issues(limit)` | List recently closed issues. |
| `get_wip_issues()` | Issues labelled `agent/wip`. |
| `get_active_label()` | Current pipeline phase label (pin or auto-advance). |
| `get_issue(number)` | Fetch a single issue by number. |
| `get_issue_body(number)` | Return the body text of a single issue. |
| `get_open_prs()` | Open PRs targeting `dev`. |
| `get_open_prs_any_base()` | All open PRs regardless of target branch. |
| `get_merged_prs()` | Merged PRs targeting `dev`. |
| `get_merged_prs_full(limit)` | Merged PRs with full metadata. |
| `get_pr_comments(pr_number)` | Comment bodies for a PR. |
| `get_issue_comments(issue_number)` | Comments on an issue. |
| `get_pr_checks(pr_number)` | CI check statuses for a PR. |
| `get_pr_reviews(pr_number)` | Review decisions for a PR. |
| `get_repo_labels(limit)` | All labels defined in the repository. |
| `get_issues_with_all_labels(labels, state, limit)` | Issues carrying every label in the list (AND semantics). |

### Public write API

| Function | Description |
|---|---|
| `add_label_to_issue(issue_number, label)` | Add a label to an issue. |
| `add_wip_label(issue_number)` | Add `agent/wip` to an issue. |
| `clear_wip_label(issue_number)` | Remove `agent/wip` from an issue. |
| `remove_label_from_issue(issue_number, label)` | Remove a label (404 treated as no-op). |
| `ensure_label_exists(name, color, description)` | Create or update a repository label. |
| `close_pr(pr_number, comment)` | Post a comment and close a PR. |
| `approve_pr(pr_number)` | Submit an APPROVE review on a PR. |
| `merge_pr(pr_number, delete_branch)` | Squash-merge a PR, optionally deleting the head branch. |
| `create_issue(title, body, labels, assignees)` | Create a new issue. |
| `update_issue(issue_number, ...)` | Update fields on an existing issue. |
| `ensure_pull_request(head, base, title, body)` | Create a PR or return the existing one (idempotent). |

### Configuration

| Setting | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | Personal access token or GitHub App token. Required. |
| `GH_REPO` | — | Repository in `owner/repo` format. Required. |
| `GITHUB_CACHE_SECONDS` | `10` | TTL for cached read results. |

### Error handling

All helpers raise `RuntimeError` on non-2xx responses (except where noted —
`remove_label_from_issue` treats 404 as a no-op, and `ensure_label_exists`
treats 422 as "already exists" and updates in place). The error message always
includes the HTTP method, path, status code, and the first 400 characters of
the response body to aid debugging.

When `GITHUB_TOKEN` is not set, `_headers()` raises `RuntimeError` immediately
with a clear message rather than letting the request fail with a 401.
