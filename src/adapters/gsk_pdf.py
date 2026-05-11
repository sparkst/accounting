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
import hashlib
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.ingestion import write_ingestion_log
from src.adapters._shared.money import parse_currency, quantize_balance
from src.adapters._shared.pdf import pdftotext_layout
from src.adapters._shared.result import BaseImportResult
from src.adapters._shared.wealth_client import WealthClientError, post_to_wealth
from src.models.brokerage import Account
from src.models.enums import Broker, IngestionStatus
from src.models.history import AccountBalanceSnapshot

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_TAG = "gsk_pdf"
"""Value written to ``account_balance_snapshot.source``."""

ADAPTER_NAME = "gsk_pdf"
"""Identifier written to ``ingestion_log.source``."""

GSK_BROKER = Broker.GSK_PENSION.value
"""Broker enum value used to look up the pre-seeded Account row."""

GSK_ACCOUNT_NUMBER = "GSK_PENSION"
"""Synthetic account_number — the PDF carries no natural identifier."""

GSK_RAW_ACCOUNT_NAME = "GSK Cash Balance Pension Plan"
"""Audit string preserved on every snapshot row."""

_CLOUD_INGEST_SOURCE = "xlsx-snapshot"
"""Workers ingest slug — GSK snapshots are AccountBalanceSnapshot rows."""


def _default_target() -> str:
    """Return WEALTH_TARGET_DEFAULT env var, defaulting to 'local'."""
    return os.environ.get("WEALTH_TARGET_DEFAULT", "local") or "local"


# Single regex on the layout text. The PDF uses month names like
# "May 7, 2026" and dollar amounts with comma thousands separators.
_CLOSING_BALANCE_RE = re.compile(
    r"Closing Balance as of\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+\$([\d,]+\.\d\d)"
)
_DATE_FORMAT = "%b %d, %Y"


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class ImportResult(BaseImportResult):
    """Summary of an import run.

    Inherits shared fields (``imported``, ``matched``, ``unmatched``,
    ``dup_skipped``, ``errors``, ``warnings``, ``distinct_accounts``) from
    :class:`~src.adapters._shared.result.BaseImportResult` and adds two
    adapter-specific fields:

    * ``parsed``: PDFs whose closing-balance regex matched.
    * ``would_insert``: snapshot rows that *would* be written on apply (dry-run).
    """

    parsed: int = 0
    """PDFs whose closing-balance regex matched."""

    would_insert: int = 0
    """Snapshot rows that *would* be written on apply (dry-run only)."""


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
    as_of: date | None = None,
) -> ImportResult:
    """Import a single GSK Cash Balance PDF.

    Args:
        path:    Path to the PDF.
        dry_run: When True, parse-and-count only; never write to ``session``.
                 Default True to protect the live DB.
        session: SQLAlchemy session. Required when ``dry_run`` is False.
        as_of:   Override the as-of date extracted from the PDF. When supplied,
                 this date wins over whatever the document contains.

    Returns:
        :class:`ImportResult` with counts and per-record errors.
    """
    result = ImportResult()
    record_label = f"{GSK_ACCOUNT_NUMBER}@{path.name}"

    # ── Extract ──────────────────────────────────────────────────────────────
    try:
        text = pdftotext_layout(path)
        extracted_as_of, balance = extract_closing_balance(text)
    except Exception as exc:  # noqa: BLE001 — per-record isolation
        result.errors.append(f"{record_label}: {exc}")
        logger.warning("gsk_pdf: extraction failed for %s: %s",
                       path, exc, exc_info=True)
        if session is not None and not dry_run:
            write_ingestion_log(
                session,
                source=ADAPTER_NAME,
                records_processed=0,
                records_failed=1,
                status=IngestionStatus.FAILURE,
                error_detail="\n".join(result.errors),
            )
        return result

    # Honour the caller's as_of override (used when re-tagging a backfill).
    snap_as_of = as_of if as_of is not None else extracted_as_of

    result.parsed = 1
    result.distinct_accounts = [GSK_ACCOUNT_NUMBER]

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
        write_ingestion_log(
            session,
            source=ADAPTER_NAME,
            records_processed=0,
            records_failed=1,
            status=IngestionStatus.FAILURE,
            error_detail="\n".join(result.errors),
        )
        return result

    row_hash = _row_hash(GSK_ACCOUNT_NUMBER, snap_as_of, balance)

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
                        as_of=snap_as_of,
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
    write_ingestion_log(
        session,
        source=ADAPTER_NAME,
        records_processed=result.imported + result.dup_skipped + result.unmatched,
        records_failed=len(result.errors),
        status=status,
        error_detail="\n".join(result.errors) or None,
    )

    return result


# ── Cloud import ─────────────────────────────────────────────────────────────


def import_pdf_cloud(
    path: Path,
    *,
    as_of: date | None = None,
) -> ImportResult:
    """Parse a GSK PDF and POST the snapshot to the Workers ingest endpoint.

    Does NOT require a DB session — all writes go to the cloud Worker.
    Idempotency is enforced by the Worker (source_row_hash UNIQUE constraint).

    Args:
        path:  Path to the PDF.
        as_of: Override the as-of date extracted from the PDF.

    Returns:
        :class:`ImportResult` with counts and per-record errors.
    """
    result = ImportResult()
    record_label = f"{GSK_ACCOUNT_NUMBER}@{path.name}"

    try:
        text = pdftotext_layout(path)
        extracted_as_of, balance = extract_closing_balance(text)
    except Exception as exc:  # noqa: BLE001 — per-record isolation
        result.errors.append(f"{record_label}: {exc}")
        logger.warning("gsk_pdf: extraction failed for %s: %s", path, exc, exc_info=True)
        return result

    snap_as_of = as_of if as_of is not None else extracted_as_of
    result.parsed = 1
    result.distinct_accounts = [GSK_ACCOUNT_NUMBER]

    row_hash = _row_hash(GSK_ACCOUNT_NUMBER, snap_as_of, balance)

    payload: dict[str, object] = {
        "rows": [
            {
                "raw_account_name": GSK_RAW_ACCOUNT_NAME,
                "as_of": snap_as_of.isoformat(),
                "balance": str(quantize_balance(balance)),
                "source": SOURCE_TAG,
                "source_row_hash": row_hash,
            }
        ]
    }

    try:
        post_to_wealth(payload, _CLOUD_INGEST_SOURCE)
        result.imported += 1
        result.matched += 1
    except WealthClientError as exc:
        result.errors.append(f"{record_label}: cloud POST failed: {exc}")
        logger.warning("gsk_pdf: cloud POST failed for %s: %s", record_label, exc)

    return result


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.adapters.gsk_pdf",
        description="Import a GSK Cash Balance pension PDF as one snapshot.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import-pdf", help="Parse one PDF and (optionally) write a snapshot.")
    s.add_argument("--file", required=True, help="Path to the GSK PDF.")
    s.add_argument(
        "--target",
        choices=["local", "cloud"],
        default=None,
        help="Destination: 'local' writes to SQLite; 'cloud' POSTs to the Workers ingest endpoint. "
             "Defaults to WEALTH_TARGET_DEFAULT env var, then 'local'.",
    )
    s.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the live DB (local) or cloud. Default is dry-run.",
    )
    s.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Override the as-of date (YYYY-MM-DD). Defaults to date in the PDF.",
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
    if args.cmd != "import-pdf":
        return 2

    dry_run = not args.apply
    pdf = Path(args.file)
    target = args.target if args.target is not None else _default_target()

    as_of: date | None = None
    if args.as_of:
        try:
            as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError as exc:
            print(f"invalid --as-of: {exc}", file=sys.stderr)
            return 2

    if dry_run:
        result = import_pdf(pdf, dry_run=True, as_of=as_of)
        _print_summary(result, dry_run=True)
        return 0

    if target == "cloud":
        result = import_pdf_cloud(pdf, as_of=as_of)
        _print_summary(result, dry_run=False)
        return 0

    try:
        from src.db.connection import get_session  # late import to keep tests light
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_pdf(pdf, dry_run=False, session=session, as_of=as_of)

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
    "import_pdf_cloud",
]
