from __future__ import annotations

"""A/B mode role-file selector for the Engineering VP (AC-504).

When A/B mode is enabled in ``pipeline-config.json``, successive batches
alternate between two role file variants based on whether the BATCH_ID
timestamp second is even (variant A) or odd (variant B).  This provides
a controlled experiment channel: everything about a batch stays constant
except which role prompt governs the agents, so outcomes can later be
correlated against the version that was active.

The BATCH_ID format is ``eng-YYYYMMDDTHHMMSSz-<hex>`` (e.g.
``eng-20260302T054843Z-54b3``).  The seconds component (``SS``) is the
digit pair that drives the selection:

    SS % 2 == 0  →  variant A (even)
    SS % 2 == 1  →  variant B (odd)

When A/B mode is disabled, or the BATCH_ID cannot be parsed, the
``default_role_file`` is returned unchanged so the caller's normal flow
is unaffected.

Typical call site (Engineering VP SEED)::

    from agentception.intelligence.ab_mode import select_role_file

    role_file = await select_role_file(
        batch_id=BATCH_ID,
        default_role_file=".agentception/roles/developer.md",
    )
"""

import logging
import re

from agentception.readers.pipeline_config import read_pipeline_config

logger = logging.getLogger(__name__)


def _extract_seconds(batch_id: str) -> int | None:
    """Return the seconds component of a BATCH_ID timestamp, or ``None``.

    Parses the canonical ``eng-YYYYMMDDTHHMMSSz-<hex>`` format.  The seconds
    are the two digits immediately before the ``Z`` separator in the time
    portion (``HHMMSS``).
    """
    match = re.search(r"\d{8}T\d{4}(\d{2})Z", batch_id)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _is_even_batch(batch_id: str) -> bool | None:
    """Return True for even-second batches, False for odd, None if unparseable.

    The parity of the seconds component determines which A/B variant to use.
    Returning ``None`` signals "cannot determine" so callers can fall back
    to the default rather than silently picking the wrong variant.
    """
    seconds = _extract_seconds(batch_id)
    if seconds is None:
        return None
    return seconds % 2 == 0


async def select_role_file(batch_id: str, default_role_file: str) -> str:
    """Return the role file path appropriate for this batch.

    Reads ``pipeline-config.json`` to check whether A/B mode is enabled.
    When enabled and the BATCH_ID is parseable, returns either ``variant_a_file``
    (even second) or ``variant_b_file`` (odd second).  Falls back to
    ``default_role_file`` in all other cases:

    - A/B mode is disabled.
    - ``target_role`` does not match this call's context (not checked here —
      the caller is responsible for passing the correct ``default_role_file``
      only when the role matches ``target_role``).
    - Either variant file is ``None`` or empty in the config.
    - ``batch_id`` does not contain a parseable timestamp.

    Parameters
    ----------
    batch_id:
        The BATCH_ID string from ``.agent-task`` (e.g. ``eng-20260302T054843Z-54b3``).
    default_role_file:
        The role file path to use when A/B mode is inactive or cannot be applied.

    Returns
    -------
    str
        Absolute or relative path to the role file that should govern this batch.
    """
    config = await read_pipeline_config()
    ab = config.ab_mode

    if not ab.enabled:
        logger.debug("A/B mode disabled — using default role file: %s", default_role_file)
        return default_role_file

    parity = _is_even_batch(batch_id)
    if parity is None:
        logger.warning(
            "⚠️  A/B mode enabled but BATCH_ID %r is not parseable — falling back to default",
            batch_id,
        )
        return default_role_file

    if parity:
        # Even second → variant A
        variant = ab.variant_a_file
        label = "A (even)"
    else:
        # Odd second → variant B
        variant = ab.variant_b_file
        label = "B (odd)"

    if not variant:
        logger.warning(
            "⚠️  A/B mode enabled but variant_%s_file is not set — falling back to default",
            "a" if parity else "b",
        )
        return default_role_file

    logger.info(
        "✅ A/B mode: batch %s → variant %s → %s",
        batch_id,
        label,
        variant,
    )
    return variant
