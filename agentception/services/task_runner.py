from __future__ import annotations

"""Everything above TaskRunner.run is runner-agnostic.
Nothing below the protocol boundary may import agentception.services.task_runner
without also implementing the protocol."""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskRunner(Protocol):
    """Protocol for task execution engines.
    
    Implementations spawn agent processes with the given prompt and context.
    The protocol decouples coordinators from specific runner implementations
    (Anthropic, local models, etc.).
    """

    def run(
        self,
        prompt: str,
        worktree_path: Path,
        mcp_server: str,
        role: str,
        run_id: str,
    ) -> str | None:
        """Execute a task with the given prompt and context.
        
        Args:
            prompt: Task description and instructions for the agent
            worktree_path: Path to the git worktree for this task
            mcp_server: MCP server endpoint for tool access
            role: Agent role (e.g., 'developer', 'reviewer')
            run_id: Unique identifier for this run
            
        Returns:
            Run result or None if execution failed
        """
        ...
