#!/bin/bash
# Container entrypoint.  Runs as root, performs privileged setup, then drops
# to the non-root `agentception` user (UID 1001) for the long-running server
# process.  All code executed after the exec is fully unprivileged.
#
# Belt-and-suspenders design:
#   1. Privileged phase (root): write resolv.conf, compile static assets,
#      run DB migrations, chown mutable volumes.
#   2. Unprivileged phase: exec gosu → agentception → "$@" (uvicorn by default).
#
# This pattern follows the Docker best practice of keeping the PID-1 server
# process non-root even when container setup requires transient root access.
set -euo pipefail

# ── 1. DNS resolver ──────────────────────────────────────────────────────────
# Override the Docker embedded resolver with Cloudflare + Google DNS.
# Docker's embedded DNS sometimes returns dead GitHub nodes; 1.1.1.1 avoids
# that by performing its own health-aware routing.  We write this before any
# network operations so all subsequent calls use the correct resolvers.
printf 'nameserver 127.0.0.11\nnameserver 1.1.1.1\nnameserver 8.8.8.8\noptions ndots:0 timeout:3 attempts:1 use-vc\n' \
  > /etc/resolv.conf

# ── 2. Git safe.directory ────────────────────────────────────────────────────
# The .git bind-mount is owned by the macOS host user; the container runs as
# agentception (UID 1001), which has a different UID.  Git 2.35.2+ rejects
# repositories where the directory owner doesn't match the effective user.
# Adding /app to the system-wide config (written to /etc/gitconfig) makes it
# trusted for both root (during this entrypoint) and agentception (after exec).
# /worktrees/* directories also need coverage because every git-worktree
# command checks the common git dir at /app, which already resolves via this
# setting, but individual worktree paths may be traversed by git internally.
git config --system --add safe.directory /app
git config --system --add safe.directory /worktrees

# ── 3. Static asset compilation ──────────────────────────────────────────────
# The bind-mounted source directories appear as root-owned inside the container
# on macOS/Docker Desktop (VirtioFS maps host UID 0 → container root).
# Compile as root so we can write to the bind-mounted static output paths.
cd /app
echo "[entrypoint] compiling SCSS …"
sass --style=compressed --no-source-map \
  agentception/static/scss/app.scss agentception/static/app.css

echo "[entrypoint] building JS bundle …"
npm run build:js

# ── 4. Database migrations ───────────────────────────────────────────────────
# Retry once on transient connection failure (Postgres may still be
# initialising on the first `docker compose up`).
echo "[entrypoint] running Alembic migrations …"
alembic -c agentception/alembic.ini upgrade head || {
  echo "[entrypoint] alembic failed, retrying in 5 s …"
  sleep 5
  alembic -c agentception/alembic.ini upgrade head
}

# ── 5. Fix ownership of mutable mount points ─────────────────────────────────
# /worktrees  — bind-mount of ${HOME}/.agentception/worktrees/agentception on
#   the host.  On macOS/Docker Desktop (VirtioFS) chown inside the container
#   propagates to the host as the host user's UID — this is intentional and
#   safe.  The agentception user must be able to create git worktree
#   directories here.
echo "[entrypoint] fixing ownership of /worktrees …"
chown agentception:agentception /worktrees

# /home/agentception/.cache/huggingface — named volume (agentception-model-cache).
#   HuggingFace Hub token file and metadata; the agentception user must be able
#   to write here.
echo "[entrypoint] fixing ownership of HuggingFace cache …"
chown -R agentception:agentception /home/agentception/.cache/huggingface

# /home/agentception/.cache/fastembed — named volume (agentception-fastembed-cache).
#   FastEmbed ONNX models.  Fix ownership so the agentception user can write here,
#   then verify both ONNX files are present.  If either is missing (fresh volume
#   or corruption), download now — this is the self-healing mechanism that means
#   models are downloaded exactly once and never lost across restarts or rebuilds.
echo "[entrypoint] fixing ownership of fastembed cache …"
mkdir -p /home/agentception/.cache/fastembed
chown -R agentception:agentception /home/agentception/.cache/fastembed

# Search from the guaranteed-present base directory so `find` never exits 1
# due to a missing subdirectory (which would abort the script under set -euo pipefail).
JINA_ONNX=$(find /home/agentception/.cache/fastembed -path '*/jinaai*' -name 'model.onnx' 2>/dev/null | head -1)
BGE_ONNX=$(find /home/agentception/.cache/fastembed -path '*/BAAI*' -name 'model.onnx' 2>/dev/null | head -1)

if [ -z "$JINA_ONNX" ] || [ -z "$BGE_ONNX" ]; then
  # In CI (GitHub Actions sets CI=true), skip the download — models are not
  # needed for mypy/pytest, and the ephemeral runner has no persistent cache.
  if [ "${CI:-}" = "true" ]; then
    echo "[entrypoint] CI mode — FastEmbed models not present, skipping download (not needed for CI checks)."
    JINA_ONNX="skip"
    BGE_ONNX="skip"
  fi
fi

if [ -z "$JINA_ONNX" ] || [ -z "$BGE_ONNX" ]; then
  echo "[entrypoint] FastEmbed ONNX models missing — downloading (one-time setup) …"
  FASTEMBED_CACHE_PATH=/home/agentception/.cache/fastembed \
  HOME=/home/agentception \
  HF_HOME=/home/agentception/.cache/huggingface \
  HF_TOKEN="${HF_TOKEN:-}" \
  python3 -c "
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
emb = TextEmbedding(model_name='jinaai/jina-embeddings-v2-base-code', cache_dir='/home/agentception/.cache/fastembed')
list(emb.embed(['warm-up']))
print('[entrypoint] Jina embedding model: ready')
rnk = TextCrossEncoder(model_name='BAAI/bge-reranker-base', cache_dir='/home/agentception/.cache/fastembed')
list(rnk.rerank('query', ['doc']))
print('[entrypoint] BGE reranker model: ready')
"
  chown -R agentception:agentception /home/agentception/.cache/fastembed
  echo "[entrypoint] FastEmbed models ready."
else
  echo "[entrypoint] FastEmbed models present — skipping download."
fi

# ── 6. Drop to non-root user ─────────────────────────────────────────────────
# gosu is a purpose-built setuid helper (analogous to sudo -u but without the
# shell overhead).  It sets UID/GID and execs "$@" — typically uvicorn — as
# PID 1's effective non-root user.  After this line no process in the
# container runs as root.
echo "[entrypoint] dropping privileges → agentception (uid=$(id -u agentception))"
exec gosu agentception "$@"
