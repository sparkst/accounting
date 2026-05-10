"""Schwab brokerage CSV adapter (TASK-07, REQ-005a..g).

Processes a folder of Schwab exports for one or more accounts. Each account has
up to three file kinds we care about in Phase 1:

    <AccountName>_*_Transactions_*.csv          → brokerage_transaction
    <AccountName>-Positions-*.csv               → position_snapshot
    <AccountName>_GainLoss_Realized_Details_*.csv → realized_gain_loss

`XXXX-X724*.CSV` (1099-form CSVs) are detected and skipped — deferred to
Phase 2. Schwab natively issues paired rows for dividend reinvestment, so
no synthetic rows are created.

Account discovery:
- AccountName extracted from filename prefix.
- Positions file metadata row supplies the masked account number from
  ``"Positions for account <name> ...<acct_id> as of ..."``.
- Account-type defaults: AMZN RSU → ``rsu``, Joint Tenant → ``joint``,
  others → ``taxable``.

Date handling:
- Schwab's ``"04/22/2026 as of 04/20/2026"`` composite is split via
  :func:`parse_date_with_as_of` so ``trade_date`` = the *as of* date and
  ``settlement_date`` = the leading date.

Currency / quantity:
- All currency-shaped fields parsed via :func:`parse_currency`.
- Quantity parsed via :func:`parse_quantity` (handles ``"1,471"``).

Idempotency: every row's ``source_row_hash`` is built from
(broker, account_number, source_file, row_index, trade_date, action,
symbol, quantity, amount). Re-ingesting the same folder is a no-op.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.adapters.brokerage_csv_helpers import (
    compute_brokerage_row_hash,
    compute_position_row_hash,
    compute_realized_lot_hash,
    parse_currency,
    parse_date_flexible,
    parse_date_with_as_of,
    parse_quantity,
    read_csv_tolerant,
)
from src.models.brokerage import (
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
)
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    Entity,
    GainLossTerm,
    IngestionStatus,
    Source,
)
from src.models.ingestion_log import IngestionLog

logger = logging.getLogger(__name__)

# ── File-kind classification ──────────────────────────────────────────────────

# 1099 form CSVs Schwab exports look like ``XXXX-X724 (1).CSV``. We detect by
# the leading "XXXX-X" segment.
_1099_FILE_RE = re.compile(r"^XXXX-X\d+", re.IGNORECASE)

_TRANSACTIONS_RE = re.compile(r"_Transactions_", re.IGNORECASE)
_POSITIONS_RE = re.compile(r"-Positions-", re.IGNORECASE)
_GAINLOSS_RE = re.compile(r"_GainLoss_Realized_Details_", re.IGNORECASE)

# ── Positions metadata row parser ─────────────────────────────────────────────

# "Positions for account AMZN RSU ...144 as of 01:17 PM ET, 2026/05/03"
# "Positions for account Joint Tenant ...724 as of 01:17 PM ET, 2026/05/03"
_POSITIONS_META_RE = re.compile(
    r"^Positions for account\s+(?P<name>.+?)\s+\.{2,}(?P<acct>\S+)\s+as of\s+"
    r"(?P<as_of>.+?)\s*$"
)

# Schwab as-of stamp inside a positions metadata row, e.g. "01:17 PM ET, 2026/05/03"
_POSITIONS_AS_OF_DT_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")

# Trailing rows in positions files that are summary/cash buckets, not lots.
_POSITIONS_TRAILING_SYMBOLS = {
    "Cash & Cash Investments",
    "Positions Total",
    "--",
}

# ── Action mapping ────────────────────────────────────────────────────────────


def map_schwab_action(action: str, account_type: str) -> str:
    """Map a Schwab native ``Action`` string to a :class:`CanonicalAction`.

    Case-sensitive exact match per PLAN.md TASK-07. ``Journaled Shares``
    routes to ``rsu_vest`` for RSU accounts (AMZN RSU 144), else ``transfer``
    (Joint Tenant cash transfers).
    """
    a = action.strip()
    if a in ("Buy", "Buy to Open"):
        return CanonicalAction.BUY.value
    if a in ("Sell", "Sell to Close"):
        return CanonicalAction.SELL.value
    if a == "Reinvest Dividend":
        # P2-005: 'Reinvest Dividend' is the cash-side row of a reinvestment pair,
        # mapped to DIVIDEND_ORDINARY. It is paired via paired_transaction_id to the
        # matching 'Reinvest Shares' row (canonical: REINVEST). Query downstream should
        # use paired_transaction_id to distinguish a reinvested dividend from a plain
        # cash dividend. Intentional — no separate CanonicalAction for this case.
        return CanonicalAction.DIVIDEND_ORDINARY.value
    if a == "Reinvest Shares":
        return CanonicalAction.REINVEST.value
    if a == "Qual Div Reinvest":
        return CanonicalAction.DIVIDEND_QUALIFIED.value
    if a == "Pr Yr Div Reinvest":
        return CanonicalAction.DIVIDEND_ORDINARY.value
    if a == "Long Term Cap Gain Reinvest":
        return CanonicalAction.CAPITAL_GAIN_LT.value
    if a == "Short Term Cap Gain Reinvest":
        return CanonicalAction.CAPITAL_GAIN_ST.value
    if a in ("Cash Dividend", "Special Dividend", "Non-Qualified Div"):
        return CanonicalAction.DIVIDEND_ORDINARY.value
    if a in ("Bank Interest", "Bond Interest", "CD Interest", "Credit Interest"):
        return CanonicalAction.INTEREST.value
    if a in ("CD Deposit Adj", "CD Deposit Funds"):
        return CanonicalAction.CONTRIBUTION.value
    if a == "Stock Split":
        return CanonicalAction.STOCK_SPLIT.value
    if a == "Cash In Lieu":
        return CanonicalAction.CASH_IN_LIEU.value
    if a == "Journaled Shares":
        if account_type == AccountType.RSU.value:
            return CanonicalAction.RSU_VEST.value
        return CanonicalAction.TRANSFER.value
    if a == "Long Term Cap Gain":
        return CanonicalAction.CAPITAL_GAIN_LT.value
    if a == "Short Term Cap Gain":
        return CanonicalAction.CAPITAL_GAIN_ST.value
    if a in ("Internal Transfer", "Security Transfer", "MoneyLink Transfer"):
        return CanonicalAction.TRANSFER.value
    if a == "Journal":
        return CanonicalAction.JOURNAL.value
    return CanonicalAction.OTHER.value


# Reinvest action pairing rules: cash-side actions whose immediately-following
# ``Reinvest Shares`` row should be linked back via ``paired_transaction_id``.
_REINVEST_FOLLOWING_PAIR_ACTIONS = {
    "Qual Div Reinvest",
    "Pr Yr Div Reinvest",
    "Long Term Cap Gain Reinvest",
    "Short Term Cap Gain Reinvest",
}


# ── File classification ───────────────────────────────────────────────────────


@dataclass
class _FileKind:
    """Sorted bucket of files by kind for one folder scan."""

    transactions: list[Path] = field(default_factory=list)
    positions: list[Path] = field(default_factory=list)
    gainloss: list[Path] = field(default_factory=list)
    skipped_1099: list[Path] = field(default_factory=list)
    unrecognized: list[Path] = field(default_factory=list)


def classify_file(path: Path) -> str:
    """Return the file-kind label for a single Schwab export.

    Labels: ``"transactions" | "positions" | "gainloss" | "1099" | "unknown"``.
    """
    name = path.name
    if _1099_FILE_RE.match(name):
        return "1099"
    if _GAINLOSS_RE.search(name):
        return "gainloss"
    if _POSITIONS_RE.search(name):
        return "positions"
    if _TRANSACTIONS_RE.search(name):
        return "transactions"
    return "unknown"


def _classify_folder(folder: Path) -> _FileKind:
    """Walk a folder of Schwab exports and bucket files by kind."""
    bucket = _FileKind()
    for child in sorted(folder.iterdir()):
        if not child.is_file():
            continue
        if child.suffix.lower() != ".csv":
            continue
        kind = classify_file(child)
        if kind == "1099":
            bucket.skipped_1099.append(child)
        elif kind == "transactions":
            bucket.transactions.append(child)
        elif kind == "positions":
            bucket.positions.append(child)
        elif kind == "gainloss":
            bucket.gainloss.append(child)
        else:
            bucket.unrecognized.append(child)
    return bucket


# ── Account discovery ────────────────────────────────────────────────────────


def _account_name_from_filename(path: Path) -> str:
    """Extract the leading AccountName from a Schwab filename.

    Examples:
        ``AMZN_RSU_XXX144_Transactions_20260503-131654.csv`` → ``AMZN RSU``
        ``AMZN RSU-Positions-2026-05-03-131716.csv``         → ``AMZN RSU``
        ``Joint Tenant-Positions-2026-05-03-131725.csv``     → ``Joint Tenant``
        ``Joint_Tenant_GainLoss_Realized_Details_*.csv``     → ``Joint Tenant``

    The leading segment is everything before the first transactions /
    positions / gainloss keyword (or the file's account-id ``XXX###`` token).
    """
    stem = path.stem
    # Try positions style: "AMZN RSU-Positions-..."
    m = _POSITIONS_RE.search(stem)
    if m:
        prefix = stem[: m.start()]
        return prefix.replace("_", " ").strip(" -_")
    # Transactions: <AccountName>_..._XXX###_Transactions_...  or
    #               <AccountName>_..._Transactions_...
    m = _TRANSACTIONS_RE.search(stem)
    if m:
        prefix = stem[: m.start()]
        # Strip a trailing ``_XXX###`` account-id token if present.
        prefix = re.sub(r"_XXX[A-Z0-9]+$", "", prefix, flags=re.IGNORECASE)
        return prefix.replace("_", " ").strip(" -_")
    # GainLoss: <AccountName>_GainLoss_Realized_Details_...
    m = _GAINLOSS_RE.search(stem)
    if m:
        prefix = stem[: m.start()]
        return prefix.replace("_", " ").strip(" -_")
    return stem.replace("_", " ").strip(" -_")


def default_account_type(account_name: str) -> str:
    """Default account type for a Schwab account name (per PLAN.md)."""
    norm = account_name.strip().upper()
    if "RSU" in norm:
        return AccountType.RSU.value
    if "JOINT" in norm:
        return AccountType.JOINT.value
    return AccountType.TAXABLE.value


def parse_positions_metadata(line: str) -> tuple[str, str, datetime | None]:
    """Parse a Schwab positions metadata row.

    Returns ``(account_name, account_number, as_of_datetime)``.

    Example input::

        Positions for account AMZN RSU ...144 as of 01:17 PM ET, 2026/05/03

    Yields ``("AMZN RSU", "144", datetime(2026,5,3,...))``.

    Falls back to ``(name, "", None)`` if the metadata is unrecognizable.
    """
    cleaned = line.strip().strip('"')
    m = _POSITIONS_META_RE.match(cleaned)
    if not m:
        return ("", "", None)
    name = m.group("name").strip()
    acct = m.group("acct").strip()
    as_of_str = m.group("as_of").strip()
    dm = _POSITIONS_AS_OF_DT_RE.search(as_of_str)
    as_of_dt: datetime | None = None
    if dm:
        try:
            as_of_dt = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        except ValueError:
            as_of_dt = None
    return (name, acct, as_of_dt)


# ── Adapter ───────────────────────────────────────────────────────────────────


@dataclass
class SchwabCsvAdapter(BaseAdapter):
    """Ingest a folder of Schwab CSV exports.

    Args:
        folder: Path to a directory containing Schwab transaction / positions /
                G/L exports for one or more accounts. Files with names matching
                ``XXXX-X724*.CSV`` are detected and skipped (1099 form data,
                Phase 2).
    """

    folder: Path

    def __post_init__(self) -> None:
        # Accept str or Path.
        self.folder = Path(self.folder)

    @property
    def source(self) -> str:
        return Source.SCHWAB_CSV.value

    # ---- Account upsert ------------------------------------------------------

    def _get_or_create_account(
        self,
        session: Session,
        account_name: str,
        account_number: str,
    ) -> Account:
        """Upsert an Account row keyed on (broker='schwab', account_number)."""
        existing = (
            session.query(Account)
            .filter(
                Account.broker == Broker.SCHWAB.value,
                Account.account_number == account_number,
            )
            .first()
        )
        if existing is not None:
            return existing
        acct = Account(
            broker=Broker.SCHWAB.value,
            account_number=account_number,
            account_name=account_name,
            account_type=default_account_type(account_name),
            entity=Entity.PERSONAL.value,
        )
        session.add(acct)
        session.flush()
        return acct

    # ---- Run -----------------------------------------------------------------

    def run(self, session: Session) -> AdapterResult:
        """Process every recognized file in ``self.folder``.

        Returns an :class:`AdapterResult` summarizing rows added / skipped /
        failed across all file kinds. Writes one IngestionLog row per run
        (REQ-005g).
        """
        result = AdapterResult(source=self.source)
        if not self.folder.exists() or not self.folder.is_dir():
            result.status = IngestionStatus.FAILURE
            result.errors.append((str(self.folder), "Folder does not exist."))
            self._write_log(session, result)
            return result

        bucket = _classify_folder(self.folder)

        for skipped in bucket.skipped_1099:
            logger.info(
                "SchwabCsvAdapter: skipping 1099 form file %s "
                "(deferred to Phase 2).",
                skipped.name,
            )

        for unknown in bucket.unrecognized:
            logger.warning(
                "SchwabCsvAdapter: unrecognized file %s — ignored.",
                unknown.name,
            )

        # Discover accounts from positions files first (canonical source for
        # masked account numbers); fall back to filename-only for txn-only
        # folders by stamping a synthetic account_number = "<NAME>:UNKNOWN".
        accounts_by_name: dict[str, Account] = {}
        for path in bucket.positions:
            try:
                acct = self._upsert_account_from_positions(session, path)
                if acct is not None:
                    accounts_by_name[acct.account_name or ""] = acct
            except Exception as exc:
                result.record_error(path.name, exc)
                with contextlib.suppress(Exception):
                    session.rollback()

        for path in bucket.transactions:
            self._process_transactions_file(session, path, accounts_by_name, result)

        for path in bucket.positions:
            self._process_positions_file(session, path, accounts_by_name, result)

        for path in bucket.gainloss:
            self._process_gainloss_file(session, path, accounts_by_name, result)

        self._write_log(session, result)
        return result

    # ---- IngestionLog writer -----------------------------------------------

    def _write_log(self, session: Session, result: AdapterResult) -> None:
        """Write one IngestionLog row for this adapter run (REQ-005g)."""
        error_detail: str | None = None
        if result.errors:
            lines = [f"{rid}: {msg}" for rid, msg in result.errors[:50]]
            error_detail = "\n\n".join(lines)
        log = IngestionLog(
            source=self.source,
            run_at=result.run_at,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=error_detail,
            retryable=result.status
            in (IngestionStatus.PARTIAL_FAILURE, IngestionStatus.FAILURE),
        )
        session.add(log)
        try:
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Failed to write IngestionLog for SchwabCsvAdapter")

    # ---- Positions -----------------------------------------------------------

    def _upsert_account_from_positions(
        self, session: Session, path: Path
    ) -> Account | None:
        """Read the metadata row of a positions file and upsert the Account."""
        rows = list(read_csv_tolerant(path))
        if not rows:
            return None
        # The first row is the metadata title. csv.reader returns it as a
        # single-element list because the entire line is one quoted field.
        meta_cells = rows[0]
        meta_line = meta_cells[0] if meta_cells else ""
        name, acct_id, _ = parse_positions_metadata(meta_line)
        if not name:
            # Fall back to filename-derived name with a synthetic acct id.
            name = _account_name_from_filename(path)
            acct_id = f"{name.upper()}:UNKNOWN"
        return self._get_or_create_account(session, name, acct_id)

    def _process_positions_file(
        self,
        session: Session,
        path: Path,
        accounts_by_name: dict[str, Account],
        result: AdapterResult,
    ) -> None:
        """Parse a Schwab positions file and insert PositionSnapshot rows."""
        try:
            rows = list(read_csv_tolerant(path))
        except Exception as exc:
            result.record_error(path.name, exc)
            return
        if len(rows) < 3:
            return

        meta_cells = rows[0]
        meta_line = meta_cells[0] if meta_cells else ""
        name, acct_id, as_of_dt = parse_positions_metadata(meta_line)
        if not name:
            name = _account_name_from_filename(path)
            acct_id = f"{name.upper()}:UNKNOWN"
        if as_of_dt is None:
            as_of_dt = datetime.now(UTC).replace(tzinfo=None)
        account = self._get_or_create_account(session, name, acct_id)
        accounts_by_name[name] = account

        # rows[0] = title, rows[1] = blank, rows[2] = header, rows[3:] = data.
        header = rows[2]
        try:
            cidx = {h.strip(): i for i, h in enumerate(header)}
        except Exception as exc:  # pragma: no cover — defensive
            result.record_error(path.name, exc)
            return

        for row_idx, raw_row in enumerate(rows[3:], start=3):
            if not raw_row or all(not c.strip() for c in raw_row):
                continue
            symbol = (raw_row[cidx.get("Symbol", 0)] if cidx else "").strip()
            if symbol in _POSITIONS_TRAILING_SYMBOLS:
                continue

            record_label = f"{path.name}:row{row_idx}"
            try:
                description = _safe_get(raw_row, cidx, "Description")
                quantity = parse_quantity(_safe_get(raw_row, cidx, "Qty (Quantity)"))
                price = parse_currency(_safe_get(raw_row, cidx, "Price"))
                market_value = parse_currency(
                    _safe_get(raw_row, cidx, "Mkt Val (Market Value)")
                )
                cost_basis = parse_currency(_safe_get(raw_row, cidx, "Cost Basis"))
                unrealized = parse_currency(
                    _safe_get(raw_row, cidx, "Gain $ (Gain/Loss $)")
                )

                row_hash = compute_position_row_hash(
                    broker=Broker.SCHWAB.value,
                    account_number=account.account_number,
                    source_file=path.name,
                    row_index=row_idx,
                    as_of_iso=as_of_dt.date().isoformat(),
                    symbol=symbol or None,
                    quantity=quantity,
                )

                if _position_exists(session, account.id, row_hash):
                    result.records_skipped += 1
                    result.records_processed += 1
                    continue

                snap = PositionSnapshot(
                    account_id=account.id,
                    as_of=as_of_dt,
                    symbol=symbol or None,
                    description=description or None,
                    quantity=quantity,
                    price=price,
                    market_value=market_value,
                    cost_basis=cost_basis,
                    unrealized_gain=unrealized,
                    source_file=path.name,
                    source_row_hash=row_hash,
                    raw_data=_zip_raw(header, raw_row),
                )
                session.add(snap)
                session.flush()  # P1-007: use flush per-row; commit once after loop
                result.records_created += 1
                result.records_processed += 1
            except Exception as exc:
                result.record_error(record_label, exc)
                result.records_processed += 1
                with contextlib.suppress(Exception):
                    session.rollback()

        # P1-007: single commit per positions file (not per row).
        with contextlib.suppress(Exception):
            session.commit()

    # ---- Transactions --------------------------------------------------------

    def _process_transactions_file(
        self,
        session: Session,
        path: Path,
        accounts_by_name: dict[str, Account],
        result: AdapterResult,
    ) -> None:
        """Parse a Schwab transactions CSV.

        Assumes header is the first row and every subsequent row is data
        (Schwab transactions exports do not have a metadata title row).
        """
        try:
            rows = list(read_csv_tolerant(path))
        except Exception as exc:
            result.record_error(path.name, exc)
            return
        if len(rows) < 2:
            return

        header = rows[0]
        cidx = {h.strip(): i for i, h in enumerate(header)}

        # Account discovery: prefer the positions-derived account, otherwise
        # synthesize one from the filename.
        name = _account_name_from_filename(path)
        account = accounts_by_name.get(name)
        if account is None:
            # Pull masked account-id token (XXX###) from filename if present.
            m = re.search(r"XXX([A-Z0-9]+)", path.name, re.IGNORECASE)
            acct_id = m.group(1) if m else f"{name.upper()}:UNKNOWN"
            account = self._get_or_create_account(session, name, acct_id)
            accounts_by_name[name] = account

        # Two-pass approach: parse all rows first so we can resolve reinvest
        # pairs by adjacency before any DB writes. Group by symbol+date for
        # per-symbol pairing safety in same-day batches.
        parsed: list[_ParsedTxRow] = []
        for row_idx, raw_row in enumerate(rows[1:], start=1):
            if not raw_row or all(not c.strip() for c in raw_row):
                continue
            try:
                parsed.append(_parse_tx_row(raw_row, cidx, row_idx, account, path.name))
            except Exception as exc:
                result.record_error(f"{path.name}:row{row_idx}", exc)

        # Resolve reinvest pairs. PLAN.md TASK-07:
        #   - ``Reinvest Shares`` pairs to the immediately-PRECEDING
        #     ``Reinvest Dividend`` row (same symbol).
        #   - ``Qual Div Reinvest`` / ``Pr Yr Div Reinvest`` / cap-gain
        #     reinvest cash rows pair to the immediately-FOLLOWING
        #     ``Reinvest Shares`` row (same symbol).
        # Both rules collapse to: each share row gets its
        # ``paired_transaction_id`` set to the nearest matching cash-side row
        # (in either direction, same symbol). We always set the FK on the
        # share side so the back-reference is unambiguous.
        cash_actions = {"Reinvest Dividend"} | _REINVEST_FOLLOWING_PAIR_ACTIONS
        pair_indices: dict[int, int] = {}  # share_idx -> cash_idx
        for i, row in enumerate(parsed):
            if row.action != "Reinvest Shares":
                continue
            # Scan immediate neighbors first (preceding, then following) for a
            # same-symbol cash-side row.
            picked: int | None = None
            if i > 0 and parsed[i - 1].symbol == row.symbol and parsed[i - 1].action in cash_actions:
                picked = i - 1
            elif (
                i + 1 < len(parsed)
                and parsed[i + 1].symbol == row.symbol
                and parsed[i + 1].action in cash_actions
            ):
                picked = i + 1
            if picked is not None:
                pair_indices[i] = picked

        # Insert all rows; remember each parsed row's DB id keyed by parse-index.
        inserted_ids: dict[int, str] = {}
        for i, row in enumerate(parsed):
            record_label = f"{path.name}:row{row.row_index}"
            try:
                existing_row = (
                    session.query(BrokerageTransaction.id)
                    .filter(
                        BrokerageTransaction.account_id == account.id,
                        BrokerageTransaction.source_row_hash == row.source_row_hash,
                    )
                    .scalar()
                )
                if existing_row is not None:
                    # P1-003: capture existing DB id so the backfill loop can still
                    # link a newly-inserted partner row to this pre-existing row.
                    inserted_ids[i] = existing_row
                    result.records_skipped += 1
                    result.records_processed += 1
                    continue
                tx = BrokerageTransaction(
                    account_id=account.id,
                    trade_date=row.trade_date,
                    settlement_date=row.settlement_date,
                    action=row.action,
                    canonical_action=row.canonical_action,
                    symbol=row.symbol,
                    description=row.description,
                    quantity=row.quantity,
                    price=row.price,
                    amount=row.amount,
                    commission=None,
                    fees=row.fees,
                    paired_transaction_id=None,
                    is_synthetic=False,
                    status=BrokerageTxStatus.IMPORTED.value,
                    source_file=path.name,
                    source_row_hash=row.source_row_hash,
                    raw_data=row.raw_data,
                )
                session.add(tx)
                session.flush()
                inserted_ids[i] = tx.id
                result.records_created += 1
                result.records_processed += 1
            except Exception as exc:
                result.record_error(record_label, exc)
                result.records_processed += 1
                with contextlib.suppress(Exception):
                    session.rollback()
                continue

        # Backfill paired_transaction_id on the share rows.
        for share_i, cash_i in pair_indices.items():
            cash_id = inserted_ids.get(cash_i)
            share_id = inserted_ids.get(share_i)
            if cash_id is None or share_id is None:
                continue
            share_tx = session.get(BrokerageTransaction, share_id)
            if share_tx is not None:
                share_tx.paired_transaction_id = cash_id

        with contextlib.suppress(Exception):
            session.commit()

    # ---- Gain/Loss -----------------------------------------------------------

    def _process_gainloss_file(
        self,
        session: Session,
        path: Path,
        accounts_by_name: dict[str, Account],
        result: AdapterResult,
    ) -> None:
        """Parse a Schwab realized G/L file → :class:`RealizedGainLoss`."""
        try:
            rows = list(read_csv_tolerant(path))
        except Exception as exc:
            result.record_error(path.name, exc)
            return
        if len(rows) < 3:
            return

        # Row 0 is the title. Row 1 is the header. Row 2+ data.
        header = rows[1]
        cidx = {h.strip(): i for i, h in enumerate(header)}

        name = _account_name_from_filename(path)
        account = accounts_by_name.get(name)
        if account is None:
            account = self._get_or_create_account(
                session, name, f"{name.upper()}:UNKNOWN"
            )
            accounts_by_name[name] = account

        for row_idx, raw_row in enumerate(rows[2:], start=2):
            if not raw_row or all(not c.strip() for c in raw_row):
                continue
            record_label = f"{path.name}:row{row_idx}"
            try:
                symbol = _safe_get(raw_row, cidx, "Symbol").strip()
                if not symbol:
                    continue
                description = _safe_get(raw_row, cidx, "Name") or None
                closed_date = parse_date_flexible(
                    _safe_get(raw_row, cidx, "Closed Date")
                )
                if closed_date is None:
                    raise ValueError("Missing Closed Date")
                opened_raw = _safe_get(raw_row, cidx, "Opened Date")
                opened_date = (
                    parse_date_flexible(opened_raw) if opened_raw and opened_raw != "Various" else None
                )
                quantity = parse_quantity(_safe_get(raw_row, cidx, "Quantity"))
                proceeds = parse_currency(_safe_get(raw_row, cidx, "Proceeds"))
                cost_basis = parse_currency(_safe_get(raw_row, cidx, "Cost Basis (CB)"))
                gain_loss = parse_currency(_safe_get(raw_row, cidx, "Gain/Loss ($)"))
                lt_gain_loss = parse_currency(
                    _safe_get(raw_row, cidx, "Long Term Gain/Loss")
                )
                st_gain_loss = parse_currency(
                    _safe_get(raw_row, cidx, "Short Term Gain/Loss")
                )
                unadjusted_cb = parse_currency(
                    _safe_get(raw_row, cidx, "Unadjusted Cost Basis")
                )
                term_raw = _safe_get(raw_row, cidx, "Term").strip()
                term: str | None
                if term_raw.lower().startswith("long"):
                    term = GainLossTerm.LONG.value
                elif term_raw.lower().startswith("short"):
                    term = GainLossTerm.SHORT.value
                else:
                    term = None
                wash_sale_raw = _safe_get(raw_row, cidx, "Wash Sale?").strip().lower()
                wash_sale = wash_sale_raw == "yes"
                disallowed = parse_currency(
                    _safe_get(raw_row, cidx, "Disallowed Loss")
                )

                # Enforce non-null contract on quantity / proceeds / cost_basis
                # / gain_loss — these are NOT NULL on the table.
                if quantity is None or proceeds is None or cost_basis is None or gain_loss is None:
                    raise ValueError(
                        "Missing required numeric (qty/proceeds/cost_basis/gain_loss)"
                    )

                row_hash = compute_realized_lot_hash(
                    broker=Broker.SCHWAB.value,
                    account_number=account.account_number,
                    source_file=path.name,
                    row_index=row_idx,
                    symbol=symbol,
                    closed_date=closed_date,
                    quantity=quantity,
                    proceeds=proceeds,
                    cost_basis=cost_basis,
                )

                if _realized_exists(session, account.id, row_hash):
                    result.records_skipped += 1
                    result.records_processed += 1
                    continue

                lot = RealizedGainLoss(
                    account_id=account.id,
                    symbol=symbol,
                    description=description,
                    opened_date=opened_date,
                    closed_date=closed_date,
                    quantity=quantity,
                    proceeds=proceeds,
                    cost_basis=cost_basis,
                    unadjusted_cost_basis=unadjusted_cb,
                    gain_loss=gain_loss,
                    lt_gain_loss=lt_gain_loss,
                    st_gain_loss=st_gain_loss,
                    term=term,
                    wash_sale=wash_sale,
                    disallowed_loss=disallowed,
                    source_file=path.name,
                    source_row_hash=row_hash,
                    raw_data=_zip_raw(header, raw_row),
                )
                session.add(lot)
                session.flush()  # P1-007: flush per-row; commit once after loop
                result.records_created += 1
                result.records_processed += 1
            except Exception as exc:
                result.record_error(record_label, exc)
                result.records_processed += 1
                with contextlib.suppress(Exception):
                    session.rollback()

        # P1-007: single commit per G/L file (not per row).
        with contextlib.suppress(Exception):
            session.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────


@dataclass
class _ParsedTxRow:
    row_index: int
    action: str
    canonical_action: str
    symbol: str | None
    description: str | None
    trade_date: date
    settlement_date: date | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    fees: Decimal | None
    source_row_hash: str
    raw_data: dict[str, Any]


def _parse_tx_row(
    raw_row: list[str],
    cidx: dict[str, int],
    row_idx: int,
    account: Account,
    source_file: str,
) -> _ParsedTxRow:
    """Parse one Schwab transactions row into a :class:`_ParsedTxRow`."""
    date_raw = _safe_get(raw_row, cidx, "Date")
    trade_date, settlement_date = parse_date_with_as_of(date_raw)
    if trade_date is None:
        raise ValueError(f"Unparseable Date: {date_raw!r}")

    action = _safe_get(raw_row, cidx, "Action").strip()
    symbol_raw = _safe_get(raw_row, cidx, "Symbol").strip()
    symbol = symbol_raw or None
    description = _safe_get(raw_row, cidx, "Description") or None
    quantity = parse_quantity(_safe_get(raw_row, cidx, "Quantity"))
    price = parse_currency(_safe_get(raw_row, cidx, "Price"))
    fees = parse_currency(_safe_get(raw_row, cidx, "Fees & Comm"))
    amount = parse_currency(_safe_get(raw_row, cidx, "Amount"))

    canonical = map_schwab_action(action, account.account_type)

    # P2-001 / REQ-005b: quantity must always be stored positive.
    # Schwab exports negative quantities for Sell (e.g. "-1,471"), Stock Split
    # can also be positive or negative. Normalize SELL to positive here;
    # the signed direction is already captured by canonical_action.
    if canonical == CanonicalAction.SELL.value and quantity is not None and quantity < 0:
        quantity = -quantity

    row_hash = compute_brokerage_row_hash(
        broker=Broker.SCHWAB.value,
        account_number=account.account_number,
        source_file=source_file,
        row_index=row_idx,
        trade_date=trade_date,
        action=action,
        symbol=symbol,
        quantity=quantity,
        amount=amount,
    )

    raw_data = {k: raw_row[v] for k, v in cidx.items() if v < len(raw_row)}
    return _ParsedTxRow(
        row_index=row_idx,
        action=action,
        canonical_action=canonical,
        symbol=symbol,
        description=description,
        trade_date=trade_date,
        settlement_date=settlement_date,
        quantity=quantity,
        price=price,
        amount=amount,
        fees=fees,
        source_row_hash=row_hash,
        raw_data=raw_data,
    )


def _safe_get(row: list[str], cidx: dict[str, int], key: str) -> str:
    """Return the cell at ``cidx[key]`` or ``""`` if absent / out of bounds."""
    i = cidx.get(key)
    if i is None or i >= len(row):
        return ""
    return row[i]


def _zip_raw(header: list[str], row: list[str]) -> dict[str, str]:
    """Build a {header: cell} dict for the raw_data JSON column."""
    return {h.strip(): (row[i] if i < len(row) else "") for i, h in enumerate(header)}


def _tx_exists(session: Session, account_id: str, row_hash: str) -> bool:
    return (
        session.query(BrokerageTransaction.id)
        .filter(
            BrokerageTransaction.account_id == account_id,
            BrokerageTransaction.source_row_hash == row_hash,
        )
        .first()
        is not None
    )


def _position_exists(session: Session, account_id: str, row_hash: str) -> bool:
    return (
        session.query(PositionSnapshot.id)
        .filter(
            PositionSnapshot.account_id == account_id,
            PositionSnapshot.source_row_hash == row_hash,
        )
        .first()
        is not None
    )


def _realized_exists(session: Session, account_id: str, row_hash: str) -> bool:
    return (
        session.query(RealizedGainLoss.id)
        .filter(
            RealizedGainLoss.account_id == account_id,
            RealizedGainLoss.source_row_hash == row_hash,
        )
        .first()
        is not None
    )


__all__ = [
    "SchwabCsvAdapter",
    "classify_file",
    "default_account_type",
    "map_schwab_action",
    "parse_positions_metadata",
]
