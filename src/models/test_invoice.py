"""Tests for Customer, Invoice, and InvoiceLineItem ORM models.

REQ-INV-007: Invoice status tracking and state machine enforcement.
REQ-INV-008: Payment reconciliation via payment_transaction_id FK.
REQ-INV-009: Invoice number uniqueness.
REQ-INV-010: Customer management with billing model and config fields.
"""

import decimal
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.models.base import Base
from src.models.enums import (
    INVOICE_STATUS_TRANSITIONS,
    BillingModel,
    Entity,
    InvoiceStatus,
)
from src.models.invoice import Customer, Invoice, InvoiceLineItem

# Import transaction model so its table is registered on Base.metadata —
# needed because Invoice.payment_transaction_id FK references transactions.id.
from src.models.transaction import Transaction  # noqa: F401

_TEST_ENGINE = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}
)
Base.metadata.create_all(_TEST_ENGINE)
_SessionCls = sessionmaker(bind=_TEST_ENGINE)


@pytest.fixture(scope="module")
def session() -> Generator[Session, None, None]:
    """In-memory SQLite session for invoice model tests."""
    s = _SessionCls()
    yield s
    s.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_customer(suffix: str = "") -> Customer:
    return Customer(
        name=f"How To Fascinate{suffix}",
        contact_name="Ben",
        contact_email=f"ben{suffix}@fascinate.com",
        billing_model=BillingModel.HOURLY.value,
        default_rate=decimal.Decimal("100.00"),
        payment_terms="Net 14",
        invoice_prefix="",
        late_fee_pct=0.10,
        calendar_patterns=["Ben / Travis", "Fascinate OS", "Fascinate"],
        calendar_exclusions=["Book with Ben"],
        address={"street": "123 Main St", "city": "Seattle", "state": "WA"},
    )


def _make_invoice(customer_id: str, number: str = "202601-001") -> Invoice:
    return Invoice(
        invoice_number=number,
        customer_id=customer_id,
        entity=Entity.SPARKRY.value,
        project="Fascinate OS",
        submitted_date="2026-01-31",
        due_date="2026-02-14",
        service_period_start="2026-01-01",
        service_period_end="2026-01-31",
        subtotal=decimal.Decimal("500.00"),
        adjustments=decimal.Decimal("0.00"),
        tax=decimal.Decimal("0.00"),
        total=decimal.Decimal("500.00"),
        status=InvoiceStatus.DRAFT.value,
        payment_terms="Net 14",
        late_fee_pct=0.10,
    )


# ── InvoiceStatus enum ────────────────────────────────────────────────────────


class TestInvoiceStatusEnum:
    def test_all_values(self) -> None:
        assert {s.value for s in InvoiceStatus} == {
            "draft", "sent", "paid", "overdue", "void"
        }

    def test_str_subclass(self) -> None:
        assert InvoiceStatus.DRAFT.value == "draft"
        assert InvoiceStatus.VOID.value == "void"


class TestInvoiceStatusTransitions:
    def test_draft_can_go_to_sent_or_void(self) -> None:
        allowed = INVOICE_STATUS_TRANSITIONS[InvoiceStatus.DRAFT]
        assert InvoiceStatus.SENT in allowed
        assert InvoiceStatus.VOID in allowed
        assert InvoiceStatus.PAID not in allowed

    def test_sent_can_go_to_paid_void_overdue(self) -> None:
        allowed = INVOICE_STATUS_TRANSITIONS[InvoiceStatus.SENT]
        assert InvoiceStatus.PAID in allowed
        assert InvoiceStatus.VOID in allowed
        assert InvoiceStatus.OVERDUE in allowed
        assert InvoiceStatus.DRAFT not in allowed

    def test_paid_can_only_go_to_void(self) -> None:
        allowed = INVOICE_STATUS_TRANSITIONS[InvoiceStatus.PAID]
        assert allowed == {InvoiceStatus.VOID}

    def test_overdue_can_go_to_paid_or_void(self) -> None:
        allowed = INVOICE_STATUS_TRANSITIONS[InvoiceStatus.OVERDUE]
        assert InvoiceStatus.PAID in allowed
        assert InvoiceStatus.VOID in allowed

    def test_void_is_terminal(self) -> None:
        assert INVOICE_STATUS_TRANSITIONS[InvoiceStatus.VOID] == set()

    def test_all_statuses_have_transition_entry(self) -> None:
        for status in InvoiceStatus:
            assert status in INVOICE_STATUS_TRANSITIONS, (
                f"{status} missing from INVOICE_STATUS_TRANSITIONS"
            )


# ── BillingModel enum ─────────────────────────────────────────────────────────


class TestBillingModelEnum:
    def test_all_values(self) -> None:
        assert {b.value for b in BillingModel} == {"hourly", "flat_rate", "project"}


# ── Customer model ────────────────────────────────────────────────────────────


class TestCustomerModel:
    def test_persist_and_retrieve(self, session: Session) -> None:
        customer = _make_customer()
        session.add(customer)
        session.commit()

        fetched = session.get(Customer, customer.id)
        assert fetched is not None
        assert fetched.name == "How To Fascinate"
        assert fetched.contact_name == "Ben"
        assert fetched.billing_model == BillingModel.HOURLY.value
        assert fetched.default_rate == decimal.Decimal("100.00")
        assert fetched.payment_terms == "Net 14"
        assert fetched.late_fee_pct == 0.10
        assert fetched.active is True

    def test_json_fields_roundtrip(self, session: Session) -> None:
        customer = _make_customer(" JSON")
        session.add(customer)
        session.commit()

        fetched = session.get(Customer, customer.id)
        assert fetched is not None
        assert fetched.calendar_patterns == ["Ben / Travis", "Fascinate OS", "Fascinate"]
        assert fetched.calendar_exclusions == ["Book with Ben"]
        assert fetched.address == {
            "street": "123 Main St", "city": "Seattle", "state": "WA"
        }

    def test_cardinal_health_customer(self, session: Session) -> None:
        ch = Customer(
            name="Cardinal Health, Inc.",
            contact_name="Charelle Lewis",
            contact_email="charelle.lewis@cardinalhealth.com",
            billing_model=BillingModel.FLAT_RATE.value,
            default_rate=decimal.Decimal("33000.00"),
            payment_terms="Net 90",
            invoice_prefix="CH",
            po_number="4700158965",
            sap_config={
                "login_url": "https://supplier.ariba.com",
                "po_number": "4700158965",
                "classification": "111811-L3",
            },
        )
        session.add(ch)
        session.commit()

        fetched = session.get(Customer, ch.id)
        assert fetched is not None
        assert fetched.invoice_prefix == "CH"
        assert fetched.po_number == "4700158965"
        assert fetched.sap_config is not None
        assert fetched.sap_config["po_number"] == "4700158965"

    def test_contract_start_date_field(self, session: Session) -> None:
        customer = _make_customer(" ContractDate")
        customer.contract_start_date = "2025-08-01"
        customer.last_invoiced_date = "2026-01-31"
        session.add(customer)
        session.commit()

        fetched = session.get(Customer, customer.id)
        assert fetched is not None
        assert fetched.contract_start_date == "2025-08-01"
        assert fetched.last_invoiced_date == "2026-01-31"

    def test_defaults(self, session: Session) -> None:
        customer = Customer(
            name="Minimal Customer",
            contact_name="Anon",
            billing_model=BillingModel.PROJECT.value,
        )
        session.add(customer)
        session.commit()

        fetched = session.get(Customer, customer.id)
        assert fetched is not None
        assert fetched.active is True
        assert fetched.late_fee_pct == 0.0
        assert fetched.invoice_prefix == ""

    def test_repr(self) -> None:
        c = _make_customer()
        assert "Customer" in repr(c)
        assert "How To Fascinate" in repr(c)


# ── Invoice model ─────────────────────────────────────────────────────────────


class TestInvoiceModel:
    def _setup_customer(self, session: Session, suffix: str = "") -> Customer:
        customer = _make_customer(f" InvTest{suffix}")
        session.add(customer)
        session.flush()
        return customer

    def test_persist_and_retrieve(self, session: Session) -> None:
        customer = self._setup_customer(session, "A")
        inv = _make_invoice(customer.id, "202601-001")
        session.add(inv)
        session.commit()

        fetched = session.get(Invoice, inv.id)
        assert fetched is not None
        assert fetched.invoice_number == "202601-001"
        assert fetched.customer_id == customer.id
        assert fetched.entity == Entity.SPARKRY.value
        assert fetched.project == "Fascinate OS"
        assert fetched.status == InvoiceStatus.DRAFT.value
        assert fetched.total == decimal.Decimal("500.00")
        assert fetched.paid_date is None
        assert fetched.payment_transaction_id is None

    def test_invoice_number_unique_constraint(self, session: Session) -> None:
        customer = self._setup_customer(session, "B")
        inv1 = _make_invoice(customer.id, "UNIQUE-001")
        session.add(inv1)
        session.commit()

        inv2 = _make_invoice(customer.id, "UNIQUE-001")
        session.add(inv2)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_payment_transaction_id_nullable_fk(self, session: Session) -> None:
        """payment_transaction_id is nullable and links to transactions.id."""
        customer = self._setup_customer(session, "C")
        # Create a real transaction to link to.
        tx = Transaction(
            source="stripe",
            source_hash="pay_link_hash_" + "x" * 50,
            date="2026-02-10",
            description="Cardinal Health ACH Payment",
            amount=decimal.Decimal("500.00"),
            raw_data={"note": "test"},
        )
        session.add(tx)
        session.flush()

        inv = _make_invoice(customer.id, "202601-PAY-001")
        inv.payment_transaction_id = tx.id
        inv.status = InvoiceStatus.PAID.value
        inv.paid_date = "2026-02-10"
        session.add(inv)
        session.commit()

        fetched = session.get(Invoice, inv.id)
        assert fetched is not None
        assert fetched.payment_transaction_id == tx.id
        assert fetched.paid_date == "2026-02-10"

    def test_sap_checklist_state_json(self, session: Session) -> None:
        customer = self._setup_customer(session, "D")
        inv = _make_invoice(customer.id, "CH20260131")
        inv.sap_checklist_state = {
            "step_1_logged_in": True,
            "step_2_order_opened": True,
            "step_3_invoice_copied": False,
            "step_4_dates_updated": False,
            "step_5_description_updated": False,
            "step_6_invoice_number_entered": False,
            "step_7_amount_verified": False,
            "step_8_submitted": False,
        }
        session.add(inv)
        session.commit()

        fetched = session.get(Invoice, inv.id)
        assert fetched is not None
        assert fetched.sap_checklist_state is not None
        assert fetched.sap_checklist_state["step_1_logged_in"] is True
        assert fetched.sap_checklist_state["step_3_invoice_copied"] is False

    def test_sap_instructions_json(self, session: Session) -> None:
        customer = self._setup_customer(session, "E")
        inv = _make_invoice(customer.id, "CH20260228")
        inv.sap_instructions = {
            "po_number": "4700158965",
            "steps": ["Log in", "Open order", "Copy invoice"],
        }
        session.add(inv)
        session.commit()

        fetched = session.get(Invoice, inv.id)
        assert fetched is not None
        assert fetched.sap_instructions is not None
        assert fetched.sap_instructions["po_number"] == "4700158965"

    def test_all_date_fields(self, session: Session) -> None:
        customer = self._setup_customer(session, "F")
        inv = _make_invoice(customer.id, "202601-DATE")
        session.add(inv)
        session.commit()

        fetched = session.get(Invoice, inv.id)
        assert fetched is not None
        assert fetched.submitted_date == "2026-01-31"
        assert fetched.due_date == "2026-02-14"
        assert fetched.service_period_start == "2026-01-01"
        assert fetched.service_period_end == "2026-01-31"

    def test_audit_timestamps_auto_populated(self, session: Session) -> None:
        from datetime import datetime

        customer = self._setup_customer(session, "G")
        inv = _make_invoice(customer.id, "202601-TS")
        session.add(inv)
        session.commit()

        fetched = session.get(Invoice, inv.id)
        assert fetched is not None
        assert isinstance(fetched.created_at, datetime)
        assert isinstance(fetched.updated_at, datetime)

    def test_repr(self, session: Session) -> None:
        customer = self._setup_customer(session, "H")
        inv = _make_invoice(customer.id, "202601-REPR")
        assert "Invoice" in repr(inv)
        assert "202601-REPR" in repr(inv)


# ── InvoiceLineItem model ─────────────────────────────────────────────────────


class TestInvoiceLineItemModel:
    def _setup_invoice(self, session: Session, number: str) -> Invoice:
        customer = _make_customer(f" LI-{number}")
        session.add(customer)
        session.flush()
        inv = _make_invoice(customer.id, number)
        session.add(inv)
        session.flush()
        return inv

    def test_persist_and_retrieve(self, session: Session) -> None:
        inv = self._setup_invoice(session, "202601-LI-001")

        item = InvoiceLineItem(
            invoice_id=inv.id,
            description="Jan 5 — Fascinate OS sync",
            quantity=decimal.Decimal("1.0"),
            unit_price=decimal.Decimal("100.00"),
            total_price=decimal.Decimal("100.00"),
            date="2026-01-05",
            sort_order=1,
        )
        session.add(item)
        session.commit()

        fetched = session.get(InvoiceLineItem, item.id)
        assert fetched is not None
        assert fetched.invoice_id == inv.id
        assert fetched.description == "Jan 5 — Fascinate OS sync"
        assert fetched.quantity == decimal.Decimal("1.0")
        assert fetched.unit_price == decimal.Decimal("100.00")
        assert fetched.total_price == decimal.Decimal("100.00")
        assert fetched.date == "2026-01-05"
        assert fetched.sort_order == 1

    def test_multiple_line_items(self, session: Session) -> None:
        inv = self._setup_invoice(session, "202601-LI-002")

        items = [
            InvoiceLineItem(
                invoice_id=inv.id,
                description=f"Jan {day} session",
                quantity=decimal.Decimal("1.0"),
                unit_price=decimal.Decimal("100.00"),
                total_price=decimal.Decimal("100.00"),
                date=f"2026-01-{day:02d}",
                sort_order=i + 1,
            )
            for i, day in enumerate([5, 12, 19, 26])
        ]
        session.add_all(items)
        session.commit()

        stored = (
            session.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == inv.id)
            .order_by(InvoiceLineItem.sort_order)
            .all()
        )
        assert len(stored) == 4
        assert stored[0].date == "2026-01-05"
        assert stored[3].date == "2026-01-26"

    def test_flat_rate_line_item(self, session: Session) -> None:
        inv = self._setup_invoice(session, "CH20260131-LI")

        item = InvoiceLineItem(
            invoice_id=inv.id,
            description="AI Product Engineering Coaching Month 3",
            quantity=decimal.Decimal("1.0"),
            unit_price=decimal.Decimal("33000.00"),
            total_price=decimal.Decimal("33000.00"),
            sort_order=1,
        )
        session.add(item)
        session.commit()

        fetched = session.get(InvoiceLineItem, item.id)
        assert fetched is not None
        assert fetched.total_price == decimal.Decimal("33000.00")
        assert fetched.date is None  # flat-rate items have no session date

    def test_repr(self) -> None:
        item = InvoiceLineItem(
            invoice_id="x" * 36,
            description="Test item",
            quantity=decimal.Decimal("1.0"),
            unit_price=decimal.Decimal("100.00"),
            total_price=decimal.Decimal("100.00"),
            sort_order=1,
        )
        assert "InvoiceLineItem" in repr(item)


# ── Schema introspection ──────────────────────────────────────────────────────


class TestInvoiceSchemaIntrospection:
    def test_all_invoice_tables_present(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        tables = set(insp.get_table_names())
        expected = {"customers", "invoices", "invoice_line_items"}
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_invoices_columns(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        cols = {c["name"] for c in insp.get_columns("invoices")}
        required = {
            "id", "invoice_number", "customer_id", "entity", "project",
            "submitted_date", "due_date", "service_period_start",
            "service_period_end", "subtotal", "adjustments", "tax", "total",
            "status", "paid_date", "notes", "late_fee_pct", "payment_terms",
            "payment_method", "po_number", "sap_instructions",
            "sap_checklist_state", "pdf_path", "payment_transaction_id",
            "created_at", "updated_at",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_customers_columns(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        cols = {c["name"] for c in insp.get_columns("customers")}
        required = {
            "id", "name", "contact_name", "contact_email", "billing_model",
            "default_rate", "payment_terms", "invoice_prefix", "late_fee_pct",
            "po_number", "sap_config", "calendar_patterns", "calendar_exclusions",
            "address", "notes", "active", "created_at",
            "contract_start_date", "last_invoiced_date",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_invoice_line_items_columns(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        cols = {c["name"] for c in insp.get_columns("invoice_line_items")}
        required = {
            "id", "invoice_id", "description", "quantity", "unit_price",
            "total_price", "date", "sort_order",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_invoice_number_unique_index_exists(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        unique_constraints = insp.get_unique_constraints("invoices")
        unique_cols = [
            col
            for uc in unique_constraints
            for col in uc["column_names"]
        ]
        # invoice_number must be covered by a unique constraint or unique index
        indexes = insp.get_indexes("invoices")
        unique_index_cols = [
            col
            for idx in indexes
            if idx.get("unique")
            for col in idx["column_names"]
        ]
        assert "invoice_number" in unique_cols or "invoice_number" in unique_index_cols
