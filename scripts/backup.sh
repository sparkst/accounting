#!/usr/bin/env bash
# backup.sh — consistent SQLite snapshot → integrity_check → wrangler r2 object;
# readback-sha verify; deterministic date keys + .meta.json sidecar;
# rolling 15-day delete via wrangler r2 object delete (no object listing needed).
# REQ-HM-006: disk-free gate, flock serialization, integrity BEFORE upload,
# readback-sha verify, per-table row-count sidecar, dead-man ping, in-progress
# sentinel.
#
# Auth: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID injected by doppler run.
# Required runtime env: R2_BUCKET, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
# Optional: HEALTHCHECK_PING_URL, CUTOVER_TS, WRANGLER_BIN, R2_DISABLE.
#
# Object key design (deterministic — no listing needed):
#   daily/accounting-<YYYY-MM-DD>.db         — snapshot
#   daily/accounting-<YYYY-MM-DD>.meta.json  — sidecar with row counts + sha256
#
# NOTE: An R2 bucket lifecycle rule is the preferred long-term mechanism for
# tiered retention (daily 15d + weekly 8w). The 15-day rolling delete below is
# belt-and-suspenders only; remove it once a lifecycle rule is configured.
#
# Testable env hooks:
#   REPO_ROOT_OVERRIDE      — override repo root (for tests)
#   R2_DISABLE=1            — skip wrangler upload + readback verify (offline/test mode)
#   DISK_FREE_GB_OVERRIDE   — inject disk-free GB (skip real df call; for tests)
#   WRANGLER_BIN            — override wrangler binary path (for tests)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
DB_PATH="$REPO_ROOT/data/accounting.db"
LOCK="$REPO_ROOT/data/.backup.lock"
SENTINEL="$REPO_ROOT/data/.backup.in-progress"
TS="$(date -u '+%Y-%m-%dT%H%M%SZ')"
TMP_SNAP="$(mktemp "${TMPDIR:-/tmp}/accounting-backup.XXXXXX.db")"
MIN_FREE_GB=5
WRANGLER="${WRANGLER_BIN:-wrangler}"

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

# ── Per-table row counts → sidecar metadata ──────────────────────────────────
_count() { sqlite3 "$TMP_SNAP" "SELECT count(*) FROM $1;" 2>/dev/null || echo 0; }

# sha256sum (GNU coreutils, Linux) with shasum -a 256 (macOS) fallback.
if command -v sha256sum >/dev/null 2>&1; then
  sha="$(sha256sum "$TMP_SNAP" | cut -d' ' -f1)"
else
  sha="$(shasum -a 256 "$TMP_SNAP" | cut -d' ' -f1)"
fi

# ── R2 upload via wrangler ────────────────────────────────────────────────────
DATE="$(date -u +%Y-%m-%d)"
DB_KEY="daily/accounting-${DATE}.db"
META_KEY="daily/accounting-${DATE}.meta.json"

# Build sidecar meta JSON (written to a temp file so wrangler can --file it).
META_TMP="$(mktemp "${TMPDIR:-/tmp}/accounting-meta.XXXXXX.json")"
cat > "$META_TMP" <<EOF
{"rows-transactions": $(_count transactions), "rows-audit_events": $(_count audit_events), "rows-invoices": $(_count invoices), "sha256": "${sha}", "cutover-ts": "${CUTOVER_TS:-}", "created-utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

if [ "${R2_DISABLE:-0}" = "1" ]; then
  echo "[backup] R2_DISABLE=1 — skipping wrangler upload (test mode). key=$DB_KEY"
  rm -f "$META_TMP"
else
  # Upload DB snapshot.
  "$WRANGLER" r2 object put "$R2_BUCKET/$DB_KEY" --file="$TMP_SNAP" --remote \
    || { echo "ERROR: r2 put db failed" >&2; rm -f "$SENTINEL" "$META_TMP"; exit 1; }

  # Upload JSON sidecar.
  "$WRANGLER" r2 object put "$R2_BUCKET/$META_KEY" --file="$META_TMP" --remote \
    || { echo "ERROR: r2 put meta failed" >&2; rm -f "$SENTINEL" "$META_TMP"; exit 1; }
  rm -f "$META_TMP"

  # ── Readback-verify: download the db we just wrote, compare sha256 ──────────
  VERIFY_TMP="$(mktemp "${TMPDIR:-/tmp}/accounting-verify.XXXXXX.db")"
  "$WRANGLER" r2 object get "$R2_BUCKET/$DB_KEY" --file="$VERIFY_TMP" --remote \
    || { echo "ERROR: r2 readback get failed" >&2; rm -f "$SENTINEL" "$VERIFY_TMP"; exit 1; }
  if command -v sha256sum >/dev/null 2>&1; then
    vsha="$(sha256sum "$VERIFY_TMP" | cut -d' ' -f1)"
  else
    vsha="$(shasum -a 256 "$VERIFY_TMP" | cut -d' ' -f1)"
  fi
  rm -f "$VERIFY_TMP"
  if [ "$vsha" != "$sha" ]; then
    echo "ERROR: R2 readback sha mismatch (got $vsha want $sha)" >&2
    rm -f "$SENTINEL"
    exit 1
  fi

  # ── Rolling retention: delete daily objects from 15 days ago ─────────────────
  # Uses GNU date -d (Linux box); silently skips if unsupported or object absent.
  # NOTE: an R2 bucket lifecycle rule is preferred for long-term tiered retention
  # (daily 15d + weekly 8w) — this in-script delete is belt-and-suspenders only.
  if OLD="$(date -u -d '15 days ago' +%Y-%m-%d 2>/dev/null)"; then
    "$WRANGLER" r2 object delete "$R2_BUCKET/daily/accounting-${OLD}.db" --remote 2>/dev/null || true
    "$WRANGLER" r2 object delete "$R2_BUCKET/daily/accounting-${OLD}.meta.json" --remote 2>/dev/null || true
  fi

  # Dead-man healthcheck ping on success (optional; no-op if URL not set).
  [ -n "${HEALTHCHECK_PING_URL:-}" ] && \
    curl -fsS --max-time 10 "$HEALTHCHECK_PING_URL" >/dev/null 2>&1 || true
fi

# ── Done ─────────────────────────────────────────────────────────────────────
rm -f "$SENTINEL"
echo "[backup] OK: $DB_KEY"
