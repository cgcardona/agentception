"""Local tool implementations for the Cursor-free agent loop.

Provides file-system and shell-execution primitives that the agent loop
dispatches when the model requests a ``read_file``, ``write_file``,
``list_directory``, ``search_text``, or ``run_command`` tool call.

All functions return a plain ``dict[str, object]`` so they can be
JSON-serialised directly into the tool-result message fed back to the model.
"""

from __future__ import annotations
