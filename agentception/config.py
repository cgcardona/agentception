"""AgentCeption service configuration.

Settings map directly to unprefixed environment variables (e.g. ``GH_REPO``,
``REPO_DIR``, ``DATABASE_URL``).  Defaults work for local development without
any env vars set.

When ``pipeline-config.json`` contains a ``projects`` list and an
``active_project`` name, the model validator applies the matching project's
``gh_repo`` over the env-var default.  ``repo_dir`` and ``worktrees_dir`` are
only overridden when the project entry explicitly provides them (non-null),
which is only needed for multi-repo setups where the active project lives in a
different directory than the one the service was started against.  For the
primary repo, omit those fields and let ``REPO_DIR`` / ``WORKTREES_DIR`` win.

:func:`settings.reload` re-applies the active project on demand.  The poller
calls it at the top of every tick so a project switch via the GUI takes effect
within one polling interval — no service restart required.
"""

from __future__ import annotations

import enum
import json
import logging
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class TaskRunnerChoice(str, enum.Enum):
    """Task runner backend for agent execution.
    
    Determines which system executes agent tasks:
    - ``cursor``: Cursor IDE with Composer agent
    - ``anthropic``: Direct Anthropic API calls (default)
    """
    cursor = "cursor"
    anthropic = "anthropic"


def _resolve_project(raw: dict[str, object], target: AgentCeptionSettings) -> None:
    """Apply the active project's overrides from *raw* onto *target* in-place.

    Only ``gh_repo`` is always applied from the project entry.  ``repo_dir``
    and ``worktrees_dir`` are applied only when the project entry explicitly
    provides a non-null string value — omitting them lets the environment
    variables (``REPO_DIR``, ``WORKTREES_DIR``) remain authoritative, which is
    the correct behaviour for the primary (single-repo) use case.

    Extracted as a module-level helper so both the Pydantic validator and
    :meth:`AgentCeptionSettings.reload` can share identical logic without
    duplication.
    """
    active_name: object = raw.get("active_project")
    projects: object = raw.get("projects", [])
    if not isinstance(projects, list) or not active_name:
        return
    for proj in projects:
        if not isinstance(proj, dict) or proj.get("name") != active_name:
            continue
        if "gh_repo" in proj and isinstance(proj["gh_repo"], str):
            target.gh_repo = proj["gh_repo"]
        if "repo_dir" in proj and isinstance(proj["repo_dir"], str):
            target.repo_dir = Path(proj["repo_dir"])
        if "worktrees_dir" in proj and isinstance(proj["worktrees_dir"], str):
            wd = proj["worktrees_dir"]
            if wd.startswith("~/"):
                wd = str(Path.home()) + wd[1:]
            target.worktrees_dir = Path(wd)
        break


class AgentCeptionSettings(BaseSettings):
    """Runtime configuration for the AgentCeption dashboard service.

    Path settings are resolved in order:
    1. Environment variables (``REPO_DIR``, ``WORKTREES_DIR``, etc.)
    2. Active project from ``pipeline-config.json`` (overrides env vars when present)

    Call :meth:`reload` to pick up a changed ``active_project`` at runtime
    without restarting the service.
    """

    model_config = SettingsConfigDict(env_prefix="")

    worktrees_dir: Path = Path.home() / ".agentception/worktrees"
    host_worktrees_dir: Path = Path.home() / ".agentception/worktrees"
    """Host-side path to the worktrees directory.

    Inside Docker, ``worktrees_dir`` is the container path (``/worktrees``).
    ``host_worktrees_dir`` is the corresponding path on the developer's machine
    (e.g. ``~/.agentception/worktrees``), used to generate paths that the
    user can open directly in Cursor.
    Set via ``HOST_WORKTREES_DIR`` in docker-compose.override.yml.
    """
    repo_dir: Path = Path.cwd()
    host_repo_dir: Path = Path.cwd()
    """Host-side path to the repository root.

    Inside Docker, ``repo_dir`` is the container path (``/app``).
    ``host_repo_dir`` is the corresponding path on the developer's machine
    (e.g. ``/Users/alice/dev/myproject``), used to generate ``ROLE_FILE``
    and ``HOST_ROLE_FILE`` paths that Cursor agents running on the host can
    actually read.  Set via ``HOST_REPO_DIR`` in docker-compose or .env.
    """
    gh_repo: str = "cgcardona/agentception"
    poll_interval_seconds: int = 5
    stale_run_threshold_minutes: int = 10
    """Minutes of inactivity before an implementing run is a reconciliation candidate.

    Set via ``STALE_RUN_THRESHOLD_MINUTES`` env var.  Runs whose
    ``last_activity_at`` is older than this value are checked against GitHub
    and transitioned to ``completed`` if their issue is closed or PR is merged.
    Defaults to 10 minutes.  Raise this value if agents are being incorrectly
    marked stale during slow operations.
    """
    agent_max_iterations: int = 100
    # TTL must be strictly less than poll_interval_seconds (currently 5) so every
    # poller tick sees live GitHub data.  Keep GITHUB_CACHE_SECONDS < POLL_INTERVAL_SECONDS.
    github_cache_seconds: int = 4
    ac_api_key: str = ""
    """Shared secret for authenticating requests to the ``/api/*`` routes.

    Set via ``AC_API_KEY`` env var.  When set, every request to ``/api/*``
    must include either:

    - ``Authorization: Bearer <key>`` header, **or**
    - ``X-API-Key: <key>`` header.

    When left empty (default), authentication is disabled — safe for local
    Docker-only deployments where the service is bound to ``127.0.0.1``.
    For any public or shared deployment this **must** be set to a random,
    high-entropy value.  Generate one with:

        python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    """
    anthropic_api_key: str = ""
    """Anthropic API key for all LLM calls (agent loop, plan phase, enrichment).

    Set via ``ANTHROPIC_API_KEY`` env var.  Obtain a key from
    https://console.anthropic.com → API Keys.  When absent the Phase Planner
    falls back to the keyword-based heuristic classifier — no LLM is required
    for the service to start.
    """
    use_local_llm: bool = False
    """When True, the developer agent uses the local LLM at ``local_llm_base_url``
    instead of Anthropic. Set via ``USE_LOCAL_LLM`` env var (e.g. ``true``).
    Planner and reviewer still use Anthropic."""

    @field_validator("use_local_llm", mode="before")
    @classmethod
    def _parse_use_local_llm(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return False

    local_llm_base_url: str = "http://host.docker.internal:8080"
    """Base URL of the local OpenAI-compatible server (e.g. mlx_lm.server).
    Used when ``use_local_llm`` is True. From Docker, use host.docker.internal
    to reach a server running on the host. Set via ``LOCAL_LLM_BASE_URL``."""

    local_llm_chat_path: str = "/v1/chat/completions"
    """Path appended to ``local_llm_base_url`` for chat requests. Some servers
    use ``/chat/completions`` without the ``/v1`` prefix. Set via
    ``LOCAL_LLM_CHAT_PATH``."""

    local_llm_model: str = ""
    """Model name sent in the request. If empty, omit so the server uses its
    loaded model (avoids 404 from mlx_lm.server when it doesn't know \"local\").
    Set via ``LOCAL_LLM_MODEL``."""

    local_llm_max_context_chars: int = 12_000
    """Max characters for the first user message when using the local LLM.
    Small models (e.g. Qwen 4B) are easily overloaded; truncating the task
    briefing keeps context manageable. Set via ``LOCAL_LLM_MAX_CONTEXT_CHARS``."""

    local_llm_max_tokens: int = 4096
    """Max tokens for local LLM completion. Small models struggle with 32k;
    use 4096 or 8192. Set via ``LOCAL_LLM_MAX_TOKENS``."""

    local_llm_max_system_chars: int = 6000
    """Max characters for the system prompt when using the local LLM. Truncates
    role + cognitive arch so small models get a digest. Set via
    ``LOCAL_LLM_MAX_SYSTEM_CHARS``."""

    github_token: str = ""
    """GitHub Personal Access Token for GitHub API calls and the GitHub MCP server.

    Set via ``GITHUB_TOKEN`` env var.  Used by the ``gh`` CLI, the
    ``readers.github`` HTTP client, and the GitHub MCP server subprocess
    (mapped to ``GITHUB_PERSONAL_ACCESS_TOKEN``).  When absent, GitHub MCP
    tools are unavailable in the agent loop.
    """
    ac_task_runner: TaskRunnerChoice = TaskRunnerChoice.anthropic
    ac_min_turn_delay_secs: float = 0.5
    """Minimum seconds between consecutive LLM calls in the agent loop.

    Proactive pacing guard that keeps token consumption under the Anthropic
    rate limit ceiling.  Calibrated for **Tier 4** (2M input / 400K output
    TPM, 4K RPM): 0.5 s allows up to ~10 concurrent agents at ~1 000 output
    tokens per turn before the output-TPM cap is reached.  Raise this value
    if observing 429 rate-limit errors in the logs.

    Set via ``AC_MIN_TURN_DELAY_SECS`` env var.
    """
    """Task runner backend for agent execution.
    
    Set via ``AC_TASK_RUNNER`` env var.  Valid values: ``cursor``, ``anthropic``.
    Defaults to ``anthropic`` when unset.  Determines which system executes
    agent tasks — Cursor IDE with Composer agent or direct Anthropic API calls.
    """
    # ── Qdrant / code search ────────────────────────────────────────────────
    qdrant_url: str = "http://agentception-qdrant:6333"
    """Internal URL of the Qdrant vector store.

    Set via ``QDRANT_URL`` env var.  Defaults to the Docker Compose service
    name on port 6333 (the Qdrant REST port inside the network).  On the host
    the Qdrant REST API is exposed at ``http://127.0.0.1:6335``.
    """
    qdrant_collection: str = "code"
    """Name of the Qdrant collection used for codebase vectors."""
    embed_model: str = "jinaai/jina-embeddings-v2-base-code"
    """FastEmbed model name for generating code chunk embeddings.

    ``jinaai/jina-embeddings-v2-base-code`` is a code-specific 768-dimensional
    model trained on English and 30 programming languages with an 8 192-token
    context window.  It substantially outperforms general-purpose text models
    (e.g. ``BAAI/bge-small-en-v1.5``) on code retrieval tasks because it
    understands identifier names, type signatures, and code patterns.  The model
    is downloaded from HuggingFace Hub on first use (~640 MB) and cached in
    ``FASTEMBED_CACHE_DIR`` (default ``/tmp/fastembed_cache``).
    """
    embed_model_dim: int = 768
    """Vector dimension produced by ``embed_model``.

    Must match the model — ``jinaai/jina-embeddings-v2-base-code`` produces
    768-dimensional vectors.  Override when switching to a different model.
    """
    rerank_model: str = "BAAI/bge-reranker-base"
    """FastEmbed cross-encoder model used to rerank hybrid search results.

    After dense+sparse retrieval, a cross-encoder scores each candidate chunk
    jointly with the query text and re-orders the list for precision.
    ``BAAI/bge-reranker-base`` (~280 MB) provides a strong relevance signal
    with acceptable CPU latency (~50 ms for 10 candidates).  Set to an empty
    string to disable reranking.
    """
    worktree_index_enabled: bool = True
    """Whether to index each agent worktree into a per-run Qdrant collection.

    When ``true`` (default), every dispatched agent run triggers a background
    ``index_codebase`` pass over its worktree, creating a ``worktree-<run_id>``
    collection the agent can search with ``search_codebase``.

    Set to ``false`` (via ``WORKTREE_INDEX_ENABLED=false``) to skip per-run
    indexing entirely.  The main ``code`` collection (full-repo index) remains
    available for all ``search_codebase`` calls — it is sufficient for code
    discovery on the current codebase.  Disabling saves ~500 MB+ of peak RSS
    per agent run by avoiding the concurrent ONNX embed batches that otherwise
    run alongside the first LLM call.
    """
    database_url: str | None = None
    """Async database URL for AgentCeption's own ac_* tables.

    Set via ``DATABASE_URL`` env var (docker-compose injects this).
    Falls back to a local SQLite file when absent so the service starts
    without Postgres in pure-filesystem dev mode.
    """

    @property
    def ac_dir(self) -> Path:
        """Canonical path to the ``.agentception/`` directory at the repo root.

        All AgentCeption-owned config files (roles, prompts, pipeline-config,
        dispatcher prompt, etc.) live here — not in ``.cursor/``, which belongs
        to the IDE.
        """
        return self.repo_dir / ".agentception"

    @model_validator(mode="after")
    def _apply_active_project(self) -> AgentCeptionSettings:
        """Override path settings from the active project in ``pipeline-config.json``.

        Reads the config file synchronously at initialisation so that all
        downstream code that imports ``settings`` sees the correct project
        paths immediately.  If the file is absent, malformed, or has no
        ``active_project`` key, the validator is a no-op.
        """
        config_path = self.repo_dir / ".agentception" / "pipeline-config.json"
        if not config_path.exists():
            return self
        try:
            raw: object = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover — filesystem error path
            logger.warning("⚠️ Could not read pipeline-config.json for project override: %s", exc)
            return self
        if not isinstance(raw, dict):
            return self
        _resolve_project(raw, self)
        return self

    def reload(self) -> None:
        """Re-read ``pipeline-config.json`` and apply the active project's paths in-place.

        Called by the poller at the start of each tick and by the
        ``switch-project`` API endpoint so project switches take effect
        within one polling interval — no service restart required.

        This method is synchronous: reading a small local JSON file is fast
        enough that wrapping it in an executor would add more overhead than
        it saves.
        """
        config_path = self.repo_dir / ".agentception" / "pipeline-config.json"
        if not config_path.exists():
            return
        try:
            raw: object = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("⚠️ Could not read pipeline-config.json during reload: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        _resolve_project(raw, self)
        logger.debug("✅ Settings reloaded — gh_repo=%s repo_dir=%s", self.gh_repo, self.repo_dir)


settings = AgentCeptionSettings()
