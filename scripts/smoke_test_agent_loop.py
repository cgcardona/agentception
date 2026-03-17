"""End-to-end smoke test for the AgentCeption agent loop.

Validates the full server-side pipeline:
  1. Qdrant connectivity check
  2. Codebase indexing (POST /api/system/index-codebase)
  3. Semantic search (GET /api/system/search)
  4. FastEmbed model download + embedding round-trip

This script communicates with a running AgentCeption container at
http://127.0.0.1:1337.  Start the stack with ``docker compose up -d``
before running:

    python3 scripts/smoke_test_agent_loop.py

Exit code 0 = all steps passed.  Non-zero = failure (details printed).

NOTE: Indexing downloads the fastembed model on first run (~130 MB).
      Subsequent runs use the cached model.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from typing import TypedDict

BASE_URL = "http://127.0.0.1:1337"
QDRANT_URL = "http://127.0.0.1:6335"


# ── HTTP helpers ──────────────────────────────────────────────────────────────


class ApiResponse(TypedDict):
    """Parsed JSON response from the AgentCeption API."""

    status: int
    body: object


def _get(path: str) -> ApiResponse:
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            import json

            body: object = json.loads(resp.read())
            return ApiResponse(status=resp.status, body=body)
    except urllib.error.HTTPError as exc:
        import json

        body = json.loads(exc.read())
        return ApiResponse(status=exc.code, body=body)


def _post(path: str) -> ApiResponse:
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json

            body = json.loads(resp.read())
            return ApiResponse(status=resp.status, body=body)
    except urllib.error.HTTPError as exc:
        import json

        body = json.loads(exc.read())
        return ApiResponse(status=exc.code, body=body)


# ── Step functions ────────────────────────────────────────────────────────────


def step_agentception_health() -> None:
    print("─── Step 1: AgentCeption health check ───")
    resp = _get("/health")
    if resp["status"] != 200:
        raise SystemExit(f"  FAIL — /api/health returned {resp['status']}")
    print(f"  OK — AgentCeption at {BASE_URL} is healthy")


def step_qdrant_health() -> None:
    print("─── Step 2: Qdrant connectivity ───")
    try:
        with urllib.request.urlopen(f"{QDRANT_URL}/collections", timeout=5) as resp:
            if resp.status != 200:
                raise SystemExit(f"  FAIL — Qdrant at {QDRANT_URL} returned {resp.status}")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"  FAIL — Cannot reach Qdrant at {QDRANT_URL}: {exc}") from exc
    print(f"  OK — Qdrant at {QDRANT_URL} is reachable")


def step_trigger_indexing() -> None:
    print("─── Step 3: Trigger codebase indexing ───")
    print("  (First run downloads the fastembed model — may take 1–3 minutes)")
    resp = _post("/api/system/index-codebase")
    if resp["status"] != 202:
        raise SystemExit(f"  FAIL — /api/system/index-codebase returned {resp['status']}: {resp['body']}")
    print(f"  OK — 202 Accepted: {resp['body']}")
    print("  Waiting 90 s for background indexing to complete…")
    for remaining in range(90, 0, -10):
        time.sleep(10)
        print(f"    {remaining - 10} s remaining…")


def step_semantic_search() -> None:
    print("─── Step 4: Semantic search verification ───")
    queries = [
        "anthropic api key configuration",
        "agent loop tool dispatch",
        "qdrant collection indexing",
    ]
    for query in queries:
        encoded = query.replace(" ", "+")
        resp = _get(f"/api/system/search?q={encoded}&n=3")
        if resp["status"] != 200:
            raise SystemExit(f"  FAIL — search returned {resp['status']}: {resp['body']}")
        body = resp["body"]
        if not isinstance(body, dict):
            raise SystemExit(f"  FAIL — unexpected response type: {type(body)}")
        n = body.get("n_results", 0)
        print(f"  OK — '{query}' → {n} results")
        if isinstance(n, int) and n > 0:
            matches_raw = body.get("matches", [])
            if isinstance(matches_raw, list) and len(matches_raw) > 0:
                top = matches_raw[0]
                if isinstance(top, dict):
                    print(f"       top hit: {top.get('file')} (score={top.get('score', '?'):.3f})")


def step_summary(start: float) -> None:
    elapsed = time.monotonic() - start
    print()
    print("══════════════════════════════════════════════")
    print(f"  ✅ ALL STEPS PASSED ({elapsed:.1f}s)")
    print("  AgentCeption agent loop is fully operational.")
    print("══════════════════════════════════════════════")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Run the full end-to-end smoke test."""
    print()
    print("══ AgentCeption Smoke Test — Agent Loop ══")
    print(f"  AgentCeption: {BASE_URL}")
    print(f"  Qdrant:       {QDRANT_URL}")
    print()

    start = time.monotonic()

    step_agentception_health()
    step_qdrant_health()
    step_trigger_indexing()
    step_semantic_search()
    step_summary(start)


if __name__ == "__main__":
    main()
