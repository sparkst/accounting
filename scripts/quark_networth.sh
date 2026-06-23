#!/usr/bin/env bash
# quark_networth.sh — fetch the LIVE net worth for Quark (the Ferengi CFO skill).
#
# The wealth data migrated to the Cloudflare wealth app (D1); the local register
# only mirrors a stale copy. This pulls the current computed net worth from the
# wealth app's internal endpoint so Quark reports today's figure (current prices +
# Plaid balances) instead of a weeks-old number.
#
# Auth: X-Internal-Key (WEALTH_INTERNAL_KEY) against
#       WEALTH_API_BASE/wealth/api/internal/networth  (bypasses Cloudflare Access
#       via the /wealth/api/internal/* exclusion, same as the ingest client).
#
# Run with the wealth creds injected:
#   doppler run --project accounting --config dev -- bash scripts/quark_networth.sh
#
# Contract (stdout, always):
#   QUARK_NETWORTH=<total | empty>
#   QUARK_NETWORTH_ASOF=<YYYY-MM-DD | empty>
#   QUARK_NETWORTH_SOURCE=<d1-live | unavailable>
# Human log goes to stderr. SOURCE=unavailable ⇒ caller falls back to the local
# (stale) brokerage_summary and must say the figure is the stale mirror.

set -uo pipefail
log() { printf '  [quark-networth] %s\n' "$*" >&2; }
emit() {
  printf 'QUARK_NETWORTH=%s\nQUARK_NETWORTH_ASOF=%s\nQUARK_NETWORTH_SOURCE=%s\n' \
    "${1:-}" "${2:-}" "$3"
  exit 0
}

base="${WEALTH_API_BASE:-}"
key="${WEALTH_INTERNAL_KEY:-}"
if [ -z "$base" ] || [ -z "$key" ]; then
  log "WEALTH_API_BASE / WEALTH_INTERNAL_KEY not set — run under: doppler run --project accounting --config dev --. Falling back to local mirror."
  emit "" "" unavailable
fi

url="${base%/}/wealth/api/internal/networth"
resp="$(curl -fsS --max-time 20 -H "X-Internal-Key: $key" "$url" 2>/dev/null)" || {
  log "wealth net-worth endpoint unreachable ($url) — not deployed yet? Falling back to local mirror."
  emit "" "" unavailable
}

# Parse JSON; reject a non-JSON body (e.g. a Cloudflare Access HTML challenge).
parsed="$(printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("total") or "") + "|" + (d.get("as_of_max") or ""))
except Exception:
    print("PARSE_FAIL")
' 2>/dev/null)"

if [ -z "$parsed" ] || [ "$parsed" = "PARSE_FAIL" ]; then
  log "unexpected response (not JSON — likely a Cloudflare Access challenge; ensure /wealth/api/internal/* is Access-excluded). Falling back to local mirror."
  emit "" "" unavailable
fi

total="${parsed%%|*}"
asof="${parsed#*|}"
log "Live net worth from D1: \$${total} (as of ${asof:-unknown})"
emit "$total" "$asof" d1-live
