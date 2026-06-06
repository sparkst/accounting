#!/usr/bin/env bash
# uptime_check.sh — external health probe of books.sparkry.ai through the tunnel.
#
# Exits non-zero (→ systemd OnFailure=accounting-alert@%p → one Resend email,
# hourly-deduped) when the PUBLIC health endpoint is not 200 + {"ok":true}.
# This exercises the whole external path: DNS → Cloudflare → tunnel → Caddy →
# uvicorn, so it catches tunnel/cloudflared/Caddy/api failures.
#
# LIMITATION: this is an ON-BOX monitor — it cannot detect a total box-down
# (if the box is off, the timer can't run). For true external coverage, run an
# off-box Cloudflare cron Worker (REQ-HM-014, not yet built).
#
# Required env (injected by `doppler run` from accounting/srv):
#   CF_ACCESS_UPTIME_CLIENT_ID, CF_ACCESS_UPTIME_CLIENT_SECRET
# Optional env:
#   UPTIME_URL              override the probe URL
#   HEALTHCHECK_PING_URL    dead-man ping on success (no-op if unset)
#   CURL_BIN                override the curl binary (for tests)
set -uo pipefail

URL="${UPTIME_URL:-https://books.sparkry.ai/api/health/ping}"
CURL="${CURL_BIN:-curl}"

resp="$("$CURL" -s --max-time 15 -w $'\n%{http_code}' \
  -H "CF-Access-Client-Id: ${CF_ACCESS_UPTIME_CLIENT_ID:-}" \
  -H "CF-Access-Client-Secret: ${CF_ACCESS_UPTIME_CLIENT_SECRET:-}" \
  "$URL" 2>&1)"

code="$(printf '%s' "$resp" | tail -n1)"
body="$(printf '%s' "$resp" | sed '$d')"

if [ "$code" = "200" ] && printf '%s' "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
  echo "uptime ok: $URL -> 200"
  # Dead-man ping on success (optional; no-op if URL not set).
  [ -n "${HEALTHCHECK_PING_URL:-}" ] && \
    "$CURL" -fsS --max-time 10 "$HEALTHCHECK_PING_URL" >/dev/null 2>&1 || true
  exit 0
fi

echo "uptime FAIL: $URL -> code=${code} body=$(printf '%s' "$body" | head -c 200)" >&2
exit 1
