#!/usr/bin/env bash
# backup.sh — consistent SQLite snapshot → integrity_check → versioned R2 upload.
# REQ-HM-006: disk-free gate, flock serialization, integrity BEFORE upload,
# etag verify, per-table row-count metadata, dead-man ping, in-progress sentinel.
#
# Retention (daily 14d + weekly 8w) is handled by an R2 bucket lifecycle rule
# OR a separate prune pass — NOT in this script to avoid fragile in-script prune.
#
# Testable env hooks:
#   REPO_ROOT_OVERRIDE      — override repo root (for tests)
#   R2_DISABLE=1            — skip R2 upload + etag verify (offline/test mode)
#   DISK_FREE_GB_OVERRIDE   — inject disk-free GB (skip real df call; for tests)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
DB_PATH="$REPO_ROOT/data/accounting.db"
LOCK="$REPO_ROOT/data/.backup.lock"
SENTINEL="$REPO_ROOT/data/.backup.in-progress"
TS="$(date -u '+%Y-%m-%dT%H%M%SZ')"
TMP_SNAP="$(mktemp "${TMPDIR:-/tmp}/accounting-backup.XXXXXX.db")"
MIN_FREE_GB=5

# ── Cleanup trap ─────────────────────────────────────────────────────────────
cleanup() {
  local exit_code=$?
  rm -f "$TMP_SNAP"
  # If we exit non-zero unexpectedly, ensure sentinel is removed so stale
  # sentinels don't accumulate (belt-and-suspenders; normal failure paths also
  # remove it explicitly).
  if [ $exit_code -ne 0 ]; then
    rm -f "$SENTINEL"
  fi
}
trap cleanup EXIT

# ── Disk-free pre-check ───────────────────────────────────────────────────────
# Abort + alert if < 5 GB free. DISK_FREE_GB_OVERRIDE injects the value for
# tests (avoids depending on real df output, which is OS-specific).
if [ -n "${DISK_FREE_GB_OVERRIDE:-}" ]; then
  free_gb="$DISK_FREE_GB_OVERRIDE"
else
  # df -BG is GNU-specific; fine on Linux (production box).
  free_gb="$(df -BG --output=avail "$REPO_ROOT/data" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)"
fi

if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  echo "ERROR: only ${free_gb} GB free (< ${MIN_FREE_GB} GB) on data/ — aborting backup" >&2
  exit 1
fi

# ── Preflight ────────────────────────────────────────────────────────────────
[ -f "$DB_PATH" ] || { echo "ERROR: db not found at $DB_PATH" >&2; exit 1; }

mkdir -p "$REPO_ROOT/data"

# ── flock: serialize against concurrent backup runs ──────────────────────────
# flock is a Linux util (util-linux); absent on macOS dev. If unavailable,
# skip locking gracefully — production (Linux) always has it.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock 9
fi

# ── Sentinel: marks backup in progress ───────────────────────────────────────
touch "$SENTINEL"

# ── WAL checkpoint ───────────────────────────────────────────────────────────
sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null

# ── Snapshot via Online Backup API ───────────────────────────────────────────
sqlite3 "$DB_PATH" ".backup '$TMP_SNAP'"

# ── Integrity check BEFORE upload ────────────────────────────────────────────
# Never overwrite a known-good R2 object with a corrupt snapshot.
res="$(sqlite3 "$TMP_SNAP" 'PRAGMA integrity_check;' 2>&1 || true)"
if [ "$res" != "ok" ]; then
  echo "ERROR: integrity_check failed on snapshot: $res" >&2
  rm -f "$SENTINEL"
  exit 1
fi

# ── Per-table row counts → object metadata ───────────────────────────────────
_count() { sqlite3 "$TMP_SNAP" "SELECT count(*) FROM $1;" 2>/dev/null || echo 0; }

# sha256sum (GNU coreutils, Linux) with shasum -a 256 (macOS) fallback.
if command -v sha256sum >/dev/null 2>&1; then
  sha="$(sha256sum "$TMP_SNAP" | cut -d' ' -f1)"
else
  sha="$(shasum -a 256 "$TMP_SNAP" | cut -d' ' -f1)"
fi

META="rows-transactions=$(_count transactions),rows-audit_events=$(_count audit_events),rows-invoices=$(_count invoices),cutover-ts=${CUTOVER_TS:-},sha256=${sha}"
OBJECT_KEY="daily/accounting-${TS}.db"

# ── R2 upload ────────────────────────────────────────────────────────────────
if [ "${R2_DISABLE:-0}" = "1" ]; then
  echo "[backup] R2_DISABLE=1 — skipping upload (test/offline mode). key=$OBJECT_KEY meta=$META"
else
  # R2 exposes an S3-compatible API; creds come from env via doppler run.
  # Required env: R2_ENDPOINT, R2_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY.
  upload_etag="$(aws s3api put-object \
      --endpoint-url "$R2_ENDPOINT" \
      --bucket "$R2_BUCKET" \
      --key "$OBJECT_KEY" \
      --body "$TMP_SNAP" \
      --metadata "$META" \
      --query ETag \
      --output text)"

  # Verify: R2 returns the md5 of the uploaded object as the ETag (quoted).
  if command -v md5sum >/dev/null 2>&1; then
    local_md5="\"$(md5sum "$TMP_SNAP" | cut -d' ' -f1)\""
  else
    local_md5="\"$(md5 -q "$TMP_SNAP")\""
  fi

  if [ "$upload_etag" != "$local_md5" ]; then
    echo "ERROR: R2 etag mismatch (got $upload_etag want $local_md5)" >&2
    rm -f "$SENTINEL"
    exit 1
  fi

  # Dead-man healthcheck ping on success (optional; no-op if URL not set).
  [ -n "${HEALTHCHECK_PING_URL:-}" ] && \
    curl -fsS --max-time 10 "$HEALTHCHECK_PING_URL" >/dev/null || true
fi

# ── Done ─────────────────────────────────────────────────────────────────────
rm -f "$SENTINEL"
echo "[backup] OK: $OBJECT_KEY"
