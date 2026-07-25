#!/usr/bin/env python3
"""One-time remediation for two B&O-measure classification defects (2026-07-25).

Found while verifying the Sparkry June-2026 and BlackLine Q2-2026 WA B&O
returns. Both defects inflate *gross receipts* — the B&O measure — so both
overstate tax due until corrected.

  1. REQ-FIX-BO-001 — processor payout legs booked as revenue.
     A Stripe/Shopify payout landing in the bank is the *settlement* of sales
     already booked by the Stripe/Shopify adapter at charge/order level. It is
     an internal transfer, not a second helping of revenue (CLAUDE.md,
     "Reconciliation vs dedup"). The classifier read the inbound ACH as
     income, so e.g. BlackLine's $86.47 Shopify payout on 2026-04-13 was
     counted as SALES_INCOME *on top of* order #1023 — the $89.36 sale it
     settles. Corrected to ``direction="transfer"`` with ``tax_category=NULL``
     and ``deductible_pct=0.0``; the amount is never touched.

     Two rows dated 2026-03-09 / 2026-03-30 already carry that shape (fixed by
     hand earlier), which is where the target state comes from. Those rows
     matched the old ``ORIG CO NAME:SHOPIFY ORIG ID:SHOPIFYPMT`` descriptor;
     the miss was the newer ``ORIG CO NAME:Shopify ORIG ID:<number>`` form, so
     ``PAYOUT_MARKERS`` matches case-insensitively on the originator name only.

     GUARDED: a payout is only reclassified when the revenue it settles is
     actually in the register (``has_backing_revenue``). Stripe and Shopify
     ingestion both stopped on 2026-06-08, so the 2026-07-06 and 2026-07-20
     payouts currently have no order/charge rows behind them — flipping those
     to ``transfer`` would delete real revenue from the books rather than
     de-duplicate it. Those rows are reported as BLOCKED and left alone until
     the adapters are re-run. Never silently open a revenue hole.

  2. REQ-FIX-BO-002 — Gmail vendor receipts booked as SUBSCRIPTION_INCOME.
     Receipts Travis *paid* (ElevenLabs, Vercel) were classified as
     subscription INCOME, adding the amount to gross receipts. Each one is
     also a duplicate: the same card charge is already in the register from
     Plaid, correctly booked as an expense against ``amex_31004``. Per
     CLAUDE.md ("Plaid is sole source of truth per linked account") the Gmail
     row is superseded — corrected to ``status="rejected"``,
     ``review_reason="superseded_by_plaid"``, matching the existing precedent.

     Rejecting is deliberate rather than re-categorising to an expense: the
     Plaid twin already carries the expense, so re-categorising would
     double-count the deduction instead of removing phantom income.

     GUARDED: only rejected when a non-rejected Plaid twin is found
     (``find_plaid_twin``). A Gmail receipt with no twin is real, unduplicated
     data — it is reported for manual re-categorisation, never rejected.

Register invariants honored (REQ-FIX-BO-003), matching
``scripts/remediate_plaid_mirrors.py``: rows are never deleted, every field
change writes an ``AuditEvent``, each row is wrapped in its own savepoint so
one bad row cannot halt the batch, split parents/children are left alone and
reported, and re-running after ``--apply`` reports zero changes.

Usage:
    python -m scripts.remediate_bo_classification            # dry-run
    python -m scripts.remediate_bo_classification --apply    # commit
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.adapters.plaid_transactions import CARD_PAYMENT_DIRECTION_TAX_FIELDS
from src.db.connection import get_session
from src.models.audit_event import AuditEvent
from src.models.enums import Direction, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.utils.constants import SPARKRY_CONTACT_EMAIL  # noqa: F401  (module import parity)

logger = logging.getLogger("remediate_bo_classification")

# ---------------------------------------------------------------------------
# Defect 1 — processor payout legs (REQ-FIX-BO-001)
# ---------------------------------------------------------------------------

#: Lowercased bank-descriptor markers identifying a processor payout arriving
#: in the bank, mapped to the adapter whose rows carry the underlying revenue.
#: Matched on the ACH originator name so both the ``SHOPIFY ORIG
#: ID:SHOPIFYPMT`` and ``Shopify ORIG ID:<number>`` descriptor forms hit.
#:
#: A bare vendor name is deliberately NOT a marker: the register also holds
#: Shopify *fee* debits described simply as "Shopify" (e.g. -$42.65 on
#: 2026-06-02, a genuine SUPPLIES expense). Those carry no "ORIG CO NAME:"
#: prefix and are further excluded by the positive-amount rule below.
PAYOUT_MARKERS: dict[str, str] = {
    "orig co name:shopify": Source.SHOPIFY.value,
    "orig co name:stripe payment": Source.STRIPE.value,
}

#: A payout is money IN. Requiring a positive amount keeps outbound rows
#: carrying the same originator (processor fee debits, chargebacks) out of
#: scope — reclassifying one of those to ``transfer`` would silently erase a
#: real deduction.
#:
#: NOTE the register's sign convention (CLAUDE.md): income/transfer are stored
#: positive, expenses negative, so this is a direction test, not a magnitude one.
_PAYOUT_MIN_AMOUNT = Decimal("0")

#: How far back to look for the sales a payout settles. Stripe's standard
#: rolling payout is T+2 and Shopify's is T+2..T+5; 14 days covers both plus a
#: weekend/holiday tail without reaching into the prior month's activity.
BACKING_LOOKBACK_DAYS = 14

#: The shape an internal-transfer leg carries: no tax metadata at all. Imported
#: rather than re-declared so this script and the live adapter's INSERT path
#: cannot drift — same concept ("a transfer is not P&L"), same three fields.
PAYOUT_DIRECTION_TAX_FIELDS = CARD_PAYMENT_DIRECTION_TAX_FIELDS

# ---------------------------------------------------------------------------
# Defect 2 — Gmail receipts superseded by Plaid (REQ-FIX-BO-002)
# ---------------------------------------------------------------------------

GMAIL_SUPERSEDE_REASON = "superseded_by_plaid"

#: Gmail receipt dates come from the email, Plaid's from the posting feed, so
#: the same charge can differ by a day or two either way.
TWIN_WINDOW_DAYS = 3

#: Income categories. A Gmail row carrying one of these AND a negative amount
#: is self-contradictory: the adapter stores every receipt as ``-abs(amount)``
#: because a receipt is something Travis paid (CLAUDE.md, "Adapter behavior"),
#: so an income category on it is always a classifier error.
INCOME_CATEGORIES: tuple[str, ...] = (
    TaxCategory.CONSULTING_INCOME.value,
    TaxCategory.SUBSCRIPTION_INCOME.value,
    TaxCategory.SALES_INCOME.value,
    TaxCategory.WHOLESALE_INCOME.value,
)

ACTOR = "remediation:bo-classification-2026-07-25"

_SPLIT_PARENT = TransactionStatus.SPLIT_PARENT.value
_REJECTED = TransactionStatus.REJECTED.value


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------


@dataclass
class Change:
    """One planned or applied field change, for the dry-run/apply table."""

    target: str
    detail: str
    field: str
    old: str | None
    new: str | None


@dataclass
class RemediationResult:
    payouts: list[Change] = field(default_factory=list)
    gmail_supersedes: list[Change] = field(default_factory=list)
    #: Payout rows whose backing revenue is NOT in the register. Reported and
    #: left alone: reclassifying them would remove revenue that nothing else
    #: accounts for. Resolve by re-running the Stripe/Shopify adapters.
    blocked_payouts: list[str] = field(default_factory=list)
    #: Gmail income rows with no Plaid twin — real, unduplicated data needing a
    #: human to pick the right expense category.
    unmatched_gmail: list[str] = field(default_factory=list)
    skipped_splits: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.payouts) + len(self.gmail_supersedes)


def _audit(
    session: Session, tx: Transaction, *, field_changed: str, old: object, new: object
) -> None:
    """Transaction-mode AuditEvent (mirrors src/api/routes/transactions.py)."""
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed=field_changed,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
            changed_by=ACTOR,
        )
    )


def _label(tx: Transaction) -> str:
    amt = tx.amount or Decimal(0)
    return f"{tx.date} {tx.entity or '-':9} {str(tx.description or '')[:38]:38} {amt:>10.2f}"


def _descriptor(tx: Transaction) -> str:
    """Lowercased searchable descriptor: description + Plaid's raw name fields.

    Plaid puts the bank descriptor in ``name``; some institutions add detail in
    ``original_description``. Reading both (plus the stored description) means
    a row whose description was edited by hand still matches on its raw data.
    """
    raw = tx.raw_data or {}
    parts = [
        tx.description or "",
        str(raw.get("name") or ""),
        str(raw.get("original_description") or ""),
    ]
    return " ".join(parts).lower()


def payout_backing_source(tx: Transaction) -> str | None:
    """Return the adapter that owns this payout's revenue, or None if not a payout."""
    descriptor = _descriptor(tx)
    for marker, backing_source in PAYOUT_MARKERS.items():
        if marker in descriptor:
            return backing_source
    return None


# ---------------------------------------------------------------------------
# Defect 1 — find / guard / apply
# ---------------------------------------------------------------------------


def has_backing_revenue(session: Session, tx: Transaction, backing_source: str) -> bool:
    """True when the sales this payout settles are already in the register.

    Looks for any non-rejected row from the payout's own processor, for the
    same entity, dated in ``[payout_date - BACKING_LOOKBACK_DAYS, payout_date]``.
    Dates are ISO ``YYYY-MM-DD`` strings, so lexical comparison is chronological.

    A malformed date is treated as "no backing revenue" — the conservative
    answer, since the consequence of a wrong True is deleting real income.
    """
    try:
        payout_date = date_cls.fromisoformat(tx.date)
    except (TypeError, ValueError):
        return False
    window_start = (payout_date - timedelta(days=BACKING_LOOKBACK_DAYS)).isoformat()
    return (
        session.query(Transaction.id)
        .filter(
            Transaction.source == backing_source,
            Transaction.entity == tx.entity,
            Transaction.status != _REJECTED,
            Transaction.date >= window_start,
            Transaction.date <= tx.date,
        )
        .first()
        is not None
    )


def find_payout_rows(session: Session) -> list[tuple[Transaction, str]]:
    """Non-rejected inbound Plaid rows that are processor payouts.

    Rows already carrying every field in ``PAYOUT_DIRECTION_TAX_FIELDS`` are
    excluded in SQL, which is what makes a second run report zero changes.
    """
    candidates = (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.PLAID.value,
            Transaction.status != _REJECTED,
            Transaction.amount > _PAYOUT_MIN_AMOUNT,
            or_(
                Transaction.direction != Direction.TRANSFER.value,
                Transaction.direction.is_(None),
                Transaction.tax_category.isnot(None),
                Transaction.deductible_pct != 0.0,
            ),
        )
        .order_by(Transaction.date, Transaction.id)
        .all()
    )
    matched: list[tuple[Transaction, str]] = []
    for tx in candidates:
        backing_source = payout_backing_source(tx)
        if backing_source is not None:
            matched.append((tx, backing_source))
    return matched


def reclassify_payouts(
    session: Session, rows: list[tuple[Transaction, str]], result: RemediationResult
) -> None:
    """Apply the transfer shape to each payout whose backing revenue exists."""
    for tx, backing_source in rows:
        if tx.status == _SPLIT_PARENT or tx.parent_id is not None:
            # Re-directing a parent without its children (or vice versa) would
            # leave the two halves of one split disagreeing about P&L.
            result.skipped_splits.append(f"payout (split) {_label(tx)}")
            continue
        if not has_backing_revenue(session, tx, backing_source):
            result.blocked_payouts.append(
                f"{_label(tx)}  [no non-rejected {backing_source} rows within "
                f"{BACKING_LOOKBACK_DAYS}d — re-run the {backing_source} adapter first]"
            )
            continue
        try:
            # Report only the fields that ACTUALLY changed. Two rows were
            # hand-fixed to direction=transfer earlier but kept
            # deductible_pct=1.0; reporting a blanket "direction: transfer ->
            # transfer" for those would read as a no-op and hide the real
            # correction.
            changed: list[tuple[str, object, object]] = []
            with session.begin_nested():
                for field_name, new_value in PAYOUT_DIRECTION_TAX_FIELDS.items():
                    old_value = getattr(tx, field_name)
                    if old_value != new_value:
                        _audit(
                            session, tx, field_changed=field_name,
                            old=old_value, new=new_value,
                        )
                        setattr(tx, field_name, new_value)
                        changed.append((field_name, old_value, new_value))
                session.flush()
            result.payouts.append(
                Change(
                    _label(tx),
                    f"settles {backing_source}",
                    ", ".join(name for name, _, _ in changed),
                    ", ".join(str(old) if old is not None else "NULL"
                              for _, old, _ in changed),
                    ", ".join(str(new) if new is not None else "NULL"
                              for _, _, new in changed),
                )
            )
        except Exception:
            result.failed.append(f"payout {_label(tx)}")
            logger.exception("payout reclass failed", extra={"transaction_id": tx.id})


# ---------------------------------------------------------------------------
# Defect 2 — find / guard / apply
# ---------------------------------------------------------------------------


def find_gmail_income_rows(session: Session) -> list[Transaction]:
    """Non-rejected Gmail receipts carrying an income tax_category.

    The negative-amount filter is what makes this a *receipt* (money out) —
    see ``INCOME_CATEGORIES``. Rows with a NULL amount are excluded: those are
    the parser's "amount is missing" rows, already rejected on their own merits.
    """
    return (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.GMAIL_N8N.value,
            Transaction.status != _REJECTED,
            Transaction.tax_category.in_(INCOME_CATEGORIES),
            Transaction.amount.isnot(None),
            Transaction.amount < 0,
        )
        .order_by(Transaction.date, Transaction.id)
        .all()
    )


def find_plaid_twin(session: Session, tx: Transaction) -> Transaction | None:
    """The non-rejected Plaid row for the same charge, or None.

    Matched on entity + identical absolute amount + date within
    ``TWIN_WINDOW_DAYS``. Amount equality is exact (both sides are stored
    ``NUMERIC(12,2)``), so this cannot pair up merely-similar charges.
    """
    try:
        receipt_date = date_cls.fromisoformat(tx.date)
    except (TypeError, ValueError):
        return None
    if tx.amount is None:
        return None
    window = timedelta(days=TWIN_WINDOW_DAYS)
    return (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.PLAID.value,
            Transaction.entity == tx.entity,
            Transaction.status != _REJECTED,
            func.abs(Transaction.amount) == abs(tx.amount),
            Transaction.date >= (receipt_date - window).isoformat(),
            Transaction.date <= (receipt_date + window).isoformat(),
        )
        .order_by(Transaction.date, Transaction.id)
        .first()
    )


def supersede_gmail_rows(
    session: Session, rows: list[Transaction], result: RemediationResult
) -> None:
    """Reject each Gmail receipt that Plaid already covers. Never deletes."""
    for tx in rows:
        if tx.status == _SPLIT_PARENT or tx.parent_id is not None:
            result.skipped_splits.append(f"gmail (split) {_label(tx)}")
            continue
        twin = find_plaid_twin(session, tx)
        if twin is None:
            # Real, unduplicated data — wrong category, but rejecting it would
            # lose the expense entirely. A human picks the right category.
            result.unmatched_gmail.append(
                f"{_label(tx)}  [tax_category={tx.tax_category}, no Plaid twin "
                "— re-categorise by hand, do NOT reject]"
            )
            continue
        try:
            with session.begin_nested():
                old_status, old_reason = tx.status, tx.review_reason
                tx.status = _REJECTED
                tx.review_reason = GMAIL_SUPERSEDE_REASON
                _audit(session, tx, field_changed="status", old=old_status, new=_REJECTED)
                _audit(
                    session, tx, field_changed="review_reason",
                    old=old_reason, new=GMAIL_SUPERSEDE_REASON,
                )
                session.flush()
            result.gmail_supersedes.append(
                Change(
                    _label(tx), f"twin {twin.date} {str(twin.description or '')[:24]}",
                    "status", old_status, _REJECTED,
                )
            )
        except Exception:
            result.failed.append(f"gmail {_label(tx)}")
            logger.exception("gmail supersede failed", extra={"transaction_id": tx.id})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _force_real_transaction(session: Session) -> None:
    """Emit an explicit ``BEGIN`` before the first ``SAVEPOINT``.

    pysqlite does not emit ``BEGIN`` for a ``SAVEPOINT`` statement, so a
    savepoint opened as the first statement of a transaction runs while SQLite
    is still in autocommit mode — SQLite then treats it as the outermost
    savepoint and its ``RELEASE`` COMMITS, making ``session.rollback()`` a
    no-op and the dry run a silent write. See
    ``scripts/remediate_plaid_mirrors.py``, where this was first found.
    """
    connection = session.connection()
    driver_connection = getattr(connection.connection, "driver_connection", None)
    if getattr(driver_connection, "in_transaction", False):
        return
    connection.exec_driver_sql("BEGIN")


def remediate(session: Session, *, apply: bool = False) -> RemediationResult:
    """Run both corrections. DRY-RUN unless ``apply`` is True."""
    _force_real_transaction(session)
    result = RemediationResult()
    reclassify_payouts(session, find_payout_rows(session), result)
    supersede_gmail_rows(session, find_gmail_income_rows(session), result)
    if apply:
        session.commit()
    else:
        session.rollback()
    return result


def _print_section(title: str, changes: list[Change]) -> None:
    print(f"\n{title}: {len(changes)} change(s)")
    for change in changes:
        print(
            f"  {change.target}  {change.field}: "
            f"{change.old or '-'} -> {change.new or 'NULL'}   [{change.detail}]"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Commit changes (default: dry-run)."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    session = get_session()
    try:
        result = remediate(session, apply=args.apply)
    finally:
        session.close()

    _print_section("Processor payout legs -> transfer (REQ-FIX-BO-001)", result.payouts)
    _print_section(
        "Gmail receipts superseded by Plaid (REQ-FIX-BO-002)", result.gmail_supersedes
    )

    if result.blocked_payouts:
        print(
            f"\nBLOCKED payout(s), NOT touched ({len(result.blocked_payouts)}): the "
            "revenue these settle is not in the register, so reclassifying them "
            "would remove income nothing else accounts for. Re-run the adapter, "
            "then re-run this script:"
        )
        for line in result.blocked_payouts:
            print(f"  {line}")
    if result.unmatched_gmail:
        print(
            f"\nGmail income row(s) with no Plaid twin ({len(result.unmatched_gmail)}): "
            "real data with a wrong category — re-categorise by hand:"
        )
        for line in result.unmatched_gmail:
            print(f"  {line}")
    if result.skipped_splits:
        print(f"\nSkipped, needs a human: {len(result.skipped_splits)}")
        for line in result.skipped_splits:
            print(f"  {line}")
    if result.failed:
        print(f"\nFAILED rows (isolated, batch continued): {len(result.failed)}")
        for line in result.failed:
            print(f"  {line}")

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"\n{mode}: {result.total_changes} change(s) across "
        f"{len(result.payouts)} payout leg(s) and "
        f"{len(result.gmail_supersedes)} Gmail receipt(s)."
    )
    if not args.apply:
        print("Rolled back. Re-run with --apply to commit.")
    elif result.total_changes:
        print("Committed; an AuditEvent row was written for every field change.")
    else:
        print("Nothing to do — already remediated.")
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
