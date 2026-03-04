from __future__ import annotations
"""Tests for resolve_arch.py cognitive architecture resolution.

Verifies that the resolver produces non-empty, well-formed output for a variety
of figure + skill-domain combinations without needing any external services.
"""
import subprocess
import sys
from pathlib import Path

import pytest

RESOLVE_ARCH = Path(__file__).parent.parent / "scripts" / "gen_prompts" / "resolve_arch.py"


@pytest.mark.parametrize(
    "arch_string",
    [
        "feynman:python",
        "ritchie:devops",
        "dijkstra:postgresql:python",
        "knuth:python",
        "hopper",
        "the_architect:python",
        "the_pragmatist:devops",
    ],
)
def test_resolve_arch_produces_non_empty_output(arch_string: str) -> None:
    """resolve_arch.py must exit 0 and produce non-empty Markdown for known arch strings."""
    result = subprocess.run(
        [sys.executable, str(RESOLVE_ARCH), arch_string],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"resolve_arch.py exited {result.returncode} for '{arch_string}':\n{result.stderr}"
    )
    assert len(result.stdout.strip()) > 0, (
        f"resolve_arch.py produced empty output for '{arch_string}'"
    )


def test_resolve_arch_feynman_python_contains_figure_name() -> None:
    """Output for feynman:python should mention Feynman by name."""
    result = subprocess.run(
        [sys.executable, str(RESOLVE_ARCH), "feynman:python"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Feynman" in result.stdout


def test_resolve_arch_fingerprint_flag_produces_fingerprint_table() -> None:
    """--fingerprint flag should include the fingerprint table in output."""
    result = subprocess.run(
        [sys.executable, str(RESOLVE_ARCH), "feynman:python",
         "--fingerprint", "--role", "python-developer",
         "--session", "test-session-1", "--batch", "test-batch"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "python-developer" in result.stdout
    assert "test-session-1" in result.stdout


def test_resolve_arch_unknown_figure_exits_nonzero() -> None:
    """An unrecognised figure name should cause resolve_arch.py to exit non-zero."""
    result = subprocess.run(
        [sys.executable, str(RESOLVE_ARCH), "totally_unknown_figure_xyz"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "resolve_arch.py should fail for an unknown figure name"
    )
