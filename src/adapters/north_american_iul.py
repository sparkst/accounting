"""North American Builder Plus IUL — indexed universal life balance importer.

Books a cash-value balance for the North American IUL policy into
``account_balance_snapshot``, upserting the owning ``Account`` row so the policy
surfaces in net worth alongside the other Phase-4 carriers (NW Mutual, F&G, GSK).

Modeling decisions (see CLAUDE.md "Amount Sign Convention" + the wealth design):

* **Surrender value is the net-worth figure.** Net worth must reflect what the
  policy could actually be *liquidated* for. A Builder Plus IUL carries a 10-year
  surrender-charge period, so the surrender value sits *below* the accumulation
  value (which itself starts below total premium paid). When only the
  accumulation value is known, we book it as a stopgap and emit a loud warning —
  it overstates realizable value until the surrender value replaces it.
* **One policy, one account.** Unlike the file-fed carriers this adapter takes
  the policy's known fields directly (portal/statement values), upserts the
  Account, and books one snapshot. A PDF parser can wrap this later.

DRY-RUN by default; ``Decimal(str(...))`` discipline via ``parse_currency``;
per-row ``begin_nested`` savepoint; idempotent on ``(policy, as_of, balance)``.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.ingestion import write_ingestion_log
from src.adapters._shared.money import parse_currency, quantize_balance
from src.adapters._shared.result import BaseImportResult
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity, IngestionStatus
from src.models.history import AccountBalanceSnapshot

logger = logging.getLogger(__name__)

SOURCE_TAG = "north_american_iul"
"""Value written to ``account_balance_snapshot.source`` and ``ingestion_log.source``."""

NA_BROKER = Broker.NORTH_AMERICAN.value
_DEFAULT_ACCOUNT_NAME = "North American Builder Plus IUL"


@dataclass
class ImportResult(BaseImportResult):
    """Summary of an IUL import run (inherits the shared counters/errors)."""


def _row_hash(policy_number: str, as_of: date, balance: Decimal) -> str:
    """SHA256 of the canonical row identity tuple (quantized balance)."""
    payload = "|".join(
        (policy_number, as_of.isoformat(), str(quantize_balance(balance)))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _upsert_account(
    session: Session,
    *,
    policy_number: str,
    account_name: str | None,
    beneficiary: str | None,
    notes: str | None,
) -> Account:
    """Find-or-create the IUL Account row; refresh mutable metadata if present."""
    account = (
        session.query(Account)
        .filter(
            Account.broker == NA_BROKER,
            Account.account_number == policy_number,
        )
        .first()
    )
    if account is None:
        account = Account(
            broker=NA_BROKER,
            account_number=policy_number,
            account_name=account_name or _DEFAULT_ACCOUNT_NAME,
            account_type=AccountType.OTHER.value,
            entity=Entity.PERSONAL.value,
            tax_sheltered=True,  # IUL cash value grows tax-deferred.
            beneficiary=beneficiary,
            notes=notes,
        )
        session.add(account)
        session.flush()  # assign account.id for the snapshot FK.
        return account

    # Existing account — refresh the human-set fields if the caller supplied them.
    if account_name is not None:
        account.account_name = account_name
    if beneficiary is not None:
        account.beneficiary = beneficiary
    if notes is not None:
        account.notes = notes
    return account


def import_policy(
    *,
    policy_number: str,
    as_of: date,
    surrender_value: object | None = None,
    accumulation_value: object | None = None,
    premium_paid: object | None = None,
    cost_basis: object | None = None,
    beneficiary: str | None = None,
    account_name: str | None = None,
    dry_run: bool = True,
    session: Session | None = None,
) -> ImportResult:
    """Import one North American IUL balance snapshot.

    Args:
        policy_number:      Carrier policy number (the Account ``account_number``).
        as_of:              Snapshot date.
        surrender_value:    Cash surrender value — the preferred net-worth figure.
        accumulation_value: Accumulation value — fallback when surrender is unknown
                            (overstates liquidation value; emits a warning).
        premium_paid:       Total premium paid to date (recorded in notes only).
        cost_basis:         Policy cost basis (recorded in notes only).
        beneficiary:        Beneficiary name for the Account row.
        account_name:       Display name for the Account row.
        dry_run:            When True, validate only; never write. Default True.
        session:            Required when ``dry_run`` is False.

    Returns:
        :class:`ImportResult` with counts and per-row errors/warnings.
    """
    result = ImportResult()
    result.distinct_accounts = [policy_number]

    # ── Resolve the balance: surrender value preferred, accumulation fallback. ──
    booked: Decimal | None = None
    try:
        if surrender_value is not None:
            booked = parse_currency(surrender_value)
        elif accumulation_value is not None:
            booked = parse_currency(accumulation_value)
            result.warnings.append(
                f"policy {policy_number}: no surrender value supplied — booked the "
                "accumulation value, which OVERSTATES liquidation value during the "
                "surrender-charge period. Replace once the surrender value is known."
            )
    except ValueError as exc:
        result.errors.append(f"policy {policy_number}: unparseable value: {exc}")
        booked = None

    if booked is None and not result.errors:
        result.errors.append(
            f"policy {policy_number}: neither surrender_value nor "
            "accumulation_value supplied — nothing to book"
        )

    if dry_run:
        return result
    if session is None:
        result.errors.append("session required when dry_run=False")
        return result
    if result.errors or booked is None:
        return result

    # Build a notes line capturing the context figures (audit, not P&L).
    note_bits: list[str] = []
    if accumulation_value is not None:
        note_bits.append(f"accumulation={parse_currency(accumulation_value)}")
    if premium_paid is not None:
        note_bits.append(f"premium_paid={parse_currency(premium_paid)}")
    if cost_basis is not None:
        note_bits.append(f"cost_basis={parse_currency(cost_basis)}")
    notes = "; ".join(note_bits) or None

    row_hash = _row_hash(policy_number, as_of, booked)
    record_label = f"policy {policy_number}@{as_of.isoformat()}"

    # Idempotency: same logical snapshot already present?
    existing = (
        session.query(AccountBalanceSnapshot.id)
        .filter(AccountBalanceSnapshot.source_row_hash == row_hash)
        .first()
    )
    if existing is not None:
        result.dup_skipped += 1
    else:
        # Upsert the Account AND book the snapshot in one savepoint, so a
        # snapshot-insert failure also rolls back a freshly-created Account
        # row (no orphan). SQLite SAVEPOINT scopes both the flush and the add.
        try:
            with session.begin_nested():
                account = _upsert_account(
                    session,
                    policy_number=policy_number,
                    account_name=account_name,
                    beneficiary=beneficiary,
                    notes=notes,
                )
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        raw_account_name=account.account_name or _DEFAULT_ACCOUNT_NAME,
                        as_of=as_of,
                        balance=booked,
                        source=SOURCE_TAG,
                        source_row_hash=row_hash,
                    )
                )
            result.imported += 1
            result.matched += 1
        except IntegrityError:
            result.dup_skipped += 1
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            result.errors.append(f"{record_label}: {exc}")
            logger.warning("north_american_iul: %s failed: %s", record_label, exc,
                           exc_info=True)

    # Exactly one IngestionLog per call (dedup-skip falls through to here).
    status = (
        IngestionStatus.SUCCESS if not result.errors else IngestionStatus.PARTIAL_FAILURE
    )
    write_ingestion_log(
        session,
        source=SOURCE_TAG,
        records_processed=result.imported + result.dup_skipped,
        records_failed=len(result.errors),
        status=status,
        error_detail="\n".join(result.errors) or None,
    )
    session.commit()
    return result


# ── CLI ────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="north_american_iul",
        description="Import a North American Builder Plus IUL balance snapshot.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import-policy", help="Book one IUL balance snapshot.")
    s.add_argument("--policy", required=True, help="Carrier policy number.")
    s.add_argument("--as-of", required=True, help="Snapshot date YYYY-MM-DD.")
    s.add_argument("--surrender", help="Cash surrender value (preferred).")
    s.add_argument("--accumulation", help="Accumulation value (fallback).")
    s.add_argument("--premium-paid", help="Total premium paid to date (notes).")
    s.add_argument("--cost-basis", help="Policy cost basis (notes).")
    s.add_argument("--beneficiary", help="Beneficiary name.")
    s.add_argument("--account-name", help="Account display name.")
    s.add_argument("--apply", action="store_true",
                   help="Write to the DB (default is DRY-RUN).")
    return p


def _print_summary(result: ImportResult, *, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"[north_american_iul] {mode}")
    print(f"  imported={result.imported} dup_skipped={result.dup_skipped}")
    for w in result.warnings:
        print(f"  ⚠ {w}")
    for e in result.errors:
        print(f"  ✗ {e}")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "import-policy":
        return 2
    as_of = date.fromisoformat(args.as_of)
    dry_run = not args.apply

    common = dict(
        policy_number=args.policy,
        as_of=as_of,
        surrender_value=args.surrender,
        accumulation_value=args.accumulation,
        premium_paid=args.premium_paid,
        cost_basis=args.cost_basis,
        beneficiary=args.beneficiary,
        account_name=args.account_name,
    )

    if dry_run:
        result = import_policy(**common, dry_run=True)
        _print_summary(result, dry_run=True)
        return 0

    try:
        from src.db.connection import get_session  # late import keeps tests light
    except ImportError as exc:  # pragma: no cover — environmental
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        result = import_policy(**common, dry_run=False, session=session)
    _print_summary(result, dry_run=False)
    return 1 if result.errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
