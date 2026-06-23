#!/usr/bin/env bash
# quark_prewarm.sh — background pre-load of Quark's data so "how are we doing" is instant.
#
# Runs the (quick) Hetzner snapshot pull AND the live D1 net-worth fetch IN PARALLEL,
# then caches the merged contract to data/.quark-cache/digest.env with a timestamp.
# Designed to be fired detached from a SessionStart hook on Claude launch.
#
# Debounced: if the cache is fresher than QUARK_PREWARM_TTL_MIN (default 25), it's a
# no-op — so opening several sessions in a row won't re-pull. Uses --quick (no prod
# Plaid sync) so launching Claude never hammers Plaid or the box's write path; the
# box's own daily timers keep the snapshot day-fresh.
#
# Cache contract (data/.quark-cache/digest.env), sourced by the Quark skill:
#   QUARK_DB / QUARK_FRESH / QUARK_ASOF / QUARK_STALE / QUARK_PLAID_REAUTH
#   QUARK_NETWORTH / QUARK_NETWORTH_ASOF / QUARK_NETWORTH_SOURCE
#   QUARK_PREWARM_AT=<epoch seconds>

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

tmp_ref="$(mktemp)"
tmp_nw="$(mktemp)"
trap 'rm -rf "$LOCK"; rm -f "$tmp_ref" "$tmp_nw"' EXIT

# --- Parallel: Hetzner snapshot pull (quick) ∥ live D1 net worth ---
bash "$ROOT/scripts/quark_refresh.sh" --quick >"$tmp_ref" 2>/dev/null &
pid_ref=$!
doppler run --project accounting --config dev -- \
  bash "$ROOT/scripts/quark_networth.sh" >"$tmp_nw" 2>/dev/null &
pid_nw=$!

wait "$pid_ref" || true
wait "$pid_nw" || true

# Merge both contracts + a stamp into the cache (atomic write via temp + mv).
tmp_cache="$(mktemp)"
{
  grep '^QUARK_' "$tmp_ref" 2>/dev/null || true
  grep '^QUARK_' "$tmp_nw" 2>/dev/null || true
  echo "QUARK_PREWARM_AT=$(now_epoch)"
} > "$tmp_cache"
mv -f "$tmp_cache" "$CACHE"
