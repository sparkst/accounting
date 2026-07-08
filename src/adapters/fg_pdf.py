"""F&G annuity PDF adapter (Phase 4 — T3).

Extracts the contract number, as-of date, and Total Account Value from one of
two F&G PDF flavors and writes a single :class:`AccountBalanceSnapshot`:

  * **annual** — the mailed *Annual Statement of Policy Values* PDF.
    Recognised by the literal ``Contract #:`` field. Two
    "Total Account Value as of <DATE>" rows appear (start-of-period and
    end-of-period); we keep the LAST match because that's the current value.
  * **portal** — the online *My Policy Details* screen-grab. Recognised by
    the literal ``Policy number`` field. The body has no statement date, so
    we look for ``Values displayed are current as of MM/DD/YYYY`` and fall
    back to the file mtime (or a CLI ``--as-of`` override).

The adapter follows the canonical pattern from
``src/adapters/xlsx_savings_plan.py``:
  * :class:`ImportResult` dataclass mirrors the savings-plan importer.
  * ``dry_run=True`` by default — the live DB is only touched on ``--apply``.
  * Per-row ``session.begin_nested()`` so an :class:`IntegrityError` rolls back
    only that row.
  * ``source_row_hash`` quantises the balance to two decimal places before
    hashing so a re-import of the same value with different trailing-zero
    formatting is correctly recognised as a duplicate.
  * :class:`IngestionLog` row written on apply.
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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.ingestion import write_cloud_ingestion_log, write_ingestion_log
from src.adapters._shared.money import parse_currency, quantize_balance
from src.adapters._shared.pdf import pdftotext_layout
from src.adapters._shared.result import BaseImportResult
from src.adapters._shared.wealth_client import WealthClientError, post_to_wealth
from src.models.brokerage import Account
from src.models.enums import Broker, IngestionStatus
from src.models.history import AccountBalanceSnapshot

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_TAG = "fg_pdf"
"""Value written to ``account_balance_snapshot.source``."""

ADAPTER_NAME = "fg_pdf"

# Cloud target — uses the xlsx-snapshot ingest path (AccountBalanceSnapshot rows).
_CLOUD_INGEST_SOURCE = "xlsx-snapshot"
_CLOUD_LOG_SOURCE = "wealth_cloud:fg_pdf"
"""``ingestion_log.source`` for the cloud-push path (REQ-FIX-WLT-007)."""


def _default_target() -> str:
    """Return the effective target from ``WEALTH_TARGET_DEFAULT`` env (default 'local')."""
    return os.environ.get("WEALTH_TARGET_DEFAULT", "local") or "local"
"""Identifier written to ``ingestion_log.source``."""

BROKER = Broker.FG_ANNUITY.value
"""Account.broker value to look up by."""


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class ImportResult(BaseImportResult):
    """Summary of a single PDF import run.

    Inherits shared fields (``imported``, ``matched``, ``unmatched``,
    ``dup_skipped``, ``errors``, ``warnings``, ``distinct_accounts``) from
    :class:`~src.adapters._shared.result.BaseImportResult`. No
    adapter-specific extra fields are required for F&G.
    """


# ── Regexes ──────────────────────────────────────────────────────────────────


# Annual statement: ``Contract #:        MZ152585``
_RE_ANNUAL_CONTRACT = re.compile(r"Contract\s*#:\s*([A-Z0-9]+)")
# Annual statement: ``Total Account Value as of MM/DD/YYYY      $   660,218.55``.
# Both start-of-period and end-of-period rows match; we keep the LAST one.
_RE_ANNUAL_BALANCE = re.compile(
    r"Total Account Value as of\s+(\d{2}/\d{2}/\d{4})\s+\$\s*([\d,]+\.\d\d)"
)

# Portal screen: ``Policy number          MZ152585``
_RE_PORTAL_POLICY = re.compile(r"Policy number\s+([A-Z0-9]+)")
# Portal screen: ``Total account value                          $660,218.55``
_RE_PORTAL_BALANCE = re.compile(
    r"Total account value\s+\$\s*([\d,]+\.\d\d)"
)
# Portal screen: ``Values displayed are current as of MM/DD/YYYY.``
_RE_PORTAL_AS_OF = re.compile(
    r"Values displayed are current as of\s+(\d{2}/\d{2}/\d{4})"
)


def _parse_us_date(s: str) -> date:
    """Parse a ``MM/DD/YYYY`` string to :class:`date`."""
    return datetime.strptime(s, "%m/%d/%Y").date()


# ── Pure extraction helpers ──────────────────────────────────────────────────


def extract_annual_statement(text: str) -> tuple[str, date, Decimal]:
    """Return ``(contract, as_of, balance)`` from an annual-statement text dump.

    Raises :class:`ValueError` if either field cannot be found. The balance
    comes from the LAST "Total Account Value as of …" row (current value).
    """
    contract_m = _RE_ANNUAL_CONTRACT.search(text)
    if contract_m is None:
        raise ValueError("annual statement: Contract # not found")
    contract = contract_m.group(1)

    matches = _RE_ANNUAL_BALANCE.findall(text)
    if not matches:
        raise ValueError("annual statement: Total Account Value rows not found")
    # Last match is the current value (the document repeats the line for the
    # surrender-value section too — pick the most recent).
    date_str, amount_str = matches[-1]
    return contract, _parse_us_date(date_str), parse_currency(amount_str)


def extract_portal_screen(
    text: str, fallback_as_of: date
) -> tuple[str, date, Decimal]:
    """Return ``(contract, as_of, balance)`` from a portal-screen text dump.

    The body has no statement date, so we look for
    ``Values displayed are current as of MM/DD/YYYY`` and fall back to
    ``fallback_as_of`` (caller's responsibility — usually file mtime or
    ``--as-of`` override). Raises :class:`ValueError` if the policy number
    or balance cannot be found.
    """
    policy_m = _RE_PORTAL_POLICY.search(text)
    if policy_m is None:
        raise ValueError("portal screen: Policy number not found")
    contract = policy_m.group(1)

    balance_m = _RE_PORTAL_BALANCE.search(text)
    if balance_m is None:
        raise ValueError("portal screen: Total account value not found")
    balance = parse_currency(balance_m.group(1))

    as_of_m = _RE_PORTAL_AS_OF.search(text)
    as_of = _parse_us_date(as_of_m.group(1)) if as_of_m else fallback_as_of

    return contract, as_of, balance


def detect_template(text: str) -> Literal["annual", "portal"]:
    """Auto-detect which F&G PDF flavor the text came from.

    Returns ``"annual"`` if a ``Contract #:`` field is present, else
    ``"portal"`` if a ``Policy number`` field is present. Raises
    :class:`ValueError` if neither matches.
    """
    if _RE_ANNUAL_CONTRACT.search(text):
        return "annual"
    if _RE_PORTAL_POLICY.search(text):
        return "portal"
    raise ValueError("F&G PDF: cannot detect template (no Contract # or Policy number)")


# ── Hashing ──────────────────────────────────────────────────────────────────


def _row_hash(contract: str, as_of: date, balance: Decimal) -> str:
    """SHA256 hex of the canonical row identity tuple.

    Quantising the balance to two decimal places ensures
    ``Decimal("660218.5")`` and ``Decimal("660218.50")`` collide as expected.
    """
    payload = "|".join(
        (contract, as_of.isoformat(), str(quantize_balance(balance)))
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
    """Import a single F&G PDF and write one ``AccountBalanceSnapshot``.

    Args:
        path: Path to the PDF file.
        dry_run: When True, parse-and-count only; never write to ``session``.
            Default True to protect the live DB during exploration.
        session: SQLAlchemy session. Required when ``dry_run`` is False.
        as_of: Override the as-of date. Used only by the portal flavor when
            the body has no embedded statement date. If omitted, the file's
            ``mtime`` is used as the fallback.

    Returns:
        :class:`ImportResult` with at most one inserted row.
    """
    result = ImportResult()

    # Parse the PDF up-front; any extraction failure becomes a single error
    # and short-circuits — we never raise out of this function.
    try:
        text = pdftotext_layout(path)
    except (FileNotFoundError, RuntimeError) as exc:
        result.errors.append(f"{path.name}: pdftotext failed: {exc}")
        if session is not None and not dry_run:
            write_ingestion_log(
                session,
                source=ADAPTER_NAME,
                records_processed=0,
                records_failed=1,
                status=IngestionStatus.FAILURE,
                error_detail=result.errors[-1],
            )
        return result

    fallback = as_of or _file_mtime_date(path)

    try:
        flavor = detect_template(text)
        if flavor == "annual":
            contract, snap_as_of, balance = extract_annual_statement(text)
            # If caller explicitly supplied --as-of, honour it (operator wins
            # over the document — useful when re-tagging a backfill).
            if as_of is not None:
                snap_as_of = as_of
        else:  # "portal"
            contract, snap_as_of, balance = extract_portal_screen(text, fallback)
    except ValueError as exc:
        result.errors.append(f"{path.name}: {exc}")
        if session is not None and not dry_run:
            write_ingestion_log(
                session,
                source=ADAPTER_NAME,
                records_processed=0,
                records_failed=1,
                status=IngestionStatus.FAILURE,
                error_detail=result.errors[-1],
            )
        return result

    raw_account_name = f"F&G Annuity {contract}"
    result.distinct_accounts = [contract]

    if dry_run:
        # We can't tell matched vs dup_skipped without the DB.
        result.unmatched = 1
        return result

    if session is None:
        result.errors.append("session required when dry_run=False")
        return result

    # ── Apply ────────────────────────────────────────────────────────────────
    record_label = f"{contract}@{snap_as_of.isoformat()}"
    row_hash = _row_hash(contract, snap_as_of, balance)

    # Account lookup; missing → error, no snapshot written.
    account = (
        session.query(Account)
        .filter(Account.broker == BROKER, Account.account_number == contract)
        .first()
    )
    if account is None:
        msg = (
            f"{record_label}: no Account row for broker={BROKER!r} "
            f"account_number={contract!r}; pre-seed via "
            f"`seed_expected_accounts confirm`"
        )
        result.errors.append(msg)
        write_ingestion_log(
            session,
            source=ADAPTER_NAME,
            records_processed=0,
            records_failed=1,
            status=IngestionStatus.FAILURE,
            error_detail=msg,
        )
        return result

    # Dedup pre-flight (cheap; saves a savepoint roundtrip on the common path).
    existing = (
        session.query(AccountBalanceSnapshot.id)
        .filter(AccountBalanceSnapshot.source_row_hash == row_hash)
        .first()
    )
    if existing is not None:
        result.dup_skipped += 1
        # Fall through to the single commit+log path below.
    else:
        try:
            with session.begin_nested():
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        raw_account_name=raw_account_name,
                        as_of=snap_as_of,
                        balance=balance,
                        source=SOURCE_TAG,
                        source_row_hash=row_hash,
                    )
                )
            result.imported = 1
            result.matched = 1
        except IntegrityError:
            # Either the natural-key UNIQUE or the hash UNIQUE caught a duplicate
            # we missed in the pre-flight (race / re-import edge case).
            result.dup_skipped += 1
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{record_label}: {exc}")
            logger.warning(
                "fg_pdf: row %s failed: %s", record_label, exc, exc_info=True
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


def _file_mtime_date(path: Path) -> date:
    """Return ``path``'s mtime truncated to a UTC date.

    Falls back to today if the file doesn't exist (caller has already errored
    in that case, but we still need a non-None date for type purity).
    """
    try:
        ts = path.stat().st_mtime
    except OSError:
        return datetime.now(tz=UTC).date()
    return datetime.fromtimestamp(ts, tz=UTC).date()


# ── Cloud-mode ───────────────────────────────────────────────────────────────


def import_pdf_cloud(
    path: Path,
    *,
    as_of: date | None = None,
    session: Session | None = None,
) -> ImportResult:
    """Parse an F&G PDF and POST one AccountBalanceSnapshot row to Workers.

    Amount sign convention: balance is a positive monetary value (asset value).

    When ``session`` is supplied, exactly one local IngestionLog row is
    written per run (success and error paths alike) so cloud pushes surface
    in delivery-health exactly like local imports (REQ-FIX-WLT-007).
    Backward compatible: with no session the log write is skipped.
    """
    result = ImportResult()

    try:
        text = pdftotext_layout(path)
    except (FileNotFoundError, RuntimeError) as exc:
        result.errors.append(f"{path.name}: pdftotext failed: {exc}")
        write_cloud_ingestion_log(session, source=_CLOUD_LOG_SOURCE, result=result)
        return result

    fallback = as_of or _file_mtime_date(path)

    try:
        flavor = detect_template(text)
        if flavor == "annual":
            contract, snap_as_of, balance = extract_annual_statement(text)
            if as_of is not None:
                snap_as_of = as_of
        else:
            contract, snap_as_of, balance = extract_portal_screen(text, fallback)
    except ValueError as exc:
        result.errors.append(f"{path.name}: {exc}")
        write_cloud_ingestion_log(session, source=_CLOUD_LOG_SOURCE, result=result)
        return result

    raw_account_name = f"F&G Annuity {contract}"
    result.distinct_accounts = [contract]
    row_hash = _row_hash(contract, snap_as_of, balance)

    payload_row = {
        "raw_account_name": raw_account_name,
        "as_of": snap_as_of.isoformat(),
        "balance": str(quantize_balance(balance)),
        "source": SOURCE_TAG,
        "source_row_hash": row_hash,
    }
    try:
        post_to_wealth({"rows": [payload_row]}, _CLOUD_INGEST_SOURCE)
        result.imported = 1
        result.unmatched = 1
    except WealthClientError as exc:
        label = f"{contract}@{snap_as_of.isoformat()}"
        result.errors.append(f"{label}: cloud POST error — {exc}")
        logger.error(
            "fg_pdf cloud POST error: %s — %s", label, type(exc).__name__,
        )

    write_cloud_ingestion_log(session, source=_CLOUD_LOG_SOURCE, result=result)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.adapters.fg_pdf",
        description="Import F&G annuity statement / portal-screen PDFs.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser(
        "import-pdf",
        help="Parse one F&G PDF and write one AccountBalanceSnapshot.",
    )
    s.add_argument("--file", required=True, help="Path to the PDF.")
    s.add_argument(
        "--target",
        choices=["local", "cloud"],
        default=None,
        help=(
            "Where to write rows: 'local' (SQLite) or 'cloud' (Workers ingest API). "
            "Default: WEALTH_TARGET_DEFAULT env var (fallback 'local')."
        ),
    )
    s.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the live DB (local) or POST to Workers (cloud). Default is dry-run.",
    )
    s.add_argument(
        "--as-of",
        default=None,
        help=(
            "Override the as-of date (YYYY-MM-DD). Used by portal flavor when "
            "the document has no embedded date, or to override an annual "
            "statement's date during a backfill."
        ),
    )
    return p


def _print_summary(result: ImportResult, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"=== fg_pdf ({mode}) ===")
    print(f"  imported     : {result.imported}")
    print(f"  matched      : {result.matched}")
    print(f"  unmatched    : {result.unmatched}")
    print(f"  dup_skipped  : {result.dup_skipped}")
    print(f"  errors       : {len(result.errors)}")
    if result.distinct_accounts:
        print(f"  contract     : {result.distinct_accounts[0]}")
    if result.errors:
        print("  --- error detail ---")
        for e in result.errors:
            print(f"    * {e}")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "import-pdf":
        return 2

    as_of: date | None = None
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()

    path = Path(args.file)
    dry_run = not args.apply
    target = args.target if args.target is not None else _default_target()

    if dry_run:
        result = import_pdf(path, dry_run=True, as_of=as_of)
        _print_summary(result, dry_run=True)
        return 0

    if target == "cloud":
        # Cloud pushes still write a LOCAL IngestionLog row so delivery-health
        # surfaces the push (REQ-FIX-WLT-007); open a local session for it.
        try:
            from src.db.connection import get_session  # late import keeps tests light
        except ImportError:
            result = import_pdf_cloud(path, as_of=as_of, session=None)
        else:
            with get_session() as session:
                result = import_pdf_cloud(path, as_of=as_of, session=session)
        _print_summary(result, dry_run=False)
        return 1 if result.errors else 0

    try:
        from src.db.connection import get_session  # late import keeps tests light
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_pdf(path, dry_run=False, session=session, as_of=as_of)

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
    "BROKER",
    "ImportResult",
    "SOURCE_TAG",
    "detect_template",
    "extract_annual_statement",
    "extract_portal_screen",
    "import_pdf",
    "import_pdf_cloud",
]
