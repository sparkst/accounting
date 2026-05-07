"""Franklin Templeton year-end / quarter-end statement PDF adapter.

Phase 4 — Adapter 5 (statements only). One ``AccountBalanceSnapshot`` is
written per parseable PDF. Modern statements (2020+) carry a single-line
``PORTFOLIO OVERVIEW`` total that this adapter regexes out. Legacy statements
(2000-2003 era) use a different layout — they yield a per-file error and the
batch continues.

The companion ``count_csv_transactions`` helper exists so the IngestionLog
summary can report the row count of ``accounthistory.csv``; actual
``BrokerageTransaction`` ingestion is deferred to Phase 5.

Canonical reference: ``src/adapters/xlsx_savings_plan.py``. Mirrors the
``ImportResult`` dataclass, ``dry_run=True`` default, per-file
``session.begin_nested()`` savepoint, and ``IngestionLog`` writer pattern.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.ingestion import write_ingestion_log
from src.adapters._shared.money import parse_currency, quantize_balance
from src.adapters._shared.pdf import pdftotext_layout
from src.adapters._shared.result import BaseImportResult
from src.models.brokerage import Account
from src.models.enums import Broker, IngestionStatus
from src.models.history import AccountBalanceSnapshot

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_TAG: Final[str] = "ft_pdf"
"""Value written to ``account_balance_snapshot.source``."""

ADAPTER_NAME: Final[str] = "ft_pdf"
"""Identifier written to ``ingestion_log.source``."""

FT_BROKER: Final[str] = Broker.FRANKLIN_TEMPLETON.value
FT_ACCOUNT_NUMBER: Final[str] = "8291"
FT_RAW_ACCOUNT_NAME: Final[str] = "Franklin Templeton — Templeton Growth Fund 8291"

# YYYY-MM-DD.pdf, anchored. The portal screen-grab and any other ad-hoc PDFs
# in the directory will not match and are reported as errors.
_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.pdf$")

# Modern statements have a single line "PORTFOLIO OVERVIEW   $XX,XXX.XX".
# pdftotext -layout collapses to a single space between heading and value.
_PORTFOLIO_OVERVIEW_RE: Final[re.Pattern[str]] = re.compile(
    r"PORTFOLIO\s+OVERVIEW\s+\$([\d,]+\.\d{2})"
)


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class ImportResult(BaseImportResult):
    """Summary of a statement-import run.

    Inherits shared fields (``imported``, ``matched``, ``unmatched``,
    ``dup_skipped``, ``errors``, ``warnings``, ``distinct_accounts``) from
    :class:`~src.adapters._shared.result.BaseImportResult` and adds one
    adapter-specific field:

    * ``files_seen``: number of ``*.pdf`` files walked (informational).
    """

    files_seen: int = 0
    """Number of ``*.pdf`` files walked (informational)."""


# ── Public extraction helpers ────────────────────────────────────────────────


def parse_statement_filename(name: str) -> date:
    """Parse a statement basename ``YYYY-MM-DD.pdf`` into the ``as_of`` date.

    Strict — anything else (the portal screen-grab, a non-PDF, an invalid
    calendar date) raises ``ValueError``. Callers pass ``Path.name`` (basename
    only); paths with directory components are stripped to basename first.
    """
    basename = Path(name).name
    match = _FILENAME_RE.match(basename)
    if match is None:
        raise ValueError(f"not a statement filename: {basename!r}")
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid date in filename {basename!r}: {exc}") from exc


def extract_portfolio_overview(text: str) -> Decimal:
    """Extract the ``PORTFOLIO OVERVIEW`` total from a layout-extracted PDF.

    Returns the value as ``Decimal`` (cents preserved). Raises ``ValueError``
    when the marker is not present — this is the signal for the batch loop to
    record a per-file error and move on.
    """
    match = _PORTFOLIO_OVERVIEW_RE.search(text)
    if match is None:
        raise ValueError("PORTFOLIO OVERVIEW line not found")
    return parse_currency(match.group(1))


def count_csv_transactions(path: Path) -> int:
    """Count non-header data rows in ``accounthistory.csv``.

    Empty/whitespace lines are not counted. Used by ops to populate the
    IngestionLog summary; v1 does not write ``BrokerageTransaction`` rows.
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        try:
            next(reader)  # consume header
        except StopIteration:
            return 0
        return sum(1 for row in reader if any(cell.strip() for cell in row))


# ── Internal helpers ─────────────────────────────────────────────────────────


def _row_hash(account_number: str, as_of: date, balance: Decimal) -> str:
    """SHA256 hex of the canonical statement-row identity tuple."""
    payload = "|".join(
        (account_number, as_of.isoformat(), str(quantize_balance(balance)))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_account(session: Session) -> Account | None:
    """Look up the single FT account by ``(broker, account_number)``."""
    return (
        session.query(Account)
        .filter(
            Account.broker == FT_BROKER,
            Account.account_number == FT_ACCOUNT_NUMBER,
        )
        .first()
    )


def _parse_one_pdf(pdf_path: Path) -> tuple[date, Decimal]:
    """Parse a single statement PDF → ``(as_of, balance)``.

    Raises ``ValueError`` on filename or content parse failures so the caller
    can append a per-file error and continue. Other exceptions (FileNotFound
    from the binary, etc.) propagate so they're not silently swallowed.
    """
    as_of = parse_statement_filename(pdf_path.name)
    text = pdftotext_layout(pdf_path)
    balance = extract_portfolio_overview(text)
    return as_of, balance


# ── Core import ──────────────────────────────────────────────────────────────


def import_statements(
    directory: Path,
    *,
    dry_run: bool = True,
    session: Session | None = None,
) -> ImportResult:
    """Walk ``directory`` for ``*.pdf`` statements and write balance snapshots.

    Per-file error isolation: a malformed filename, missing PORTFOLIO OVERVIEW
    line, or pdftotext failure on one file appends an error to ``result.errors``
    and the batch continues. Defaults to dry-run; pass ``dry_run=False`` and
    a live ``session`` to actually write rows.

    Args:
        directory: Path containing the ``*.pdf`` statements.
        dry_run:   When True, parse-and-count only; never write to ``session``.
        session:   SQLAlchemy session. Required when ``dry_run`` is False.
    """
    result = ImportResult()
    directory = Path(directory)

    pdf_paths = sorted(directory.glob("*.pdf"))
    result.files_seen = len(pdf_paths)

    # Parse every PDF up front (cheap, no DB writes). Collect (path, as_of,
    # balance) for the writer loop; collect per-file errors as we go.
    parsed: list[tuple[Path, date, Decimal]] = []
    for pdf_path in pdf_paths:
        try:
            as_of, balance = _parse_one_pdf(pdf_path)
        except ValueError as exc:
            result.errors.append(f"{pdf_path.name}: {exc}")
            logger.info("ft_pdf: skipping %s: %s", pdf_path.name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{pdf_path.name}: {exc}")
            logger.warning(
                "ft_pdf: failed to parse %s: %s", pdf_path.name, exc, exc_info=True
            )
            continue
        parsed.append((pdf_path, as_of, balance))

    if dry_run:
        return result

    if session is None:
        result.errors.append("session required when dry_run=False")
        return result

    # ── Apply ────────────────────────────────────────────────────────────────
    # Resolve the account once. Unmapped → every parsed row errors so the
    # operator sees exactly which institution needs seeding.
    account = _resolve_account(session)
    if account is None:
        for pdf_path, _as_of, _bal in parsed:
            result.errors.append(
                f"{pdf_path.name}: unmapped account "
                f"(broker={FT_BROKER}, account_number={FT_ACCOUNT_NUMBER})"
            )
        write_ingestion_log(
            session,
            source=ADAPTER_NAME,
            records_processed=0,
            records_failed=len(result.errors),
            status=IngestionStatus.FAILURE,
            error_detail="\n".join(result.errors) or None,
        )
        return result

    for pdf_path, as_of, balance in parsed:
        record_label = f"{pdf_path.name}@{as_of.isoformat()}"
        row_hash = _row_hash(FT_ACCOUNT_NUMBER, as_of, balance)

        existing = (
            session.query(AccountBalanceSnapshot.id)
            .filter(AccountBalanceSnapshot.source_row_hash == row_hash)
            .first()
        )
        if existing is not None:
            result.dup_skipped += 1
            continue

        try:
            with session.begin_nested():
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        raw_account_name=FT_RAW_ACCOUNT_NAME,
                        as_of=as_of,
                        balance=balance,
                        source=SOURCE_TAG,
                        source_row_hash=row_hash,
                    )
                )
            result.imported += 1
            result.matched += 1
        except IntegrityError:
            # UNIQUE on (account_id, as_of, source) caught the same logical row.
            result.dup_skipped += 1
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{record_label}: {exc}")
            logger.warning(
                "ft_pdf: row %s failed: %s", record_label, exc, exc_info=True
            )

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


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.adapters.ft_pdf",
        description="Import Franklin Templeton statement PDFs into "
        "account_balance_snapshot.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "import-statements",
        help="Walk a directory of FT statement PDFs and import portfolio totals.",
    )
    s.add_argument("--dir", required=True, help="Directory containing FT *.pdf files.")
    s.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the live DB. Default is dry-run.",
    )
    return p


def _print_summary(result: ImportResult, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"=== ft_pdf:import-statements ({mode}) ===")
    print(f"  files_seen   : {result.files_seen}")
    print(f"  imported     : {result.imported}")
    print(f"  matched      : {result.matched}")
    print(f"  dup_skipped  : {result.dup_skipped}")
    print(f"  errors       : {len(result.errors)}")
    if result.errors:
        print("  --- error detail ---")
        for e in result.errors[:20]:
            print(f"    * {e}")
        if len(result.errors) > 20:
            print(f"    ... {len(result.errors) - 20} more")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "import-statements":
        return 2

    directory = Path(args.dir)
    dry_run = not args.apply

    if dry_run:
        result = import_statements(directory, dry_run=True, session=None)
        _print_summary(result, dry_run=True)
        return 0

    try:
        from src.db.connection import get_session  # late import to keep tests light
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_statements(directory, dry_run=False, session=session)

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
    "FT_ACCOUNT_NUMBER",
    "FT_BROKER",
    "FT_RAW_ACCOUNT_NAME",
    "ImportResult",
    "SOURCE_TAG",
    "count_csv_transactions",
    "extract_portfolio_overview",
    "import_statements",
    "parse_statement_filename",
]
