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
                "IMPORTANT: the anchor must be an exact substring of the file. "
                "If you are unsure what text is near the insertion point, use "
                "read_file_lines first to read the surrounding lines, then copy the exact "
                "text as your anchor. "
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
                        "description": "Max total matching lines to return across all files (default 30).",
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
                        "JSON array of file paths to stage (passed to git add). "
                        "MUST be an array, not a string — e.g. [\".\"] not \".\". "
                        "Use [\".\"] to stage all changes in the worktree."
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
            "YOUR FIRST TOOL CALL FOR ANY CODE DISCOVERY. "
            "One semantic search replaces 5–10 sequential grep/cat/read calls. "
            "The index covers every .py, .md, .j2, .yaml, .toml, .json file in the repo. "
            "Call this BEFORE any grep, rg, cat, or read_file_lines when you need to "
            "locate a class, function, pattern, or concept. "
            "Results include the matched code block with its file path and line numbers. "
            "READ THE CHUNK CONTENT DIRECTLY — it already contains the relevant code. "
            "DO NOT follow up with read_file_lines on the same region; the chunk is the code. "
            "Only use read_file_lines if you need lines adjacent to the matched region. "
            "Examples of what to search: 'where is AgentStatus defined', "
            "'how does the poller detect stalled agents', 'pattern for adding a persist helper', "
            "'alembic migration that adds a column'. "
            "Returns the most relevant code chunks ordered by cosine similarity."
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
                "collection": {
                    "type": "string",
                    "description": (
                        "Qdrant collection to search. "
                        "Omit to search the main 'code' collection "
                        "which indexes the full repository. "
                        "Pass 'worktree-<your-run-id>' to search only the files "
                        "in your worktree (available after the background indexing "
                        "completes, usually within 30s of run start)."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
)

READ_SYMBOL_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="read_symbol",
        description=(
            "Return the complete body of a function or class by name — including decorators. "
            "PREFER this over read_file_lines when you know the symbol name: "
            "it uses the Python AST for exact boundaries and returns the whole definition. "
            "No follow-up read needed. "
            "If the symbol is not found, falls back to a heuristic line scan. "
            "Relative paths are resolved from the worktree root."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the source file (relative or absolute).",
                },
                "symbol_name": {
                    "type": "string",
                    "description": "Exact function or class name, e.g. '_truncate_tool_results'.",
                },
            },
            "required": ["path", "symbol_name"],
            "additionalProperties": False,
        },
    ),
)

READ_WINDOW_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="read_window",
        description=(
            "Read a generous window of lines centered on a given line number. "
            "PREFER this over read_file_lines for exploration: plug in the line "
            "number from a search result and receive 80 lines before + 120 after "
            "— enough to capture most complete function definitions without knowing "
            "exact boundaries. "
            "Relative paths are resolved from the worktree root."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative or absolute).",
                },
                "center_line": {
                    "type": "integer",
                    "description": "1-indexed line to center the window on (e.g. from a search result).",
                    "minimum": 1,
                },
                "before": {
                    "type": "integer",
                    "description": "Lines to include before center_line (default 80).",
                    "default": 80,
                    "minimum": 1,
                },
                "after": {
                    "type": "integer",
                    "description": "Lines to include after center_line (default 120).",
                    "default": 120,
                    "minimum": 1,
                },
            },
            "required": ["path", "center_line"],
            "additionalProperties": False,
        },
    ),
)

FIND_CALL_SITES_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="find_call_sites",
        description=(
            "Find all call sites and import locations of a function or class using ripgrep. "
            "Use this AFTER read_symbol to understand usage patterns before editing — "
            "knowing call sites prevents breaking changes. "
            "Returns file paths and matching lines."
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Function or class name to find (e.g. 'persist_agent_run_dispatch').",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Max total matching lines to return across all files (default 30).",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["symbol_name"],
            "additionalProperties": False,
        },
    ),
)

UPDATE_WORKING_MEMORY_TOOL_DEF: ToolDefinition = ToolDefinition(
    type="function",
    function=ToolFunction(
        name="update_working_memory",
        description=(
            "Update your persistent working memory — a structured scratch-pad that "
            "survives history pruning and is injected fresh into every turn. "
            "Call this IMMEDIATELY after any discovery so you never lose a finding. "
            "Call it BEFORE complex reasoning to record your plan and next steps. "
            "Only supply the fields you want to change; others are preserved. "
            "The 'findings' dict is union-merged so you can add individual keys. "
            "Use 'files_examined' to record every file you read so you can skip re-reads. "
            "Memory is rendered in your system context at the start of every turn."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "High-level implementation plan for this session.",
                },
                "files_examined": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Complete list of file paths you have read or examined. "
                        "Replaces the stored list — include all files, not just new ones."
                    ),
                },
                "findings": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Key/value findings to record. Keys are file paths or topic slugs; "
                        "values are short notes. Merged into existing findings — "
                        "you only need to supply new or updated entries."
                    ),
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Architecture or approach decisions locked in this session.",
                },
                "next_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered queue of remaining work items (replaces stored list).",
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Anything blocking progress. Clear when resolved.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
)
