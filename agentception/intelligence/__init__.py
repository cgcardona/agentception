from __future__ import annotations

"""Intelligence layer for AgentCeption.

Provides ticket analysis, dependency DAG construction, and role-inference
utilities used by the Eng VP seed loop and the REST API.

Also contains anomaly detection and guard functions for detecting pipeline
problems that require human intervention. Guards reason about the *state* of
the pipeline and produce actionable signals, unlike the readers (data access)
or routes (HTTP handlers).
"""
from __future__ import annotations
