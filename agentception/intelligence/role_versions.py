from __future__ import annotations

"""Role version tracking for AgentCeption (AC-503).

Maintains ``.agentception/role-versions.json`` — a persistent ledger that records
which SHA of each managed role file was active during each agent batch/wave.
This enables retrospective A/B testing and outcome correlation: given a
batch ID, callers can retrieve the exact role file content that governed the
agents in that batch.

The JSON schema is:

    {
      "versions": {
        "<slug>": {
            "current": "v1",
            "history": [
                {"sha": "<git sha>", "label": "v1", "timestamp": 1234567890}
            ]
        }
      },
      "ab_mode": {
          "enabled": false,
          "target_role": null,
          "variant_a_sha": null,
          "variant_b_sha": null
      }
    }

Every call to ``record_version_bump`` appends a new history entry and
increments the version label (v1, v2, …). The ``batch_index`` embedded in
``get_version_for_batch`` matches the batch-ID timestamp prefix against the
history to determine which version was active when the batch ran.
"""

import asyncio
import logging
import time
from pathlib import Path

from agentception.config import settings

logger = logging.getLogger(__name__)

_ROLE_VERSIONS_REL = ".agentception/role-versions.json"


def _versions_path() -> Path:
    """Return the absolute path to role-versions.json in the configured repo."""
    return settings.repo_dir / _ROLE_VERSIONS_REL


async def read_role_versions() -> dict[str, object]:
    """Read and parse role-versions.json, returning an empty scaffold if absent.

    The returned dict always contains ``versions`` (dict) and ``ab_mode``
    (dict) keys — callers may rely on this contract without defensive checks.
    """
    path = _versions_path()
    if not path.exists():
        logger.warning("⚠️  role-versions.json not found at %s — returning empty scaffold", path)
        return _empty_scaffold()

    import json

    try:
        text = await asyncio.get_event_loop().run_in_executor(
            None, lambda: path.read_text(encoding="utf-8")
        )
        data: dict[str, object] = json.loads(text)
        # Ensure required top-level keys exist even if the file was hand-edited.
        if "versions" not in data:
            data["versions"] = {}
        if "ab_mode" not in data:
            data["ab_mode"] = _default_ab_mode()
        return data
    except Exception as exc:
        logger.error("❌ Failed to parse role-versions.json: %s", exc)
        return _empty_scaffold()


async def write_role_versions(data: dict[str, object]) -> None:
    """Atomically write ``data`` to role-versions.json with 2-space indentation.

    Creates the parent ``.cursor/`` directory if it does not yet exist so the
    first write (bootstrap scenario) succeeds without any pre-setup.
    """
    import json

    path = _versions_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    serialised = json.dumps(data, indent=2)
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: path.write_text(serialised + "\n", encoding="utf-8")
    )
    logger.info("✅ role-versions.json written (%d bytes)", len(serialised))


async def record_version_bump(slug: str, new_sha: str) -> None:
    """Record that role ``slug`` was updated to commit ``new_sha``.

    Appends a new entry to the history for ``slug`` and increments its version
    label.  Idempotent for the same SHA — if ``new_sha`` already matches the
    most recent history entry the call is a no-op (prevents duplicate entries
    when the commit endpoint is retried).

    This function is called by the ``POST /api/roles/{slug}/commit`` handler
    immediately after a successful ``git commit`` so that every committed role
    change is permanently linked to a version label and timestamp.
    """
    data = await read_role_versions()
    versions_raw: object = data.get("versions", {})
    versions: dict[str, object] = versions_raw if isinstance(versions_raw, dict) else {}

    slug_entry_raw: object = versions.get(slug, {})
    slug_entry: dict[str, object] = slug_entry_raw if isinstance(slug_entry_raw, dict) else {}

    history_raw: object = slug_entry.get("history", [])
    history: list[object] = history_raw if isinstance(history_raw, list) else []

    # Idempotency guard: skip if the SHA is already the latest.
    if history and isinstance(history[-1], dict) and history[-1].get("sha") == new_sha:
        logger.info("ℹ️  role-versions: SHA %s already recorded for %s — skipping", new_sha[:8], slug)
        return

    next_version = f"v{len(history) + 1}"
    history.append({"sha": new_sha, "label": next_version, "timestamp": int(time.time())})
    slug_entry["current"] = next_version
    slug_entry["history"] = history
    versions[slug] = slug_entry
    data["versions"] = versions

    await write_role_versions(data)
    logger.info("✅ role-versions: recorded %s → %s (%s)", slug, next_version, new_sha[:8])


async def get_version_for_batch(slug: str, batch_id: str) -> str | None:
    """Return the version label active for ``slug`` when ``batch_id`` ran.

    ``batch_id`` is expected to contain a UTC timestamp in the format
    ``eng-YYYYMMDDTHHMMSSz-<hex>`` (e.g. ``eng-20260301T120000Z-1a2b``).
    The timestamp is parsed and compared against the ``timestamp`` field of
    each history entry.  The version whose commit timestamp most closely
    precedes the batch start time is considered the "active" version.

    Returns ``None`` when:
    - The slug has no history.
    - The ``batch_id`` timestamp cannot be parsed.
    - No history entry precedes the batch timestamp (the role did not exist yet).

    Callers should treat ``None`` as "version unknown at that time."
    """
    data = await read_role_versions()
    versions_raw2: object = data.get("versions", {})
    if not isinstance(versions_raw2, dict):
        return None
    versions2: dict[str, object] = versions_raw2

    slug_entry = versions2.get(slug)
    if not isinstance(slug_entry, dict):
        return None

    history2_raw: object = slug_entry.get("history", [])
    if not isinstance(history2_raw, list) or not history2_raw:
        return None
    history2: list[object] = history2_raw

    batch_ts = _parse_batch_timestamp(batch_id)
    if batch_ts is None:
        logger.warning("⚠️  Cannot parse batch_id timestamp: %s", batch_id)
        return None

    # Walk history forward (oldest first): keep updating active_version each time an entry
    # precedes batch_ts, then break on the first entry that post-dates the batch.
    active_version: str | None = None
    for entry in history2:
        if not isinstance(entry, dict):
            continue
        entry_ts = entry.get("timestamp")
        if not isinstance(entry_ts, (int, float)):
            continue
        if entry_ts <= batch_ts:
            active_version = str(entry.get("label", ""))
        else:
            break  # history is ordered chronologically — no earlier entries follow

    return active_version if active_version else None


# ── Helpers ────────────────────────────────────────────────────────────────────


def _empty_scaffold() -> dict[str, object]:
    """Return the baseline empty role-versions structure."""
    return {"versions": {}, "ab_mode": _default_ab_mode()}


def _default_ab_mode() -> dict[str, object]:
    """Return the default (disabled) A/B mode configuration."""
    return {
        "enabled": False,
        "target_role": None,
        "variant_a_sha": None,
        "variant_b_sha": None,
    }


def _parse_batch_timestamp(batch_id: str) -> int | None:
    """Parse the UTC timestamp from a batch ID string.

    Handles the canonical format ``eng-YYYYMMDDTHHMMSSz-<hex>`` and also
    accepts bare ISO-8601 timestamps for testing convenience.  Returns seconds
    since epoch, or ``None`` if parsing fails.
    """
    import re
    from datetime import datetime, timezone

    # Pattern: eng-20260301T120000Z-1a2b
    match = re.search(r"(\d{8}T\d{6}Z)", batch_id)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            return int(dt.timestamp())
        except ValueError:
            pass

    # Fallback: try parsing the whole string as an integer (unix ts for tests).
    try:
        return int(batch_id)
    except ValueError:
        return None
