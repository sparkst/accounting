#!/usr/bin/env bash
# quark_digest.sh — the FAST Quark data path. NO 31 MB snapshot pull.
#
# Computes the register digest READ-ONLY *on the box* (~KB over SSH) IN PARALLEL
# with the live D1 net-worth fetch, and caches both. This replaces pulling the
# whole 31 MB prod DB just to read a few hundred KB of register data (half of
# which is raw_data JSON Quark never touches) — net worth already lives at the
# Cloudflare edge (D1), so the local wealth tables aren't needed here either.
#
# Run with the wealth creds injected (for the D1 net-worth call):
#   doppler run --project accounting --config dev -- bash scripts/quark_digest.sh
#
# Writes data/.quark-cache/:
#   register.json   — pulse / per-entity P&L / hygiene / AR / next deadline (from the box)
#   networth.json   — live net worth + by_broker (from D1), or absent if unavailable
#   digest.env      — QUARK_NETWORTH_SOURCE=<d1-live|unavailable> + QUARK_DIGEST_AT=<epoch>
#
# Deep/ad-hoc local SQL or the brokerage_summary fallback still use the full
# snapshot via scripts/quark_refresh.sh — that's opt-in now, not the default.

set -uo pipefail
readonly BOX="ubuntu"
readonly BOX_USER="travis"
readonly BOX_DB="/home/travis/accounting/data/accounting.db"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="$ROOT/data/.quark-cache"
mkdir -p "$CACHE"
log() { printf '  [quark-digest] %s\n' "$*" >&2; }
is_json() { python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" 2>/dev/null; }

reg="$CACHE/register.json"
nw="$CACHE/networth.json"
tmp_reg="$(mktemp)"
tmp_nw="$(mktemp)"
trap 'rm -f "$tmp_reg" "$tmp_nw"' EXIT

# --- Parallel: register digest on the box (read-only) ∥ live D1 net worth ---
ssh "${SSH_OPTS[@]}" "$BOX_USER@$BOX" "python3 - '$BOX_DB'" \
  < "$ROOT/scripts/quark_register_digest.py" > "$tmp_reg" 2>/dev/null &
pid_reg=$!

if [ -n "${WEALTH_API_BASE:-}" ] && [ -n "${WEALTH_INTERNAL_KEY:-}" ]; then
  curl -fsS --max-time 20 -H "X-Internal-Key: $WEALTH_INTERNAL_KEY" \
    "${WEALTH_API_BASE%/}/wealth/api/internal/networth" > "$tmp_nw" 2>/dev/null &
else
  : > "$tmp_nw" &
fi
pid_nw=$!

wait "$pid_reg" || true
wait "$pid_nw" || true

if is_json "$tmp_reg"; then
  mv -f "$tmp_reg" "$reg"
else
  log "⚠ register digest failed (box unreachable?) — keeping any prior register.json"
fi

src="unavailable"
if is_json "$tmp_nw"; then
  mv -f "$tmp_nw" "$nw"
  src="d1-live"
else
  log "⚠ live net worth unavailable (endpoint not deployed/reachable) — Quark falls back to the stale local mirror and must say so"
fi

printf 'QUARK_NETWORTH_SOURCE=%s\nQUARK_DIGEST_AT=%s\n' "$src" "$(date +%s)" > "$CACHE/digest.env"
log "digest ready — register.json + networth.json ($src). No snapshot pulled."
