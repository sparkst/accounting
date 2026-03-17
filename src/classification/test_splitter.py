"""Tests for the transaction splitter module.

REQ-ID: SPLIT-001  split_transaction creates child transactions.
REQ-ID: SPLIT-002  Parent gets split_parent status; children get individual classifications.
REQ-ID: SPLIT-003  Children amounts must sum to parent total.
REQ-ID: SPLIT-004  Rejecting a split_parent cascades reject to all children.
REQ-ID: SPLIT-005  Cannot confirm a child if parent is rejected.
REQ-ID: SPLIT-006  Hotel keyword detection pre-populates suggested splits.
REQ-ID: SPLIT-007  Cannot re-split an already-split parent.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection as _conn  # noqa: F401 — registers all ORM models
from src.classification.splitter import (
    HotelSplitSuggestion,
    SplitLineItem,
    SplitValidationError,
    cascade_reject_children,
    is_hotel_transaction,
    split_transaction,
    suggest_hotel_splits,
    validate_split_amounts,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

_TEST_ENGINE = create_engine(
    "sqlite+pysqlite:///file:splitter_test?mode=memory&cache=shared&uri=true",
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_TEST_ENGINE, "connect")
def _set_pragmas(conn, _record):  # type: ignore[no-untyped-def]
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_TEST_ENGINE)
_TestSession: sessionmaker[Session] = sessionmaker(
    bind=_TEST_ENGINE, autocommit=False, autoflush=False
)


@pytest.fixture(autouse=True)
def clean_db():
    """Wipe tables before each test for isolation."""
    from sqlalchemy import text

    with _TEST_ENGINE.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    description: str = "Test Vendor",
    amount: Decimal = Decimal("-120.00"),
    entity: str | None = Entity.SPARKRY.value,
    status: str = TransactionStatus.NEEDS_REVIEW.value,
    parent_id: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.GMAIL_N8N.value,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date="2025-06-15",
        description=description,
        amount=amount,
        currency="USD",
        entity=entity,
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.TRAVEL.value,
        status=status,
        confidence=0.5,
        parent_id=parent_id,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.AUTO.value,
    )
    session.add(tx)
    session.commit()
    return tx


# ---------------------------------------------------------------------------
# validate_split_amounts
# ---------------------------------------------------------------------------


class TestValidateSplitAmounts:
    def test_valid_split_passes(self) -> None:
        """Amounts summing to parent total pass without error."""
        items = [
            SplitLineItem(amount=Decimal("-80.00")),
            SplitLineItem(amount=Decimal("-40.00")),
        ]
        validate_split_amounts(Decimal("-120.00"), items)  # should not raise

    def test_amounts_do_not_sum_raises(self) -> None:
        """Mismatched totals raise SplitValidationError with a clear message."""
        items = [
            SplitLineItem(amount=Decimal("-80.00")),
            SplitLineItem(amount=Decimal("-30.00")),  # sum = -110, not -120
        ]
        with pytest.raises(SplitValidationError, match="must sum to parent total"):
            validate_split_amounts(Decimal("-120.00"), items)

    def test_single_item_equal_to_parent_passes(self) -> None:
        items = [SplitLineItem(amount=Decimal("-120.00"))]
        validate_split_amounts(Decimal("-120.00"), items)

    def test_empty_items_raises(self) -> None:
        with pytest.raises(SplitValidationError, match="At least one"):
            validate_split_amounts(Decimal("-120.00"), [])

    def test_floating_point_tolerance(self) -> None:
        """Rounding to 2dp avoids floating-point noise."""
        items = [
            SplitLineItem(amount=Decimal("-33.33")),
            SplitLineItem(amount=Decimal("-33.33")),
            SplitLineItem(amount=Decimal("-33.34")),
        ]
        validate_split_amounts(Decimal("-100.00"), items)

    def test_positive_amounts_income_split(self) -> None:
        """Income transactions (positive amounts) also validate correctly."""
        items = [
            SplitLineItem(amount=Decimal("500.00")),
            SplitLineItem(amount=Decimal("250.00")),
        ]
        validate_split_amounts(Decimal("750.00"), items)


# ---------------------------------------------------------------------------
# split_transaction
# ---------------------------------------------------------------------------


class TestSplitTransaction:
    def test_creates_children(self) -> None:
        """split_transaction creates one child per line item."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-120.00"))
            parent_id = parent.id

            items = [
                SplitLineItem(
                    amount=Decimal("-80.00"),
                    entity=Entity.SPARKRY.value,
                    tax_category=TaxCategory.TRAVEL.value,
                    description="Flight",
                ),
                SplitLineItem(
                    amount=Decimal("-40.00"),
                    entity=Entity.SPARKRY.value,
                    tax_category=TaxCategory.MEALS.value,
                    description="Dinner",
                ),
            ]
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            children = (
                s.query(Transaction)
                .filter(Transaction.parent_id == parent_id)
                .all()
            )
            assert len(children) == 2
            amounts = sorted(str(c.amount) for c in children)
            assert amounts == ["-80.00", "-40.00"] or sorted(
                [str(c.amount) for c in children]
            ) == sorted(["-80.00", "-40.00"])

    def test_parent_status_becomes_split_parent(self) -> None:
        """Parent status is changed to split_parent after the split."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"))
            parent_id = parent.id

            items = [SplitLineItem(amount=Decimal("-100.00"), description="All")]
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            tx = s.query(Transaction).filter(Transaction.id == parent_id).first()
            assert tx is not None
            assert tx.status == TransactionStatus.SPLIT_PARENT.value

    def test_classified_child_gets_auto_classified_status(self) -> None:
        """Children with entity + tax_category get auto_classified status."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"))

            items = [
                SplitLineItem(
                    amount=Decimal("-100.00"),
                    entity=Entity.SPARKRY.value,
                    tax_category=TaxCategory.TRAVEL.value,
                    description="Room",
                ),
            ]
            split_result = split_transaction(s, parent, items)
            s.commit()
            assert split_result.children[0].status == TransactionStatus.AUTO_CLASSIFIED.value

    def test_unclassified_child_gets_needs_review_status(self) -> None:
        """Children without entity/category get needs_review status."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"))

            items = [
                SplitLineItem(
                    amount=Decimal("-100.00"),
                    description="Unknown",
                    # no entity, no tax_category
                ),
            ]
            needs_review_result = split_transaction(s, parent, items)
            s.commit()
            assert needs_review_result.children[0].status == TransactionStatus.NEEDS_REVIEW.value

    def test_children_inherit_parent_fields(self) -> None:
        """Children inherit date, currency, direction, payment_method from parent."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"), description="Hotel Stay")
            parent_id = parent.id

            items = [SplitLineItem(amount=Decimal("-100.00"), description="Child")]
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            parent_tx = s.query(Transaction).filter(Transaction.id == parent_id).first()
            assert parent_tx is not None
            child = (
                s.query(Transaction)
                .filter(Transaction.parent_id == parent_id)
                .first()
            )
            assert child is not None
            assert child.date == parent_tx.date
            assert child.currency == parent_tx.currency
            assert child.direction == parent_tx.direction

    def test_audit_events_created(self) -> None:
        """AuditEvent rows are created for parent status change and child creation."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"))
            parent_id = parent.id

            items = [SplitLineItem(amount=Decimal("-100.00"), description="Item")]
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            # Parent status change audit
            parent_events = (
                s.query(AuditEvent)
                .filter(AuditEvent.transaction_id == parent_id)
                .all()
            )
            assert any(e.field_changed == "status" for e in parent_events)
            status_event = next(
                e for e in parent_events if e.field_changed == "status"
            )
            assert status_event.new_value == TransactionStatus.SPLIT_PARENT.value

        with _TestSession() as s:
            # Child creation audit
            child = (
                s.query(Transaction)
                .filter(Transaction.parent_id == parent_id)
                .first()
            )
            assert child is not None
            child_events = (
                s.query(AuditEvent)
                .filter(AuditEvent.transaction_id == child.id)
                .all()
            )
            assert any(e.field_changed == "created_via_split" for e in child_events)

    def test_child_description_defaults_to_parent(self) -> None:
        """If no description is given for a line item, it uses the parent's description."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"), description="Hilton Hotel")
            parent_id = parent.id

            items = [SplitLineItem(amount=Decimal("-100.00"))]  # no description
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            child = (
                s.query(Transaction)
                .filter(Transaction.parent_id == parent_id)
                .first()
            )
            assert child is not None
            assert child.description == "Hilton Hotel"


# ---------------------------------------------------------------------------
# cascade_reject_children
# ---------------------------------------------------------------------------


class TestCascadeRejectChildren:
    def test_rejects_all_children(self) -> None:
        """All children of a split_parent get rejected when parent is rejected."""
        with _TestSession() as s:
            parent = _make_tx(
                s,
                amount=Decimal("-100.00"),
                status=TransactionStatus.SPLIT_PARENT.value,
            )
            parent_id = parent.id

            items = [
                SplitLineItem(amount=Decimal("-60.00"), description="Child A"),
                SplitLineItem(amount=Decimal("-40.00"), description="Child B"),
            ]
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            parent_tx = s.query(Transaction).filter(Transaction.id == parent_id).first()
            assert parent_tx is not None
            children = cascade_reject_children(s, parent_tx)
            s.commit()
            assert len(children) == 2
            assert all(c.status == TransactionStatus.REJECTED.value for c in children)

    def test_creates_audit_events_for_each_child(self) -> None:
        """An AuditEvent is created for each child status change on cascade reject."""
        with _TestSession() as s:
            parent = _make_tx(
                s,
                amount=Decimal("-100.00"),
                status=TransactionStatus.SPLIT_PARENT.value,
            )
            parent_id = parent.id

            items = [
                SplitLineItem(amount=Decimal("-100.00"), description="Single child"),
            ]
            split_transaction(s, parent, items)
            s.commit()

        with _TestSession() as s:
            parent_tx = s.query(Transaction).filter(Transaction.id == parent_id).first()
            assert parent_tx is not None
            children = cascade_reject_children(s, parent_tx)
            s.commit()
            child_id = children[0].id

        with _TestSession() as s:
            events = (
                s.query(AuditEvent)
                .filter(
                    AuditEvent.transaction_id == child_id,
                    AuditEvent.field_changed == "status",
                    AuditEvent.new_value == TransactionStatus.REJECTED.value,
                )
                .all()
            )
            assert len(events) >= 1

    def test_no_children_returns_empty_list(self) -> None:
        """cascade_reject_children on a transaction with no children returns []."""
        with _TestSession() as s:
            parent = _make_tx(s, amount=Decimal("-100.00"))
            parent_id = parent.id

        with _TestSession() as s:
            parent_tx = s.query(Transaction).filter(Transaction.id == parent_id).first()
            assert parent_tx is not None
            result = cascade_reject_children(s, parent_tx)
            assert result == []


# ---------------------------------------------------------------------------
# Hotel detection and suggestions
# ---------------------------------------------------------------------------


class TestHotelDetection:
    @pytest.mark.parametrize(
        "description",
        [
            "Marriott Seattle Downtown",
            "HILTON HOTEL PORTLAND",
            "Hyatt Place Denver",
            "Hotel Monaco",
            "Embassy Suites Conference",
            "Hampton Inn Chicago",
            "Westin Grand",
        ],
    )
    def test_hotel_keywords_detected(self, description: str) -> None:
        assert is_hotel_transaction(description) is True

    @pytest.mark.parametrize(
        "description",
        [
            "Anthropic, PBC",
            "AWS Invoice",
            "Delta Airlines",
            "Hertz Car Rental",
            "Conference Registration",
        ],
    )
    def test_non_hotel_not_detected(self, description: str) -> None:
        assert is_hotel_transaction(description) is False


class TestSuggestHotelSplits:
    def test_hotel_suggestion_returned(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="Marriott Seattle",
                amount=Decimal("-200.00"),
                entity=Entity.SPARKRY.value,
            )
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is not None
        assert isinstance(suggestion, HotelSplitSuggestion)

    def test_suggestion_amounts_sum_to_parent(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="Hilton Chicago",
                amount=Decimal("-300.00"),
                entity=Entity.SPARKRY.value,
            )
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is not None
        total = suggestion.room_amount + suggestion.meals_amount
        assert total == Decimal("-300.00")

    def test_suggestion_80_20_split(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="Hyatt Regency",
                amount=Decimal("-200.00"),
                entity=Entity.BLACKLINE.value,
            )
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is not None
        assert suggestion.room_amount == Decimal("-160.00")
        assert suggestion.meals_amount == Decimal("-40.00")

    def test_no_suggestion_for_non_hotel(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(s, description="Delta Airlines", amount=Decimal("-500.00"))
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is None

    def test_no_suggestion_when_amount_is_none(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(s, description="Marriott", amount=Decimal("-100.00"))
            tx.amount = None
            s.commit()
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is None

    def test_as_line_items_returns_correct_categories(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="Marriott PDX",
                amount=Decimal("-100.00"),
                entity=Entity.SPARKRY.value,
            )
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is not None
        items = suggestion.as_line_items
        assert len(items) == 2
        categories = {item.tax_category for item in items}
        assert TaxCategory.TRAVEL.value in categories
        assert TaxCategory.MEALS.value in categories

    def test_entity_propagated_to_suggestions(self) -> None:
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="Westin Convention",
                amount=Decimal("-250.00"),
                entity=Entity.BLACKLINE.value,
            )
            suggestion = suggest_hotel_splits(tx)

        assert suggestion is not None
        for item in suggestion.as_line_items:
            assert item.entity == Entity.BLACKLINE.value
