#!/usr/bin/env bash
# restore.sh — Restore data/accounting.db from a backup file.
# Usage: bash scripts/restore.sh <path-to-backup.db>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DB_PATH="$REPO_ROOT/data/accounting.db"

# ── Args ─────────────────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
  echo "Usage: $(basename "$0") <backup-file>" >&2
  echo "Example: $(basename "$0") data/backups/accounting-2026-03-24-020000.db" >&2
  exit 1
fi

BACKUP_FILE="$1"

# Resolve relative paths against repo root so callers can use either form.
if [[ "$BACKUP_FILE" != /* ]]; then
  BACKUP_FILE="$REPO_ROOT/$BACKUP_FILE"
fi

# ── Preflight: backup file ────────────────────────────────────────────────────

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "ERROR: backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

echo "[restore] Verifying backup integrity: $BACKUP_FILE …"
RESULT="$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>&1)"
if [[ "$RESULT" != "ok" ]]; then
  echo "ERROR: integrity_check on backup failed: $RESULT" >&2
  exit 1
fi
echo "[restore] Backup integrity OK."

# ── Stop the API server if running ───────────────────────────────────────────

UVICORN_PID="$(lsof -ti tcp:8000 2>/dev/null || true)"
if [[ -n "$UVICORN_PID" ]]; then
  echo "[restore] Stopping process on port 8000 (PID $UVICORN_PID) …"
  kill "$UVICORN_PID" 2>/dev/null || true
  # Give it a moment to release the file lock.
  sleep 1
  echo "[restore] Server stopped."
else
  echo "[restore] No server running on port 8000."
fi

# ── Restore ───────────────────────────────────────────────────────────────────

# Back up the current database first so an accidental restore is recoverable.
if [[ -f "$DB_PATH" ]]; then
  SAFETY_COPY="$DB_PATH.pre-restore-$(date '+%Y%m%d-%H%M%S')"
  echo "[restore] Saving current database as safety copy: $SAFETY_COPY …"
  cp "$DB_PATH" "$SAFETY_COPY"
fi

# Remove any leftover WAL / SHM files so SQLite starts clean.
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"

echo "[restore] Copying backup to $DB_PATH …"
cp "$BACKUP_FILE" "$DB_PATH"

# ── Verify the restored database ─────────────────────────────────────────────

echo "[restore] Verifying restored database …"
RESULT="$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;" 2>&1)"
if [[ "$RESULT" != "ok" ]]; then
  echo "ERROR: integrity_check on restored database failed: $RESULT" >&2
  echo "       Safety copy is at: $SAFETY_COPY" >&2
  exit 1
fi

echo "[restore] Success. Database restored from: $BACKUP_FILE"
echo "[restore] Safety copy of previous database: ${SAFETY_COPY:-none}"
echo "[restore] Start the API server with: uvicorn src.api.main:app --reload --port 8000"
