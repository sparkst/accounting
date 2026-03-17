"""API integration tests.

Uses httpx TestClient (synchronous) with a shared-cache named in-memory SQLite
database so tests are fully isolated from the production data/accounting.db.

A shared-cache URI (``file:accounting_test?mode=memory&cache=shared``) is
used instead of ``sqlite:///:memory:`` so that all connections — including
those opened in FastAPI's thread-pool workers — share the same database
instance and therefore see the same tables and rows.

REQ-ID: API-TEST-001  GET /api/transactions returns paginated list.
REQ-ID: API-TEST-002  GET /api/transactions supports entity/status/date/search filters.
REQ-ID: API-TEST-003  GET /api/transactions/review returns only needs_review items.
REQ-ID: API-TEST-004  GET /api/transactions/{id} returns single transaction or 404.
REQ-ID: API-TEST-005  PATCH /api/transactions/{id} updates fields and creates AuditEvent.
REQ-ID: API-TEST-006  PATCH confirms transaction, sets confirmed_by=human, triggers learning loop.
REQ-ID: API-TEST-007  POST /api/ingest/run triggers Gmail adapter and returns summary.
REQ-ID: API-TEST-008  GET /api/health returns source freshness and classification stats.
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

# Import connection before any other src.models imports — it registers all ORM
# model classes on Base.metadata as a side-effect of its noqa import block,
# ensuring create_all() below sees every table.
import src.db.connection as _conn  # noqa: F401
from src.adapters.base import AdapterResult
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    IngestionStatus,
    Source,
    TaxCategory,
    TransactionStatus,
    VendorRuleSource,
)
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

# ---------------------------------------------------------------------------
# Shared-cache in-memory database
# ---------------------------------------------------------------------------
# ``file:accounting_test?mode=memory&cache=shared`` creates a named shared
# in-memory SQLite database.  Every connection that uses this URI string sees
# the SAME database, so FastAPI thread-pool workers share tables and rows with
# the test setup code running in the main thread.
#
# ``uri=True`` tells SQLAlchemy to pass the string verbatim to sqlite3 as a
# URI rather than treating it as a file path.
# ``check_same_thread=False`` silences the cross-thread usage check that
# SQLite raises without this flag.

_TEST_DB_URI = "file:accounting_test?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Create all tables once at import time.  All model classes are registered on
# Base.metadata because src.db.connection was imported above.
Base.metadata.create_all(bind=_test_engine)

_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    """Truncate all tables before each test for isolation."""
    # Truncate before (not after) so the DB is clean at the start of every test
    # even if a previous test failed mid-cleanup.
    with _test_engine.begin() as conn:
        # Disable FK constraints during truncation to avoid ordering issues.
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


# ---------------------------------------------------------------------------
# App client fixture — wires the test engine into the API routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Return a TestClient with all route sessions redirected to the test DB."""
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import transactions as _tx_module

    # Patch the SessionLocal used by each route module so every new session
    # opens a connection to the shared test database.
    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_ingest_module, "SessionLocal", _TestSession),
        # Prevent lifespan from running init_db / seed_rules against the real DB.
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Helper: build a Transaction and persist it
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
    review_reason: str | None = None,
    date: str = "2025-06-15",
    source: str = Source.GMAIL_N8N.value,
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
        review_reason=review_reason,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.AUTO.value,
    )
    session.add(tx)
    session.commit()
    return tx


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "classification_stats" in data
        assert "total_transactions" in data
        assert "source_freshness" in data

    def test_health_counts_transactions(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, status=TransactionStatus.CONFIRMED.value)
            _make_tx(s, status=TransactionStatus.NEEDS_REVIEW.value)

        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_transactions"] == 2
        assert data["classification_stats"]["confirmed"] == 1
        assert data["classification_stats"]["needs_review"] == 1

    def test_health_shows_ingestion_log(self, client: TestClient) -> None:
        with _TestSession() as s:
            log = IngestionLog(
                source=Source.GMAIL_N8N.value,
                status=IngestionStatus.SUCCESS.value,
                records_processed=5,
                records_failed=0,
            )
            s.add(log)
            s.commit()

        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        freshness = data["source_freshness"]
        # All known sources are returned (including those with no log entries).
        gmail_entry = next(
            (f for f in freshness if f["source"] == "gmail_n8n"), None
        )
        assert gmail_entry is not None
        assert gmail_entry["records_processed"] == 5
        assert gmail_entry["freshness_status"] in ("green", "amber", "red")

    def test_health_includes_tax_deadlines_and_failure_log(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "tax_deadlines" in data
        assert "failure_log" in data
        assert isinstance(data["tax_deadlines"], list)
        assert isinstance(data["failure_log"], list)
        assert "needs_review_count" in data

    def test_health_failure_log_contains_failures(self, client: TestClient) -> None:
        with _TestSession() as s:
            fail_log = IngestionLog(
                source=Source.SHOPIFY.value,
                status=IngestionStatus.FAILURE.value,
                records_processed=0,
                records_failed=3,
                error_detail="Connection timeout",
            )
            s.add(fail_log)
            s.commit()

        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        failure_log = data["failure_log"]
        assert len(failure_log) == 1
        assert failure_log[0]["source"] == "shopify"
        assert failure_log[0]["error_detail"] == "Connection timeout"


# ---------------------------------------------------------------------------
# GET /api/transactions
# ---------------------------------------------------------------------------


class TestListTransactions:
    def test_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/transactions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_returns_transactions(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s)
            _make_tx(s, description="Another Vendor")

        resp = client.get("/api/transactions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_filter_by_entity(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, entity=Entity.SPARKRY.value)
            _make_tx(s, entity=Entity.BLACKLINE.value, description="BL Vendor")

        resp = client.get("/api/transactions?entity=sparkry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["entity"] == "sparkry"

    def test_filter_by_status(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, status=TransactionStatus.CONFIRMED.value)
            _make_tx(s, status=TransactionStatus.NEEDS_REVIEW.value, description="B")

        resp = client.get("/api/transactions?status=confirmed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "confirmed"

    def test_filter_by_date_range(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, date="2025-01-01")
            _make_tx(s, date="2025-06-15", description="B")
            _make_tx(s, date="2025-12-31", description="C")

        resp = client.get("/api/transactions?date_from=2025-06-01&date_to=2025-12-01")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["date"] == "2025-06-15"

    def test_search_description(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, description="Anthropic, PBC")
            _make_tx(s, description="AWS Invoice")

        resp = client.get("/api/transactions?search=anthropic")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert "Anthropic" in data["items"][0]["description"]

    def test_pagination(self, client: TestClient) -> None:
        with _TestSession() as s:
            for i in range(5):
                _make_tx(s, description=f"Vendor {i}")

        resp = client.get("/api/transactions?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5
        assert data["limit"] == 2

        resp2 = client.get("/api/transactions?limit=2&offset=4")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["items"]) == 1  # only 1 item at offset 4

    def test_invalid_entity_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/transactions?entity=invalid_entity")
        assert resp.status_code == 422

    def test_sort_by_date_asc(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, date="2025-12-01", description="Late")
            _make_tx(s, date="2025-01-01", description="Early")

        resp = client.get("/api/transactions?sort_by=date&sort_dir=asc")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["date"] == "2025-01-01"
        assert items[1]["date"] == "2025-12-01"


# ---------------------------------------------------------------------------
# GET /api/transactions/review
# ---------------------------------------------------------------------------


class TestReviewTransactions:
    def test_returns_only_needs_review(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, status=TransactionStatus.CONFIRMED.value)
            _make_tx(
                s,
                status=TransactionStatus.NEEDS_REVIEW.value,
                description="Review Me",
            )

        resp = client.get("/api/transactions/review")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["status"] == "needs_review"

    def test_priority_order(self, client: TestClient) -> None:
        """Amount-extraction failures sort before low-confidence items."""
        with _TestSession() as s:
            _make_tx(
                s,
                description="Low Conf",
                status=TransactionStatus.NEEDS_REVIEW.value,
                confidence=0.3,
                review_reason="Some other reason",
            )
            _make_tx(
                s,
                description="Amount Failure",
                status=TransactionStatus.NEEDS_REVIEW.value,
                confidence=0.0,
                review_reason="Amount could not be extracted from body_text",
            )

        resp = client.get("/api/transactions/review")
        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["description"] == "Amount Failure"
        assert items[1]["description"] == "Low Conf"

    def test_empty_when_none_pending(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, status=TransactionStatus.CONFIRMED.value)

        resp = client.get("/api/transactions/review")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/transactions/{id}
# ---------------------------------------------------------------------------


class TestGetTransaction:
    def test_returns_transaction(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(s, description="Specific Vendor")
            tx_id = tx.id

        resp = client.get(f"/api/transactions/{tx_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == tx_id
        assert data["description"] == "Specific Vendor"

    def test_404_for_missing(self, client: TestClient) -> None:
        resp = client.get(f"/api/transactions/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/transactions/{id}
# ---------------------------------------------------------------------------


class TestPatchTransaction:
    def test_update_entity_and_category(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(s, entity=None, tax_category=None)
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={
                "entity": "blackline",
                "tax_category": "SUPPLIES",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["entity"] == "blackline"
        assert data["tax_category"] == "SUPPLIES"

    def test_creates_audit_events_on_change(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(s, entity=Entity.SPARKRY.value)
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"entity": "blackline"},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            events = (
                s.query(AuditEvent)
                .filter(AuditEvent.transaction_id == tx_id)
                .all()
            )
            assert len(events) >= 1
            entity_events = [e for e in events if e.field_changed == "entity"]
            assert len(entity_events) == 1
            assert entity_events[0].old_value == "sparkry"
            assert entity_events[0].new_value == "blackline"
            assert entity_events[0].changed_by == "human"

    def test_no_audit_event_when_nothing_changes(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(s, entity=Entity.SPARKRY.value)
            tx_id = tx.id

        # Patching with the same value should not create an audit event.
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"entity": "sparkry"},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            events = (
                s.query(AuditEvent)
                .filter(AuditEvent.transaction_id == tx_id)
                .all()
            )
            assert len(events) == 0

    def test_confirm_sets_confirmed_by_human(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(
                s,
                status=TransactionStatus.NEEDS_REVIEW.value,
            )
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"status": "confirmed"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"
        assert data["confirmed_by"] == "human"

    def test_confirm_creates_vendor_rule_learning_loop(self, client: TestClient) -> None:
        """Confirming a fully-classified transaction creates a VendorRule."""
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="NewVendor Inc",
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.OFFICE_EXPENSE.value,
                direction=Direction.EXPENSE.value,
                status=TransactionStatus.NEEDS_REVIEW.value,
            )
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"status": "confirmed"},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            rule = (
                s.query(VendorRule)
                .filter(
                    VendorRule.vendor_pattern == "NewVendor Inc",
                    VendorRule.entity == Entity.SPARKRY.value,
                )
                .first()
            )
            assert rule is not None
            assert rule.source == VendorRuleSource.LEARNED.value
            assert rule.tax_category == TaxCategory.OFFICE_EXPENSE.value
            assert rule.examples == 1

    def test_confirm_increments_existing_vendor_rule(self, client: TestClient) -> None:
        """Confirming a second transaction for the same vendor increments examples."""
        with _TestSession() as s:
            # Pre-existing learned rule.
            rule = VendorRule(
                vendor_pattern="RepeatVendor",
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.SUPPLIES.value,
                direction=Direction.EXPENSE.value,
                confidence=0.80,
                source=VendorRuleSource.LEARNED.value,
                examples=3,
            )
            s.add(rule)
            s.commit()

            tx = _make_tx(
                s,
                description="RepeatVendor",
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.SUPPLIES.value,
                direction=Direction.EXPENSE.value,
                status=TransactionStatus.NEEDS_REVIEW.value,
            )
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"status": "confirmed"},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            updated = (
                s.query(VendorRule)
                .filter(
                    VendorRule.vendor_pattern == "RepeatVendor",
                    VendorRule.entity == Entity.SPARKRY.value,
                )
                .first()
            )
            assert updated is not None
            assert updated.examples == 4

    def test_confirm_without_entity_does_not_create_rule(self, client: TestClient) -> None:
        """If entity is missing, the learning loop must not create a broken rule."""
        with _TestSession() as s:
            tx = _make_tx(
                s,
                description="UnknownVendor",
                entity=None,
                tax_category=None,
                direction=None,
                status=TransactionStatus.NEEDS_REVIEW.value,
            )
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"status": "confirmed"},
        )
        assert resp.status_code == 200

        with _TestSession() as s:
            count = (
                s.query(VendorRule)
                .filter(VendorRule.vendor_pattern == "UnknownVendor")
                .count()
            )
            assert count == 0

    def test_patch_invalid_entity_returns_422(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(s)
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"entity": "not_a_real_entity"},
        )
        assert resp.status_code == 422

    def test_patch_404_for_missing(self, client: TestClient) -> None:
        resp = client.patch(
            f"/api/transactions/{uuid.uuid4()}",
            json={"status": "confirmed"},
        )
        assert resp.status_code == 404

    def test_patch_notes(self, client: TestClient) -> None:
        with _TestSession() as s:
            tx = _make_tx(s)
            tx_id = tx.id

        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"notes": "This is a test note."},
        )
        assert resp.status_code == 200
        assert resp.json()["notes"] == "This is a test note."


# ---------------------------------------------------------------------------
# POST /api/ingest/run
# ---------------------------------------------------------------------------


class TestIngestRun:
    def test_ingest_run_returns_summary(self, client: TestClient) -> None:
        """POST /api/ingest/run runs the adapter and returns a summary dict."""
        mock_result = AdapterResult(
            source=Source.GMAIL_N8N.value,
            status=IngestionStatus.SUCCESS,
            records_processed=3,
            records_created=2,
            records_skipped=1,
            records_failed=0,
        )

        with (
            patch(
                "src.api.routes.ingest.GmailN8nAdapter.run",
                return_value=mock_result,
            ),
            patch(
                "src.api.routes.ingest.classify",
                side_effect=Exception("LLM unavailable"),
            ),
        ):
            resp = client.post("/api/ingest/run")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ingested_count"] == 2
        assert "needs_review_count" in data
        assert "classified_count" in data
        assert isinstance(data["errors"], list)

    def test_ingest_run_adapter_error_captured(self, client: TestClient) -> None:
        """Adapter crash surfaces in errors, not as HTTP 500."""
        with patch(
            "src.api.routes.ingest.GmailN8nAdapter.run",
            side_effect=RuntimeError("Connection refused"),
        ):
            resp = client.post("/api/ingest/run")

        assert resp.status_code == 200
        data = resp.json()
        assert any("gmail_n8n" in e for e in data["errors"])

    def test_ingest_run_classifies_pending(self, client: TestClient) -> None:
        """If transactions already in DB are needs_review, classify is called."""
        from src.classification.engine import ClassificationResult

        with _TestSession() as s:
            _make_tx(
                s,
                status=TransactionStatus.NEEDS_REVIEW.value,
                entity=None,
                tax_category=None,
                direction=None,
                confidence=0.0,
            )

        mock_result = AdapterResult(
            source=Source.GMAIL_N8N.value,
            status=IngestionStatus.SUCCESS,
            records_processed=0,
            records_created=0,
        )

        classify_result = ClassificationResult(
            entity=Entity.SPARKRY,
            tax_category=TaxCategory.SUPPLIES,
            direction=Direction.EXPENSE,
            confidence=0.9,
            tier_used=1,
            reasoning="Test rule match",
        )

        with (
            patch(
                "src.api.routes.ingest.GmailN8nAdapter.run",
                return_value=mock_result,
            ),
            patch(
                "src.api.routes.ingest.classify",
                return_value=classify_result,
            ),
        ):
            resp = client.post("/api/ingest/run")

        assert resp.status_code == 200
        data = resp.json()
        assert data["classified_count"] >= 1
