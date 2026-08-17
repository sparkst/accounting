#!/usr/bin/env bash
# Issue #53 — install/refresh the accounting-uptime-check units from the repo.
# Run as ROOT on the box AFTER `scripts/deploy_box.py --apply` synced the repo:
#
#   ssh root@ubuntu 'bash /home/travis/accounting/deploy/install_uptime_check.sh'
#
# Idempotent: copies deploy/accounting-uptime-check.{service,timer} into
# /etc/systemd/system, daemon-reloads (the running timer keeps its schedule;
# the new TimeoutStartSec applies from the next activation), and prints the
# effective start timeout so the change is verifiable in one line.
set -euo pipefail

REPO=/home/travis/accounting
DEPLOY="$REPO/deploy"
SYSD=/etc/systemd/system

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
for f in accounting-uptime-check.service accounting-uptime-check.timer; do
  [ -f "$DEPLOY/$f" ] || { echo "repo not synced: $DEPLOY/$f missing" >&2; exit 1; }
  install -m 644 "$DEPLOY/$f" "$SYSD/$f"
  echo "  installed $f"
done
systemctl daemon-reload
systemctl enable --now accounting-uptime-check.timer >/dev/null
echo "== effective =="
systemctl show accounting-uptime-check.service -p TimeoutStartUSec,Type --no-pager
systemctl list-timers accounting-uptime-check.timer --no-pager | head -3
