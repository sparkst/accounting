"""Learning-loop tests for PATCH /api/transactions/{id} confirm (REQ-FIX-ING-004).

Uses the same shared-cache in-memory SQLite pattern as src/api/test_api.py.

REQ-ID: REQ-FIX-ING-004  Confirming a human-edited transaction updates the
                          MATCHED vendor rule's category/direction/
                          deductible_pct (not a raw pattern==description
                          lookup); a divergent correction on an exact-literal
                          rule resets confidence to base; a divergent
                          correction under a broader rule never mutates the
                          broad rule and instead creates/updates the precise
                          exact-literal rule, which then outranks the broad
                          rule (REQ-FIX-ING-009) for the next matching
                          transaction — the flip test.
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
from src.classification.rules import lookup_vendor_rule
from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    TaxCategory,
    TransactionStatus,
    VendorRuleSource,
)
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

# ---------------------------------------------------------------------------
# Shared-cache in-memory database (own namespace to avoid cross-file collision)
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:learning_loop_test?mode=memory&cache=shared&uri=true"

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


def _make_tx(
    session: Session,
    *,
    description: str = "Test Vendor",
    amount: Decimal = Decimal("-50.00"),
    entity: str | None = Entity.SPARKRY.value,
    tax_category: str | None = TaxCategory.SUPPLIES.value,
    direction: str | None = Direction.EXPENSE.value,
    deductible_pct: float = 1.0,
    status: str = TransactionStatus.NEEDS_REVIEW.value,
    confidence: float = 0.5,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source="gmail_n8n",
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date="2025-06-15",
        description=description,
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        deductible_pct=deductible_pct,
        status=status,
        confidence=confidence,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.AUTO.value,
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _make_rule(
    session: Session,
    *,
    pattern: str,
    is_regex: bool = False,
    entity: str = Entity.SPARKRY.value,
    tax_category: str = TaxCategory.SUPPLIES.value,
    direction: str = Direction.EXPENSE.value,
    deductible_pct: float = 1.0,
    confidence: float = 0.80,
    source: str = VendorRuleSource.LEARNED.value,
    examples: int = 1,
) -> VendorRule:
    rule = VendorRule(
        vendor_pattern=pattern,
        is_regex=is_regex,
        entity=entity,
        tax_category=tax_category,
        direction=direction,
        deductible_pct=deductible_pct,
        confidence=confidence,
        source=source,
        examples=examples,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


# ---------------------------------------------------------------------------
# Agreeing confirm
# ---------------------------------------------------------------------------


def test_agreeing_confirm_increments_examples_fields_unchanged(client: TestClient) -> None:
    with _TestSession() as s:
        rule = _make_rule(
            s,
            pattern="Agreeable Vendor",
            tax_category=TaxCategory.SUPPLIES.value,
            examples=3,
            confidence=0.83,
        )
        rule_id = rule.id
        tx = _make_tx(
            s,
            description="Agreeable Vendor",
            tax_category=TaxCategory.SUPPLIES.value,
        )
        tx_id = tx.id

    resp = client.patch(f"/api/transactions/{tx_id}", json={"status": "confirmed"})
    assert resp.status_code == 200

    with _TestSession() as s:
        rule = s.query(VendorRule).filter(VendorRule.id == rule_id).one()
        assert rule.examples == 4
        assert rule.tax_category == TaxCategory.SUPPLIES.value
        assert rule.confidence == pytest.approx(0.84)


# ---------------------------------------------------------------------------
# Divergent confirm — exact-literal learned rule
# ---------------------------------------------------------------------------


def test_divergent_confirm_overwrites_exact_literal_learned_rule(client: TestClient) -> None:
    """The matched rule IS the exact-literal rule for this description —
    overwrite its fields in place and reset confidence/examples to base."""
    with _TestSession() as s:
        rule = _make_rule(
            s,
            pattern="Wrong Category Vendor",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
            examples=10,
            confidence=0.90,
            source=VendorRuleSource.LEARNED.value,
        )
        rule_id = rule.id
        tx = _make_tx(
            s,
            description="Wrong Category Vendor",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,  # matches the (wrong) rule
        )
        tx_id = tx.id

    # Human corrects the category on confirm.
    resp = client.patch(
        f"/api/transactions/{tx_id}",
        json={"status": "confirmed", "tax_category": "SUPPLIES"},
    )
    assert resp.status_code == 200

    with _TestSession() as s:
        rule = s.query(VendorRule).filter(VendorRule.id == rule_id).one()
        assert rule.tax_category == TaxCategory.SUPPLIES.value
        assert rule.examples == 1
        assert rule.confidence == pytest.approx(0.80)


def test_divergent_confirm_human_seed_keeps_high_confidence(client: TestClient) -> None:
    """A human correcting a human-seed rule overwrites fields, resets
    examples, but keeps confidence=0.95 (still fully trusted)."""
    with _TestSession() as s:
        rule = _make_rule(
            s,
            pattern="Seeded Vendor",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
            examples=20,
            confidence=0.95,
            source=VendorRuleSource.HUMAN.value,
        )
        rule_id = rule.id
        tx = _make_tx(
            s,
            description="Seeded Vendor",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
        )
        tx_id = tx.id

    resp = client.patch(
        f"/api/transactions/{tx_id}",
        json={"status": "confirmed", "tax_category": "SUPPLIES"},
    )
    assert resp.status_code == 200

    with _TestSession() as s:
        rule = s.query(VendorRule).filter(VendorRule.id == rule_id).one()
        assert rule.tax_category == TaxCategory.SUPPLIES.value
        assert rule.examples == 1
        assert rule.confidence == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Divergent confirm — broad rule matched, never mutated
# ---------------------------------------------------------------------------


def test_divergent_confirm_under_broad_rule_creates_precise_rule_and_flips_next_match(
    client: TestClient,
) -> None:
    """The classic regression: a fat generic rule ("amazon", learned wrong)
    misclassifies an AWS charge. The human corrects it on confirm. The broad
    rule must be left untouched (still correct for other Amazon charges),
    and a precise exact-literal rule must be created that flips the
    classification of the NEXT transaction with the same description."""
    with _TestSession() as s:
        broad_rule = _make_rule(
            s,
            pattern="amazon",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
            examples=40,
            confidence=0.90,
            source=VendorRuleSource.LEARNED.value,
        )
        broad_rule_id = broad_rule.id
        tx = _make_tx(
            s,
            description="Amazon Web Services",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,  # inherited from the broad rule
        )
        tx_id = tx.id

    resp = client.patch(
        f"/api/transactions/{tx_id}",
        json={"status": "confirmed", "tax_category": "SUPPLIES"},
    )
    assert resp.status_code == 200

    with _TestSession() as s:
        # Broad rule is untouched.
        broad_rule = s.query(VendorRule).filter(VendorRule.id == broad_rule_id).one()
        assert broad_rule.tax_category == TaxCategory.OFFICE_EXPENSE.value
        assert broad_rule.examples == 40
        assert broad_rule.confidence == pytest.approx(0.90)

        # A precise exact-literal rule now exists.
        precise = (
            s.query(VendorRule)
            .filter(
                VendorRule.vendor_pattern == "Amazon Web Services",
                VendorRule.entity == Entity.SPARKRY.value,
            )
            .one()
        )
        assert precise.tax_category == TaxCategory.SUPPLIES.value
        assert precise.is_regex is False
        assert precise.examples == 1
        assert precise.confidence == pytest.approx(0.80)

        # The flip test: the NEXT classification of the same description
        # returns the corrected fields, not the broad rule's.
        result = lookup_vendor_rule("Amazon Web Services", s)
        assert result is not None
        assert result.tax_category == TaxCategory.SUPPLIES


def test_divergent_confirm_under_broad_rule_updates_existing_precise_rule(
    client: TestClient,
) -> None:
    """If a precise exact-literal rule already exists but the broad rule
    still won the match (e.g. length tie broken by examples), a second
    divergent correction updates the EXISTING precise rule rather than
    creating a duplicate."""
    with _TestSession() as s:
        _make_rule(
            s,
            pattern="amazon",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
            examples=40,
            confidence=0.90,
        )
        precise = _make_rule(
            s,
            pattern="Amazon Web Services",
            tax_category=TaxCategory.SUPPLIES.value,
            examples=1,
            confidence=0.80,
        )
        precise_id = precise.id
        tx = _make_tx(
            s,
            description="Amazon Web Services",
            tax_category=TaxCategory.SUPPLIES.value,
        )
        tx_id = tx.id

    resp = client.patch(
        f"/api/transactions/{tx_id}",
        json={"status": "confirmed", "tax_category": "COGS"},
    )
    assert resp.status_code == 200

    with _TestSession() as s:
        precise = s.query(VendorRule).filter(VendorRule.id == precise_id).one()
        assert precise.tax_category == TaxCategory.COGS.value
        assert precise.examples == 1
        # Only ONE precise rule exists for this pattern/entity.
        count = (
            s.query(VendorRule)
            .filter(
                VendorRule.vendor_pattern == "Amazon Web Services",
                VendorRule.entity == Entity.SPARKRY.value,
            )
            .count()
        )
        assert count == 1


def test_divergent_confirm_under_broad_regex_full_span_tie_flips_next_match(
    client: TestClient,
) -> None:
    """P2-a1c-2 / REQ-FIX-ING-004 + REQ-FIX-ING-009: when the matched BROAD
    rule is a REGEX whose matched span equals the ENTIRE description (e.g. a
    seed rule like ``\\bshopify\\b`` against a description that cleans down
    to the single token "Shopify"), the new precise literal rule ties the
    broad rule on match-length. Before the is_regex tiebreak in _rank_best,
    this tie fell through to `examples`, where a well-established broad seed
    rule (examples=5) beat the brand-new precise rule (examples=1) and the
    human's correction never took effect on the next lookup. This pins that
    the precise literal rule now wins the tie outright."""
    with _TestSession() as s:
        broad_rule = _make_rule(
            s,
            pattern=r"\bshopify\b",
            is_regex=True,
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
            examples=5,
            confidence=0.90,
            source=VendorRuleSource.HUMAN.value,
        )
        broad_rule_id = broad_rule.id
        tx = _make_tx(
            s,
            description="Shopify",
            tax_category=TaxCategory.OFFICE_EXPENSE.value,  # inherited from the broad regex rule
        )
        tx_id = tx.id

    resp = client.patch(
        f"/api/transactions/{tx_id}",
        json={"status": "confirmed", "tax_category": "SUPPLIES"},
    )
    assert resp.status_code == 200

    with _TestSession() as s:
        # Broad regex rule is untouched.
        broad_rule = s.query(VendorRule).filter(VendorRule.id == broad_rule_id).one()
        assert broad_rule.tax_category == TaxCategory.OFFICE_EXPENSE.value
        assert broad_rule.examples == 5

        # A precise exact-literal rule now exists, tied on match length with
        # the broad regex rule (both match the full 7-char description).
        precise = (
            s.query(VendorRule)
            .filter(
                VendorRule.vendor_pattern == "Shopify",
                VendorRule.entity == Entity.SPARKRY.value,
                VendorRule.is_regex.is_(False),
            )
            .one()
        )
        assert precise.tax_category == TaxCategory.SUPPLIES.value
        assert precise.examples == 1

        # The flip test: despite the length tie and fewer examples, the
        # precise literal rule outranks the broad regex rule on the next
        # lookup.
        result = lookup_vendor_rule("Shopify", s)
        assert result is not None
        assert result.tax_category == TaxCategory.SUPPLIES


# ---------------------------------------------------------------------------
# Entity change creates a parallel rule under the new entity
# ---------------------------------------------------------------------------


def test_entity_change_creates_rule_under_new_entity_old_untouched(
    client: TestClient,
) -> None:
    with _TestSession() as s:
        sparkry_rule = _make_rule(
            s,
            pattern="Shared Vendor",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            examples=5,
            confidence=0.85,
        )
        sparkry_rule_id = sparkry_rule.id
        tx = _make_tx(
            s,
            description="Shared Vendor",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
        )
        tx_id = tx.id

    resp = client.patch(
        f"/api/transactions/{tx_id}",
        json={"status": "confirmed", "entity": "blackline"},
    )
    assert resp.status_code == 200

    with _TestSession() as s:
        # Old-entity rule untouched.
        sparkry_rule = s.query(VendorRule).filter(VendorRule.id == sparkry_rule_id).one()
        assert sparkry_rule.examples == 5
        assert sparkry_rule.entity == Entity.SPARKRY.value

        # New rule created for the new entity.
        blackline_rule = (
            s.query(VendorRule)
            .filter(
                VendorRule.vendor_pattern == "Shared Vendor",
                VendorRule.entity == Entity.BLACKLINE.value,
            )
            .one()
        )
        assert blackline_rule.examples == 1
