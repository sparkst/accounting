"""Transaction splitter — create child line items from a parent transaction.

REQ-ID: SPLIT-001  POST /api/transactions/{id}/split creates child transactions.
REQ-ID: SPLIT-002  Parent gets status split_parent, children get individual classifications.
REQ-ID: SPLIT-003  Children amounts must sum to parent total (validation error if not).
REQ-ID: SPLIT-004  Rejecting a split_parent cascades reject to all children.
REQ-ID: SPLIT-005  Cannot confirm a child transaction if parent is rejected.
REQ-ID: SPLIT-006  Hotel keyword detection pre-populates suggested splits.
REQ-ID: SPLIT-007  Cannot re-split an already-split parent (return 422).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from src.models.audit_event import AuditEvent
from src.models.enums import ConfirmedBy, TaxCategory, TransactionStatus
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Hotel keyword detection
# ---------------------------------------------------------------------------

_HOTEL_KEYWORDS: frozenset[str] = frozenset(
    [
        "hotel",
        "marriott",
        "hilton",
        "hyatt",
        "westin",
        "sheraton",
        "courtyard",
        "hampton inn",
        "holiday inn",
        "doubletree",
        "embassy suites",
        "fairfield",
        "four seasons",
        "ritz",
        "kimpton",
        "radisson",
        "ihg",
        "wyndham",
        "best western",
        "la quinta",
        "motel",
        "inn",
        "resort",
        "lodge",
        "suites",
    ]
)

_HOTEL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_HOTEL_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def is_hotel_transaction(description: str) -> bool:
    """Return True if the description contains hotel-related keywords."""
    return bool(_HOTEL_PATTERN.search(description))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SplitLineItem:
    """One line item in a split request."""

    amount: Decimal
    entity: str | None = None
    tax_category: str | None = None
    description: str | None = None


@dataclass
class HotelSplitSuggestion:
    """Pre-populated split suggestion for a hotel transaction.

    Room charge goes to TRAVEL; meals/incidentals go to MEALS.
    The suggested amounts are 80/20 placeholders — the user adjusts them.
    """

    room_amount: Decimal
    meals_amount: Decimal
    entity: str | None
    parent_amount: Decimal

    @property
    def as_line_items(self) -> list[SplitLineItem]:
        return [
            SplitLineItem(
                amount=self.room_amount,
                entity=self.entity,
                tax_category=TaxCategory.TRAVEL.value,
                description="Room charge",
            ),
            SplitLineItem(
                amount=self.meals_amount,
                entity=self.entity,
                tax_category=TaxCategory.MEALS.value,
                description="Meals / incidentals",
            ),
        ]


@dataclass
class SplitResult:
    """Outcome of a successful split operation."""

    parent: Transaction
    children: list[Transaction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class SplitValidationError(ValueError):
    """Raised when split input fails validation."""


def _to_decimal(value: object) -> Decimal:
    """Convert int, float, str, or Decimal to Decimal, raising SplitValidationError."""
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise SplitValidationError(f"Invalid amount: {value!r}") from exc


def validate_split_amounts(
    parent_amount: Decimal,
    line_items: list[SplitLineItem],
) -> None:
    """Raise SplitValidationError if line item amounts do not sum to parent total.

    Comparison is done to 2 decimal places to avoid floating-point noise.
    """
    if not line_items:
        raise SplitValidationError("At least one line item is required.")

    child_total = sum(item.amount for item in line_items)
    # Round both to 2dp before comparing.
    if round(child_total, 2) != round(parent_amount, 2):
        raise SplitValidationError(
            f"Line item amounts ({child_total}) must sum to parent total "
            f"({parent_amount}). Difference: {abs(child_total - parent_amount)}."
        )


# ---------------------------------------------------------------------------
# Core splitter
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def split_transaction(
    session: Session,
    parent: Transaction,
    line_items: list[SplitLineItem],
) -> SplitResult:
    """Split *parent* into child transactions, one per line item.

    Preconditions (caller must have already validated):
    - parent.status is not split_parent (no re-splitting)
    - parent.status is not rejected
    - parent.parent_id is None (top-level only; children cannot be split)
    - line_items amounts sum to parent.amount

    Postconditions:
    - parent.status = split_parent
    - Each child: parent_id=parent.id, status=needs_review (or auto_classified
      if entity/tax_category provided)
    - AuditEvent rows created for parent status change and each child created.
    """
    now = _now()

    # ── Update parent ─────────────────────────────────────────────────────────
    old_status = parent.status
    parent.status = TransactionStatus.SPLIT_PARENT.value
    parent.updated_at = now

    audit_parent = AuditEvent(
        transaction_id=parent.id,
        field_changed="status",
        old_value=old_status,
        new_value=TransactionStatus.SPLIT_PARENT.value,
        changed_by=ConfirmedBy.HUMAN.value,
        changed_at=now,
    )
    session.add(audit_parent)

    # ── Create children ───────────────────────────────────────────────────────
    children: list[Transaction] = []
    for i, item in enumerate(line_items):
        child_status = (
            TransactionStatus.AUTO_CLASSIFIED.value
            if (item.entity and item.tax_category)
            else TransactionStatus.NEEDS_REVIEW.value
        )

        child_description = item.description or parent.description

        child = Transaction(
            id=_new_uuid(),
            source=parent.source,
            source_id=parent.source_id,
            # source_hash must be unique — derive from parent hash + index.
            source_hash=f"{parent.source_hash}__split_{i}",
            date=parent.date,
            description=child_description,
            amount=item.amount,
            currency=parent.currency,
            entity=item.entity,
            direction=parent.direction,
            tax_category=item.tax_category,
            status=child_status,
            confidence=0.0,
            parent_id=parent.id,
            payment_method=parent.payment_method,
            raw_data=parent.raw_data,
            confirmed_by=ConfirmedBy.AUTO.value,
            created_at=now,
            updated_at=now,
        )
        session.add(child)
        children.append(child)

        audit_child = AuditEvent(
            transaction_id=child.id,
            field_changed="created_via_split",
            old_value=None,
            new_value=f"split from parent {parent.id}",
            changed_by=ConfirmedBy.HUMAN.value,
            changed_at=now,
        )
        session.add(audit_child)

    session.flush()  # assign IDs without committing
    return SplitResult(parent=parent, children=children)


def cascade_reject_children(session: Session, parent: Transaction) -> list[Transaction]:
    """Set all children of *parent* to rejected status.

    Called when a split_parent is being rejected. Returns the list of children
    that were updated.
    """
    now = _now()
    children: list[Transaction] = (
        session.query(Transaction)
        .filter(Transaction.parent_id == parent.id)
        .all()
    )

    for child in children:
        old_status = child.status
        child.status = TransactionStatus.REJECTED.value
        child.updated_at = now

        audit = AuditEvent(
            transaction_id=child.id,
            field_changed="status",
            old_value=old_status,
            new_value=TransactionStatus.REJECTED.value,
            changed_by=ConfirmedBy.HUMAN.value,
            changed_at=now,
        )
        session.add(audit)

    return children


# ---------------------------------------------------------------------------
# Hotel suggestion helper
# ---------------------------------------------------------------------------


def suggest_hotel_splits(parent: Transaction) -> HotelSplitSuggestion | None:
    """Return a pre-populated hotel split suggestion if the description looks like a hotel.

    Splits 80% to TRAVEL (room) and 20% to MEALS (meals/incidentals).
    Returns None if the description does not match hotel keywords or if amount
    is unknown.
    """
    if not is_hotel_transaction(parent.description):
        return None
    if parent.amount is None:
        return None

    total = Decimal(str(parent.amount))
    # For expense transactions, amount is negative. Work with the absolute value
    # for the split ratio, then restore the sign.
    sign = Decimal("-1") if total < 0 else Decimal("1")
    abs_total = abs(total)

    room_amount = (abs_total * Decimal("0.80")).quantize(Decimal("0.01"))
    meals_amount = abs_total - room_amount  # ensures exact sum

    return HotelSplitSuggestion(
        room_amount=sign * room_amount,
        meals_amount=sign * meals_amount,
        entity=parent.entity,
        parent_amount=total,
    )
