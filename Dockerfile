FROM python:3.12-slim

WORKDIR /app

# Install git, curl, and the GitHub CLI (gh) — needed for subprocess git/gh calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

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
