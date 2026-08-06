#!/usr/bin/env bash
# Alerting-consolidation cutover (plan §5, REQ-FIX-ALR-010) — run as ROOT on the box.
#
#   sudo bash /home/travis/accounting/deploy/cutover_alert_webhook.sh
#
# Prereq: the repo at /home/travis/accounting is already at the commit that
# flipped deploy/*.service OnFailure= to accounting-alert-webhook@%p.service
# (normally via scripts/deploy_box.py --apply from the Mac).
#
# What it does (idempotent):
#   1. installs accounting-alert-webhook@.service to /etc/systemd/system/
#   2. installs every deploy/*.service + *.timer (they now carry the webhook
#      OnFailure=) EXCEPT the units the plan says never to auto-enable
#   3. daemon-reload
#   4. smoke test: fires accounting-alert-webhook@smoke-test.service — expect
#      ONE sev3 Telegram message within ~10 s
#   5. leaves accounting-alert@.service installed but unreferenced (rollback:
#      sed OnFailure= back and daemon-reload)
set -euo pipefail

REPO=/home/travis/accounting
DEPLOY="$REPO/deploy"
SYSD=/etc/systemd/system

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$DEPLOY/accounting-alert-webhook@.service" ] || { echo "repo not synced: $DEPLOY missing webhook template" >&2; exit 1; }

# Guard: the synced repo must already carry the flipped OnFailure= lines.
if grep -l "OnFailure=accounting-alert@%p.service" "$DEPLOY"/*.service >/dev/null 2>&1; then
  echo "ABORT: $DEPLOY still references the email template — sync the repo first" >&2
  exit 1
fi

echo "== installing units =="
install -m 644 "$DEPLOY/accounting-alert-webhook@.service" "$SYSD/"
for f in "$DEPLOY"/*.service "$DEPLOY"/*.timer; do
  base=$(basename "$f")
  # Only refresh units that are already installed on the box — this script
  # flips alerting, it does not enable new services (tax-forecast stays gated,
  # sellability stays disabled, per CLAUDE.md).
  if [ -f "$SYSD/$base" ]; then
    install -m 644 "$f" "$SYSD/$base"
    echo "  refreshed $base"
  fi
done

# Units with NO copy in deploy/ (the hand-installed serving stack:
# accounting-api, accounting-dashboard, caddy, cloudflared,
# accounting-uptime-check, backups, disk-check, …) are exactly the sev2
# class the webhook handler was written for — sed their OnFailure= in place
# so NOTHING keeps referencing the email template after it is retired.
echo "== flipping OnFailure= on box-only units =="
for f in "$SYSD"/*.service; do
  if grep -q "OnFailure=accounting-alert@%p.service" "$f"; then
    sed -i 's/OnFailure=accounting-alert@%p.service/OnFailure=accounting-alert-webhook@%p.service/' "$f"
    echo "  flipped $(basename "$f")"
  fi
done

systemctl daemon-reload

echo "== smoke test: one sev3 Telegram message MUST arrive =="
# Clear the smoke unit's hourly dedup sentinel so a same-hour re-run of this
# script still sends a real message (otherwise alert_webhook.py dedups it and
# a successful re-run looks like a broken webhook path).
rm -f "$REPO"/data/.alerts/alert-webhook-smoke-test-*.sent
if ! systemctl start accounting-alert-webhook@smoke-test.service; then
  echo "ABORT: smoke-test unit failed to start — webhook path is broken; NOT cut over cleanly" >&2
  systemctl --no-pager --lines=15 status accounting-alert-webhook@smoke-test.service >&2 || true
  exit 1
fi
result=$(systemctl show -p Result --value accounting-alert-webhook@smoke-test.service)
if [ "$result" != "success" ]; then
  echo "ABORT: smoke-test unit Result=$result — webhook path is broken; NOT cut over cleanly" >&2
  systemctl --no-pager --lines=15 status accounting-alert-webhook@smoke-test.service >&2 || true
  exit 1
fi
echo "  smoke-test unit succeeded — journal (confirm it says 'sent', not 'falling back to Resend email'):"
journalctl -u accounting-alert-webhook@smoke-test.service -n 5 --no-pager -o cat 2>/dev/null | sed 's/^/    /' || true

echo "== verifying no live unit still targets the email template =="
if grep -l "OnFailure=accounting-alert@%p.service" "$SYSD"/*.service 2>/dev/null | grep -v "alert-webhook"; then
  echo "ABORT: units above still reference the email template" >&2
  exit 1
fi
echo "  clean: all OnFailure= lines target accounting-alert-webhook@"

echo "== done. Rollback = sed OnFailure= back to accounting-alert@%p.service + daemon-reload (email template + Resend fallback stay installed) =="
