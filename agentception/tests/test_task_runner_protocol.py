from __future__ import annotations

from pathlib import Path

import pytest

from agentception.services import TaskRunner


class MinimalTaskRunner:
    """Minimal implementation for structural subtyping test."""

    def run(
        self,
        prompt: str,
        worktree_path: Path,
        mcp_server: str,
        role: str,
        run_id: str,
    ) -> str | None:
        """Matches TaskRunner.run signature."""
        return None


def test_task_runner_protocol_structural_subtyping() -> None:
    """Verify that a class with matching signature satisfies the protocol."""
    instance = MinimalTaskRunner()
    assert isinstance(instance, TaskRunner)
