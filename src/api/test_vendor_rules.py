"""Vendor rules API integration tests.

Uses the same shared-cache in-memory SQLite pattern as test_api.py.

REQ-ID: VR-TEST-001  GET /api/vendor-rules returns paginated list.
REQ-ID: VR-TEST-002  GET /api/vendor-rules?search= filters by vendor_pattern.
REQ-ID: VR-TEST-003  GET /api/vendor-rules?entity= filters by entity.
REQ-ID: VR-TEST-004  GET /api/vendor-rules/{id} returns rule with match_count and last_matches.
REQ-ID: VR-TEST-005  POST /api/vendor-rules creates a new rule (201).
REQ-ID: VR-TEST-006  POST /api/vendor-rules with invalid enum returns 422.
REQ-ID: VR-TEST-007  PATCH /api/vendor-rules/{id} updates specified fields.
REQ-ID: VR-TEST-008  PATCH /api/vendor-rules/{id} with unknown id returns 404.
REQ-ID: VR-TEST-009  DELETE /api/vendor-rules/{id} removes the rule (204).
REQ-ID: VR-TEST-010  DELETE /api/vendor-rules/{id} with unknown id returns 404.
REQ-ID: VR-TEST-011  GET /api/vendor-rules/{id} match_count counts matching transactions.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection as _conn  # noqa: F401
from src.models.base import Base
from src.models.enums import (
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
# Shared-cache in-memory database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:vr_test?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI,
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
    """Truncate all tables before each test."""
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


# ---------------------------------------------------------------------------
# Client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Return a TestClient with sessions redirected to the test DB."""
    from src.api import main as _main_module
    from src.api.routes import vendor_rules as _vr_module

    with (
        patch.object(_vr_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={"customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0}),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(
    session: Session,
    pattern: str = "Test Vendor",
    entity: str = Entity.SPARKRY,
    tax_category: str = TaxCategory.OFFICE_EXPENSE,
    direction: str = Direction.EXPENSE,
    confidence: float = 0.95,
    examples: int = 3,
    source: str = VendorRuleSource.HUMAN,
) -> VendorRule:
    rule = VendorRule(
        id=str(uuid.uuid4()),
        vendor_pattern=pattern,
        entity=entity,
        tax_category=tax_category,
        direction=direction,
        deductible_pct=1.0,
        confidence=confidence,
        source=source,
        examples=examples,
        last_matched=None,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def _make_tx(
    session: Session,
    description: str = "Test Vendor",
    entity: str = Entity.SPARKRY,
    date: str = "2026-01-15",
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.BANK_CSV,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=-100.00,
        entity=entity,
        direction=Direction.EXPENSE,
        tax_category=TaxCategory.OFFICE_EXPENSE,
        status=TransactionStatus.CONFIRMED,
        confidence=0.95,
        raw_data={},
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_vendor_rules_empty(client: TestClient) -> None:
    """REQ-ID: VR-TEST-001 — Empty list returned when no rules exist."""
    resp = client.get("/api/vendor-rules")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_list_vendor_rules_pagination(client: TestClient) -> None:
    """REQ-ID: VR-TEST-001 — Pagination returns correct slice."""
    with _TestSession() as session:
        for i in range(5):
            _make_rule(session, pattern=f"Vendor {i}")

    resp = client.get("/api/vendor-rules?limit=2&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0


def test_list_vendor_rules_search(client: TestClient) -> None:
    """REQ-ID: VR-TEST-002 — Search filters by vendor_pattern substring."""
    with _TestSession() as session:
        _make_rule(session, pattern="Anthropic PBC")
        _make_rule(session, pattern="Amazon Web Services")

    resp = client.get("/api/vendor-rules?search=anthropic")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["vendor_pattern"] == "Anthropic PBC"


def test_list_vendor_rules_entity_filter(client: TestClient) -> None:
    """REQ-ID: VR-TEST-003 — Entity filter narrows results."""
    with _TestSession() as session:
        _make_rule(session, pattern="Sparkry Vendor", entity=Entity.SPARKRY)
        _make_rule(session, pattern="BlackLine Vendor", entity=Entity.BLACKLINE)

    resp = client.get("/api/vendor-rules?entity=sparkry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["entity"] == "sparkry"


def test_list_vendor_rules_invalid_entity(client: TestClient) -> None:
    """REQ-ID: VR-TEST-003 — Invalid entity returns 422."""
    resp = client.get("/api/vendor-rules?entity=badentity")
    assert resp.status_code == 422


def test_get_vendor_rule(client: TestClient) -> None:
    """REQ-ID: VR-TEST-004 — Single rule returned with match_count and last_matches."""
    with _TestSession() as session:
        rule = _make_rule(session, pattern="Stripe Inc")
        rule_id = rule.id

    resp = client.get(f"/api/vendor-rules/{rule_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rule_id
    assert data["vendor_pattern"] == "Stripe Inc"
    assert "match_count" in data
    assert "last_matches" in data
    assert isinstance(data["last_matches"], list)


def test_get_vendor_rule_match_count(client: TestClient) -> None:
    """REQ-ID: VR-TEST-011 — match_count reflects transactions matching pattern."""
    with _TestSession() as session:
        rule = _make_rule(session, pattern="Stripe")
        rule_id = rule.id
        _make_tx(session, description="Stripe Inc")
        _make_tx(session, description="Stripe Payout")
        _make_tx(session, description="Unrelated")

    resp = client.get(f"/api/vendor-rules/{rule_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["match_count"] == 2
    assert len(data["last_matches"]) == 2


def test_get_vendor_rule_not_found(client: TestClient) -> None:
    """REQ-ID: VR-TEST-004 — 404 for unknown rule id."""
    resp = client.get("/api/vendor-rules/nonexistent-id")
    assert resp.status_code == 404


def test_create_vendor_rule(client: TestClient) -> None:
    """REQ-ID: VR-TEST-005 — POST creates rule and returns 201."""
    payload = {
        "vendor_pattern": "Anthropic PBC",
        "entity": "sparkry",
        "tax_category": "OFFICE_EXPENSE",
        "direction": "expense",
        "deductible_pct": 1.0,
        "confidence": 0.95,
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["vendor_pattern"] == "Anthropic PBC"
    assert data["entity"] == "sparkry"
    assert data["tax_category"] == "OFFICE_EXPENSE"
    assert "id" in data

    # Confirm persisted
    resp2 = client.get("/api/vendor-rules")
    assert resp2.json()["total"] == 1


def test_create_vendor_rule_invalid_entity(client: TestClient) -> None:
    """REQ-ID: VR-TEST-006 — Invalid entity enum returns 422."""
    payload = {
        "vendor_pattern": "Test",
        "entity": "invalid_entity",
        "tax_category": "OFFICE_EXPENSE",
        "direction": "expense",
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 422


def test_create_vendor_rule_invalid_tax_category(client: TestClient) -> None:
    """REQ-ID: VR-TEST-006 — Invalid tax_category returns 422."""
    payload = {
        "vendor_pattern": "Test",
        "entity": "sparkry",
        "tax_category": "NOT_A_CATEGORY",
        "direction": "expense",
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 422


def test_patch_vendor_rule(client: TestClient) -> None:
    """REQ-ID: VR-TEST-007 — PATCH updates specified fields only."""
    with _TestSession() as session:
        rule = _make_rule(session, pattern="Old Pattern", confidence=0.8)
        rule_id = rule.id

    resp = client.patch(
        f"/api/vendor-rules/{rule_id}",
        json={"vendor_pattern": "New Pattern", "confidence": 0.95},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_pattern"] == "New Pattern"
    assert data["confidence"] == 0.95
    # Unchanged field preserved
    assert data["entity"] == "sparkry"


def test_patch_vendor_rule_not_found(client: TestClient) -> None:
    """REQ-ID: VR-TEST-008 — PATCH returns 404 for unknown rule."""
    resp = client.patch("/api/vendor-rules/no-such-id", json={"confidence": 0.5})
    assert resp.status_code == 404


def test_patch_vendor_rule_invalid_direction(client: TestClient) -> None:
    """REQ-ID: VR-TEST-007 — PATCH with invalid direction returns 422."""
    with _TestSession() as session:
        rule = _make_rule(session)
        rule_id = rule.id

    resp = client.patch(f"/api/vendor-rules/{rule_id}", json={"direction": "sideways"})
    assert resp.status_code == 422


def test_delete_vendor_rule(client: TestClient) -> None:
    """REQ-ID: VR-TEST-009 — DELETE removes the rule and returns 204."""
    with _TestSession() as session:
        rule = _make_rule(session)
        rule_id = rule.id

    resp = client.delete(f"/api/vendor-rules/{rule_id}")
    assert resp.status_code == 204

    resp2 = client.get("/api/vendor-rules")
    assert resp2.json()["total"] == 0


def test_delete_vendor_rule_not_found(client: TestClient) -> None:
    """REQ-ID: VR-TEST-010 — DELETE returns 404 for unknown rule."""
    resp = client.delete("/api/vendor-rules/no-such-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — REQ-FIX-ING-005: is_regex field + write-time validation
# ---------------------------------------------------------------------------


def test_create_vendor_rule_defaults_is_regex_false(client: TestClient) -> None:
    payload = {
        "vendor_pattern": "Anthropic PBC",
        "entity": "sparkry",
        "tax_category": "OFFICE_EXPENSE",
        "direction": "expense",
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 201
    assert resp.json()["is_regex"] is False


def test_create_vendor_rule_valid_regex_accepted(client: TestClient) -> None:
    payload = {
        "vendor_pattern": r"amazon.*aws|aws\.amazon",
        "is_regex": True,
        "entity": "sparkry",
        "tax_category": "SUPPLIES",
        "direction": "expense",
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 201
    assert resp.json()["is_regex"] is True


def test_create_vendor_rule_invalid_regex_returns_422(client: TestClient) -> None:
    payload = {
        "vendor_pattern": r"amazon(unclosed",
        "is_regex": True,
        "entity": "sparkry",
        "tax_category": "SUPPLIES",
        "direction": "expense",
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 422


def test_create_vendor_rule_invalid_regex_but_not_flagged_is_ok(client: TestClient) -> None:
    """A literal (is_regex=False, the default) pattern is never regex-validated
    — any string is a valid literal, even one containing '(' or '.'."""
    payload = {
        "vendor_pattern": "A.B Corp (West)",
        "entity": "sparkry",
        "tax_category": "SUPPLIES",
        "direction": "expense",
    }
    resp = client.post("/api/vendor-rules", json=payload)
    assert resp.status_code == 201
    assert resp.json()["is_regex"] is False


def test_patch_is_regex_true_validates_existing_pattern(client: TestClient) -> None:
    """Flipping is_regex=true on PATCH validates the rule's EXISTING pattern,
    even when vendor_pattern itself isn't part of this PATCH body."""
    with _TestSession() as session:
        rule = _make_rule(session, pattern="amazon(unclosed")
        rule_id = rule.id

    resp = client.patch(f"/api/vendor-rules/{rule_id}", json={"is_regex": True})
    assert resp.status_code == 422


def test_patch_new_pattern_validated_against_existing_is_regex(client: TestClient) -> None:
    """PATCHing vendor_pattern on a rule that is already is_regex=true
    validates the NEW pattern."""
    with _TestSession() as session:
        rule = _make_rule(session, pattern="amazon")
        rule.is_regex = True
        session.commit()
        rule_id = rule.id

    resp = client.patch(
        f"/api/vendor-rules/{rule_id}", json={"vendor_pattern": "amazon(unclosed"}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# REQ-FIX-ING-022 — the PATCH edit path must enforce the same guard as POST.
# qreview P0-a1b / P0-a1b-2: the dashboard rule editor is PATCH-based, so an
# unguarded PATCH leaves the whole incident reachable through the primary
# human edit surface.
# ---------------------------------------------------------------------------

_POISONED = "cardinal.*health|fascinate.*os"


def test_patch_cannot_turn_a_literal_rule_into_a_dead_regex(
    client: TestClient,
) -> None:
    """Setting a regex pattern while is_regex stays False must 422, not 200."""
    with _TestSession() as session:
        rule_id = _make_rule(session, pattern="Cardinal Health East Cafe").id

    resp = client.patch(
        f"/api/vendor-rules/{rule_id}", json={"vendor_pattern": _POISONED}
    )
    assert resp.status_code == 422, resp.text

    with _TestSession() as session:
        rule = session.query(VendorRule).filter(VendorRule.id == rule_id).one()
        assert rule.vendor_pattern == "Cardinal Health East Cafe"


def test_patch_cannot_clear_is_regex_on_a_regex_rule(client: TestClient) -> None:
    """Flipping is_regex to false on a rule that already holds a regex is the
    same poisoning, arrived at from the other direction."""
    with _TestSession() as session:
        rule = _make_rule(session, pattern=_POISONED)
        rule.is_regex = True
        session.commit()
        rule_id = rule.id

    resp = client.patch(f"/api/vendor-rules/{rule_id}", json={"is_regex": False})
    assert resp.status_code == 422, resp.text

    with _TestSession() as session:
        assert (
            session.query(VendorRule).filter(VendorRule.id == rule_id).one().is_regex
            is True
        )


def test_patch_accepts_a_regex_pattern_when_flagged_regex(
    client: TestClient,
) -> None:
    with _TestSession() as session:
        rule_id = _make_rule(session, pattern="Anthropic Headquarters").id

    resp = client.patch(
        f"/api/vendor-rules/{rule_id}",
        json={"vendor_pattern": _POISONED, "is_regex": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_regex"] is True


def test_patch_still_accepts_a_punctuated_literal(client: TestClient) -> None:
    """No false positives: real descriptors keep working through PATCH."""
    with _TestSession() as session:
        rule_id = _make_rule(session, pattern="Old Name").id

    resp = client.patch(
        f"/api/vendor-rules/{rule_id}", json={"vendor_pattern": "SQ *COFFEE SHOP"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["vendor_pattern"] == "SQ *COFFEE SHOP"
