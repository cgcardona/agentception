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
            name="read_file_lines",
            description=(
                "Read a specific line range from a file (1-indexed, inclusive). "
                "PREFER this over read_file when you only need a section of a large file — "
                "it returns only the requested lines, keeping the context window small. "
                "Use search_text first to locate the relevant line numbers, then call "
                "read_file_lines to fetch just that region. "
                "Bounds are clamped to the actual file length; total_lines is always returned "
                "so you can plan follow-up reads. "
                "Relative paths are resolved from the worktree root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read (relative or absolute).",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to return (1-indexed).",
                        "minimum": 1,
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to return (1-indexed, inclusive).",
                        "minimum": 1,
                    },
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        ),
    ),
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
            name="replace_in_file",
            description=(
                "Replace an exact string in a file with new text. "
                "PREFER this over write_file for targeted edits — only the matched region changes, "
                "the rest of the file is untouched. "
                "old_string must match exactly (including whitespace and newlines). "
                "Fails if old_string is not found, or if it matches more than once and "
                "allow_multiple is not set — use a longer anchor to make it unique. "
                "Relative paths are resolved from the worktree root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit (relative or absolute).",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "allow_multiple": {
                        "type": "boolean",
                        "description": (
                            "When true, replace every occurrence of old_string. "
                            "Default false — fails if old_string matches more than once."
                        ),
                        "default": False,
                    },
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=ToolFunction(
            name="insert_after_in_file",
            description=(
                "Insert new content immediately after an anchor string in a file. "
                "Use this when you want to ADD new lines after a specific point without "
                "changing the anchor itself — for example, adding a new function after an "
                "import block, or inserting a new route after an existing one. "
                "The anchor must appear exactly once; use a longer anchor if it matches "
                "multiple times. "
                "Relative paths are resolved from the worktree root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit (relative or absolute).",
                    },
                    "anchor": {
                        "type": "string",
                        "description": (
                            "Exact text that marks the insertion point. "
                            "New content is inserted immediately after this text. "
                            "Must appear exactly once in the file."
                        ),
                    },
                    "new_content": {
                        "type": "string",
                        "description": "Text to insert after the anchor.",
                    },
                },
                "required": ["path", "anchor", "new_content"],
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
                "Overwrites the entire file — use replace_in_file for targeted edits. "
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

GIT_COMMIT_AND_PUSH_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="git_commit_and_push",
        description=(
            "Create a git branch, stage files, commit, and push to origin in one call. "
            "USE THIS instead of four separate run_command calls for the standard "
            "end-of-task git workflow. "
            "If the worktree is already on the target branch, the checkout step is skipped. "
            "After this call succeeds, open a pull request against dev using the GitHub MCP "
            "create_pull_request tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": (
                        "Feature branch name to create, e.g. 'fix/issue-42' or 'feat/my-feature'."
                    ),
                },
                "commit_message": {
                    "type": "string",
                    "description": "Commit message string.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of file paths to stage (passed to git add). "
                        "Use ['.'] to stage all changes in the worktree."
                    ),
                    "minItems": 1,
                },
                "base": {
                    "type": "string",
                    "description": "Ref to branch from. Defaults to 'origin/dev'.",
                    "default": "origin/dev",
                },
            },
            "required": ["branch", "commit_message", "paths"],
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
