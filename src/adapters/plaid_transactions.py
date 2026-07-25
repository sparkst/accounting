"""Plaid Transactions sync — REQ-PT-001..016.

Mirrors src/adapters/plaid_balance.py: DRY-RUN default, sync_one_item /
sync_all_active, three layers of error isolation. Cursor-based
/transactions/sync handles added/modified/removed; pending→posted reconcile
keys off Plaid's pending_transaction_id. payment_method is the join key for
entity-stamp, CSV supersede, and CSV-skip (the register has no account FK).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.plaid_client import (
    PlaidErrorBase,
    RetryablePlaidError,
    TerminalPlaidError,
    call_with_retry,
)
from src.classification.engine import classify
from src.close.autoconfirm import auto_confirm_if_eligible
from src.models.audit_event import AuditEvent
from src.models.brokerage import Account
from src.models.enums import (
    ConfirmedBy,
    Direction,
    IngestionStatus,
    Source,
    TransactionStatus,
)
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidItem
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash
from src.utils.plaid_crypto import InvalidCiphertextError, decrypt_token

logger = logging.getLogger(__name__)

SOURCE = "plaid"
_AUTO_THRESHOLD = 0.7

# ── REQ-WBR-LED-014: duplicate-Item account allowlist ─────────────────────────
#: account_ids Plaid returns under a DUPLICATE Item that mirrors a sibling
#: Item's own accounts (the 2026-07-24 incident — see
#: scripts/remediate_plaid_mirrors.py, which imports this SAME tuple so the
#: ingest allowlist and the one-time remediation can never drift apart,
#: P2-006). Transactions on these account_ids are skipped SILENTLY-BUT-COUNTED
#: at ingest — this is the known-harmless case. ANY OTHER unrecognized
#: account_id is treated as a genuinely NEW, not-yet-mapped account and raises
#: instead (see process_added / UnrecognizedPlaidAccountError) — Plaid's
#: /transactions/sync never re-delivers a passed cursor, so silently skipping
#: an unrecognized-but-not-a-known-mirror account_id would be PERMANENT data
#: loss for a newly-added card on an existing login.
KNOWN_MIRROR_ACCOUNT_IDS = frozenset(
    {
        "rJLQP5OJJmTx1wPD4aEBI7QKLYYRadiVYdQAB",
        "Z0p7Yzg0MqI1x0rBjgnjs8zZnk6ek8F88QaKg",
        "8wBN3pLwXKUVx51oRzERUnR9J0b40nFYY54X4",
    }
)


class UnrecognizedPlaidAccountError(RuntimeError):
    """A transaction's ``account_id`` is neither mapped to an Account for this
    Item NOR a KNOWN_MIRROR_ACCOUNT_IDS entry (REQ-WBR-LED-014 case B: a
    genuinely new account, not a known-harmless duplicate-Item mirror).

    Raised (rather than skip-and-continue) so the caller's per-row savepoint
    in ``sync_one_item`` counts this as a failure: the cursor is held (this
    row is re-delivered next run once the account is mapped) and the daily
    sync's OnFailure alert fires, instead of the transaction being silently
    and permanently dropped.
    """

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"unrecognized plaid account_id: {account_id!r}")


# ── Credit-card payment legs (REQ-WBR-LED-015) ────────────────────────────────
# Paying a card off produces TWO register rows: the credit landing on the card
# account and the matching ACH debit leaving checking. Neither is P&L — they are
# the two legs of one internal move — but the classifier reads the card-side
# credit as income and the checking-side debit as an expense, so a single
# $1,637.65 Amex payoff landed as +1637.65 income AND -1637.65 expense in the
# same week's ledger.
#
# Card side is deterministic from Plaid's own metadata.
_CARD_PAYMENT_TXN_CODE = "payment"
_CARD_PAYMENT_PFC_DETAILED = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
#: P1-d7e: Plaid's bare `transaction_code == "payment"` is a generic
#: bank-channel taxonomy (any bill payment, not specifically a card payoff) —
#: unscoped, it would reintroduce exactly the harm the bare-"AUTOPAY" name
#: exclusion below guards against (a genuine deductible bill payment silently
#: dropped from P&L/B&O). It is only trusted alongside one of these two extra
#: signals: the Account is itself a credit-card account, OR Plaid's own PFC
#: primary agrees this is a loan/card payment.
_CREDIT_ACCOUNT_TYPE = "credit_card"
_LOAN_PAYMENTS_PFC_PRIMARY = "LOAN_PAYMENTS"

#: The exact field set a card-payment leg gets, whether newly ingested
#: (`_make_card_payment_transaction`) or corrected by the one-time
#: remediation (`scripts/remediate_plaid_mirrors.reclassify_card_payments`,
#: which imports this same dict) — P2-007. These must never diverge, or a
#: remediated row and a freshly-ingested one end up with two different shapes
#: for the same logical condition: `deductible_pct` in particular is read
#: directly (no direction filter) by src/reports/tax_forecast.py and
#: src/export/basis.py. `confidence`/`status`/`review_reason` are
#: INSERT-time-only (a remediated row keeps its existing confidence/status),
#: so they live outside this shared dict.
CARD_PAYMENT_DIRECTION_TAX_FIELDS: dict[str, Any] = {
    "direction": Direction.TRANSFER.value,
    "tax_category": None,
    "deductible_pct": 0.0,
}

# Checking side carries none of that metadata (Plaid categorises the outbound
# ACH inconsistently), so it is matched on the bank descriptor: a
# case-insensitive substring test against ``name`` + ``original_description``.
# Every pattern names a card issuer explicitly. A bare "AUTOPAY" is deliberately
# NOT a pattern — "VERIZON AUTOPAY" is a genuine deductible expense, and
# matching it would silently drop it from P&L and B&O gross.
#
# P2-004: sparkry-crm-wbr2's `CARD_PAYMENT_NAME_RE`
# (src/lib/server/wealth/wbr/constants.ts) independently matches
# AUTOPAY/PAYMENT-THANK-YOU/ONLINE-PAYMENT name patterns as a DISPLAY-ONLY
# fallback for weeks this adapter hasn't remediated yet — see that file's
# comment for why it deliberately DOES match a bare "AUTOPAY" (a display-only
# regression there is far less costly than a P&L exclusion here, since
# `category === 'Transfer'` from THIS adapter is what actually excludes a row
# from money-in/out; the CRM's regex only relabels a still-visible row). If
# you add an issuer descriptor pattern here, consider whether the CRM's regex
# needs a matching update — the two are intentionally NOT required to be
# identical, but a card-payment shape that only ONE side recognizes is a
# footnote/label inconsistency the reader could notice.
CARD_PAYMENT_DESCRIPTOR_PATTERNS = (
    "ORIG CO NAME:AMERICAN EXPRESS",  # Amex pulling an ACH card payment
    "CREDIT CRD AUTOPAY",  # Chase "CHASE CREDIT CRD AUTOPAY PMT"
    "CREDIT CRD EPAY",  # Chase manual card payment, same descriptor family
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _json_safe(obj: Any) -> Any:
    """Coerce a Plaid ``to_dict()`` payload into a JSON-serializable structure.

    Plaid's SDK keeps ``date``/``datetime`` (and occasionally Decimal) values as
    native objects, which the SQLAlchemy JSON ``raw_data`` column cannot encode
    ("Object of type date is not JSON serializable"). Round-tripping through
    ``json.dumps(..., default=str)`` stringifies any such values while preserving
    the full payload for the audit trail.
    """
    return json.loads(json.dumps(obj, default=str))


def _audit_field_change(
    session: Session,
    tx: Transaction,
    *,
    field: str,
    old_value: Any,
    new_value: Any,
) -> None:
    """Append a transaction-mode AuditEvent for an automated field change.

    Mirrors the canonical register-edit pattern in
    src/api/routes/transactions.py::_create_audit_events (transaction_id set;
    entity_id/entity_type NULL). changed_by is 'auto' because the change is
    performed by the Plaid sync job, not a human.
    """
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed=field,
            old_value=str(old_value) if old_value is not None else None,
            new_value=str(new_value) if new_value is not None else None,
            changed_by=ConfirmedBy.AUTO.value,
        )
    )


def _audit_status_change(
    session: Session, tx: Transaction, old_status: str | None, new_status: str
) -> None:
    """Append a transaction-mode AuditEvent for an automated status flip."""
    _audit_field_change(
        session, tx, field="status", old_value=old_status, new_value=new_status
    )


def build_tx_fields(plaid_txn: Any) -> dict[str, Any]:
    """Map a Plaid transaction object to register-Transaction field kwargs.

    Sign: Plaid depository convention is positive = money out. DB convention is
    expense negative / income positive, so db_amount = -plaid_amount.
    """
    txn_id = plaid_txn.transaction_id
    amount = Decimal(str(-plaid_txn.amount))
    description = getattr(plaid_txn, "merchant_name", None) or plaid_txn.name
    return {
        "source": SOURCE,
        "source_id": txn_id,
        "source_hash": compute_source_hash(SOURCE, txn_id),
        "date": str(plaid_txn.date),
        "description": description,
        "amount": amount,
        "currency": "USD",
        "raw_data": _json_safe(plaid_txn.to_dict()),
    }


def _is_plaid_transfer_category(plaid_txn: Any) -> bool:
    """Detect a Plaid TRANSFER-category txn from its metadata.

    A Plaid transaction carries a transfer category when its
    personal_finance_category.primary starts with "TRANSFER" OR
    transaction_code == "transfer". Handles object, dict, and None shapes
    defensively (the SDK returns objects; tests use SimpleNamespace/None).

    IMPORTANT: this is NOT proof of an internal account-to-account transfer.
    Plaid's PFC taxonomy assigns primary="TRANSFER_IN" (e.g.
    TRANSFER_IN_DEPOSIT / TRANSFER_IN_ACCOUNT_TRANSFER) to ordinary inbound
    ACH, wire, Zelle, and check deposits — which for the Sparkry / BlackLine
    business depository accounts are exactly how client payments / revenue
    arrive. We therefore do NOT auto-set direction=transfer (that would silently
    drop real income from P&L / B&O). Instead a transfer-category txn is routed
    to needs_review so a human confirms transfer-vs-income before any inbound
    amount leaves the income aggregation.
    """
    pfc = getattr(plaid_txn, "personal_finance_category", None)
    primary = getattr(pfc, "primary", None) if pfc is not None else None
    if primary is None and isinstance(pfc, dict):
        primary = pfc.get("primary")
    if isinstance(primary, str) and primary.upper().startswith("TRANSFER"):
        return True
    code = getattr(plaid_txn, "transaction_code", None)
    return isinstance(code, str) and code.lower() == "transfer"


def card_payment_signal(
    *,
    transaction_code: Any = None,
    pfc_primary: Any = None,
    pfc_detailed: Any = None,
    descriptor_parts: Iterable[Any] = (),
    account_type: Any = None,
) -> str | None:
    """Return the signal identifying a credit-card payment leg, else None.

    REQ-WBR-LED-015. Returns the matched signal string rather than a bool so a
    metadata match can be told apart from a descriptor match in logs and in the
    remediation script's dry-run table. Takes plain values (not a Plaid object)
    so the live adapter and the one-time remediation over stored ``raw_data``
    share one implementation.

    P1-d7e: the bare ``transaction_code == "payment"`` signal is scoped — it
    only counts alongside the Account itself being a credit-card account, OR
    Plaid's own PFC primary agreeing this is a loan/card payment. Unscoped, it
    is a generic bank-channel taxonomy (ANY bill payment, e.g. a utility
    bill-pay from checking), not specific to a card payoff, and would
    reintroduce the exact harm the bare-"AUTOPAY" descriptor exclusion below
    already guards against: a genuine deductible expense silently dropped from
    P&L / B&O gross. ``pfc_detailed`` and the issuer descriptors need no such
    scoping — they are already specific enough on their own.
    """
    if (
        isinstance(transaction_code, str)
        and transaction_code.lower() == _CARD_PAYMENT_TXN_CODE
        and (
            (
                isinstance(account_type, str)
                and account_type.lower() == _CREDIT_ACCOUNT_TYPE
            )
            or (
                isinstance(pfc_primary, str)
                and pfc_primary.upper() == _LOAN_PAYMENTS_PFC_PRIMARY
            )
        )
    ):
        return f"transaction_code={_CARD_PAYMENT_TXN_CODE}"
    if (
        isinstance(pfc_detailed, str)
        and pfc_detailed.upper() == _CARD_PAYMENT_PFC_DETAILED
    ):
        return f"pfc_detailed={_CARD_PAYMENT_PFC_DETAILED}"
    haystack = " ".join(
        part for part in descriptor_parts if isinstance(part, str)
    ).upper()
    for pattern in CARD_PAYMENT_DESCRIPTOR_PATTERNS:
        if pattern in haystack:
            return f"descriptor={pattern}"
    return None


def _pfc_detailed(pfc: Any) -> Any:
    """Read ``personal_finance_category.detailed`` from an object or a dict.

    The Plaid SDK returns an object; tests use SimpleNamespace and stored
    ``raw_data`` is a plain dict, so both shapes are handled.
    """
    if pfc is None:
        return None
    detailed = getattr(pfc, "detailed", None)
    if detailed is None and isinstance(pfc, dict):
        detailed = pfc.get("detailed")
    return detailed


def _pfc_primary(pfc: Any) -> Any:
    """Read ``personal_finance_category.primary`` from an object or a dict."""
    if pfc is None:
        return None
    primary = getattr(pfc, "primary", None)
    if primary is None and isinstance(pfc, dict):
        primary = pfc.get("primary")
    return primary


def card_payment_signal_for_txn(plaid_txn: Any, *, account_type: Any = None) -> str | None:
    """``card_payment_signal`` applied to a Plaid SDK transaction object.

    ``account_type`` is the RESOLVED Account's ``account_type`` (e.g.
    ``"credit_card"``) — callers pass it through from the Account they already
    looked up (REQ-WBR-LED-014's ``account_index``), so this scoping never
    requires an extra query.
    """
    pfc = getattr(plaid_txn, "personal_finance_category", None)
    return card_payment_signal(
        transaction_code=getattr(plaid_txn, "transaction_code", None),
        pfc_primary=_pfc_primary(pfc),
        pfc_detailed=_pfc_detailed(pfc),
        descriptor_parts=(
            getattr(plaid_txn, "name", None),
            getattr(plaid_txn, "original_description", None),
        ),
        account_type=account_type,
    )


def card_payment_signal_for_raw(raw: Any, *, account_type: Any = None) -> str | None:
    """``card_payment_signal`` applied to a stored ``Transaction.raw_data`` dict."""
    if not isinstance(raw, dict):
        return None
    pfc = raw.get("personal_finance_category")
    return card_payment_signal(
        transaction_code=raw.get("transaction_code"),
        pfc_primary=_pfc_primary(pfc),
        pfc_detailed=_pfc_detailed(pfc),
        descriptor_parts=(raw.get("name"), raw.get("original_description")),
        account_type=account_type,
    )


def _make_card_payment_transaction(
    fields: dict[str, Any], *, entity: str | None, payment_method: str | None, signal: str
) -> Transaction:
    """Build a ``direction=transfer`` row for a credit-card payment leg.

    REQ-WBR-LED-015: ``classify()`` is NOT called. The signal is deterministic,
    so running the 3-tier pipeline would only spend a Tier-3 LLM call to reach
    the wrong answer (income on the card-side credit, expense on the
    checking-side debit). The amount from ``build_tx_fields`` passes through
    untouched — the DB sign convention already holds for both legs.
    ``tax_category`` is NULL and ``deductible_pct`` 0.0 because a transfer is
    not P&L; that mirrors how scripts/remediate_misclassified_income.py records
    processor payouts. An unmapped entity still forces needs_review, matching
    the classified path.

    P2-005: this short-circuits ``classify()`` — and therefore the Tier-1
    VendorRule learning loop — entirely, so a human's corrective VendorRule
    for this vendor can never override it. The matched ``signal`` is recorded
    on ``raw_data._card_payment_signal`` (minimum fix per the review) so an
    operator debugging "why didn't my rule apply" sees the row never reached
    the classifier, rather than assuming their rule silently failed.
    """
    unmapped = entity is None
    raw_data = fields.get("raw_data")
    if isinstance(raw_data, dict):
        fields = {**fields, "raw_data": {**raw_data, "_card_payment_signal": signal}}
    return Transaction(
        **fields,
        entity=entity,
        payment_method=payment_method,
        **CARD_PAYMENT_DIRECTION_TAX_FIELDS,
        confidence=1.0,
        status=(
            TransactionStatus.NEEDS_REVIEW.value if unmapped
            else TransactionStatus.AUTO_CLASSIFIED.value
        ),
        review_reason=(
            "plaid: account not mapped to an entity" if unmapped else None
        ),
    )


def make_transaction(
    plaid_txn: Any,
    *,
    session: Session,
    entity: str | None,
    payment_method: str | None,
    account_type: Any = None,
) -> Transaction:
    """Build a classified Transaction. Entity is authoritative from the mapped
    account (overrides the classifier). Unmapped (entity None) -> needs_review.

    A credit-card payment leg short-circuits to a transfer row before the
    classifier runs (REQ-WBR-LED-015) — the specific rule wins over the generic
    ``_is_plaid_transfer_category`` needs_review routing below. ``account_type``
    is the resolved Account's type (e.g. ``"credit_card"``), used to scope the
    bare ``transaction_code == "payment"`` signal (P1-d7e).
    """
    fields = build_tx_fields(plaid_txn)
    signal = card_payment_signal_for_txn(plaid_txn, account_type=account_type)
    if signal is not None:
        return _make_card_payment_transaction(
            fields, entity=entity, payment_method=payment_method, signal=signal
        )
    tx = Transaction(
        **fields, entity=entity, payment_method=payment_method, confidence=0.0,
        status=TransactionStatus.NEEDS_REVIEW.value,
    )
    result = classify(tx, session)
    tx.tax_category = result.tax_category.value
    tx.tax_subcategory = result.tax_subcategory
    tx.direction = result.direction.value
    tx.deductible_pct = result.deductible_pct
    tx.confidence = result.confidence
    # REQ-FIX-ING-008: review_reason is set EXCLUSIVELY by the reasons block
    # below (needs_review) or cleared (auto_classified) — no initial
    # assignment here. Previously `tx.review_reason = result.review_reason`
    # persisted the veto's mismatch text even when confidence ended up high
    # enough to auto-classify, and result.status was never consulted at all:
    # a sign-veto (_reconcile_sign -> NEEDS_REVIEW) with confidence >= 0.7
    # landed as auto_classified.
    # Entity was set at construction (entity=entity above); classify() returns a
    # result object and never mutates tx.entity, so no re-assignment is needed.
    # A Plaid TRANSFER-category txn is NOT auto-set to direction=transfer: that
    # over-broad rule silently dropped real inbound income (client ACH/Zelle/wire
    # categorised TRANSFER_IN) from P&L/B&O. Instead we keep the classifier's
    # direction as a suggestion and force needs_review so a human confirms
    # transfer-vs-income. See _is_plaid_transfer_category for the rationale.
    is_transfer_category = _is_plaid_transfer_category(plaid_txn)
    # REQ-FIX-ING-008: honor the engine's sign-reconciliation veto
    # (_reconcile_sign -> NEEDS_REVIEW) regardless of confidence — a vetoed
    # result must never auto-classify just because its confidence score
    # happens to be >= threshold.
    vetoed = result.status == TransactionStatus.NEEDS_REVIEW
    needs_review = (
        entity is None
        or is_transfer_category
        or vetoed
        or result.confidence < _AUTO_THRESHOLD
    )
    tx.status = (
        TransactionStatus.NEEDS_REVIEW.value if needs_review
        else TransactionStatus.AUTO_CLASSIFIED.value
    )
    # Build review_reason from ALL applicable signals. When BOTH entity is
    # unmapped AND it's a transfer-category, an if/elif chain would drop the
    # transfer flag — an operator querying needs_review for transfer patterns
    # would miss these dual-condition rows. Concatenate instead (P3-002).
    # Also preserve the classifier's low-confidence detail (the tier + score)
    # rather than letting the entity/transfer note clobber it (P3-001-CQ): an
    # operator triaging a mapped + transfer + low-confidence row needs all three
    # signals, not just the transfer flag.
    reasons: list[str] = []
    if entity is None:
        reasons.append("account not mapped to an entity")
    if is_transfer_category:
        reasons.append("transfer-category — confirm transfer vs income")
    if vetoed and result.review_reason:
        # Veto text survives regardless of confidence (REQ-FIX-ING-008) — a
        # vetoed result's confidence score is irrelevant to whether the veto
        # reasoning is shown to the reviewer.
        reasons.append(result.review_reason)
    elif result.confidence < _AUTO_THRESHOLD and result.review_reason:
        reasons.append(result.review_reason)
    if reasons:
        tx.review_reason = "plaid: " + "; ".join(reasons)
    elif tx.status == TransactionStatus.AUTO_CLASSIFIED.value:
        # No stale mismatch/low-confidence text survives on a clean row.
        tx.review_reason = None
    # REQ-MCA-002: a Tier-1 match on a >=0.90 vendor rule is auto-confirmed at
    # ingest. Uses the engine's ClassificationResult (tier_used/rule_id/status)
    # — never re-derives eligibility. Transfer-category / vetoed rows already
    # sit at needs_review above, so the helper's tx.status guard excludes them.
    auto_confirm_if_eligible(session, tx, result)
    return tx


def _existing_by_source_id(session: Session, source_id: str) -> Transaction | None:
    """Look up the register row for a Plaid source_id — PARENTS ONLY.

    REQ-FIX-ING-007: split children copy their parent's source_id
    (src/classification/splitter.py) so, without the parent_id IS NULL
    filter, an upstream Plaid event (modified/removed/pending-posted) could
    select and mutate a CHILD — breaking the split-sum invariant (children no
    longer sum to parent). A child is never a valid target for any Plaid
    sync mutation; only the parent (or an unsplit row) is.
    """
    return (
        session.query(Transaction)
        .filter(
            Transaction.source == SOURCE,
            Transaction.source_id == source_id,
            Transaction.parent_id.is_(None),
        )
        .first()
    )


def _flag_split_parent_for_review(
    session: Session, parent: Transaction, reason: str
) -> None:
    """REQ-FIX-ING-007: an upstream Plaid event arrived for a transaction
    that has since been split by a human. The parent's amount is structural
    (children sum to it) and must never be mutated by automation — but the
    human needs to know their split may now be stale. Sets
    ``review_reason`` on the parent (status stays ``split_parent`` — flipping
    it to ``needs_review`` would destroy the split) and flips every
    non-rejected child to ``needs_review`` with the same reason. Every
    mutation is audited; a human-rejected child is left alone (its rejection
    sticks, mirroring the top-level `added` re-verify rule).
    """
    old_parent_reason = parent.review_reason
    if old_parent_reason != reason:
        parent.review_reason = reason
        _audit_field_change(
            session, parent, field="review_reason",
            old_value=old_parent_reason, new_value=reason,
        )

    children: list[Transaction] = (
        session.query(Transaction).filter(Transaction.parent_id == parent.id).all()
    )
    for child in children:
        if child.status == TransactionStatus.REJECTED.value:
            continue
        old_status = child.status
        old_reason = child.review_reason
        child.status = TransactionStatus.NEEDS_REVIEW.value
        child.review_reason = reason
        if old_status != TransactionStatus.NEEDS_REVIEW.value:
            _audit_status_change(
                session, child, old_status, TransactionStatus.NEEDS_REVIEW.value
            )
        if old_reason != reason:
            _audit_field_change(
                session, child, field="review_reason",
                old_value=old_reason, new_value=reason,
            )
    session.flush()


def _apply_update(session: Session, tx: Transaction, ptxn: Any) -> bool:
    """Refresh volatile fields from a modified/posted Plaid txn. Preserves human
    classification (entity/tax_category/direction are NOT touched here).

    Returns False (no-op) if tx is a split_parent — overwriting a split parent's
    amount would break the split-sum invariant (children no longer sum to
    parent). Centralized guard so every caller (process_modified, the
    pending→posted reconcile path, the readded reactivation path) is protected
    uniformly; previously each call site needed its own guard and the reconcile
    path was repeatedly found missing one. Returns True when the update applied.

    A material amount change (pending→posted settlement or a Plaid `modified`
    delta — tips/holds can differ) is audited so the register keeps a field-level
    trail of the automated mutation."""
    if tx.status == TransactionStatus.SPLIT_PARENT.value:
        logger.warning("plaid _apply_update skipped: split_parent row %s", tx.id)
        return False
    fields = build_tx_fields(ptxn)
    old_amount = tx.amount
    new_amount = fields["amount"]
    if old_amount != new_amount:
        _audit_field_change(
            session, tx, field="amount", old_value=old_amount, new_value=new_amount
        )
    tx.amount = new_amount
    tx.date = fields["date"]
    tx.description = fields["description"]
    tx.raw_data = fields["raw_data"]
    return True


def _apply_card_payment_reclassification(
    session: Session, tx: Transaction, ptxn: Any, *, account_type: Any = None
) -> bool:
    """Reclassify ``tx`` direction=transfer if ``ptxn``'s metadata NOW signals a
    card-payment leg (REQ-WBR-LED-015), auditing each field change.

    P1-b2d: ``make_transaction`` only ever runs on the INSERT path, so a card
    payment that first arrives PENDING (a generic "PAYMENT" descriptor, no
    ``transaction_code``/PFC yet) is classified normally by the 3-tier engine
    and keeps its income/expense direction and tax_category even after the
    POSTED payload — carrying the card-payment signal — later reconciles onto
    that same row. Plaid can also add/enrich this metadata on a plain
    ``modified`` payload for an already-posted row. Factored out of
    ``make_transaction`` so every mutation site (pending→posted promotion,
    ``process_modified``, the ``plaid_readded`` reactivation branch) applies
    the identical check and field set (``CARD_PAYMENT_DIRECTION_TAX_FIELDS`` —
    P2-007) rather than each re-implementing it.

    No-ops (returns False) for split_parent/rejected rows — those statuses are
    structural/human-vetoed and must never be silently reclassified — and when
    the row is already ``direction=transfer`` with a NULL ``tax_category``
    (avoids a duplicate no-op AuditEvent on every subsequent sync).
    """
    if tx.status in (
        TransactionStatus.SPLIT_PARENT.value,
        TransactionStatus.REJECTED.value,
    ):
        return False
    if (
        tx.direction == Direction.TRANSFER.value
        and tx.tax_category is None
        and tx.deductible_pct == 0.0
    ):
        return False  # already reclassified — nothing to do
    signal = card_payment_signal_for_txn(ptxn, account_type=account_type)
    if signal is None:
        return False
    for field_name, new_value in CARD_PAYMENT_DIRECTION_TAX_FIELDS.items():
        old_value = getattr(tx, field_name)
        if old_value != new_value:
            _audit_field_change(
                session, tx, field=field_name, old_value=old_value, new_value=new_value
            )
            setattr(tx, field_name, new_value)
    return True


def process_modified(
    session: Session, modified: list[Any], *, account_index: dict[str, Account] | None = None
) -> int:
    """Refresh volatile fields on existing rows (amount/date/description/raw_data).
    Human classification on the row is preserved — _apply_update never touches
    entity/tax_category/direction/status.

    P1-b2d: after refreshing, re-check the card-payment signal (Plaid can
    enrich a row's metadata after posting) via ``account_index`` — the same
    Item-scoped map ``process_added``/``sync_one_item`` already build — so a
    card payment that only becomes recognizable on a `modified` payload is
    still reclassified rather than permanently keeping its original
    income/expense direction.
    """
    updated = 0
    for ptxn in modified:
        row = _existing_by_source_id(session, ptxn.transaction_id)
        if row is None:
            continue
        # REQ-FIX-ING-007: split_parent gets no field mutation — instead flag
        # it (+ non-rejected children) for human re-verification. Checked
        # before _apply_update (which also refuses split_parent centrally as
        # defense-in-depth) so this call site can flag rather than silently
        # no-op. A flag is NOT counted as a "modify".
        if row.status == TransactionStatus.SPLIT_PARENT.value:
            _flag_split_parent_for_review(
                session, row,
                "plaid_modified: upstream txn changed after split — re-verify split",
            )
            continue
        if not _apply_update(session, row, ptxn):
            continue
        acct = (account_index or {}).get(ptxn.account_id)
        _apply_card_payment_reclassification(
            session, row, ptxn, account_type=acct.account_type if acct else None
        )
        session.flush()
        updated += 1
    return updated


def process_removed(session: Session, removed: list[Any]) -> int:
    """Plaid removed a txn (e.g. a settled pending). Mark rejected, never delete
    (audit rule). No-op when already reconciled away or never seen.

    REQ-FIX-ING-007: `removed` on an already-rejected row is a no-op guard —
    Plaid can redeliver a `removed` entry for a transaction already rejected
    (e.g. re-synced after a prior `removed` was processed, or a row that was
    independently rejected). Without this guard a redelivery would write a
    duplicate no-op status="rejected"->"rejected" AuditEvent every time and
    inflate the returned count for a row that saw no actual change."""
    count = 0
    for r in removed:
        # Plaid SDK removed-entries are typed objects; dict support is retained
        # for the test fixtures (which pass {"transaction_id": ...}).
        rid = r["transaction_id"] if isinstance(r, dict) else r.transaction_id
        row = _existing_by_source_id(session, rid)
        if row is None:
            continue
        if row.status == TransactionStatus.SPLIT_PARENT.value:
            # REQ-FIX-ING-007: never reject a split parent (would orphan its
            # children) — flag it (+ non-rejected children) for review instead.
            _flag_split_parent_for_review(
                session, row,
                "plaid_removed: upstream txn removed after split — re-verify split",
            )
            continue
        if row.status == TransactionStatus.REJECTED.value:
            # Already rejected — skip re-audit (no-op guard, see docstring).
            continue
        old_status = row.status
        row.status = TransactionStatus.REJECTED.value
        row.review_reason = "plaid_removed"
        _audit_status_change(session, row, old_status, TransactionStatus.REJECTED.value)
        session.flush()
        count += 1
    return count


def _sync_request(access_token: str, cursor: str | None) -> Any:
    # Deferred import: the plaid SDK model modules are imported lazily so the
    # adapter module can be imported in environments where the Plaid SDK is not
    # installed/initialized (mirrors the deferred-import pattern in routes/plaid.py).
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    if cursor:
        return TransactionsSyncRequest(access_token=access_token, cursor=cursor)
    return TransactionsSyncRequest(access_token=access_token)


def fetch_all_pages(
    client: Any, access_token: str, *, cursor: str | None
) -> tuple[list[Any], list[Any], list[Any], str]:
    """Loop /transactions/sync until has_more is False. Returns
    (added, modified, removed, next_cursor)."""
    added: list[Any] = []
    modified: list[Any] = []
    removed: list[Any] = []
    while True:
        req = _sync_request(access_token, cursor)

        def _do_sync(r: Any = req) -> Any:  # default-bind r to this loop's req
            return client.transactions_sync(r)

        resp = call_with_retry(_do_sync)
        added += list(resp.added)
        modified += list(resp.modified)
        removed += list(resp.removed)
        cursor = resp.next_cursor
        if not resp.has_more:
            break
    return added, modified, removed, cursor


def supersede_csv_rows(
    session: Session, *, payment_method: str | None, covered_min: str, covered_max: str
) -> int:
    """Mark bank-CSV rows for this payment_method label, within Plaid's covered
    date range, as rejected (superseded). Audit rule: never delete. A blank label
    disables supersede (returns 0, logged).

    Scope is intentionally narrowed to ``source == 'bank_csv'`` — the only source
    the documented "Plaid replaces CSV history" supersede is meant to replace.
    Gmail/Stripe/Shopify rows (and reconciliation-pair legs) that merely share a
    payment_method label are NOT collateral. Confirmed rows ARE superseded (that
    is the intended purpose: replacing confirmed CSV history with Plaid), but
    split_parent rows are excluded so rejecting a parent never orphans its split
    children. REQ-FIX-ING-007: split CHILDREN are excluded too
    (``parent_id IS NULL``) — a child's status is never ``split_parent``, so
    without this filter a child could be silently rejected, breaking the
    split-sum invariant even though its parent was correctly skipped.
    Mutations are wrapped in a savepoint so a mid-supersede failure doesn't
    leave a partial audit trail.
    """
    if not payment_method:
        logger.warning("plaid supersede skipped: account has no payment_method label")
        return 0
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.BANK_CSV.value,
            Transaction.payment_method == payment_method,
            Transaction.date >= covered_min,
            Transaction.date <= covered_max,
            Transaction.status != TransactionStatus.REJECTED.value,
            Transaction.status != TransactionStatus.SPLIT_PARENT.value,
            Transaction.parent_id.is_(None),
        )
        .all()
    )
    with session.begin_nested():
        for row in rows:
            old_status = row.status
            row.status = TransactionStatus.REJECTED.value
            row.review_reason = "superseded_by_plaid"
            _audit_status_change(session, row, old_status, TransactionStatus.REJECTED.value)
        session.flush()
    return len(rows)


@dataclass
class AddedCounts:
    """Outcome of process_added: rows newly inserted vs previously-removed rows
    reactivated (plaid_readded). Operators need the reactivated count separately
    so a reinstated row isn't invisible in the added metric.

    ``skipped_unknown_account`` maps a KNOWN-MIRROR ``account_id`` (see
    KNOWN_MIRROR_ACCOUNT_IDS) to how many of its transactions were skipped
    silently-but-counted (REQ-WBR-LED-014 case A). Any OTHER unrecognized
    account_id raises ``UnrecognizedPlaidAccountError`` instead (case B) —
    see ``sync_one_item`` for how that's counted."""

    inserted: int = 0
    reactivated: int = 0
    skipped_unknown_account: dict[str, int] = field(default_factory=dict)


def process_added(
    session: Session, item: PlaidItem, added: list[Any], *, account_index: dict[str, Account]
) -> AddedCounts:
    """Insert added txns; idempotent on (source, source_id). Returns AddedCounts
    (inserted + reactivated).

    Pending→posted reconcile: if a posted txn carries pending_transaction_id that
    matches an existing row, we UPDATE that row in place (promoting source_id to
    the posted id) rather than inserting a duplicate.

    Accepts a list and loops internally, but the orchestrator (sync_one_item)
    passes single-element slices so each row gets its own begin_nested() savepoint
    (per-row isolation lives in the caller; this function is idempotent either
    way and tests exercise it with full lists directly).
    """
    counts = AddedCounts()
    for ptxn in added:
        # REQ-WBR-LED-014: account allowlist. Two Plaid Items covering the same
        # bank login each return the FULL account set for that login, so the
        # second Item mirrors accounts it does not own — under item-scoped
        # account_ids that miss this index. Ingesting them created a phantom
        # duplicate of every transaction (entity None, so the classifier
        # guessed). Checked before ANY other branch: a mirror must not promote
        # a pending, reactivate a removed row, or insert.
        #
        # Two distinct cases, deliberately NOT treated the same (P1-002/
        # P1-c4f): a KNOWN mirror (this Item's duplicate-login sibling) is
        # skipped SILENTLY-BUT-COUNTED — safe, harmless, expected. Any OTHER
        # unrecognized account_id is a genuinely NEW account Plaid returned
        # under this Item that has no Account row yet — silently skipping that
        # would be PERMANENT data loss (/transactions/sync never re-delivers a
        # passed cursor), so it RAISES instead: the per-row savepoint in
        # sync_one_item counts it as a failure, holding the cursor (this row
        # is re-delivered once the account is mapped) and tripping the daily
        # sync's OnFailure alert.
        acct = account_index.get(ptxn.account_id)
        if acct is None:
            if ptxn.account_id in KNOWN_MIRROR_ACCOUNT_IDS:
                counts.skipped_unknown_account[ptxn.account_id] = (
                    counts.skipped_unknown_account.get(ptxn.account_id, 0) + 1
                )
                continue
            raise UnrecognizedPlaidAccountError(ptxn.account_id)
        existing = _existing_by_source_id(session, ptxn.transaction_id)
        if existing is not None:
            # Per-site split_parent guard, kept because it changes control flow:
            # a split_parent must skip the ENTIRE existing-row block (including
            # the plaid_removed reactivation branch below), not just the
            # _apply_update call. _apply_update now also refuses split_parent
            # centrally (defense-in-depth), but that alone wouldn't stop the
            # status/review_reason flip + reactivated count here. Skip + warn.
            if existing.status == TransactionStatus.SPLIT_PARENT.value:
                logger.warning(
                    "plaid readded skipped: split_parent row %s", existing.id
                )
                continue
            # Removed-then-readded: Plaid can re-deliver a previously-removed id.
            # A row we rejected via process_removed represents real activity that
            # Plaid now considers live again, so reactivate it instead of skipping
            # (which would strand it as rejected forever, dropping it from P&L).
            if (
                existing.status == TransactionStatus.REJECTED.value
                and existing.review_reason == "plaid_removed"
            ):
                old_status = existing.status
                # NOTE: _apply_update overwrites amount with Plaid's re-delivered
                # value. This is acceptable: a plaid_removed row was a vanished
                # pending. A human-confirmed, amount-adjusted row that Plaid later
                # removed then re-added is an edge case where the human amount is
                # overwritten — see P3-002 in the qloop R2 review.
                # _apply_update centrally refuses split_parent rows (returning
                # False); the per-site guard above (line 382) already makes that
                # path unreachable here, but we honour the contract uniformly so
                # a future caller can rely on the bool without auditing each site.
                if not _apply_update(session, existing, ptxn):
                    continue
                existing.status = TransactionStatus.NEEDS_REVIEW.value
                existing.review_reason = "plaid_readded"
                _audit_status_change(
                    session, existing, old_status, TransactionStatus.NEEDS_REVIEW.value
                )
                # P1-b2d: the re-added payload may now carry the card-payment
                # signal even if the original pre-removal row didn't (or
                # predates the fix) — reclassify it the same as every other
                # mutation site rather than leaving it income/expense.
                _apply_card_payment_reclassification(
                    session, existing, ptxn, account_type=acct.account_type
                )
                session.flush()
                counts.reactivated += 1
            continue
        pending_id = getattr(ptxn, "pending_transaction_id", None)
        if pending_id:
            prior = _existing_by_source_id(session, pending_id)
            if prior is not None:
                # REQ-FIX-ING-007: if the prior pending row was split by a
                # human, overwriting its amount would break the split-sum
                # invariant — and promoting source_id while skipping the
                # amount update would desync the id from the children. Leave
                # the parent entirely untouched (structurally — no field
                # mutation) but flag it (+ non-rejected children) for human
                # re-verification; the posted txn itself is skipped.
                if prior.status == TransactionStatus.SPLIT_PARENT.value:
                    _flag_split_parent_for_review(
                        session, prior,
                        "plaid: posted txn arrived for split pending — re-verify split",
                    )
                    continue
                # REQ-FIX-ING-007: a human-rejected prior sticks — promote the
                # id/hash (so the posted txn is never re-inserted as a
                # duplicate on a future sync) and refresh fields, but the
                # status must NEVER flip back to needs_review. REJECTED is
                # therefore in the transfer-recheck exempt set below alongside
                # NEEDS_REVIEW/SPLIT_PARENT.
                if not _apply_update(session, prior, ptxn):
                    continue
                prior.source_id = ptxn.transaction_id
                prior.source_hash = compute_source_hash(SOURCE, ptxn.transaction_id)
                # P1-b2d: re-check the card-payment signal on the POSTED txn.
                # A card payment routinely arrives PENDING first with only a
                # generic "PAYMENT" descriptor (no transaction_code/PFC yet),
                # so `make_transaction` classified it normally on insert; the
                # signal only becomes visible once the posted payload lands
                # here. Checked BEFORE the generic transfer-category recheck
                # below (mirrors make_transaction's ordering: the specific
                # card-payment rule wins over the generic one).
                _apply_card_payment_reclassification(
                    session, prior, ptxn, account_type=acct.account_type
                )
                # Re-evaluate transfer-category on the POSTED txn. A pending that
                # carried a non-transfer PFC but posts as TRANSFER_IN/OUT would
                # otherwise keep its prior status and slip through as
                # auto_classified — silently dropping a real internal transfer
                # signal. If it now reads as transfer-category and the row isn't
                # already in needs_review, demote it so a human confirms
                # transfer-vs-income (P3-001). split_parent rows are exempt: their
                # status is structural, not a classification, and must not flip.
                # REJECTED rows are exempt too (REQ-FIX-ING-007): a human veto
                # sticks — Plaid re-categorizing a settled, human-rejected row
                # as a transfer must not resurrect it into needs_review.
                if (
                    _is_plaid_transfer_category(ptxn)
                    and prior.status
                    not in (
                        TransactionStatus.NEEDS_REVIEW.value,
                        TransactionStatus.SPLIT_PARENT.value,
                        TransactionStatus.REJECTED.value,
                    )
                ):
                    old_status = prior.status
                    prior.status = TransactionStatus.NEEDS_REVIEW.value
                    prior.review_reason = (
                        "plaid: transfer-category — confirm transfer vs income"
                    )
                    _audit_status_change(
                        session, prior, old_status,
                        TransactionStatus.NEEDS_REVIEW.value,
                    )
                session.flush()
                continue
        tx = make_transaction(
            ptxn,
            session=session,
            entity=acct.entity,
            payment_method=acct.payment_method,
            account_type=acct.account_type,
        )
        session.add(tx)
        session.flush()
        counts.inserted += 1
    return counts


@dataclass
class TxItemResult:
    item_id: str
    institution_name: str
    status: str = "ok"          # 'ok' | 'error' | 'institution_down'
    added: int = 0
    reactivated: int = 0
    modified: int = 0
    removed: int = 0
    failed: int = 0
    superseded: int = 0
    error_code: str | None = None
    #: REQ-WBR-LED-014 case A — account_id -> count of transactions skipped
    #: silently-but-counted because the account_id is a KNOWN mirror (a
    #: duplicate Item's own login sibling; see KNOWN_MIRROR_ACCOUNT_IDS).
    skipped_unknown_account: dict[str, int] = field(default_factory=dict)
    #: REQ-WBR-LED-014 case B (P1-002/P1-c4f) — account_id -> count of
    #: transactions that failed because the account_id is NEITHER mapped to
    #: an Account for this Item NOR a known mirror: a genuinely new,
    #: not-yet-mapped account. Each occurrence is also counted in `failed`
    #: (the cursor is held so these rows are re-delivered once mapped).
    unrecognized_account_ids: dict[str, int] = field(default_factory=dict)

    @property
    def skipped_unknown_total(self) -> int:
        return sum(self.skipped_unknown_account.values())


def sync_one_item(session: Session, item: PlaidItem, *, client: Any) -> TxItemResult:
    """Sync one Item's transactions. Caller owns the outer commit.

    First sync (item.cursor is None) triggers CSV supersede. Cursor advances
    ONLY after a clean page-loop, so a crash re-fetches from the last good
    cursor (idempotent via source_hash)."""
    result = TxItemResult(item_id=item.id, institution_name=item.institution_name)
    pulled_at = _utcnow()
    log_row = IngestionLog(source=f"plaid_tx:{item.institution_name}",
                           status=IngestionStatus.SUCCESS.value, run_at=pulled_at)
    session.add(log_row)
    first_sync = item.cursor is None

    accounts = session.query(Account).filter_by(plaid_item_id=item.id).all()
    account_index = {a.plaid_account_id: a for a in accounts if a.plaid_account_id}

    try:
        try:
            access_token = decrypt_token(item.access_token_encrypted)
        except InvalidCiphertextError as exc:
            raise TerminalPlaidError("INVALID_ACCESS_TOKEN",
                                     message="cannot decrypt token") from exc

        added, modified, removed, next_cursor = fetch_all_pages(
            client, access_token, cursor=item.cursor
        )

        for ptxn in added:
            try:
                with session.begin_nested():
                    counts = process_added(session, item, [ptxn],
                                           account_index=account_index)
                    result.added += counts.inserted
                    result.reactivated += counts.reactivated
                    for acct_id, n in counts.skipped_unknown_account.items():
                        result.skipped_unknown_account[acct_id] = (
                            result.skipped_unknown_account.get(acct_id, 0) + n
                        )
            except UnrecognizedPlaidAccountError as exc:
                # P1-002/P1-c4f: a genuinely new, unmapped account_id — count
                # as a failure (holds the cursor so this row is re-delivered
                # once the account is mapped) rather than the silent,
                # unrecoverable drop the bare allowlist skip used to be.
                result.failed += 1
                result.unrecognized_account_ids[exc.account_id] = (
                    result.unrecognized_account_ids.get(exc.account_id, 0) + 1
                )
                logger.error(
                    "plaid tx add failed: unrecognized account_id %s (not a "
                    "known mirror) — cursor held; map it to an Account to "
                    "clear this",
                    exc.account_id,
                    extra={"plaid_item_id": item.id},
                )
            except Exception:
                result.failed += 1
                logger.exception("plaid tx added failure",
                                 extra={"plaid_item_id": item.id,
                                        "txn": getattr(ptxn, "transaction_id", "?")})
        for ptxn in modified:
            try:
                with session.begin_nested():
                    result.modified += process_modified(
                        session, [ptxn], account_index=account_index
                    )
            except Exception:
                result.failed += 1
        # Per-row savepoint isolation for removals (mirrors added/modified) so one
        # bad removal doesn't roll back the rest (REQ-PT-007).
        for r in removed:
            try:
                with session.begin_nested():
                    result.removed += process_removed(session, [r])
            except Exception:
                result.failed += 1

        # REQ-WBR-LED-014: skipped account_ids are a WARNING, not a failure —
        # the expected case is a duplicate Item mirroring a sibling's accounts,
        # but the same log line is how a genuinely new, unmapped account
        # announces itself. It must never be silent.
        if result.skipped_unknown_account:
            logger.warning(
                "plaid tx skipped %d txn(s) on account_ids this item does not "
                "own: %s",
                result.skipped_unknown_total,
                dict(sorted(result.skipped_unknown_account.items())),
                extra={"plaid_item_id": item.id},
            )

        # Supersede is keyed on the dates of THIS sync's added txns, grouped per
        # account. A first sync that returns only modified/removed (no added)
        # therefore performs no supersede — acceptable, since nothing new was
        # ingested to supersede the CSV history against. Each account uses only
        # its own added-txn date range so a multi-account Item can't widen one
        # account's coverage with another account's dates.
        if first_sync and added:
            dates_by_acct: dict[str, list[str]] = defaultdict(list)
            for t in added:
                dates_by_acct[t.account_id].append(str(t.date))
            for acct in accounts:
                if not acct.plaid_account_id:
                    continue
                acct_dates = dates_by_acct.get(acct.plaid_account_id, [])
                if not acct_dates:
                    continue
                # Isolate supersede per account: a supersede failure is
                # classified (result.failed += 1) rather than escaping to the
                # outer UNEXPECTED handler, and — because result.failed is now
                # non-zero — the cursor is correctly held for a clean re-run.
                try:
                    with session.begin_nested():
                        result.superseded += supersede_csv_rows(
                            session, payment_method=acct.payment_method,
                            covered_min=min(acct_dates), covered_max=max(acct_dates),
                        )
                except Exception:
                    result.failed += 1
                    logger.exception(
                        "plaid supersede failure",
                        extra={"plaid_item_id": item.id,
                               "plaid_account_id": acct.plaid_account_id},
                    )

        # Advance the cursor ONLY on a fully clean page-loop. If any row failed,
        # leave item.cursor UNCHANGED so the next sync re-fetches from the last
        # good cursor and re-delivers the failed rows (re-fetch is idempotent via
        # source_hash, so already-succeeded rows are no-ops). Advancing past a
        # failed `added` row would permanently drop it (Plaid's /transactions/sync
        # is incremental and never re-delivers a passed cursor). REQ-PT-006.
        if result.failed == 0:
            item.cursor = next_cursor
            item.last_sync_status = "ok"
            item.last_error = None
            result.status = "ok"
        else:
            # Partial failure: surface it on the Item and hold the cursor.
            item.last_sync_status = "error"
            item.last_error = "PARTIAL_FAILURE"
            result.status = "error"
        item.last_sync_at = pulled_at
        log_row.records_processed = (
            result.added + result.reactivated + result.modified + result.removed
        )
        log_row.records_failed = result.failed
        log_row.status = (IngestionStatus.PARTIAL_FAILURE.value if result.failed
                          else IngestionStatus.SUCCESS.value)
        # P1-002/P1-c4f: persist the skip/unrecognized-account counts on the
        # durable audit trail, not just a journald WARNING/ERROR log line that
        # rotates out — `records_processed`/`records_failed` don't carry
        # per-account_id detail, so without this the only trace of WHICH
        # account_ids were involved is whatever's left in the log files.
        detail_parts: list[str] = []
        if result.skipped_unknown_account:
            detail_parts.append(
                "skipped_known_mirror_account="
                f"{dict(sorted(result.skipped_unknown_account.items()))}"
            )
        if result.unrecognized_account_ids:
            detail_parts.append(
                "unrecognized_account_ids="
                f"{dict(sorted(result.unrecognized_account_ids.items()))}"
            )
        if detail_parts:
            log_row.error_detail = "; ".join(detail_parts)

    except RetryablePlaidError as exc:
        item.last_sync_status = ("institution_down"
            if exc.error_code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING") else "error")
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.retryable = True
        log_row.error_detail = exc.error_code
        result.status = item.last_sync_status
        result.error_code = exc.error_code
    except (TerminalPlaidError, PlaidErrorBase) as exc:
        item.last_sync_status = "error"
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = exc.error_code
        result.status = "error"
        result.error_code = exc.error_code
    except Exception as exc:
        item.last_sync_status = "error"
        item.last_error = "UNEXPECTED"
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = f"unexpected: {type(exc).__name__}"
        result.status = "error"
        result.error_code = "UNEXPECTED"
        logger.exception("plaid tx per-item failure", extra={"plaid_item_id": item.id})

    return result


@dataclass
class TxBatchResult:
    items: list[TxItemResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total_added(self) -> int:
        return sum(i.added for i in self.items)

    @property
    def total_reactivated(self) -> int:
        return sum(i.reactivated for i in self.items)

    @property
    def total_modified(self) -> int:
        return sum(i.modified for i in self.items)

    @property
    def total_removed(self) -> int:
        return sum(i.removed for i in self.items)

    @property
    def total_failed(self) -> int:
        return sum(i.failed for i in self.items)

    @property
    def total_superseded(self) -> int:
        return sum(i.superseded for i in self.items)

    @property
    def total_skipped_unknown_account(self) -> int:
        """REQ-WBR-LED-014 — transactions skipped across all Items because their
        account_id was not owned by the syncing Item."""
        return sum(i.skipped_unknown_total for i in self.items)


def sync_all_active(session: Session, *, client: Any, dry_run: bool = True) -> TxBatchResult:
    """Sync transactions for every active PlaidItem. DRY-RUN default."""
    batch = TxBatchResult(dry_run=dry_run)
    items = (
        session.query(PlaidItem)
        .filter(PlaidItem.status == "active", ~PlaidItem.item_id.like("placeholder_%"))
        .all()
    )
    for item in items:
        batch.items.append(sync_one_item(session, item, client=client))
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return batch
