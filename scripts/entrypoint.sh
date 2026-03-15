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

# ── 2. Static asset compilation ──────────────────────────────────────────────
# The bind-mounted source directories appear as root-owned inside the container
# on macOS/Docker Desktop (VirtioFS maps host UID 0 → container root).
# Compile as root so we can write to the bind-mounted static output paths.
cd /app
echo "[entrypoint] compiling SCSS …"
sass --style=compressed --no-source-map \
  agentception/static/scss/app.scss agentception/static/app.css

echo "[entrypoint] building JS bundle …"
npm run build:js

# ── 3. Database migrations ───────────────────────────────────────────────────
# Retry once on transient connection failure (Postgres may still be
# initialising on the first `docker compose up`).
echo "[entrypoint] running Alembic migrations …"
alembic -c agentception/alembic.ini upgrade head || {
  echo "[entrypoint] alembic failed, retrying in 5 s …"
  sleep 5
  alembic -c agentception/alembic.ini upgrade head
}

# ── 4. Fix ownership of mutable mount points ─────────────────────────────────
# /worktrees  — bind-mount of ${HOME}/.agentception/worktrees/agentception on
#   the host.  On macOS/Docker Desktop (VirtioFS) chown inside the container
#   propagates to the host as the host user's UID — this is intentional and
#   safe.  The agentception user must be able to create git worktree
#   directories here.
echo "[entrypoint] fixing ownership of /worktrees …"
chown agentception:agentception /worktrees

# /home/agentception/.cache/huggingface — named volume (agentception-model-cache).
#   FastEmbed downloads ONNX models on first dispatch; the agentception user
#   must be able to write here (and to the hub token file under it).
echo "[entrypoint] fixing ownership of model cache …"
chown -R agentception:agentception /home/agentception/.cache/huggingface

# ── 5. Drop to non-root user ─────────────────────────────────────────────────
# gosu is a purpose-built setuid helper (analogous to sudo -u but without the
# shell overhead).  It sets UID/GID and execs "$@" — typically uvicorn — as
# PID 1's effective non-root user.  After this line no process in the
# container runs as root.
echo "[entrypoint] dropping privileges → agentception (uid=$(id -u agentception))"
exec gosu agentception "$@"
