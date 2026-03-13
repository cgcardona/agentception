#!/usr/bin/env bash
# Write a plan/phase summary into the Knowtation vault (K1 bridge).
# Usage:
#   echo "$SUMMARY" | scripts/knowtation_write_phase_summary.sh OWNER/REPO INITIATIVE BATCH_ID
#   cat summary.md | scripts/knowtation_write_phase_summary.sh owner/repo my-initiative batch-abc123
# If Knowtation CLI is not in PATH, prints the target path and the command to run manually.
set -euo pipefail

REPO="${1:-}"
INITIATIVE="${2:-}"
BATCH_ID="${3:-}"

if [[ -z "$REPO" || -z "$INITIATIVE" || -z "$BATCH_ID" ]]; then
  echo "Usage: <summary on stdin> | $0 OWNER/REPO INITIATIVE BATCH_ID" >&2
  echo "Example: cat summary.md | $0 cgcardona/agentception auth-rewrite batch-abc123" >&2
  exit 1
fi

# Conventional path: vault/projects/<repo_slug>/plans/<batch_id>-summary.md
REPO_SLUG="${REPO//\//-}"
VAULT_PATH="vault/projects/${REPO_SLUG}/plans/${BATCH_ID}-summary.md"

if command -v knowtation &>/dev/null; then
  knowtation write "$VAULT_PATH" --stdin
  echo "Wrote to vault: $VAULT_PATH" >&2
else
  echo "Knowtation CLI not found. To write this summary to the vault, run:" >&2
  echo "  knowtation write \"$VAULT_PATH\" --stdin < summary.md" >&2
  echo "Path: $VAULT_PATH" >&2
  # Write stdin to a temp file so the user can use it with the printed command
  TMP=$(mktemp)
  trap 'rm -f "$TMP"' EXIT
  cat > "$TMP"
  echo "Summary saved to $TMP — use: knowtation write \"$VAULT_PATH\" --stdin < \"$TMP\"" >&2
fi
