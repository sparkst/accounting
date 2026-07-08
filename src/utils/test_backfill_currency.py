"""Tests for src/utils/backfill_currency.py — REQ-FIX-ING-002.

Decimal-only exchange-rate math: the reference-rate path (USD amount already
present, foreign amount detected for reference only) previously computed
``float(abs(tx.amount)) / best.amount`` — a ``float / Decimal`` TypeError on
every transaction reaching that branch. ``exchange_rate`` is
``Numeric(18,8, asdecimal=True)`` — Decimal end to end.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.utils.backfill_currency as backfill_mod
from src.models.base import Base
from src.models.transaction import Transaction


@pytest.fixture()
def session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[sessionmaker[Session], None, None]:
    """Redirect backfill_currency's SessionLocal/init_db to an isolated
    in-memory engine so backfill() can be exercised without touching the
    real database."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    monkeypatch.setattr(backfill_mod, "init_db", lambda: None)
    monkeypatch.setattr(backfill_mod, "SessionLocal", TestSession)

    yield TestSession
    engine.dispose()


def _make_gmail_tx(
    session: Session,
    *,
    amount: Decimal | None,
    body_text: str,
) -> Transaction:
    tx = Transaction(
        source="gmail_n8n",
        source_id="test-id-1",
        source_hash="test-hash-1",
        date="2025-05-01",
        description="Foreign Vendor",
        amount=amount,
        currency="USD",
        status="needs_review",
        confidence=0.0,
        raw_data={"body_text": body_text, "body_html": ""},
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


class TestBackfillReferenceRateDecimalMath:
    """REQ-FIX-ING-002: USD amount already present + foreign amount detected
    -> reference-only path. Must compute a Decimal exchange_rate without a
    float/Decimal TypeError."""

    def test_reference_rate_is_decimal_no_typeerror(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as s:
            _make_gmail_tx(
                s,
                amount=Decimal("-10.00"),
                body_text="Receipt total $10.00. Local charge: £8.00",
            )

        # Must not raise TypeError (the pre-fix bug: float(...) / Decimal).
        stats = backfill_mod.backfill(dry_run=False)

        assert stats["errors"] == 0
        assert stats["updated_reference"] == 1

        with session_factory() as s:
            tx = s.query(Transaction).filter_by(source_id="test-id-1").one()
            assert tx.currency_code == "GBP"
            assert tx.amount_foreign == Decimal("8.00")
            assert tx.exchange_rate is not None
            assert isinstance(tx.exchange_rate, Decimal)
            # 10.00 / 8.00 = 1.25
            assert tx.exchange_rate == Decimal("1.25000000")
            assert tx.exchange_rate_source == "email_extracted"

    def test_dry_run_does_not_write(self, session_factory: sessionmaker[Session]) -> None:
        with session_factory() as s:
            _make_gmail_tx(
                s,
                amount=Decimal("-20.00"),
                body_text="Receipt total $20.00. Local charge: €16.00",
            )

        stats = backfill_mod.backfill(dry_run=True)
        assert stats["errors"] == 0
        assert stats["updated_reference"] == 1

        with session_factory() as s:
            tx = s.query(Transaction).filter_by(source_id="test-id-1").one()
            assert tx.currency_code is None  # nothing written in dry-run
            assert tx.exchange_rate is None

    def test_already_has_currency_code_is_skipped(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as s:
            tx = _make_gmail_tx(
                s,
                amount=Decimal("-10.00"),
                body_text="Receipt total $10.00. Local charge: £8.00",
            )
            tx.currency_code = "GBP"
            s.commit()

        stats = backfill_mod.backfill(dry_run=False)
        assert stats["skipped"] == 1
        assert stats["updated_reference"] == 0
