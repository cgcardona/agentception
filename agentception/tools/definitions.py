"""OpenAI-format tool definitions for the local file, shell, and search tools.

These are imported by ``agent_loop.py`` and merged with the MCP tool
catalogue before being sent to the model on every iteration.

Schemas follow JSON Schema draft-07 (the subset OpenAI / Anthropic accept).
"""

from __future__ import annotations

from agentception.services.llm import ToolDefinition, ToolFunction

FILE_TOOL_DEFS: list[ToolDefinition] = [
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="read_file",
            description=(
                "Read the text content of a file. "
                "Relative paths are resolved from the worktree root. "
                "Returns the file content (truncated at 128 KiB if very large)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read (relative or absolute).",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="write_file",
            description=(
                "Write text content to a file, creating parent directories as needed. "
                "Overwrites existing content. "
                "Relative paths are resolved from the worktree root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Destination path (relative or absolute).",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write (UTF-8).",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="list_directory",
            description=(
                "List entries in a directory. "
                "Directories are suffixed with '/'. "
                "Relative paths are resolved from the worktree root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to list (default '.' = worktree root).",
                        "default": ".",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="search_text",
            description=(
                "Search for a regex or literal pattern in files using ripgrep. "
                "Returns matching lines with file names and line numbers. "
                "Respects .gitignore. "
                "Relative paths are resolved from the worktree root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex or literal pattern to search for.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search (default '.' = worktree root).",
                        "default": ".",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Max matching lines to return (default 30).",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 200,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        ),
    ),
]

SHELL_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="run_command",
        description=(
            "Run a shell command and return stdout, stderr, and exit code. "
            "ENVIRONMENT: you are inside the AgentCeption Docker container, "
            "so run Python tools directly (python3, pytest, mypy) without "
            "'docker compose exec agentception'. "
            "The default working directory is the worktree root. "
            "Dangerous operations (rm -rf /, sudo, shutdown, …) are blocked."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (passed to /bin/sh -c).",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory override. "
                        "Defaults to the worktree root when omitted."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    ),
)

SEARCH_CODEBASE_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="search_codebase",
        description=(
            "Semantically search the codebase using natural language. "
            "More powerful than pattern matching — use it to find code by concept: "
            "'where is authentication handled?', 'find the GitHub API client', "
            "'show me the error handling for LLM calls'. "
            "Requires the codebase to have been indexed via POST /api/system/index-codebase. "
            "Returns the most relevant code chunks with their file paths and line numbers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of what you are looking for.",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 20).",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
)
