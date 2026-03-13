FROM python:3.12-slim

WORKDIR /app

# Install git + gh CLI (needed for subprocess calls to git/gh) + Dart Sass
# Dart Sass is the official Sass implementation (standalone binary, no Node required).
# It compiles agentception/static/scss/app.scss → agentception/static/app.css at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Dart Sass (standalone binary — no Node.js required).
# Pin the version for reproducible builds; bump deliberately when upgrading.
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

COPY agentception/requirements.txt /app/agentception/requirements.txt
RUN pip install --no-cache-dir -r /app/agentception/requirements.txt

COPY agentception/ /app/agentception/

# Compile SCSS → CSS at build time.
# The generated app.css is committed to .gitignore-able but generated here
# so the container always has a fresh compile from the SCSS source of truth.
RUN sass --style=compressed --no-source-map \
    /app/agentception/static/scss/app.scss \
    /app/agentception/static/app.css

CMD ["uvicorn", "agentception.app:app", "--host", "0.0.0.0", "--port", "10003"]
