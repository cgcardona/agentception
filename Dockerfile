FROM python:3.12-slim

WORKDIR /app

# Install git, curl, ripgrep, and the GitHub CLI (gh).
# ripgrep (rg) is used by the search_text agent tool for fast codebase search.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ripgrep \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install the GitHub askpass helper and configure system-wide git auth.
#
# Git's git-receive-pack (push) and git-upload-pack (fetch/clone) endpoints
# only accept HTTP Basic auth — Bearer tokens are rejected with 401. The askpass
# helper supplies "x-access-token" / $GITHUB_TOKEN so git generates a valid
# Basic auth header without any token ever touching remote.origin.url or a
# per-worktree git config file (which risks GitHub secret scanning revocation).
#
# --system writes to /etc/gitconfig (baked into the image layer), so it applies
# regardless of which $HOME the container runs with. GIT_TERMINAL_PROMPT=0
# prevents git from falling back to an interactive TTY prompt when credentials
# are missing — agents run non-interactively and must fail fast instead.
COPY scripts/github-askpass /usr/local/bin/github-askpass
RUN chmod +x /usr/local/bin/github-askpass \
    && git config --system core.askPass /usr/local/bin/github-askpass

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

# Install Python dependencies.
COPY agentception/requirements.txt /app/agentception/requirements.txt
RUN pip install --no-cache-dir -r /app/agentception/requirements.txt

# Copy the full package.
COPY agentception/ /app/agentception/
COPY pyproject.toml /app/pyproject.toml

# Install the package itself so `agentception.*` imports resolve.
RUN pip install --no-cache-dir -e /app

# Compile SCSS → CSS at build time.
RUN sass --style=compressed --no-source-map \
    /app/agentception/static/scss/app.scss \
    /app/agentception/static/app.css

CMD ["uvicorn", "agentception.app:app", "--host", "0.0.0.0", "--port", "10003"]
