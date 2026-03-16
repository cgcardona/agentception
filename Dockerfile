FROM python:3.12-slim

WORKDIR /app

# Install system dependencies:
#   git       — version control for worktree operations
#   curl      — downloading tool binaries (gh, sass, MCP server)
#   ripgrep   — fast codebase search used by the search_text agent tool
#   gosu      — minimal setuid helper for privilege dropping in entrypoint.sh
#               (purpose-built alternative to sudo/su; avoids TTY issues and
#               signal-forwarding bugs that plague plain `su`)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ripgrep \
    gosu \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create a non-root service user.  UID/GID 1001 is chosen to avoid collision
# with common system UIDs (0=root, 1-999=system, 1000=typical first human
# user).  The agentception user owns its home directory and the model cache;
# /worktrees ownership is fixed at runtime by entrypoint.sh after the bind
# mount is available.
RUN groupadd -g 1001 agentception \
    && useradd -r -u 1001 -g agentception -m -d /home/agentception -s /bin/bash agentception \
    && mkdir -p /worktrees /home/agentception/.cache/huggingface \
    && chown -R agentception:agentception /home/agentception/.cache

# Install Node.js 22.x — enables sass/esbuild builds at container startup
# and npm run type-check / npm test inside the container.
ARG NODE_VERSION=22
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version

# Install the GitHub askpass helper and configure system-wide git settings.
#
# Git's git-receive-pack (push) and git-upload-pack (fetch/clone) endpoints
# only accept HTTP Basic auth — Bearer tokens are rejected with 401.  The
# askpass helper supplies "x-access-token" / $GITHUB_TOKEN so git generates a
# valid Basic auth header without any token ever touching remote.origin.url or
# a per-worktree git config file (which risks GitHub secret scanning revocation).
#
# --system writes to /etc/gitconfig (baked into the image layer), so it applies
# regardless of which $HOME the container runs with.  GIT_TERMINAL_PROMPT=0
# prevents git from falling back to an interactive TTY prompt when credentials
# are missing — agents run non-interactively and must fail fast.
#
# http.proxy routes git HTTPS operations through the egress allowlist proxy.
# The proxy service name resolves inside the Docker network at runtime; this
# config has no effect during the Docker build phase (proxy not running then).
COPY scripts/github-askpass /usr/local/bin/github-askpass
RUN chmod +x /usr/local/bin/github-askpass \
    && git config --system core.askPass /usr/local/bin/github-askpass \
    && git config --system user.email "agentception@localhost" \
    && git config --system user.name "AgentCeption" \
    && git config --system http.proxy "http://proxy:8888"

# Install the GitHub MCP server binary — provides the agent loop with typed
# GitHub tools (get_issue, list_issues, add_issue_comment, create_pull_request,
# merge_pull_request, etc.) via the MCP stdio protocol.
ARG GH_MCP_VERSION=0.32.0
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) GH_MCP_ARCH="x86_64" ;; \
         arm64) GH_MCP_ARCH="arm64" ;; \
         *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
       esac \
    && curl -fsSL \
       "https://github.com/github/github-mcp-server/releases/download/v${GH_MCP_VERSION}/github-mcp-server_Linux_${GH_MCP_ARCH}.tar.gz" \
       | tar xz -C /usr/local/bin github-mcp-server \
    && chmod +x /usr/local/bin/github-mcp-server \
    && github-mcp-server --version

# Install Dart Sass — compiles SCSS at build time (no Node.js required).
ARG DART_SASS_VERSION=1.85.0
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) SASS_ARCH="x64" ;; \
         arm64) SASS_ARCH="arm64" ;; \
         *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/sass/dart-sass/releases/download/${DART_SASS_VERSION}/dart-sass-${DART_SASS_VERSION}-linux-${SASS_ARCH}.tar.gz" \
       | tar xz -C /usr/local \
    && ln -s /usr/local/dart-sass/sass /usr/local/bin/sass \
    && sass --version

# Install Node.js dependencies — Linux-native binaries baked into the image.
# A named volume (node_modules) in docker-compose preserves this layer at runtime
# so the ./:/app bind mount cannot shadow it with macOS host binaries.
COPY package.json package-lock.json tsconfig.json vitest.config.ts /app/
RUN npm ci --include=dev

# Install Python dependencies.
COPY agentception/requirements.txt /app/agentception/requirements.txt
RUN pip install --no-cache-dir -r /app/agentception/requirements.txt

# Copy the full package.
COPY agentception/ /app/agentception/
COPY pyproject.toml /app/pyproject.toml

# Install the package itself so `agentception.*` imports resolve.
RUN pip install --no-cache-dir -e /app

# Pre-download all ONNX models used by code_indexer into the image layer.
# Docker build has unrestricted network access; the runtime proxy does not,
# so downloading at runtime inside the container will fail (403 CONNECT tunnel).
# Baking the models here means they are always present after a clean build and
# survive volume recreation without any manual recovery steps.
#
# HF_HOME must be set to the agentception user's cache directory so the models
# land in the same path the runtime app reads from.  Without this, the build
# runs as root and writes to /root/.cache/huggingface — a cache miss every
# startup — causing fastembed to re-download at runtime.
#
# Models baked here:
#   - jinaai/jina-embeddings-v2-base-code  (embedding — TextEmbedding)
#   - BAAI/bge-reranker-base               (reranker  — TextCrossEncoder)
RUN HF_HOME=/home/agentception/.cache/huggingface python3 -c "\
from fastembed import TextEmbedding, TextCrossEncoder; \
emb = TextEmbedding(model_name='jinaai/jina-embeddings-v2-base-code'); \
list(emb.embed(['warm-up'])); \
print('Embedding model pre-downloaded.'); \
rnk = TextCrossEncoder(model_name='BAAI/bge-reranker-base'); \
list(rnk.rerank('query', ['doc'])); \
print('Reranker model pre-downloaded.')" \
    && chown -R agentception:agentception /home/agentception/.cache

# Entrypoint: performs privileged startup (resolv.conf, asset compilation,
# DB migrations, ownership fixes) then drops to the agentception user via
# gosu before exec'ing the server command.
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "agentception.app:app", "--host", "0.0.0.0", "--port", "1337"]
