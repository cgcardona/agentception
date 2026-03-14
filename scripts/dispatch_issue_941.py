#!/usr/bin/env python3
"""One-off: dispatch a developer for GitHub issue #941 via POST /api/dispatch/issue.

Run from repo root. Requires AgentCeption running (e.g. docker compose up -d).
Fetches issue title/body from GitHub API, then POSTs to localhost:10003.
"""
from __future__ import annotations

import json
import os
import urllib.request

GITHUB_ISSUE_URL = "https://api.github.com/repos/cgcardona/agentception/issues/941"
DISPATCH_URL = os.environ.get("AC_DISPATCH_URL", "http://localhost:10003/api/dispatch/issue")


def main() -> None:
    # Fetch issue from GitHub (no auth for public repo)
    req = urllib.request.Request(
        GITHUB_ISSUE_URL,
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    title = data["title"]
    body = data["body"] or ""

    payload = {
        "issue_number": 941,
        "issue_title": title,
        "issue_body": body,
        "role": "developer",
        "repo": "cgcardona/agentception",
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    dispatch_req = urllib.request.Request(
        DISPATCH_URL,
        data=body_bytes,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(dispatch_req, timeout=60) as resp:
        out = json.loads(resp.read().decode())
    print(json.dumps(out, indent=2))
    print("run_id:", out.get("run_id"), "— agent loop fired.")


if __name__ == "__main__":
    main()
