"""GSK cash-balance pension PDF adapter (Phase 4 — Adapter 4 / T5).

Source file: ``GSK Cash Balance Account Activity.pdf`` — a single-page PDF
with one ``Closing Balance as of <Mon DD, YYYY> $<balance>`` line.

The PDF carries no natural account identifier (Travis has one cash-balance
pension), so a synthetic ``account_number = "GSK_PENSION"`` is used. The
operator pre-seeds an :class:`Account` row with
``(broker='gsk_pension', account_number='GSK_PENSION')`` via
``scripts/seed_expected_accounts confirm``; this adapter errors per-record if
that row is missing rather than auto-creating it (Phase-4 design — see
``proposals/brokerage-phase4/IDEATION.md``).

Output: one :class:`AccountBalanceSnapshot` per PDF. Idempotent across
re-imports via ``source_row_hash`` (sha256 over account_number, as_of,
quantized balance) backed by the table's UNIQUE constraint.

Pattern: follows ``src/adapters/xlsx_savings_plan.py`` line-for-line —
``ImportResult`` dataclass, ``dry_run=True`` default, ``session.begin_nested()``
per-row savepoint, ``IngestionLog`` row on apply, ``--apply`` opt-in CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.money import parse_currency, quantize_balance
from src.adapters._shared.pdf import pdftotext_layout
from src.models.brokerage import Account
from src.models.enums import IngestionStatus
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_TAG = "gsk_pdf"
"""Value written to ``account_balance_snapshot.source``."""

ADAPTER_NAME = "gsk_pdf"
"""Identifier written to ``ingestion_log.source``."""

GSK_BROKER = "gsk_pension"
"""Broker enum value used to look up the pre-seeded Account row."""

GSK_ACCOUNT_NUMBER = "GSK_PENSION"
"""Synthetic account_number — the PDF carries no natural identifier."""

GSK_RAW_ACCOUNT_NAME = "GSK Cash Balance Pension Plan"
"""Audit string preserved on every snapshot row."""

# Single regex on the layout text. The PDF uses month names like
# "May 7, 2026" and dollar amounts with comma thousands separators.
_CLOSING_BALANCE_RE = re.compile(
    r"Closing Balance as of\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+\$([\d,]+\.\d\d)"
)
_DATE_FORMAT = "%b %d, %Y"


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class ImportResult:
    """Summary of an import run.

    ``parsed`` and ``would_insert`` are populated in dry-run; ``imported``,
    ``dup_skipped``, ``matched`` are populated on apply. ``errors`` collects
    per-record failure strings (PDF parse, missing Account, etc.).
    """

    parsed: int = 0
    """PDFs whose closing-balance regex matched."""

    would_insert: int = 0
    """Snapshot rows that *would* be written on apply (dry-run only)."""

    imported: int = 0
    """Newly inserted snapshot rows."""

    matched: int = 0
    """Rows whose Account row was found and FK populated."""

    dup_skipped: int = 0
    """Rows skipped because an equivalent snapshot already exists."""

    errors: list[str] = field(default_factory=list)
    """Per-record error strings."""


# ── Pure parsing ─────────────────────────────────────────────────────────────


def extract_closing_balance(text: str) -> tuple[date, Decimal]:
    """Parse ``(as_of, balance)`` from a GSK PDF's layout text.

    Raises ``ValueError`` if the closing-balance line is missing or malformed.
    """
    match = _CLOSING_BALANCE_RE.search(text)
    if match is None:
        raise ValueError(
            "Closing Balance line not found in GSK PDF text"
        )
    date_str, balance_str = match.group(1), match.group(2)
    try:
        as_of = datetime.strptime(date_str, _DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(
            f"unparseable Closing Balance date {date_str!r}: {exc}"
        ) from exc
    balance = parse_currency(balance_str)
    return as_of, balance


# ── Hashing ──────────────────────────────────────────────────────────────────


def _row_hash(account_number: str, as_of: date, balance: Decimal) -> str:
    """SHA256 hex of the canonical (account_number, as_of, balance) tuple."""
    payload = "|".join(
        (account_number, as_of.isoformat(), str(quantize_balance(balance)))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Core import ──────────────────────────────────────────────────────────────


def import_pdf(
    path: Path,
    *,
    dry_run: bool = True,
    session: Session | None = None,
) -> ImportResult:
    """Import a single GSK Cash Balance PDF.

    Args:
        path:    Path to the PDF.
        dry_run: When True, parse-and-count only; never write to ``session``.
                 Default True to protect the live DB.
        session: SQLAlchemy session. Required when ``dry_run`` is False.

    Returns:
        :class:`ImportResult` with counts and per-record errors.
    """
    result = ImportResult()
    record_label = f"{GSK_ACCOUNT_NUMBER}@{path.name}"

    # ── Extract ──────────────────────────────────────────────────────────────
    try:
        text = pdftotext_layout(path)
        as_of, balance = extract_closing_balance(text)
    except Exception as exc:  # noqa: BLE001 — per-record isolation
        result.errors.append(f"{record_label}: {exc}")
        logger.warning("gsk_pdf: extraction failed for %s: %s",
                       path, exc, exc_info=True)
        if session is not None and not dry_run:
            _log_run(session, result, status=IngestionStatus.FAILURE,
                     error_detail="\n".join(result.errors))
        return result

    result.parsed = 1

    if dry_run:
        result.would_insert = 1
        return result

    if session is None:
        result.errors.append("session required when dry_run=False")
        return result

    # ── Apply ────────────────────────────────────────────────────────────────
    account = (
        session.query(Account)
        .filter(
            Account.broker == GSK_BROKER,
            Account.account_number == GSK_ACCOUNT_NUMBER,
        )
        .first()
    )
    if account is None:
        result.errors.append(
            f"{record_label}: no Account row for "
            f"(broker={GSK_BROKER!r}, account_number={GSK_ACCOUNT_NUMBER!r}); "
            "seed via scripts/seed_expected_accounts confirm"
        )
        _log_run(session, result, status=IngestionStatus.FAILURE,
                 error_detail="\n".join(result.errors))
        return result

    row_hash = _row_hash(GSK_ACCOUNT_NUMBER, as_of, balance)

    existing = (
        session.query(AccountBalanceSnapshot.id)
        .filter(AccountBalanceSnapshot.source_row_hash == row_hash)
        .first()
    )
    if existing is not None:
        result.dup_skipped += 1
    else:
        try:
            with session.begin_nested():
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        raw_account_name=GSK_RAW_ACCOUNT_NAME,
                        as_of=as_of,
                        balance=balance,
                        source=SOURCE_TAG,
                        source_row_hash=row_hash,
                    )
                )
            result.imported += 1
            result.matched += 1
        except IntegrityError:
            # Natural-key UNIQUE collision — same logical snapshot exists.
            result.dup_skipped += 1
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{record_label}: {exc}")
            logger.warning("gsk_pdf: row %s failed: %s",
                           record_label, exc, exc_info=True)

    session.commit()

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
        prog="python -m src.adapters.gsk_pdf",
        description="Import a GSK Cash Balance pension PDF as one snapshot.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import", help="Parse one PDF and (optionally) write a snapshot.")
    s.add_argument("--file", required=True, help="Path to the GSK PDF.")
    s.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the live DB. Default is dry-run.",
    )
    return p


def _print_summary(result: ImportResult, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"=== gsk_pdf ({mode}) ===")
    print(f"  parsed       : {result.parsed}")
    print(f"  would_insert : {result.would_insert}")
    print(f"  imported     : {result.imported}")
    print(f"  matched      : {result.matched}")
    print(f"  dup_skipped  : {result.dup_skipped}")
    print(f"  errors       : {len(result.errors)}")
    if result.errors:
        print("  --- error detail ---")
        for e in result.errors:
            print(f"    * {e}")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "import":
        return 2

    dry_run = not args.apply
    pdf = Path(args.file)

    if dry_run:
        result = import_pdf(pdf, dry_run=True)
        _print_summary(result, dry_run=True)
        return 0

    try:
        from src.db.connection import get_session  # late import to keep tests light
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_pdf(pdf, dry_run=False, session=session)

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
    "GSK_ACCOUNT_NUMBER",
    "GSK_BROKER",
    "GSK_RAW_ACCOUNT_NAME",
    "SOURCE_TAG",
    "ImportResult",
    "extract_closing_balance",
    "import_pdf",
]
