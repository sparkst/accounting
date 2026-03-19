"""Tests for reimbursable expense linking and overdue flagging.

REQ-ID: REIMB-001  POST /api/transactions/{id}/link-reimbursement sets bidirectional link.
REQ-ID: REIMB-002  link-reimbursement validates expense direction (reimbursable|expense).
REQ-ID: REIMB-003  link-reimbursement validates reimbursement direction (income).
REQ-ID: REIMB-004  link-reimbursement rejects same-direction pairs (both expense, both income).
REQ-ID: REIMB-005  link-reimbursement creates AuditEvent for both transactions.
REQ-ID: REIMB-006  GET ?direction=reimbursable&overdue=true returns unlinked expenses > 30 days.
REQ-ID: REIMB-007  Overdue filter excludes recently-created reimbursable expenses.
REQ-ID: REIMB-008  Overdue filter excludes already-linked reimbursable expenses.
REQ-ID: REIMB-009  Editing amount on a linked expense flags partner as needs_review.
REQ-ID: REIMB-010  Rejecting a linked expense flags partner as needs_review.
REQ-ID: REIMB-011  link-reimbursement returns 404 if expense tx not found.
REQ-ID: REIMB-012  link-reimbursement returns 404 if reimbursement tx not found.
REQ-ID: REIMB-013  link-reimbursement returns 422 when linking a tx to itself.
REQ-ID: REIMB-014  GET ?direction=reimbursable filters by direction.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection as _conn  # noqa: F401
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
# Shared-cache in-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:accounting_reimb_test?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_test_engine)

_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    """Truncate all tables before each test for isolation."""
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Return a TestClient with all route sessions redirected to the test DB."""
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import transactions as _tx_module

    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_ingest_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    description: str = "Test Vendor",
    amount: Decimal = Decimal("-50.00"),
    entity: str | None = Entity.SPARKRY.value,
    tax_category: str | None = TaxCategory.SUPPLIES.value,
    direction: str | None = Direction.EXPENSE.value,
    status: str = TransactionStatus.NEEDS_REVIEW.value,
    confidence: float = 0.5,
    date: str = "2025-06-15",
    source: str = Source.GMAIL_N8N.value,
    reimbursement_link: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=source,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        status=status,
        confidence=confidence,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.AUTO.value,
        reimbursement_link=reimbursement_link,
    )
    session.add(tx)
    session.commit()
    return tx


# ---------------------------------------------------------------------------
# POST /api/transactions/{id}/link-reimbursement
# ---------------------------------------------------------------------------


class TestLinkReimbursement:
    def test_link_sets_bidirectional_link(self, client: TestClient) -> None:
        """REQ-ID: REIMB-001 — both sides get reimbursement_link set."""
        with _TestSession() as s:
            expense = _make_tx(
                s,
                description="Cardinal Health Expense",
                amount=Decimal("-500.00"),
                direction=Direction.REIMBURSABLE.value,
            )
            income = _make_tx(
                s,
                description="Cardinal Health Reimbursement",
                amount=Decimal("500.00"),
                direction=Direction.INCOME.value,
            )
            expense_id = expense.id
            income_id = income.id

        resp = client.post(
            f"/api/transactions/{expense_id}/link-reimbursement",
            json={"reimbursement_id": income_id},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["expense"]["id"] == expense_id
        assert data["expense"]["reimbursement_link"] == income_id
        assert data["reimbursement"]["id"] == income_id
        assert data["reimbursement"]["reimbursement_link"] == expense_id

    def test_link_with_direction_expense(self, client: TestClient) -> None:
        """REQ-ID: REIMB-002 — direction=expense is also valid for the expense side."""
        with _TestSession() as s:
            expense = _make_tx(
                s,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-200.00"),
            )
            income = _make_tx(
                s,
                direction=Direction.INCOME.value,
                amount=Decimal("200.00"),
                description="Reimbursement Income",
            )
            expense_id = expense.id
            income_id = income.id

        resp = client.post(
            f"/api/transactions/{expense_id}/link-reimbursement",
            json={"reimbursement_id": income_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["expense"]["reimbursement_link"] == income_id

    def test_invalid_expense_direction_returns_422(self, client: TestClient) -> None:
        """REQ-ID: REIMB-002 — expense with direction=transfer must be rejected."""
        with _TestSession() as s:
            expense = _make_tx(s, direction=Direction.TRANSFER.value)
            income = _make_tx(
                s,
                direction=Direction.INCOME.value,
                description="Income",
            )
            expense_id = expense.id
            income_id = income.id

        resp = client.post(
            f"/api/transactions/{expense_id}/link-reimbursement",
            json={"reimbursement_id": income_id},
        )
        assert resp.status_code == 422
        assert "reimbursable" in resp.json()["detail"].lower() or "expense" in resp.json()["detail"].lower()

    def test_invalid_reimbursement_direction_returns_422(self, client: TestClient) -> None:
        """REQ-ID: REIMB-003 — reimbursement tx must have direction=income."""
        with _TestSession() as s:
            expense = _make_tx(s, direction=Direction.REIMBURSABLE.value)
            not_income = _make_tx(
                s,
                direction=Direction.EXPENSE.value,
                description="Another Expense",
            )
            expense_id = expense.id
            not_income_id = not_income.id

        resp = client.post(
            f"/api/transactions/{expense_id}/link-reimbursement",
            json={"reimbursement_id": not_income_id},
        )
        assert resp.status_code == 422
        assert "income" in resp.json()["detail"].lower()

    def test_same_direction_pair_rejected(self, client: TestClient) -> None:
        """REQ-ID: REIMB-004 — two income transactions cannot be linked."""
        with _TestSession() as s:
            income1 = _make_tx(
                s,
                direction=Direction.INCOME.value,
                amount=Decimal("100.00"),
                description="Income 1",
            )
            income2 = _make_tx(
                s,
                direction=Direction.INCOME.value,
                amount=Decimal("100.00"),
                description="Income 2",
            )
            id1 = income1.id
            id2 = income2.id

        # income1 as "expense" side must fail direction validation
        resp = client.post(
            f"/api/transactions/{id1}/link-reimbursement",
            json={"reimbursement_id": id2},
        )
        assert resp.status_code == 422

    def test_link_creates_audit_events(self, client: TestClient) -> None:
        """REQ-ID: REIMB-005 — AuditEvent rows created for both transactions."""
        with _TestSession() as s:
            expense = _make_tx(s, direction=Direction.REIMBURSABLE.value)
            income = _make_tx(
                s,
                direction=Direction.INCOME.value,
                amount=Decimal("50.00"),
                description="Income",
            )
            expense_id = expense.id
            income_id = income.id

        resp = client.post(
            f"/api/transactions/{expense_id}/link-reimbursement",
            json={"reimbursement_id": income_id},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            expense_events = (
                s.query(AuditEvent)
                .filter(
                    AuditEvent.transaction_id == expense_id,
                    AuditEvent.field_changed == "reimbursement_link",
                )
                .all()
            )
            income_events = (
                s.query(AuditEvent)
                .filter(
                    AuditEvent.transaction_id == income_id,
                    AuditEvent.field_changed == "reimbursement_link",
                )
                .all()
            )
            assert len(expense_events) == 1
            assert expense_events[0].new_value == income_id
            assert len(income_events) == 1
            assert income_events[0].new_value == expense_id

    def test_expense_not_found_returns_404(self, client: TestClient) -> None:
        """REQ-ID: REIMB-011 — 404 when expense tx missing."""
        resp = client.post(
            f"/api/transactions/{uuid.uuid4()}/link-reimbursement",
            json={"reimbursement_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404
        assert "expense" in resp.json()["detail"].lower()

    def test_reimbursement_not_found_returns_404(self, client: TestClient) -> None:
        """REQ-ID: REIMB-012 — 404 when reimbursement tx missing."""
        with _TestSession() as s:
            expense = _make_tx(s, direction=Direction.REIMBURSABLE.value)
            expense_id = expense.id

        resp = client.post(
            f"/api/transactions/{expense_id}/link-reimbursement",
            json={"reimbursement_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404
        assert "reimbursement" in resp.json()["detail"].lower()

    def test_self_link_returns_422(self, client: TestClient) -> None:
        """REQ-ID: REIMB-013 — A transaction cannot be linked to itself."""
        with _TestSession() as s:
            tx = _make_tx(s, direction=Direction.REIMBURSABLE.value)
            tx_id = tx.id

        resp = client.post(
            f"/api/transactions/{tx_id}/link-reimbursement",
            json={"reimbursement_id": tx_id},
        )
        assert resp.status_code == 422
        assert "itself" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/transactions?direction=reimbursable&overdue=true
# ---------------------------------------------------------------------------


class TestOverdueReimbursableFilter:
    def test_overdue_returns_old_unlinked_reimbursables(self, client: TestClient) -> None:
        """REQ-ID: REIMB-006 — unlinked reimbursable older than 30 days returned."""
        with _TestSession() as s:
            # 60 days ago — should appear
            _make_tx(
                s,
                description="Old Unreimbursed Expense",
                direction=Direction.REIMBURSABLE.value,
                date="2025-01-15",  # well over 30 days before 2026-03-16
            )

        resp = client.get("/api/transactions?direction=reimbursable&overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["description"] == "Old Unreimbursed Expense"

    def test_overdue_excludes_recent_reimbursables(self, client: TestClient) -> None:
        """REQ-ID: REIMB-007 — reimbursable within 30 days not returned."""
        with _TestSession() as s:
            # 10 days ago — should NOT appear
            _make_tx(
                s,
                description="Recent Expense",
                direction=Direction.REIMBURSABLE.value,
                date="2026-03-06",  # 10 days before 2026-03-16
            )

        resp = client.get("/api/transactions?direction=reimbursable&overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    def test_overdue_excludes_linked_reimbursables(self, client: TestClient) -> None:
        """REQ-ID: REIMB-008 — already-linked reimbursable not returned even if old."""
        with _TestSession() as s:
            income = _make_tx(
                s,
                description="Reimbursement Income",
                direction=Direction.INCOME.value,
                amount=Decimal("100.00"),
                date="2025-01-20",
            )
            # Old reimbursable that is already linked
            _make_tx(
                s,
                description="Already Reimbursed",
                direction=Direction.REIMBURSABLE.value,
                amount=Decimal("-100.00"),
                date="2025-01-15",
                reimbursement_link=income.id,
            )

        resp = client.get("/api/transactions?direction=reimbursable&overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    def test_direction_filter_without_overdue(self, client: TestClient) -> None:
        """REQ-ID: REIMB-014 — direction=reimbursable filters without the overdue flag."""
        with _TestSession() as s:
            _make_tx(s, direction=Direction.REIMBURSABLE.value, description="R1")
            _make_tx(s, direction=Direction.EXPENSE.value, description="E1")
            _make_tx(s, direction=Direction.INCOME.value, description="I1")

        resp = client.get("/api/transactions?direction=reimbursable")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["description"] == "R1"

    def test_overdue_mixed_set(self, client: TestClient) -> None:
        """Overdue filter only returns old, unlinked reimbursable items."""
        with _TestSession() as s:
            income = _make_tx(
                s,
                description="Existing Income",
                direction=Direction.INCOME.value,
                amount=Decimal("50.00"),
                date="2025-01-10",
            )
            # Old + already linked — excluded
            _make_tx(
                s,
                description="Already Linked Old",
                direction=Direction.REIMBURSABLE.value,
                date="2025-01-10",
                reimbursement_link=income.id,
            )
            # Old + unlinked — included
            _make_tx(
                s,
                description="Overdue Pending",
                direction=Direction.REIMBURSABLE.value,
                date="2025-01-10",
            )
            # Recent + unlinked — excluded
            _make_tx(
                s,
                description="Recent Unlinked",
                direction=Direction.REIMBURSABLE.value,
                date="2026-03-10",
            )
            # Non-reimbursable — excluded
            _make_tx(s, direction=Direction.EXPENSE.value, description="Just an Expense")

        resp = client.get("/api/transactions?direction=reimbursable&overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["description"] == "Overdue Pending"

    def test_invalid_direction_returns_422(self, client: TestClient) -> None:
        """Invalid direction value returns 422."""
        resp = client.get("/api/transactions?direction=invalid_dir")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Partner flagging on PATCH
# ---------------------------------------------------------------------------


class TestReimbursementPartnerFlagging:
    def test_editing_amount_flags_partner(self, client: TestClient) -> None:
        """REQ-ID: REIMB-009 — editing amount on a linked expense flags the partner."""
        with _TestSession() as s:
            income = _make_tx(
                s,
                description="Income Side",
                direction=Direction.INCOME.value,
                amount=Decimal("100.00"),
                status=TransactionStatus.CONFIRMED.value,
            )
            expense = _make_tx(
                s,
                description="Expense Side",
                direction=Direction.REIMBURSABLE.value,
                amount=Decimal("-100.00"),
                status=TransactionStatus.CONFIRMED.value,
                reimbursement_link=income.id,
            )
            # Set partner link on income too (bidirectional)
            income.reimbursement_link = expense.id
            s.commit()
            expense_id = expense.id
            income_id = income.id

        # Edit amount on the expense side
        resp = client.patch(
            f"/api/transactions/{expense_id}",
            json={"amount": -150.0},
        )
        assert resp.status_code == 200

        # Partner (income) should be flagged
        with _TestSession() as s:
            partner = s.query(Transaction).filter(Transaction.id == income_id).first()
            assert partner is not None
            assert partner.status == TransactionStatus.NEEDS_REVIEW.value
            assert partner.review_reason is not None
            assert "modified" in partner.review_reason.lower()

    def test_rejecting_linked_expense_flags_partner(self, client: TestClient) -> None:
        """REQ-ID: REIMB-010 — rejecting a linked expense flags the partner."""
        with _TestSession() as s:
            income = _make_tx(
                s,
                description="Income Partner",
                direction=Direction.INCOME.value,
                amount=Decimal("75.00"),
                status=TransactionStatus.CONFIRMED.value,
            )
            expense = _make_tx(
                s,
                description="Expense To Reject",
                direction=Direction.REIMBURSABLE.value,
                amount=Decimal("-75.00"),
                status=TransactionStatus.CONFIRMED.value,
                reimbursement_link=income.id,
            )
            income.reimbursement_link = expense.id
            s.commit()
            expense_id = expense.id
            income_id = income.id

        resp = client.patch(
            f"/api/transactions/{expense_id}",
            json={"status": "rejected"},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            partner = s.query(Transaction).filter(Transaction.id == income_id).first()
            assert partner is not None
            assert partner.status == TransactionStatus.NEEDS_REVIEW.value

    def test_unlinked_patch_does_not_flag_partner(self, client: TestClient) -> None:
        """Editing a transaction with no reimbursement_link never touches other rows."""
        with _TestSession() as s:
            standalone = _make_tx(
                s,
                description="Standalone",
                direction=Direction.EXPENSE.value,
                amount=Decimal("-20.00"),
                status=TransactionStatus.CONFIRMED.value,
            )
            other = _make_tx(
                s,
                description="Unrelated",
                direction=Direction.INCOME.value,
                amount=Decimal("20.00"),
                status=TransactionStatus.CONFIRMED.value,
            )
            standalone_id = standalone.id
            other_id = other.id

        client.patch(
            f"/api/transactions/{standalone_id}",
            json={"amount": -25.0},
        )

        with _TestSession() as s:
            other_tx = s.query(Transaction).filter(Transaction.id == other_id).first()
            assert other_tx is not None
            assert other_tx.status == TransactionStatus.CONFIRMED.value

    def test_partner_already_needs_review_not_double_flagged(
        self, client: TestClient
    ) -> None:
        """If partner is already needs_review, we don't overwrite its review_reason."""
        with _TestSession() as s:
            income = _make_tx(
                s,
                description="Already Flagged Income",
                direction=Direction.INCOME.value,
                amount=Decimal("60.00"),
                status=TransactionStatus.NEEDS_REVIEW.value,
            )
            income.review_reason = "Pre-existing review reason"
            s.commit()
            expense = _make_tx(
                s,
                description="Expense Side",
                direction=Direction.REIMBURSABLE.value,
                amount=Decimal("-60.00"),
                status=TransactionStatus.CONFIRMED.value,
                reimbursement_link=income.id,
            )
            income.reimbursement_link = expense.id
            s.commit()
            expense_id = expense.id
            income_id = income.id

        client.patch(
            f"/api/transactions/{expense_id}",
            json={"amount": -70.0},
        )

        with _TestSession() as s:
            partner = s.query(Transaction).filter(Transaction.id == income_id).first()
            assert partner is not None
            # Still needs_review, but reason should be the original (not overwritten)
            assert partner.status == TransactionStatus.NEEDS_REVIEW.value
            assert partner.review_reason == "Pre-existing review reason"

    def test_partner_flagging_creates_audit_event(self, client: TestClient) -> None:
        """Partner flagging writes AuditEvent rows for the partner transaction."""
        with _TestSession() as s:
            income = _make_tx(
                s,
                description="Income to Flag",
                direction=Direction.INCOME.value,
                amount=Decimal("30.00"),
                status=TransactionStatus.CONFIRMED.value,
            )
            expense = _make_tx(
                s,
                description="Expense That Changes",
                direction=Direction.REIMBURSABLE.value,
                amount=Decimal("-30.00"),
                status=TransactionStatus.CONFIRMED.value,
                reimbursement_link=income.id,
            )
            income.reimbursement_link = expense.id
            s.commit()
            expense_id = expense.id
            income_id = income.id

        client.patch(
            f"/api/transactions/{expense_id}",
            json={"amount": -35.0},
        )

        with _TestSession() as s:
            audit_events = (
                s.query(AuditEvent)
                .filter(
                    AuditEvent.transaction_id == income_id,
                    AuditEvent.field_changed == "status",
                )
                .all()
            )
            assert len(audit_events) >= 1
            status_event = audit_events[-1]
            assert status_event.new_value == TransactionStatus.NEEDS_REVIEW.value
