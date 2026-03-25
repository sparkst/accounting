"""Tests for global error handlers (S2-008, S2-009) and PATCH input validation (S2-006).

REQ-ID: S2-006  deductible_pct and amount have range validators on PATCH endpoints.
REQ-ID: S2-008  Unhandled exceptions return error_id without traceback.
REQ-ID: S2-009  RequestValidationError returns 422 with field-level errors; HTTPException stripped.
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
from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    Source,
    TaxCategory,
    TransactionStatus,
    VendorRuleSource,
)
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:error_handler_test?mode=memory&cache=shared&uri=true"

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
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import transactions as _tx_module
    from src.api.routes import vendor_rules as _vr_module

    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_vr_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_ingest_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx(
    *,
    amount: Decimal = Decimal("-50.00"),
    entity: str = Entity.SPARKRY.value,
    direction: str = Direction.EXPENSE.value,
    status: str = TransactionStatus.NEEDS_REVIEW.value,
) -> str:
    """Insert a Transaction and return its ID (session is closed before return)."""
    tx_id = str(uuid.uuid4())
    with _TestSession() as session:
        tx = Transaction(
            id=tx_id,
            source=Source.GMAIL_N8N.value,
            source_id=str(uuid.uuid4()),
            source_hash=str(uuid.uuid4()),
            date="2026-01-15",
            description="Test Vendor",
            amount=amount,
            currency="USD",
            entity=entity,
            direction=direction,
            tax_category=TaxCategory.ADVERTISING.value,
            deductible_pct=1.0,
            status=status,
            confidence=0.9,
            review_reason=None,
            parent_id=None,
            confirmed_by=ConfirmedBy.AUTO.value,
            raw_data="{}",
        )
        session.add(tx)
        session.commit()
    return tx_id


def _make_rule() -> str:
    """Insert a VendorRule and return its ID (session is closed before return)."""
    rule_id = str(uuid.uuid4())
    with _TestSession() as session:
        rule = VendorRule(
            id=rule_id,
            vendor_pattern="Test Vendor",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.ADVERTISING.value,
            direction=Direction.EXPENSE.value,
            deductible_pct=1.0,
            confidence=0.9,
            source=VendorRuleSource.HUMAN.value,
            examples=1,
            last_matched=None,
        )
        session.add(rule)
        session.commit()
    return rule_id


# ---------------------------------------------------------------------------
# S2-006 — TransactionPatch validation
# ---------------------------------------------------------------------------


class TestTransactionPatchValidation:
    """PATCH /api/transactions/{id} field range validation."""

    def test_deductible_pct_above_1_rejected(self, client: TestClient) -> None:
        tx_id = _make_tx()
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"deductible_pct": 1.5},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_deductible_pct_below_0_rejected(self, client: TestClient) -> None:
        tx_id = _make_tx()
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"deductible_pct": -0.1},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_deductible_pct_boundary_values_accepted(self, client: TestClient) -> None:
        tx_id = _make_tx()
        for value in (0.0, 0.5, 1.0):
            resp = client.patch(
                f"/api/transactions/{tx_id}",
                json={"deductible_pct": value},
                headers={"X-Api-Key": ""},
            )
            assert resp.status_code == 200, f"Expected 200 for deductible_pct={value}, got {resp.status_code}: {resp.text}"

    def test_amount_above_bound_rejected(self, client: TestClient) -> None:
        tx_id = _make_tx()
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"amount": 1_000_001.0},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_amount_below_bound_rejected(self, client: TestClient) -> None:
        tx_id = _make_tx()
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"amount": -1_000_001.0},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_amount_within_bounds_accepted(self, client: TestClient) -> None:
        tx_id = _make_tx()
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"amount": -999.99},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# S2-006 — VendorRulePatch validation
# ---------------------------------------------------------------------------


class TestVendorRulePatchValidation:
    """PATCH /api/vendor-rules/{id} field range validation."""

    def test_deductible_pct_above_1_rejected(self, client: TestClient) -> None:
        rule_id = _make_rule()
        resp = client.patch(
            f"/api/vendor-rules/{rule_id}",
            json={"deductible_pct": 2.0},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_confidence_above_1_rejected(self, client: TestClient) -> None:
        rule_id = _make_rule()
        resp = client.patch(
            f"/api/vendor-rules/{rule_id}",
            json={"confidence": 1.1},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_confidence_below_0_rejected(self, client: TestClient) -> None:
        rule_id = _make_rule()
        resp = client.patch(
            f"/api/vendor-rules/{rule_id}",
            json={"confidence": -0.5},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_valid_patch_accepted(self, client: TestClient) -> None:
        rule_id = _make_rule()
        resp = client.patch(
            f"/api/vendor-rules/{rule_id}",
            json={"confidence": 0.75, "deductible_pct": 0.5},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# S2-006 — VendorRuleCreate validation
# ---------------------------------------------------------------------------


class TestVendorRuleCreateValidation:
    """POST /api/vendor-rules field range validation."""

    def test_deductible_pct_above_1_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/vendor-rules",
            json={
                "vendor_pattern": "ACME",
                "entity": Entity.SPARKRY.value,
                "tax_category": TaxCategory.ADVERTISING.value,
                "direction": Direction.EXPENSE.value,
                "deductible_pct": 1.5,
            },
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text

    def test_confidence_above_1_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/vendor-rules",
            json={
                "vendor_pattern": "ACME",
                "entity": Entity.SPARKRY.value,
                "tax_category": TaxCategory.ADVERTISING.value,
                "direction": Direction.EXPENSE.value,
                "confidence": 1.5,
            },
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# S2-008 — Global 500 handler: error_id without traceback
# ---------------------------------------------------------------------------


class TestGlobalExceptionHandler:
    """Unhandled exceptions must return error_id without Python traceback."""

    def test_500_returns_error_id_not_traceback(self, client: TestClient) -> None:
        """Verify the global exception handler returns error_id without traceback.

        We test this by directly invoking the handler function (not via HTTP),
        because Starlette's ServerErrorMiddleware intercepts unhandled exceptions
        before FastAPI's @app.exception_handler(Exception) can respond.
        """
        import asyncio

        from src.api.main import _global_exception_handler

        class _FakeRequest:
            pass

        exc = RuntimeError("Simulated unexpected database failure")
        response = asyncio.run(
            _global_exception_handler(_FakeRequest(), exc)  # type: ignore[arg-type]
        )
        body = response.body
        import json

        data = json.loads(body)
        assert response.status_code == 500
        assert "error_id" in data, f"Missing error_id in: {data}"
        assert "error" in data
        assert "traceback" not in str(data).lower()
        assert "RuntimeError" not in str(data)


# ---------------------------------------------------------------------------
# S2-009 — RequestValidationError returns 422 with field-level errors
# ---------------------------------------------------------------------------


class TestValidationErrorHandler:
    """422 responses must include field-level detail without internal info."""

    def test_422_contains_field_detail(self, client: TestClient) -> None:
        """Send invalid JSON body to a PATCH endpoint and expect structured 422."""
        tx_id = _make_tx()

        # deductible_pct > 1 triggers Pydantic field validation → 422
        resp = client.patch(
            f"/api/transactions/{tx_id}",
            json={"deductible_pct": 999},
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 422
        body = resp.json()
        # Our handler wraps errors under "detail" key
        assert "detail" in body or "error" in body

    def test_404_does_not_leak_internals(self, client: TestClient) -> None:
        """HTTPException 404 must return JSON without Python traceback text."""
        resp = client.get(
            "/api/transactions/nonexistent-id-xyz",
            headers={"X-Api-Key": ""},
        )
        assert resp.status_code == 404
        # Must return valid JSON (not an HTML traceback page)
        body = resp.json()
        assert isinstance(body, dict)
        # The detail field must be present and contain no traceback text
        assert "detail" in body
        assert "traceback" not in resp.text.lower()
        assert "Traceback" not in resp.text
