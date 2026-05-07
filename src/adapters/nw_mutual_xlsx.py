"""Northwestern Mutual whole-life XLSX importer (Phase 4 — Adapter 3).

Reads the single-sheet ``allAccounts.xlsx`` Northwestern Mutual exports, with
columns ``Insured | Account Number | Net Death Benefit | Annualized Premium |
Last Annual Dividend | Loans | Net Accumulated Value``. Writes one
:class:`AccountBalanceSnapshot` per policy, using ``Net Accumulated Value`` as
the balance.

Key behaviors (matching IDEATION.md and the canonical
``xlsx_savings_plan.py`` pattern):

- Policies whose ``Net Accumulated Value`` is the literal string ``"N/A"``
  (NW Mutual's marker for term-only policies with no cash value) are
  skip-with-warning — appended to ``ImportResult.errors`` so the operator
  sees them, but the batch continues.
- Account-id lookup is by ``(broker='nw_mutual', account_number=<policy>)``.
  Missing Account rows produce a per-row error; the adapter does NOT
  auto-create accounts (operator must seed via ``seed_expected_accounts``).
- ``as_of`` defaults to the file's filesystem mtime (truncated to date).
  CLI ``--as-of YYYY-MM-DD`` overrides.
- Idempotent: rerunning the same workbook for the same ``as_of`` produces
  zero new rows because ``source_row_hash`` collisions are caught.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.money import parse_currency, quantize_balance
from src.models.brokerage import Account
from src.models.enums import IngestionStatus
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_TAG = "nw_mutual_xlsx"
"""Value written to ``account_balance_snapshot.source``."""

ADAPTER_NAME = "nw_mutual_xlsx"
"""Identifier written to ``ingestion_log.source``."""

SHEET_NAME = "Life Insurance"
NW_MUTUAL_BROKER = "nw_mutual"

# Sentinel string NW Mutual writes for policies with no cash value (term-only).
_NA_MARKERS = frozenset({"N/A", "n/a", "NA", "na", "--", "-"})


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class ImportResult:
    """Summary of an import run."""

    parsed: int = 0
    """Total policy rows parsed from the workbook."""

    would_insert: int = 0
    """Rows that would be inserted in --apply (parsed minus N/A skips)."""

    imported: int = 0
    """Newly inserted snapshot rows (apply mode only)."""

    dup_skipped: int = 0
    """Rows skipped because an equivalent snapshot already exists."""

    errors: list[str] = field(default_factory=list)
    """Per-row error/warning strings (record_label: message)."""


# ── Parsing ──────────────────────────────────────────────────────────────────


def _is_na(value: object) -> bool:
    """True if a Net Accumulated Value cell is NW Mutual's "no cash value" marker."""
    if value is None:
        return True
    return isinstance(value, str) and value.strip() in _NA_MARKERS


def _coerce_money(value: object) -> Decimal:
    """Parse a money cell that is *known* to be numeric (not N/A)."""
    return parse_currency(value)


def parse_workbook(path: Path) -> list[dict[str, Any]]:
    """Read the workbook and return one dict per policy row.

    Each dict carries ``policy_number``, ``insured``, and the five money fields
    as ``Decimal`` (``net_accum_value`` is ``None`` when the cell is ``"N/A"``).
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise ValueError(f"workbook missing sheet '{SHEET_NAME}'")
    ws = wb[SHEET_NAME]

    out: list[dict[str, Any]] = []
    for row_idx in range(2, ws.max_row + 1):
        insured_cell = ws.cell(row=row_idx, column=1).value
        policy_cell = ws.cell(row=row_idx, column=2).value
        if insured_cell is None and policy_cell is None:
            continue
        insured = str(insured_cell).strip() if insured_cell is not None else ""
        policy_number = str(policy_cell).strip() if policy_cell is not None else ""
        if not policy_number:
            continue

        nav_raw = ws.cell(row=row_idx, column=7).value
        net_accum_value = None if _is_na(nav_raw) else _coerce_money(nav_raw)

        out.append(
            {
                "policy_number": policy_number,
                "insured": insured,
                "net_death_benefit": _coerce_money(ws.cell(row=row_idx, column=3).value),
                "annualized_premium": _coerce_money(ws.cell(row=row_idx, column=4).value),
                "last_annual_dividend": _coerce_money(ws.cell(row=row_idx, column=5).value),
                "loans": _coerce_money(ws.cell(row=row_idx, column=6).value),
                "net_accum_value": net_accum_value,
            }
        )
    return out


# ── Hashing ──────────────────────────────────────────────────────────────────


def _row_hash(policy_number: str, as_of: date, balance: Decimal) -> str:
    """SHA256 of the canonical row identity tuple."""
    payload = "|".join(
        (policy_number, as_of.isoformat(), str(quantize_balance(balance)))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Import ───────────────────────────────────────────────────────────────────


def import_balances(
    path: Path,
    *,
    dry_run: bool = True,
    session: Session | None = None,
    as_of: date | None = None,
) -> ImportResult:
    """Import NW Mutual policy balances into ``account_balance_snapshot``.

    Args:
        path:    Path to the XLSX workbook.
        dry_run: When True, parse-and-count only; never write to ``session``.
                 Default True to protect the live DB during exploration.
        session: SQLAlchemy session. Required when ``dry_run`` is False.
        as_of:   Snapshot date. Defaults to the file's mtime truncated to date.

    Returns:
        :class:`ImportResult` with counts and per-row errors/warnings.
    """
    result = ImportResult()
    path = Path(path)
    if as_of is None:
        as_of = date.fromtimestamp(path.stat().st_mtime)

    try:
        rows = parse_workbook(path)
    except Exception as exc:  # noqa: BLE001 — surface workbook-level failures
        result.errors.append(f"parse_workbook failed: {exc}")
        if session is not None and not dry_run:
            _log_run(session, result, status=IngestionStatus.FAILURE,
                     error_detail=result.errors[-1])
        return result

    result.parsed = len(rows)

    # Partition into writable rows + N/A skips with warning.
    writable: list[dict[str, Any]] = []
    for row in rows:
        if row["net_accum_value"] is None:
            result.errors.append(
                f"policy {row['policy_number']} ({row['insured']}): "
                "Net Accumulated Value is N/A — skipped"
            )
            continue
        writable.append(row)
    result.would_insert = len(writable)

    if dry_run:
        return result

    if session is None:
        result.errors.append("session required when dry_run=False")
        return result

    # ── Apply ────────────────────────────────────────────────────────────────
    for row in writable:
        policy_number = row["policy_number"]
        insured = row["insured"]
        balance: Decimal = row["net_accum_value"]
        raw_account_name = f"NW Mutual {insured} {policy_number}".strip()
        record_label = f"{raw_account_name}@{as_of.isoformat()}"
        row_hash = _row_hash(policy_number, as_of, balance)

        # Idempotency check by the dedup hash.
        existing = (
            session.query(AccountBalanceSnapshot.id)
            .filter(AccountBalanceSnapshot.source_row_hash == row_hash)
            .first()
        )
        if existing is not None:
            result.dup_skipped += 1
            continue

        # Resolve the Account row (NW Mutual broker, account_number == policy).
        account = (
            session.query(Account)
            .filter(
                Account.broker == NW_MUTUAL_BROKER,
                Account.account_number == policy_number,
            )
            .first()
        )
        if account is None:
            result.errors.append(
                f"policy {policy_number}: no Account row "
                f"(broker={NW_MUTUAL_BROKER}, account_number={policy_number})"
            )
            continue

        try:
            with session.begin_nested():
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        raw_account_name=raw_account_name,
                        as_of=as_of,
                        balance=balance,
                        source=SOURCE_TAG,
                        source_row_hash=row_hash,
                    )
                )
            result.imported += 1
        except IntegrityError:
            # Natural-key UNIQUE collision — same logical row already exists.
            result.dup_skipped += 1
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{record_label}: {exc}")
            logger.warning("nw_mutual_xlsx: row %s failed: %s",
                           record_label, exc, exc_info=True)

    session.commit()

    # ── Audit log ────────────────────────────────────────────────────────────
    status = (
        IngestionStatus.SUCCESS
        if not result.errors
        else IngestionStatus.PARTIAL_FAILURE
    )
    _log_run(session, result, status=status,
             error_detail="\n".join(result.errors) or None)
    return result


def _log_run(
    session: Session,
    result: ImportResult,
    *,
    status: IngestionStatus,
    error_detail: str | None,
) -> None:
    """Record an IngestionLog entry. Failures here are swallowed (log-only)."""
    try:
        log = IngestionLog(
            source=ADAPTER_NAME,
            status=status.value,
            records_processed=result.imported + result.dup_skipped,
            records_failed=len(result.errors),
            error_detail=error_detail,
        )
        session.add(log)
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to write IngestionLog for %s", ADAPTER_NAME)
        with contextlib.suppress(Exception):
            session.rollback()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.adapters.nw_mutual_xlsx",
        description="Import Northwestern Mutual whole-life balances from XLSX.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser(
        "import",
        help="Read allAccounts.xlsx and import policy balance snapshots.",
    )
    s.add_argument("--file", required=True, help="Path to the XLSX workbook.")
    s.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the live DB. Default is dry-run.",
    )
    s.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Snapshot date YYYY-MM-DD. Defaults to file mtime.",
    )
    return p


def _print_summary(result: ImportResult, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"=== nw_mutual_xlsx ({mode}) ===")
    print(f"  parsed       : {result.parsed}")
    print(f"  would_insert : {result.would_insert}")
    print(f"  imported     : {result.imported}")
    print(f"  dup_skipped  : {result.dup_skipped}")
    print(f"  errors       : {len(result.errors)}")
    if result.errors:
        print("  --- error/warning detail ---")
        for e in result.errors[:20]:
            print(f"    * {e}")
        if len(result.errors) > 20:
            print(f"    ... {len(result.errors) - 20} more")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "import":
        return 2

    file_path = Path(args.file)
    as_of: date | None = None
    if args.as_of is not None:
        as_of = date.fromisoformat(args.as_of)

    dry_run = not args.apply

    if dry_run:
        result = import_balances(file_path, dry_run=True, as_of=as_of)
        _print_summary(result, dry_run=True)
        return 0

    try:
        from src.db.connection import get_session  # late import to keep tests light
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_balances(
            file_path, dry_run=False, session=session, as_of=as_of
        )
    _print_summary(result, dry_run=False)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via CLI
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1) from None


__all__ = [
    "ADAPTER_NAME",
    "ImportResult",
    "SOURCE_TAG",
    "import_balances",
    "parse_workbook",
]
