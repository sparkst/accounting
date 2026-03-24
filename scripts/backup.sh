#!/usr/bin/env bash
# backup.sh — SQLite backup for data/accounting.db
# Creates a versioned backup in data/backups/, keeps last 30, exits non-zero on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DB_PATH="$REPO_ROOT/data/accounting.db"
BACKUP_DIR="$REPO_ROOT/data/backups"
TIMESTAMP="$(date '+%Y-%m-%d-%H%M%S')"
BACKUP_PATH="$BACKUP_DIR/accounting-$TIMESTAMP.db"
KEEP=30

# ── Preflight ────────────────────────────────────────────────────────────────

if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: database not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

# ── WAL checkpoint ───────────────────────────────────────────────────────────
# Flush all WAL pages into the main database file before copying.

echo "[backup] Running WAL checkpoint on $DB_PATH …"
sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" || {
  echo "ERROR: WAL checkpoint failed" >&2
  exit 1
}

# ── Backup ───────────────────────────────────────────────────────────────────
# sqlite3 .backup uses the Online Backup API — safe even with a live database.

echo "[backup] Writing backup to $BACKUP_PATH …"
sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH'" || {
  echo "ERROR: sqlite3 .backup failed" >&2
  exit 1
}

# ── Verify the backup is a valid SQLite file ─────────────────────────────────

echo "[backup] Verifying integrity …"
RESULT="$(sqlite3 "$BACKUP_PATH" "PRAGMA integrity_check;" 2>&1)"
if [[ "$RESULT" != "ok" ]]; then
  echo "ERROR: integrity_check on backup failed: $RESULT" >&2
  rm -f "$BACKUP_PATH"
  exit 1
fi

echo "[backup] Backup verified OK: $BACKUP_PATH"

# ── Rotate: keep only the most recent $KEEP backups ──────────────────────────

BACKUP_COUNT="$(ls -1 "$BACKUP_DIR"/accounting-*.db 2>/dev/null | wc -l | tr -d ' ')"
if (( BACKUP_COUNT > KEEP )); then
  DELETE_COUNT=$(( BACKUP_COUNT - KEEP ))
  echo "[backup] Rotating — removing $DELETE_COUNT old backup(s) …"
  ls -1t "$BACKUP_DIR"/accounting-*.db | tail -n "$DELETE_COUNT" | xargs rm -f
fi

REMAINING="$(ls -1 "$BACKUP_DIR"/accounting-*.db 2>/dev/null | wc -l | tr -d ' ')"
echo "[backup] Done. $REMAINING backup(s) retained in $BACKUP_DIR"
