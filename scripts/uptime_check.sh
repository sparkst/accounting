#!/usr/bin/env bash
# uptime_check.sh — local serving-stack health probe.
#
# Hits the LOCAL Caddy reverse proxy (127.0.0.1:9000 → uvicorn), so it exercises
# Caddy routing + the api. Exits non-zero (→ systemd OnFailure=accounting-alert@%p
# → one Resend email, hourly-deduped) unless the response is 200 + {"ok":true}.
#
# WHY LOCAL, NOT THE PUBLIC URL: probing https://books.sparkry.ai from the box's
# own IP trips Cloudflare's managed challenge (403 "Just a moment…") → false
# alarms. The local check is reliable and catches Caddy/uvicorn failures.
#
# LIMITATIONS (both need an OFF-BOX Cloudflare cron Worker — REQ-HM-014, deferred):
#   * does NOT verify the public tunnel path (DNS / CF Access / cloudflared);
#   * cannot detect a total box-down (if the box is off, the timer can't run).
#
# Optional env:
#   UPTIME_URL              override the probe URL (default local Caddy)
#   HEALTHCHECK_PING_URL    dead-man ping on success (no-op if unset)
#   CURL_BIN                override the curl binary (for tests)
set -uo pipefail

URL="${UPTIME_URL:-http://127.0.0.1:9000/api/health/ping}"
CURL="${CURL_BIN:-curl}"

resp="$("$CURL" -s --max-time 15 -w $'\n%{http_code}' "$URL" 2>&1)"

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
