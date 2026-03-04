"""In-memory active-label pin for the AgentCeption poller.

By default the active label is determined automatically by scanning open
GitHub issues against the ordered ``active_labels_order`` list in
``pipeline-config.json`` (earliest phase with open work wins).

Operators can override this by calling :func:`set_pin` with any label string.
The override is cleared by :func:`clear_pin` or on process restart — it is
intentionally not persisted so a restart always returns to automatic mode.
"""
from __future__ import annotations

_pin: str | None = None


def get_pin() -> str | None:
    """Return the currently pinned label, or ``None`` when in auto mode."""
    return _pin


def set_pin(label: str) -> None:
    """Pin the active label to *label*, overriding automatic selection."""
    global _pin
    _pin = label


def clear_pin() -> None:
    """Clear the manual pin and return to automatic label selection."""
    global _pin
    _pin = None
