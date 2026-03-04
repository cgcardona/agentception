"""Readers sub-package for AgentCeption.

Each reader is responsible for collecting raw data from a specific source
(filesystem worktrees, Cursor transcript files, GitHub API). The poller
orchestrates them into a unified ``PipelineState``.
"""
from __future__ import annotations
