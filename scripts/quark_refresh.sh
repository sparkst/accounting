#!/usr/bin/env bash
# quark_refresh.sh — Lazy-load the latest financials for Quark (the Ferengi CFO skill).
#
# Production runs on the Hetzner box; the local data/accounting.db is a stale
# secondary. This script brings the books current and drops a READ-ONLY snapshot
# at data/accounting.live.db, which the Quark skill reads via DATABASE_PATH —
# never touching the local source-of-truth DB.
#
# Flow (best-effort; falls back to the local DB and SAYS SO rather than ever
# reporting stale/corrupt data as fresh):
#   1. (default) Trigger the box's daily Plaid sync jobs so every account is
#      current. Skip with --quick. These are the exact sanctioned prod jobs;
#      a single ITEM_LOGIN_REQUIRED makes a unit exit non-zero — logged, not fatal.
#   2. Take a consistent .backup snapshot on the box (WAL-safe), gzip, rsync down.
#   3. Decompress, verify integrity (PRAGMA quick_check), and report freshness.
#
# Contract — the LAST three stdout lines are always, on every exit path:
#   QUARK_DB=<path or empty>     # the DB Quark should read ("" => advisory-only)
#   QUARK_FRESH=<yes|no>         # yes only if pulled from the box AND books are recent
#   QUARK_ASOF=<YYYY-MM-DD|>     # newest non-rejected transaction date in that DB
# Everything else is human-readable log on STDERR.
#
# Usage:
#   bash scripts/quark_refresh.sh           # full: sync accounts, then pull (~1 min)
#   bash scripts/quark_refresh.sh --quick   # snapshot only, no sync (~15s)

set -uo pipefail

# --- Production identity is HARDCODED (not env-overridable) ----------------
# Deriving the host/user/repo from env would let a caller point the root-SSH
# systemctl call or the remote sqlite command at an arbitrary host or inject
# shell via a crafted repo path. The box is a stable, documented prod fact;
# if it ever moves, edit these three lines.
readonly BOX="ubuntu"                              # Tailscale host
readonly BOX_USER="travis"
readonly BOX_DB="/home/travis/accounting/data/accounting.db"
readonly STALE_AFTER_DAYS=3                         # books older than this => FRESH=no

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly LIVE_DB="$REPO_ROOT/data/accounting.live.db"
readonly LOCAL_DB="$REPO_ROOT/data/accounting.db"

DO_SYNC=1
[ "${1:-}" = "--quick" ] && DO_SYNC=0

SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
readonly SSH_RSH="ssh -o ConnectTimeout=10 -o BatchMode=yes"
log() { printf '  [quark-refresh] %s\n' "$*" >&2; }

mkdir -p "$REPO_ROOT/data"

# --- emit: the single exit point. Computes as-of, decides freshness, prints
#     the three-line contract, and exits 0. ----------------------------------
emit() {
  local db="$1" fresh="$2"
  if [ -z "$db" ] || [ ! -f "$db" ]; then
    log "✗ No usable DB — Quark must operate advisory-only on pasted numbers."
    printf 'QUARK_DB=\nQUARK_FRESH=no\nQUARK_ASOF=\n'
    exit 0
  fi
  local asof
  asof="$(sqlite3 "$db" "SELECT MAX(date) FROM transactions WHERE status!='rejected';" 2>/dev/null)" || asof=""
  if [ -z "$asof" ]; then
    fresh="no"
  else
    local cutoff
    cutoff="$(date -v-"${STALE_AFTER_DAYS}"d +%Y-%m-%d 2>/dev/null \
             || date -d "-${STALE_AFTER_DAYS} days" +%Y-%m-%d 2>/dev/null)"
    # If the cutoff can't be computed, conservatively report not-fresh.
    if [[ -z "$cutoff" || "$asof" < "$cutoff" ]]; then
      fresh="no"
    fi
  fi
  if [ "$fresh" = "yes" ]; then
    log "✓ Fresh books from the box — newest transaction $asof."
  else
    log "⚠ STALE/FALLBACK DB ($db) — newest transaction ${asof:-unknown}. Tell Travis the books aren't current."
  fi
  printf 'QUARK_DB=%s\nQUARK_FRESH=%s\nQUARK_ASOF=%s\n' "$db" "$fresh" "$asof"
  exit 0
}

# Always start from a clean slate so a prior run's snapshot can never be
# silently reused when this run fails to pull a new one.
rm -f "$LIVE_DB" "$LIVE_DB.gz"

# --- Reachability check ----------------------------------------------------
if ! ssh "${SSH_OPTS[@]}" "$BOX_USER@$BOX" 'true' 2>/dev/null; then
  log "⚠ Box ($BOX_USER@$BOX) unreachable — falling back to the LOCAL DB."
  emit "$LOCAL_DB" no
fi

# --- 1. Bring accounts current (sanctioned prod sync jobs) -----------------
if [ "$DO_SYNC" = "1" ]; then
  log "Syncing accounts on the box (balances + transactions)…"
  # oneshot units (RemainAfterExit=no): `start` blocks until ExecStart finishes.
  # A single Item needing re-auth exits non-zero by design — log and continue.
  if ssh "${SSH_OPTS[@]}" "root@$BOX" \
      'systemctl start --wait plaid-balance-sync.service plaid-transactions-sync.service'; then
    log "✓ Account sync complete."
  else
    log "⚠ A sync job reported an error (likely one Item needs re-auth). Continuing with latest available data."
  fi
else
  log "Quick mode: skipping account sync (snapshot reflects the last daily timer run)."
fi

# --- 2. Consistent snapshot on the box, then pull it down ------------------
# Remote does it all in one shell: mktemp (unique, no PID collision), .backup,
# gzip, print the gz path. A trap cleans the uncompressed temp on any failure.
# BOX_DB is a trusted constant, so interpolation here is safe.
log "Taking a WAL-safe snapshot on the box…"
remote_gz="$(ssh "${SSH_OPTS[@]}" "$BOX_USER@$BOX" "
  set -e
  t=\$(mktemp /tmp/quark-snap.XXXXXX.db)
  trap 'rm -f \"\$t\"' EXIT
  sqlite3 '$BOX_DB' \".backup \$t\"
  gzip -f \"\$t\"
  echo \"\$t.gz\"
")"
if [ -z "$remote_gz" ]; then
  log "⚠ Snapshot failed on the box — falling back to the LOCAL DB."
  emit "$LOCAL_DB" no
fi

log "Pulling snapshot down…"
remote_cleanup() { ssh "${SSH_OPTS[@]}" "$BOX_USER@$BOX" "rm -f '$remote_gz'" 2>/dev/null || true; }
if ! rsync -z -e "$SSH_RSH" "$BOX_USER@$BOX:$remote_gz" "$LIVE_DB.gz"; then
  log "⚠ Pull (rsync) failed — falling back to the LOCAL DB."
  remote_cleanup
  rm -f "$LIVE_DB.gz"
  emit "$LOCAL_DB" no
fi
remote_cleanup

# --- 3. Decompress + integrity-gate before trusting it --------------------
if ! gunzip -f "$LIVE_DB.gz"; then
  log "⚠ Decompress failed (partial/corrupt transfer) — falling back to the LOCAL DB."
  rm -f "$LIVE_DB" "$LIVE_DB.gz"
  emit "$LOCAL_DB" no
fi
qc="$(sqlite3 "$LIVE_DB" 'PRAGMA quick_check;' 2>/dev/null)" || qc="error"
if [ "$qc" != "ok" ]; then
  log "⚠ Integrity check failed on the pulled snapshot ($qc) — falling back to the LOCAL DB."
  rm -f "$LIVE_DB"
  emit "$LOCAL_DB" no
fi

# A .backup of a WAL-mode DB inherits WAL mode; a first-access `sqlite3 -readonly`
# on it fails with CANTOPEN because readonly can't create the -shm. Normalize the
# disposable snapshot to a self-contained rollback journal so every read just works.
sqlite3 "$LIVE_DB" 'PRAGMA journal_mode=DELETE;' >/dev/null 2>&1 || true

# --- Informative per-entity freshness report (stderr only) ----------------
log "Snapshot summary:"
sqlite3 "$LIVE_DB" \
  "SELECT COALESCE(NULLIF(entity,''),'(unassigned)'), COUNT(*), MAX(date)
   FROM transactions WHERE status!='rejected' GROUP BY 1 ORDER BY 1;" 2>/dev/null \
  | while IFS='|' read -r ent n thru; do log "    ${ent}  txns=${n}  through ${thru}"; done
nw_asof="$(sqlite3 "$LIVE_DB" "SELECT MAX(as_of) FROM account_balance_snapshot;" 2>/dev/null)"
[ -n "$nw_asof" ] && log "    wealth snapshots through $nw_asof"

emit "$LIVE_DB" yes
