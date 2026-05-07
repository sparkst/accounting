"""Vanguard CSV adapter — Phase 4 / T2.

Ingests Vanguard CSV exports into :class:`PositionSnapshot`.

Each Vanguard CSV has TWO blocks separated by blank lines:

1. **Positions block** (top) — wide form, multiple ``Account Number`` values:
   * Brokerage flavor (6 cols, trailing comma):
     ``Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,``
   * 529 flavor (5 cols, no trailing comma):
     ``Fund Account Number,Fund Name,Price,Shares,Total Value``
2. **Transactions block** (bottom) — wider header. Phase-4 scope counts
   these rows for the ``IngestionLog`` summary but does NOT write them to
   ``BrokerageTransaction``; that flows in Phase 5 once reinvest pairing
   is designed.

Account-id mapping is by lookup on ``(broker='vanguard', account_number)``
against ``account``. Unmapped account numbers append an error per row and
skip insertion — the adapter NEVER auto-creates ``Account`` rows so the
operator always sees what's missing before bulk apply.

Pattern conformance with ``src/adapters/xlsx_savings_plan.py``:

* :class:`ImportResult` dataclass with the same field set (plus
  ``parsed`` and ``transactions_seen`` for the position/transaction split).
* ``dry_run=True`` default to protect the live DB during exploration.
* Per-row ``session.begin_nested()`` savepoint — one bad row never halts a
  batch.
* Numeric coercion via :mod:`src.adapters._shared.money`
  (``Decimal(str(value))`` boundary).
* ``source_row_hash`` is SHA-256 of pipe-joined identity tuple with
  shares/price/market-value quantized to broker precision so re-import is
  idempotent regardless of trailing-zero formatting.
* ``IngestionLog`` row written on every ``--apply`` run.
* ``argparse`` CLI with ``import-positions`` subcommand mirroring
  ``xlsx_savings_plan``'s ``import-balances`` shape.
"""

from __future__ import annotations

import argparse
import csv as _csv
import hashlib
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.ingestion import write_ingestion_log
from src.adapters._shared.money import (
    parse_currency,
    quantize_balance,
    quantize_shares,
)
from src.adapters._shared.result import BaseImportResult
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import Broker, IngestionStatus

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_TAG = "vanguard_csv"
"""Conceptual source key for the adapter (mirrors xlsx_savings_plan.SOURCE_TAG)."""

ADAPTER_NAME = "vanguard_csv"
"""Identifier written to ``ingestion_log.source``."""

FLAVOR_BROKERAGE = "brokerage"
FLAVOR_529 = "529"

# First two header tokens are enough to distinguish the flavors, and avoid
# false positives on the transactions header (which also starts with
# "Account Number" but never has "Investment Name" as its second column).
_BROKERAGE_HEADER_PREFIX = "Account Number,Investment Name"
_K529_HEADER_PREFIX = "Fund Account Number,Fund Name"


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class ImportResult(BaseImportResult):
    """Summary of an import run.

    Inherits shared fields (``imported``, ``matched``, ``unmatched``,
    ``dup_skipped``, ``errors``, ``warnings``, ``distinct_accounts``) from
    :class:`~src.adapters._shared.result.BaseImportResult` and adds two
    adapter-specific fields:

    * ``parsed``: positions-block rows successfully parsed (regardless of
      dry_run / mapping errors — a measure of file shape).
    * ``transactions_seen``: rows in the transactions block. Phase-4
      counts but does not write these.
    """

    parsed: int = 0
    """Position-block rows successfully parsed (independent of dry_run)."""

    transactions_seen: int = 0
    """Transactions-block rows observed; not written in Phase 4."""


# ── Helpers ──────────────────────────────────────────────────────────────────


def detect_csv_flavor(header_line: str) -> str:
    """Return ``"brokerage"`` or ``"529"`` for a positions-block header line.

    Raises ``ValueError`` if the header matches neither flavor.
    """
    cleaned = (header_line or "").strip()
    if cleaned.startswith(_BROKERAGE_HEADER_PREFIX):
        return FLAVOR_BROKERAGE
    if cleaned.startswith(_K529_HEADER_PREFIX):
        return FLAVOR_529
    raise ValueError(f"unknown vanguard csv flavor: {cleaned[:80]!r}")


_HEADER_PREFIXES = ("Account Number,", "Fund Account Number,")


def _is_header_line(line: str) -> bool:
    """A line is a section header if it starts with a Vanguard column-row prefix."""
    return any(line.startswith(p) for p in _HEADER_PREFIXES)


def split_blocks(text: str) -> list[tuple[str, list[list[str]]]]:
    """Split a Vanguard CSV into ``[(header, rows), ...]`` per section header.

    Vanguard's CSV layout: a section header (positions or transactions),
    followed by data rows for one or more accounts. Single blank lines appear
    BETWEEN accounts within the same section — those are not section breaks.
    A new section is detected by encountering another header line.

    Each row is parsed by the stdlib ``csv`` module so commas inside quoted
    cells survive. Trailing empty cells (which Vanguard emits because every
    row ends with ``,``) are NOT trimmed; the row→dict mapper drops them by
    header-key.
    """
    out: list[tuple[str, list[list[str]]]] = []
    current_header: str | None = None
    current_rows: list[list[str]] = []

    def _flush() -> None:
        nonlocal current_header, current_rows
        if current_header is not None:
            out.append((current_header, current_rows))
        current_header = None
        current_rows = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            # Blank line within a section → not a boundary. Drop it.
            continue
        if _is_header_line(line):
            _flush()
            current_header = line
            current_rows = []
            continue
        if current_header is None:
            # Pre-header garbage; ignore.
            continue
        row = next(_csv.reader(StringIO(line)))
        current_rows.append(row)
    _flush()
    return out


def _row_to_dict(header: str, row: list[str]) -> dict[str, str]:
    """Build a {column: cell} dict, stripping each side and dropping empty keys.

    Header-row trailing commas become empty keys; we drop them so callers
    see a clean column→value map.
    """
    cols = next(_csv.reader(StringIO(header)))
    out: dict[str, str] = {}
    for col, val in zip(cols, row, strict=False):
        key = (col or "").strip()
        if not key:
            continue
        out[key] = (val or "").strip()
    return out


# ── Hash ─────────────────────────────────────────────────────────────────────


def _row_hash(
    *,
    account_number: str,
    symbol: str | None,
    shares: Decimal,
    price: Decimal,
    market_value: Decimal,
    as_of: date,
) -> str:
    """SHA-256 hex of the canonical position-row identity tuple.

    Numeric components are quantized so trailing-zero variance
    (``Decimal('10.5')`` vs ``'10.50'``) doesn't change the hash and break
    re-import idempotency.
    """
    # price uses quantize_balance (2 decimals) rather than quantize_shares so
    # that trailing-zero CSV variants of the same per-share price (e.g.
    # "659.35" vs "659.350") land on the same hash bucket and re-import
    # is idempotent.  Storage precision is determined by the DB column
    # (Numeric(18, 8)), not by the hash payload.
    payload = "|".join(
        (
            account_number,
            symbol or "-",
            str(quantize_shares(shares)),
            str(quantize_balance(price)),
            str(quantize_balance(market_value)),
            as_of.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Parsed-row dataclass (positions only) ────────────────────────────────────


@dataclass
class _PositionRow:
    """A normalised position row from either flavor."""

    account_number: str
    symbol: str | None
    description: str | None
    shares: Decimal
    price: Decimal
    market_value: Decimal
    raw: dict[str, str]


def _parse_brokerage_position(cells: dict[str, str]) -> _PositionRow:
    account_number = cells.get("Account Number", "").strip()
    if not account_number:
        raise ValueError("missing Account Number")
    return _PositionRow(
        account_number=account_number,
        symbol=(cells.get("Symbol", "").strip() or None),
        description=(cells.get("Investment Name", "").strip() or None),
        shares=parse_currency(cells.get("Shares", "")),
        price=parse_currency(cells.get("Share Price", "")),
        market_value=parse_currency(cells.get("Total Value", "")),
        raw=cells,
    )


def _parse_529_position(cells: dict[str, str]) -> _PositionRow:
    account_number = cells.get("Fund Account Number", "").strip()
    if not account_number:
        raise ValueError("missing Fund Account Number")
    return _PositionRow(
        account_number=account_number,
        symbol=None,  # 529 funds have no ticker.
        description=(cells.get("Fund Name", "").strip() or None),
        shares=parse_currency(cells.get("Shares", "")),
        price=parse_currency(cells.get("Price", "")),
        market_value=parse_currency(cells.get("Total Value", "")),
        raw=cells,
    )


def _parse_position(flavor: str, cells: dict[str, str]) -> _PositionRow:
    if flavor == FLAVOR_BROKERAGE:
        return _parse_brokerage_position(cells)
    if flavor == FLAVOR_529:
        return _parse_529_position(cells)
    raise ValueError(f"unsupported flavor: {flavor}")


# ── Core import ──────────────────────────────────────────────────────────────


def _resolve_as_of(path: Path, as_of: date | None) -> date:
    """Pick the snapshot date.

    Override > file mtime. mtime is converted in UTC so the date is
    consistent regardless of the local timezone.
    """
    if as_of is not None:
        return as_of
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=UTC).date()
    except OSError:
        return datetime.now(tz=UTC).date()


def import_positions(
    path: Path,
    *,
    dry_run: bool = True,
    session: Session | None = None,
    as_of: date | None = None,
) -> ImportResult:
    """Import the positions block of one Vanguard CSV file.

    Args:
        path:    Path to the CSV.
        dry_run: When True (default), parse-and-count only — never write to
                 ``session``. Account-mapping errors are still reported so
                 the operator sees what's missing pre-apply.
        session: SQLAlchemy session. Required when ``dry_run=False``; also
                 used during dry-run to detect unmapped account numbers.
        as_of:   Override snapshot date. Defaults to the file's mtime.

    Returns:
        :class:`ImportResult` summarising the run.
    """
    result = ImportResult()
    path = Path(path)
    source_file = path.name
    snap_date = _resolve_as_of(path, as_of)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.errors.append(f"{source_file}: {exc}")
        if session is not None and not dry_run:
            write_ingestion_log(session, source=ADAPTER_NAME,
                                records_processed=0, records_failed=1,
                                status=IngestionStatus.FAILURE,
                                error_detail=result.errors[-1])
        return result

    blocks = split_blocks(text)
    if not blocks:
        result.errors.append(f"{source_file}: no blocks parsed")
        if session is not None and not dry_run:
            write_ingestion_log(session, source=ADAPTER_NAME,
                                records_processed=0, records_failed=1,
                                status=IngestionStatus.FAILURE,
                                error_detail=result.errors[-1])
        return result

    # Block 0 is the positions block; block 1 (if present) is transactions.
    pos_header, pos_rows = blocks[0]
    try:
        flavor = detect_csv_flavor(pos_header)
    except ValueError as exc:
        result.errors.append(f"{source_file}: {exc}")
        if session is not None and not dry_run:
            write_ingestion_log(session, source=ADAPTER_NAME,
                                records_processed=0, records_failed=1,
                                status=IngestionStatus.FAILURE,
                                error_detail=result.errors[-1])
        return result

    if len(blocks) >= 2:
        # Phase-4 scope: count transactions but never write them.
        result.transactions_seen = sum(len(rows) for _, rows in blocks[1:])

    # ── Parse positions (independent of session) ─────────────────────────────
    parsed_rows: list[_PositionRow] = []
    for idx, raw_row in enumerate(pos_rows, start=1):
        cells = _row_to_dict(pos_header, raw_row)
        try:
            parsed = _parse_position(flavor, cells)
        except (ValueError, InvalidOperation) as exc:
            label = f"{source_file}:row{idx}"
            result.errors.append(f"{label}: {exc}")
            continue
        parsed_rows.append(parsed)
        result.parsed += 1

    result.distinct_accounts = sorted({r.account_number for r in parsed_rows})

    # ── Account-id resolution ────────────────────────────────────────────────
    # Even on dry-run we look these up so the operator sees what's missing.
    account_id_by_number: dict[str, str] = {}
    if session is not None:
        for acct_no in result.distinct_accounts:
            row = session.execute(
                select(Account.id).where(
                    Account.broker == Broker.VANGUARD.value,
                    Account.account_number == acct_no,
                )
            ).first()
            if row is not None:
                account_id_by_number[acct_no] = row[0]

    # Surface unmapped accounts as errors regardless of dry_run.
    for parsed in parsed_rows:
        if parsed.account_number not in account_id_by_number:
            label = f"{source_file}:{parsed.account_number}"
            msg = f"{label}: unmapped vanguard account_number"
            # Avoid duplicating the same account_number in errors many times.
            if msg not in result.errors:
                result.errors.append(msg)

    if dry_run:
        return result

    if session is None:
        result.errors.append("session required when dry_run=False")
        return result

    # ── Apply ────────────────────────────────────────────────────────────────
    # Per-row savepoint isolates IntegrityError without losing earlier rows;
    # outer commit batches the whole file into one fsync at the end.
    for parsed in parsed_rows:
        account_id = account_id_by_number.get(parsed.account_number)
        if account_id is None:
            # Already recorded as an error above — don't double-log.
            result.unmatched += 1
            continue

        record_label = (
            f"{source_file}:{parsed.account_number}:"
            f"{parsed.symbol or parsed.description or '-'}"
        )

        try:
            row_hash = _row_hash(
                account_number=parsed.account_number,
                symbol=parsed.symbol,
                shares=parsed.shares,
                price=parsed.price,
                market_value=parsed.market_value,
                as_of=snap_date,
            )
        except (InvalidOperation, ValueError) as exc:
            result.errors.append(f"{record_label}: hash failed: {exc}")
            continue

        existing = session.execute(
            select(PositionSnapshot.id).where(
                PositionSnapshot.account_id == account_id,
                PositionSnapshot.source_row_hash == row_hash,
            )
        ).first()
        if existing is not None:
            result.dup_skipped += 1
            continue

        try:
            with session.begin_nested():
                session.add(
                    PositionSnapshot(
                        account_id=account_id,
                        as_of=datetime.combine(snap_date, datetime.min.time()),
                        symbol=parsed.symbol,
                        description=parsed.description,
                        quantity=parsed.shares,
                        price=parsed.price,
                        market_value=parsed.market_value,
                        source_file=source_file,
                        source_row_hash=row_hash,
                        raw_data=parsed.raw,
                    )
                )
            result.imported += 1
        except IntegrityError:
            # UNIQUE on (account_id, source_row_hash) — same logical row exists.
            result.dup_skipped += 1
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{record_label}: {exc}")
            logger.warning("vanguard_csv: row %s failed: %s",
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
        error_detail=_format_log_detail(result),
    )

    return result


def _format_log_detail(result: ImportResult) -> str | None:
    """Compose the IngestionLog ``error_detail`` summary.

    Includes the transactions_seen count even on success runs so the
    operator can see how many tx-block rows were observed but skipped.
    """
    parts: list[str] = []
    if result.transactions_seen:
        parts.append(f"transactions_seen={result.transactions_seen}")
    if result.errors:
        parts.append("errors:\n" + "\n".join(result.errors))
    return "\n".join(parts) if parts else None


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.adapters.vanguard_csv",
        description="Ingest Vanguard CSV exports into PositionSnapshot.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "import-positions",
        help=(
            "Read a Vanguard CSV and import the positions block. "
            "Transactions block is parsed and counted but not written."
        ),
    )
    s.add_argument("--file", required=True, help="Path to the CSV.")
    s.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the live DB. Default is dry-run.",
    )
    s.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Snapshot date (YYYY-MM-DD). Defaults to file mtime.",
    )
    return p


def _print_summary(result: ImportResult, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"=== vanguard_csv:positions ({mode}) ===")
    print(f"  parsed             : {result.parsed}")
    print(f"  imported           : {result.imported}")
    print(f"  dup_skipped        : {result.dup_skipped}")
    print(f"  transactions_seen  : {result.transactions_seen}")
    print(f"  errors             : {len(result.errors)}")
    print(f"  distinct accounts ({len(result.distinct_accounts)}):")
    for n in result.distinct_accounts:
        print(f"    - {n}")
    if result.errors:
        print("  --- error detail ---")
        for e in result.errors[:20]:
            print(f"    * {e}")
        if len(result.errors) > 20:
            print(f"    ... {len(result.errors) - 20} more")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "import-positions":
        return 2

    dry_run = not args.apply
    as_of: date | None = None
    if args.as_of:
        try:
            as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError as exc:
            print(f"invalid --as-of: {exc}", file=sys.stderr)
            return 2

    path = Path(args.file)

    if dry_run:
        # We still want a session for unmapped-account detection; open one if
        # available, otherwise proceed without (errors will then omit the
        # mapping check).
        try:
            from src.db.connection import get_session  # late import keeps tests light
        except ImportError:
            result = import_positions(path, dry_run=True, session=None,
                                      as_of=as_of)
        else:
            with get_session() as session:
                result = import_positions(path, dry_run=True, session=session,
                                          as_of=as_of)
        _print_summary(result, dry_run=True)
        return 0

    try:
        from src.db.connection import get_session  # late import
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_positions(path, dry_run=False, session=session,
                                  as_of=as_of)

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
    "FLAVOR_529",
    "FLAVOR_BROKERAGE",
    "ImportResult",
    "SOURCE_TAG",
    "detect_csv_flavor",
    "import_positions",
    "split_blocks",
]
