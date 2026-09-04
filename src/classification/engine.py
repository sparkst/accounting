"""Classification engine — 3-tier orchestrator.

Runs tiers in order: Tier 1 (vendor rules) → Tier 2 (structural patterns) →
Tier 3 (LLM via Gemini API, ``gemini-2.5-flash-lite``). Stops at the first
result with confidence >= 0.7. If no tier reaches the threshold the
transaction is flagged needs_review.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus

if TYPE_CHECKING:
    from src.models.transaction import Transaction


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    """Output of the classification engine.

    All fields are populated on every successful classification. When the
    engine cannot reach ``confidence >= 0.7`` the status is set to
    ``needs_review`` and ``review_reason`` explains why.
    """

    entity: Entity
    tax_category: TaxCategory
    direction: Direction
    confidence: float
    tier_used: int  # 1, 2, or 3
    reasoning: str
    status: TransactionStatus = field(default=TransactionStatus.AUTO_CLASSIFIED)
    review_reason: str | None = field(default=None)
    tax_subcategory: str | None = field(default=None)
    deductible_pct: float = field(default=1.0)
    # REQ-MCA-002: id of the winning Tier-1 VendorRule (populated only by
    # ``rules.lookup_vendor_rule``); None for Tier-2/3 results. The auto-confirm
    # policy keys on this + the rule's confidence.
    rule_id: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Minimum confidence required for auto-classification.
_AUTO_CLASSIFY_THRESHOLD = 0.7

# Sources where the ingesting adapter assigns the entity AUTHORITATIVELY — Stripe
# from the per-account API key, Shopify from the store. Classification (esp. the
# Tier-3 LLM) must never reassign which business owns these rows; it was guessing
# wrong (parent-account Substack charges/fees/payouts → BlackLine). Only the
# tax category is open to (re)classification for these.
_ENTITY_AUTHORITATIVE_SOURCES = frozenset({Source.STRIPE.value, Source.SHOPIFY.value})

# Sources whose stored ``amount`` sign is the authoritative cash direction
# (negative = real outflow). For these, an income classification on a negative
# amount is internally contradictory and must be vetoed. NOTE: gmail_n8n is
# deliberately excluded — it stores income as -abs(amount) *before* the
# classifier assigns direction=income (see CLAUDE.md "Adapter behavior"), so a
# negative Gmail amount tagged income is not a *sign* mismatch. Gmail income is
# instead vetoed on the tax category by _veto_gmail_income() (accounting#85).
_AUTHORITATIVE_SIGN_SOURCES = frozenset({Source.PLAID.value, Source.BANK_CSV.value})

# accounting#85: phrase the gmail adapter writes into review_reason when the
# upstream payload was corrupted. It must survive classification, so
# apply_result() re-asserts NEEDS_REVIEW whenever it is already present.
_CORRUPTED_MARKER = "Corrupted upstream payload"

_INCOME_TAX_CATEGORIES = frozenset({
    TaxCategory.CONSULTING_INCOME,
    TaxCategory.SUBSCRIPTION_INCOME,
    TaxCategory.SALES_INCOME,
    TaxCategory.WHOLESALE_INCOME,
})


def _reconcile_sign(
    transaction: Transaction, result: ClassificationResult
) -> ClassificationResult:
    """Veto income/expense classifications that contradict an authoritative sign.

    A Plaid/bank row's stored amount sign is ground truth for cash direction.
    Two mirror-image vetoes (REQ-FIX-ING-008):

    - Outflow (``amount < 0``) classified income: real money left the
      account, so it can never be income (e.g. a vendor keyword like
      "subscription" or "shopify" on a credit-card *charge*). Override to
      ``OTHER_EXPENSE`` and route to ``needs_review`` — the override is safe
      because an outflow can never legitimately be income.
    - Inflow (``amount >= 0``) classified expense: money arriving can never
      be a real outflow. Route to ``needs_review`` WITHOUT overriding
      category/direction — a positive-amount "expense" is usually a refund,
      and whether the human wants it recorded as refund-income or a category
      reversal is a genuine judgment call an automated override would get
      wrong roughly half the time. This asymmetry (override vs. no-override)
      is intentional.

    The outflow-on-income veto does NOT explicitly gate on direction, so it can
    override a TRANSFER or REIMBURSABLE row if assigned an income tax_category
    (theoretical — reimbursables/transfers never get income categories in practice).
    The inflow-on-expense veto preserves direction/category without override (kept
    for human review).
    Returns *result* unchanged when consistent with the authoritative sign.
    """
    if transaction.source not in _AUTHORITATIVE_SIGN_SOURCES:
        return result
    if transaction.amount is None:
        return result
    try:
        is_outflow = Decimal(str(transaction.amount)) < 0
    except (InvalidOperation, ValueError):
        return result

    # P3-b2e: transfer/reimbursable rows are EXPLICITLY exempt — the
    # tax_category disjunct alone would otherwise let an income-category label
    # on a transfer clobber its direction. The is_expense mirror branch below
    # is direction-gated and needs no equivalent guard.
    is_income = result.direction not in (
        Direction.TRANSFER,
        Direction.REIMBURSABLE,
    ) and (
        result.direction == Direction.INCOME
        or result.tax_category in _INCOME_TAX_CATEGORIES
    )
    if is_outflow and is_income:
        return replace(
            result,
            direction=Direction.EXPENSE,
            tax_category=TaxCategory.OTHER_EXPENSE,
            status=TransactionStatus.NEEDS_REVIEW,
            deductible_pct=1.0,
            review_reason=(
                f"Sign/category mismatch: {transaction.source} amount "
                f"{transaction.amount} is an outflow but was classified as income "
                f"({result.tax_category.value}, tier {result.tier_used}). Overridden "
                "to expense for review."
            ),
        )

    # REQ-FIX-ING-008: mirror veto — an authoritative-signed INFLOW classified
    # as an expense is equally internally contradictory (a positive Plaid/
    # bank amount can never be a real outflow). Unlike the income-on-outflow
    # branch above, category/direction are NOT overridden here: a
    # positive-amount "expense" is usually a refund, and whether the human
    # wants it recorded as refund-income or a category reversal is a genuine
    # judgment call an automated override would get wrong half the time.
    # transfer/reimbursable directions are exempt in both branches (an
    # inbound transfer or a reimbursement receipt is not a sign mismatch).
    is_inflow = not is_outflow
    is_expense = result.direction == Direction.EXPENSE
    if is_inflow and is_expense:
        return replace(
            result,
            status=TransactionStatus.NEEDS_REVIEW,
            review_reason=(
                f"Sign/category mismatch: {transaction.source} amount "
                f"{transaction.amount} is an inflow but was classified as expense "
                f"({result.tax_category.value}, tier {result.tier_used}) — likely "
                "refund or misclassification; confirm."
            ),
        )

    return result


def _veto_gmail_income(
    transaction: Transaction, result: ClassificationResult
) -> ClassificationResult:
    """Route gmail-sourced rows classified as income to needs_review.

    accounting#85: ``gmail_n8n``'s adapter contract is
    ``signed_amount = -abs(amount)`` — every gmail receipt is an expense by
    construction, so gmail is deliberately excluded from
    ``_AUTHORITATIVE_SIGN_SOURCES`` and ``_reconcile_sign()`` never fires on
    it. That left a hole: a corrupted description (e.g. the ``[object
    Object]`` literal) defeats tiers 1-2, and a tier-3 mis-guess of an income
    category was silently AUTO_CLASSIFIED, inflating Sparkry B&O gross.

    Category/direction are preserved (mirroring the inflow-on-expense veto):
    only the status and review_reason change, so the human sees the tier's
    suggestion while the row stays out of the automated income totals.
    """
    if transaction.source != Source.GMAIL_N8N.value:
        return result
    # REQ-GMOBJ-02: ANY income tax_category counts, whatever the direction —
    # gross receipts are aggregated on tax_category alone, so a
    # TRANSFER/REIMBURSABLE direction is no exemption.
    is_income = (
        result.direction == Direction.INCOME
        or result.tax_category in _INCOME_TAX_CATEGORIES
    )
    if not is_income:
        return result
    reason = (
        f"Source/direction mismatch: gmail receipts are always expenses, "
        f"but tier {result.tier_used} classified this row as income "
        f"({result.tax_category.value}). Routed to review."
    )
    if result.review_reason:
        # Keep the tier's own explanation (e.g. the low-confidence note).
        reason = f"{result.review_reason} {reason}"
    return replace(
        result,
        status=TransactionStatus.NEEDS_REVIEW,
        review_reason=reason,
    )


def _finalise(
    transaction: Transaction, result: ClassificationResult
) -> ClassificationResult:
    """Apply the sign and gmail-income vetoes to a tier's result."""
    return _veto_gmail_income(transaction, _reconcile_sign(transaction, result))


def classify(
    transaction: Transaction,
    session: Session,
    *,
    llm_api_key: str | None = None,
) -> ClassificationResult:
    """Classify *transaction* using the 3-tier pipeline.

    Tiers are imported lazily to avoid circular imports during module load,
    but are referenced by their module-qualified names so that
    ``unittest.mock.patch`` can intercept them in tests.

    Args:
        transaction: The Transaction ORM instance to classify. The instance
            is **not** mutated here — callers are responsible for applying
            the result back to the model and committing.
        session: An open SQLAlchemy session used by Tier 1 to query
            VendorRule rows.
        llm_api_key: Optional API key override for Tier 3 (Gemini). When
            *None* the LLM classifier falls back to the ``GEMINI_API_KEY``
            environment variable. REQ-FIX-ING-010: renamed from
            ``anthropic_api_key`` — Tier 3 is Gemini, and the old name let
            callers accidentally inject an Anthropic key into
            ``genai.Client(api_key=...)``, clobbering the correct
            ``GEMINI_API_KEY`` env fallback. No back-compat shim — both
            call sites are in-repo.

    Returns:
        A :class:`ClassificationResult` populated by whichever tier succeeded.
    """
    # Late imports break the circular-import cycle at load time while still
    # allowing patch() to intercept calls during tests — patch the functions
    # at their *home* module, e.g. ``src.classification.rules.lookup_vendor_rule``.
    from src.classification import llm_classifier as _llm_mod
    from src.classification import patterns as _pat_mod
    from src.classification import rules as _rules_mod

    # ── Tier 1: Vendor rules ────────────────────────────────────────────────
    tier1 = _rules_mod.lookup_vendor_rule(transaction.description, session)
    if tier1 is not None and tier1.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier1.tier_used = 1
        tier1.status = TransactionStatus.AUTO_CLASSIFIED
        return _finalise(transaction, tier1)

    # ── Tier 2: Structural patterns ─────────────────────────────────────────
    tier2 = _pat_mod.match_structural_pattern(transaction)
    if tier2 is not None and tier2.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier2.tier_used = 2
        tier2.status = TransactionStatus.AUTO_CLASSIFIED
        return _finalise(transaction, tier2)

    # ── Tier 3: LLM classification ──────────────────────────────────────────
    tier3 = _llm_mod.llm_classify(transaction, api_key=llm_api_key, _session=session)
    if tier3.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier3.tier_used = 3
        tier3.status = TransactionStatus.AUTO_CLASSIFIED
        return _finalise(transaction, tier3)

    # ── Needs review ────────────────────────────────────────────────────────
    # Best partial result is kept so the reviewer has a pre-filled suggestion.
    tier3.tier_used = 3
    tier3.status = TransactionStatus.NEEDS_REVIEW
    tier3.review_reason = (
        f"Low confidence ({tier3.confidence:.2f}) from Tier 3 LLM: "
        f"{tier3.reasoning}"
    )
    return _finalise(transaction, tier3)


def apply_result(transaction: Transaction, result: ClassificationResult) -> None:
    """Write a :class:`ClassificationResult` back onto a Transaction ORM instance.

    Does **not** commit the session — that is the caller's responsibility.
    """
    # Preserve the adapter's entity for sources where it is structurally
    # determined (Stripe per-account, Shopify store). Classification may set the
    # entity only when the adapter could not (e.g. gmail/bank → entity is None).
    if (
        transaction.source not in _ENTITY_AUTHORITATIVE_SOURCES
        or transaction.entity is None
    ):
        transaction.entity = result.entity.value
    prior_reason = transaction.review_reason or ""
    transaction.tax_category = result.tax_category.value
    transaction.direction = result.direction.value
    transaction.confidence = result.confidence
    transaction.status = result.status.value
    transaction.review_reason = result.review_reason
    if result.tax_subcategory:
        transaction.tax_subcategory = result.tax_subcategory
    transaction.deductible_pct = result.deductible_pct

    # accounting#85: a corrupted-payload flag set at ingest must survive
    # classification — otherwise a confident tier-3 expense result silently
    # auto-classifies the row and the marker is lost.
    if _CORRUPTED_MARKER in prior_reason:
        transaction.status = TransactionStatus.NEEDS_REVIEW.value
        transaction.review_reason = (
            f"{transaction.review_reason} {prior_reason}".strip()
            if transaction.review_reason
            else prior_reason
        )

    # Missing amounts always need review regardless of classification confidence
    if transaction.amount is None and transaction.status != TransactionStatus.NEEDS_REVIEW.value:
        transaction.status = TransactionStatus.NEEDS_REVIEW.value
        reason = (transaction.review_reason or "")
        transaction.review_reason = (
            reason + " Amount is missing — manual entry required."
        ).strip()
