#!/usr/bin/env bash
# quark_prewarm.sh — background pre-load of Quark's data so "how are we doing" is instant.
#
# Fires the FAST digest (scripts/quark_digest.sh): the register digest computed
# READ-ONLY on the box (~KB over SSH) IN PARALLEL with the live D1 net worth. NO
# 31 MB snapshot pull. Designed to be fired detached from a SessionStart hook.
#
# Debounced: if the cache is fresher than QUARK_PREWARM_TTL_MIN (default 25), it's a
# no-op — so opening several sessions in a row won't re-pull.
#
# Populates data/.quark-cache/{register.json, networth.json, digest.env}, which the
# Quark skill reads first (instant) before falling back to a live pull.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="$ROOT/data/.quark-cache"
CACHE="$CACHE_DIR/digest.env"
TTL_MIN="${QUARK_PREWARM_TTL_MIN:-25}"

mkdir -p "$CACHE_DIR"

now_epoch() { date +%s; }
file_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null; }

# Debounce — skip if the cache is still fresh.
if [ -f "$CACHE" ]; then
  mt="$(file_mtime "$CACHE")"
  if [ -n "$mt" ] && [ "$(( ($(now_epoch) - mt) / 60 ))" -lt "$TTL_MIN" ]; then
    exit 0
  fi
fi

# Single-flight lock so overlapping launches don't double-pull.
LOCK="$CACHE_DIR/.prewarm.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  exit 0   # another prewarm is running
fi
trap 'rm -rf "$LOCK"' EXIT

# Run the fast digest (itself parallel: box register read ∥ D1 net worth). KB, ~2.5s.
doppler run --project accounting --config dev -- \
  bash "$ROOT/scripts/quark_digest.sh" >/dev/null 2>&1 || true
