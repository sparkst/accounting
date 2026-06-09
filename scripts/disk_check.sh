#!/bin/sh
# disk_check.sh — exit 1 if < 5 GB free on the accounting data dir (REQ-HM-014).
# External script because systemd's ExecStart C-tokenizer mangles inline shell.
# DISK_FREE_GB_OVERRIDE injects the value for tests (skips the OS-specific df).
DIR="${ACCOUNTING_DATA_DIR:-/home/travis/accounting/data}"
if [ -n "${DISK_FREE_GB_OVERRIDE:-}" ]; then
  free="$DISK_FREE_GB_OVERRIDE"
else
  free="$(df -BG --output=avail "$DIR" 2>/dev/null | tail -1 | tr -dc '0-9')"
fi
[ -n "$free" ] || { echo "disk_check: could not determine free space on $DIR" >&2; exit 1; }
if [ "$free" -ge 5 ]; then
  echo "disk ok: ${free} GB free on $DIR"
  exit 0
fi
echo "disk low: ${free} GB free on $DIR (< 5 GB)" >&2
exit 1
