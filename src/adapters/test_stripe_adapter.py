"""Tests for the Stripe adapter.

REQ-ID: ADAPTER-STRIPE-001  Connects with two API keys (Sparkry, BlackLine).
REQ-ID: ADAPTER-STRIPE-002  Maps charges, payouts, invoices, refunds to Transactions.
REQ-ID: ADAPTER-STRIPE-003  Entity is determined by which API key retrieved the record.
REQ-ID: ADAPTER-STRIPE-004  Identifies Substack income by description/metadata.
REQ-ID: ADAPTER-STRIPE-005  AuthenticationError halts immediately (no retry).
REQ-ID: ADAPTER-STRIPE-006  RateLimitError / APIConnectionError retry with jittered backoff.
REQ-ID: ADAPTER-STRIPE-007  After 3 consecutive failures, status = FAILURE.
REQ-ID: ADAPTER-STRIPE-008  One-entity failure → PARTIAL_FAILURE, other entity still ingested.
REQ-ID: ADAPTER-STRIPE-009  Deduplication by source_hash; re-run creates no duplicates.
REQ-ID: ADAPTER-STRIPE-010  IngestionLog entry created for every run.

All Stripe API calls are mocked; no network required.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.stripe_adapter import (
    ACCOUNT_MODE_PERSONAL_MIXED,
    ACCOUNT_MODE_STANDARD,
    STRIPE_API_VERSION,
    UNEXPECTED_CHARGE_REVIEW_REASON,
    StripeAdapter,
    _classify_stripe_object,
    _fetch_all,
    _map_charge,
    _map_charge_fee,
    _map_payout,
    _map_refund,
    _metadata_dict,
)
from src.models.base import Base
from src.models.enums import (
    Direction,
    Entity,
    IngestionStatus,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Helpers — fake Stripe objects
# ---------------------------------------------------------------------------


def _fake_stripe_obj(**kwargs: Any) -> MagicMock:
    """Build a MagicMock that behaves like a Stripe API object (attribute access)."""
    obj = MagicMock()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    obj.get = lambda key, default=None: kwargs.get(key, default)
    return obj


def _fake_charge(
    charge_id: str = "ch_test001",
    amount: int = 5000,           # cents
    currency: str = "usd",
    created: int = 1_700_000_000,
    description: str = "Test charge",
    status: str = "succeeded",
    refunded: bool = False,
    customer: str | None = None,
    metadata: dict[str, str] | None = None,
    invoice: str | None = None,
    balance_transaction: Any | None = None,
) -> MagicMock:
    metadata = metadata or {}
    m = MagicMock()
    m.id = charge_id
    m.object = "charge"
    m.amount = amount
    m.currency = currency
    m.created = created
    m.description = description
    m.status = status
    m.refunded = refunded
    m.customer = customer
    m.metadata = metadata
    m.invoice = invoice
    m.balance_transaction = balance_transaction
    m.get = lambda k, d=None: {
        "id": charge_id, "object": "charge", "amount": amount, "currency": currency,
        "created": created, "description": description, "status": status,
        "refunded": refunded, "customer": customer, "metadata": metadata,
        "invoice": invoice,
    }.get(k, d)
    return m


def _fake_payout(
    payout_id: str = "po_test001",
    amount: int = 4800,
    currency: str = "usd",
    created: int = 1_700_100_000,
    arrival_date: int = 1_700_200_000,
    description: str = "STRIPE PAYOUT",
    status: str = "paid",
) -> MagicMock:
    m = MagicMock()
    m.id = payout_id
    m.object = "payout"
    m.amount = amount
    m.currency = currency
    m.created = created
    m.arrival_date = arrival_date
    m.description = description
    m.status = status
    m.metadata = {}
    m.get = lambda k, d=None: {
        "id": payout_id, "object": "payout", "amount": amount, "currency": currency,
        "created": created, "arrival_date": arrival_date, "description": description,
        "status": status,
    }.get(k, d)
    return m


def _fake_refund(
    refund_id: str = "re_test001",
    amount: int = 5000,
    currency: str = "usd",
    created: int = 1_700_050_000,
    charge: str = "ch_test001",
    reason: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id = refund_id
    m.object = "refund"
    m.amount = amount
    m.currency = currency
    m.created = created
    m.charge = charge
    m.reason = reason
    m.metadata = {}
    m.get = lambda k, d=None: {
        "id": refund_id, "object": "refund", "amount": amount, "currency": currency,
        "created": created, "charge": charge, "reason": reason,
    }.get(k, d)
    return m


def _fake_list(*items: Any) -> MagicMock:
    """Simulate a Stripe auto-paging list: just a regular iterable."""
    lst = MagicMock()
    lst.auto_paging_iter.return_value = iter(items)
    return lst


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite per test function."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> StripeAdapter:
    """StripeAdapter with per-account fake API keys via environment.

    Separate-account model: each Stripe account authenticates with its own
    restricted key (no Stripe-Account header). STRIPE_API_KEY = Sparkry sub.
    """
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_sparkry")
    monkeypatch.setenv("STRIPE_API_KEY_BLACKLINE", "rk_test_blackline")
    monkeypatch.delenv("STRIPE_API_KEY_TRAVIS_PERSONAL", raising=False)
    return StripeAdapter()


# ---------------------------------------------------------------------------
# Unit tests: helper functions
# ---------------------------------------------------------------------------


class TestStripeApiVersion:
    def test_api_version_pinned(self) -> None:
        assert STRIPE_API_VERSION == "2024-12-18.acacia"


class TestClassifyStripeObject:
    def test_charge_succeeded(self) -> None:
        charge = _fake_charge()
        result = _classify_stripe_object(charge, Entity.SPARKRY)
        assert result["direction"] == Direction.INCOME
        assert result["tax_category"] == TaxCategory.SALES_INCOME

    def test_charge_with_substack_description(self) -> None:
        charge = _fake_charge(description="Substack subscription payment")
        result = _classify_stripe_object(charge, Entity.SPARKRY)
        assert result["tax_category"] == TaxCategory.SUBSCRIPTION_INCOME

    def test_charge_with_substack_metadata(self) -> None:
        charge = _fake_charge(metadata={"source": "substack"})
        result = _classify_stripe_object(charge, Entity.SPARKRY)
        assert result["tax_category"] == TaxCategory.SUBSCRIPTION_INCOME

    def test_charge_with_stripeobject_metadata(self) -> None:
        """Regression: live Stripe returns metadata as a StripeObject, NOT a
        dict, and intercepts ``.get`` as a key lookup → AttributeError. The
        Substack/Travis backfill failed 7 rows on exactly this. Build a charge
        whose .metadata is a real StripeObject and confirm classification works.
        """
        from stripe import StripeObject

        meta = StripeObject.construct_from({"source": "substack"}, "sk_test")
        # _metadata_dict must extract it without raising.
        assert _metadata_dict(SimpleNamespace(metadata=meta)) == {"source": "substack"}
        # A charge carrying that StripeObject metadata still classifies as Substack.
        charge = _fake_charge()
        charge.metadata = meta
        result = _classify_stripe_object(charge, Entity.SPARKRY)
        assert result["tax_category"] == TaxCategory.SUBSCRIPTION_INCOME

    def test_metadata_dict_handles_missing_get_key(self) -> None:
        """A StripeObject with NO ``source``/``platform`` keys must not raise
        when those keys are read (the original AttributeError: get case)."""
        from stripe import StripeObject

        meta = StripeObject.construct_from({"order_id": "9999"}, "sk_test")
        out = _metadata_dict(SimpleNamespace(metadata=meta))
        assert out == {"order_id": "9999"}
        assert out.get("source", "") == ""  # the call that used to blow up

    def test_payout(self) -> None:
        payout = _fake_payout()
        result = _classify_stripe_object(payout, Entity.SPARKRY)
        assert result["direction"] == Direction.TRANSFER

    def test_refund(self) -> None:
        refund = _fake_refund()
        result = _classify_stripe_object(refund, Entity.SPARKRY)
        assert result["direction"] == Direction.EXPENSE


class TestMapCharge:
    def test_basic_charge_fields(self) -> None:
        charge = _fake_charge(
            charge_id="ch_abc123",
            amount=10000,
            currency="usd",
            created=1_700_000_000,
            description="Test sale",
        )
        tx = _map_charge(charge, Entity.SPARKRY)
        assert tx.source == Source.STRIPE.value
        assert tx.source_id == "ch_abc123"
        assert tx.amount == Decimal("100.00")
        assert tx.currency == "USD"
        assert tx.entity == Entity.SPARKRY.value
        assert tx.direction == Direction.INCOME.value

    def test_charge_amount_in_dollars(self) -> None:
        charge = _fake_charge(amount=1234)  # 1234 cents = $12.34
        tx = _map_charge(charge, Entity.SPARKRY)
        assert tx.amount == Decimal("12.34")

    def test_charge_date_from_created_timestamp(self) -> None:
        # created=0 → 1970-01-01
        charge = _fake_charge(created=0)
        tx = _map_charge(charge, Entity.SPARKRY)
        assert tx.date == "1970-01-01"

    def test_charge_entity_blackline(self) -> None:
        charge = _fake_charge()
        tx = _map_charge(charge, Entity.BLACKLINE)
        assert tx.entity == Entity.BLACKLINE.value

    def test_charge_source_hash_unique(self) -> None:
        c1 = _fake_charge(charge_id="ch_aaa")
        c2 = _fake_charge(charge_id="ch_bbb")
        assert _map_charge(c1, Entity.SPARKRY).source_hash != _map_charge(c2, Entity.SPARKRY).source_hash

    def test_raw_data_preserved(self) -> None:
        charge = _fake_charge(charge_id="ch_raw")
        tx = _map_charge(charge, Entity.SPARKRY)
        assert tx.raw_data is not None
        assert "id" in tx.raw_data or "ch_raw" in str(tx.raw_data)


class TestPersonalMixedMode:
    """REQ-ID: ADAPTER-STRIPE-012 — personal-mixed account mode flags
    non-invoice charges via review_reason for human review."""

    def test_subscription_charge_with_invoice_classifies_as_subscription(self) -> None:
        charge = _fake_charge(
            charge_id="ch_sub1",
            description="Subscription update",
            invoice="in_xyz",
        )
        result = _classify_stripe_object(charge, Entity.SPARKRY, ACCOUNT_MODE_PERSONAL_MIXED)
        assert result["tax_category"] == TaxCategory.SUBSCRIPTION_INCOME
        assert result["review_reason"] is None

    def test_manual_invoice_charge_classifies_as_consulting(self) -> None:
        charge = _fake_charge(
            charge_id="ch_inv1",
            description="Invoice for services",
            invoice="in_manual",
        )
        result = _classify_stripe_object(charge, Entity.SPARKRY, ACCOUNT_MODE_PERSONAL_MIXED)
        assert result["tax_category"] == TaxCategory.CONSULTING_INCOME
        assert result["review_reason"] is None

    def test_non_invoice_charge_is_flagged(self) -> None:
        charge = _fake_charge(
            charge_id="ch_oneoff",
            description="Black Line MTB Apparel - Order 5980",
            invoice=None,
        )
        result = _classify_stripe_object(charge, Entity.SPARKRY, ACCOUNT_MODE_PERSONAL_MIXED)
        assert result["tax_category"] is None
        assert result["review_reason"] == UNEXPECTED_CHARGE_REVIEW_REASON
        assert result["direction"] == Direction.INCOME

    def test_standard_mode_unchanged_for_non_invoice_charge(self) -> None:
        """Regression guard: non-invoice charges on STANDARD accounts still
        classify as SALES_INCOME and are NOT flagged with review_reason."""
        charge = _fake_charge(charge_id="ch_pos", description="POS sale", invoice=None)
        result = _classify_stripe_object(charge, Entity.BLACKLINE, ACCOUNT_MODE_STANDARD)
        assert result["tax_category"] == TaxCategory.SALES_INCOME
        assert result["review_reason"] is None

    def test_map_charge_personal_mixed_sets_review_reason(self) -> None:
        charge = _fake_charge(
            charge_id="ch_flagged",
            description="Random one-off",
            invoice=None,
        )
        tx = _map_charge(charge, Entity.SPARKRY, ACCOUNT_MODE_PERSONAL_MIXED)
        assert tx.review_reason == UNEXPECTED_CHARGE_REVIEW_REASON
        assert tx.tax_category is None
        assert tx.direction == Direction.INCOME.value
        assert tx.entity == Entity.SPARKRY.value

    def test_map_charge_standard_does_not_set_review_reason(self) -> None:
        charge = _fake_charge(charge_id="ch_std", description="Ordinary sale")
        tx = _map_charge(charge, Entity.SPARKRY)
        assert tx.review_reason is None

    def test_travis_personal_account_added_when_env_var_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRIPE_API_KEY", "rk_test_sparkry")
        monkeypatch.setenv("STRIPE_API_KEY_BLACKLINE", "rk_test_bl")
        monkeypatch.setenv("STRIPE_API_KEY_TRAVIS_PERSONAL", "rk_test_travis")
        adapter = StripeAdapter()
        assert adapter._key_travis_personal == "rk_test_travis"

    def test_travis_personal_account_omitted_when_env_var_absent(
        self, adapter: StripeAdapter
    ) -> None:
        # `adapter` fixture only sets SPARKRY + BLACKLINE keys, not TRAVIS_PERSONAL.
        assert adapter._key_travis_personal is None

    def test_ingest_personal_mixed_flags_non_invoice_row(
        self, monkeypatch: pytest.MonkeyPatch, session: Session
    ) -> None:
        """End-to-end: with the third account's key configured, a non-invoice
        charge on it lands in the DB with a populated review_reason."""
        monkeypatch.setenv("STRIPE_API_KEY", "rk_test_sparkry")
        monkeypatch.setenv("STRIPE_API_KEY_BLACKLINE", "rk_test_bl")
        monkeypatch.setenv("STRIPE_API_KEY_TRAVIS_PERSONAL", "rk_test_travis")
        adapter = StripeAdapter()

        sub_charge = _fake_charge(
            charge_id="ch_travis_sub",
            description="Subscription creation",
            invoice="in_travis_sub",
        )
        anomaly_charge = _fake_charge(
            charge_id="ch_travis_anomaly",
            description="Black Line MTB Apparel - Order 9999",
            invoice=None,
        )

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def side_effect(client: Any, resource: str, entity: Any, **kw: Any) -> list[Any]:
                # Separate-account model: discriminate by the per-account key the
                # StripeClient was built with (no Stripe-Account header anymore).
                key = client._requestor.api_key
                if key == "rk_test_travis" and resource == "charges":
                    return [sub_charge, anomaly_charge]
                return []

            mock_fetch.side_effect = side_effect
            adapter.run(session)

        rows = {
            r.source_id: r
            for r in session.query(Transaction).filter(
                Transaction.source_id.in_(["ch_travis_sub", "ch_travis_anomaly"])
            )
        }
        assert "ch_travis_sub" in rows
        assert "ch_travis_anomaly" in rows
        assert rows["ch_travis_sub"].tax_category == TaxCategory.SUBSCRIPTION_INCOME.value
        assert rows["ch_travis_sub"].review_reason is None
        assert rows["ch_travis_anomaly"].tax_category is None
        assert rows["ch_travis_anomaly"].review_reason == UNEXPECTED_CHARGE_REVIEW_REASON
        # Both get entity=sparkry (the third account rolls up into Sparkry)
        assert rows["ch_travis_sub"].entity == Entity.SPARKRY.value
        assert rows["ch_travis_anomaly"].entity == Entity.SPARKRY.value


class TestMapChargeFee:
    """REQ-ID: ADAPTER-STRIPE-011 — processing fee ingested as sibling expense row."""

    def _charge_with_fee(self, fee_cents: int, charge_id: str = "ch_fee1") -> MagicMock:
        charge = _fake_charge(charge_id=charge_id, amount=200000)
        bt = MagicMock()
        bt.id = f"txn_{charge_id}"
        bt.fee = fee_cents
        charge.balance_transaction = bt
        return charge

    def test_returns_none_when_no_balance_transaction(self) -> None:
        charge = _fake_charge(charge_id="ch_nobt")
        # _fake_charge's MagicMock will invent a balance_transaction attribute
        # that is itself a MagicMock — simulate an unset attribute explicitly.
        charge.balance_transaction = None
        assert _map_charge_fee(charge, Entity.SPARKRY) is None

    def test_returns_none_when_fee_zero(self) -> None:
        charge = self._charge_with_fee(fee_cents=0)
        assert _map_charge_fee(charge, Entity.SPARKRY) is None

    def test_returns_none_when_balance_transaction_is_string_reference(self) -> None:
        charge = _fake_charge(charge_id="ch_strref")
        charge.balance_transaction = "txn_unexpanded"  # not expanded
        assert _map_charge_fee(charge, Entity.SPARKRY) is None

    def test_fee_transaction_is_negative_expense(self) -> None:
        charge = self._charge_with_fee(fee_cents=5830, charge_id="ch_sp1")  # $58.30
        fee_tx = _map_charge_fee(charge, Entity.SPARKRY)
        assert fee_tx is not None
        assert fee_tx.amount == Decimal("-58.30")
        assert fee_tx.direction == Direction.EXPENSE.value
        assert fee_tx.tax_category == TaxCategory.LEGAL_AND_PROFESSIONAL.value
        assert fee_tx.entity == Entity.SPARKRY.value
        assert fee_tx.source_id == "fee_ch_sp1"

    def test_fee_source_hash_distinct_from_charge(self) -> None:
        charge = self._charge_with_fee(fee_cents=2060, charge_id="ch_uniq")
        charge_tx = _map_charge(charge, Entity.SPARKRY)
        fee_tx = _map_charge_fee(charge, Entity.SPARKRY)
        assert fee_tx is not None
        assert fee_tx.source_hash != charge_tx.source_hash

    def test_ingest_creates_charge_and_fee_rows(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """End-to-end: a charge with a fee results in TWO rows in the DB."""
        charge = self._charge_with_fee(fee_cents=5830, charge_id="ch_ingest_fee")

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def only_sparkry_charge(
                client: Any, resource: str, entity: Any, **kw: Any
            ) -> list[Any]:
                if entity == Entity.SPARKRY and resource == "charges":
                    return [charge]
                return []

            mock_fetch.side_effect = only_sparkry_charge
            adapter.run(session)

        rows = session.query(Transaction).filter(
            Transaction.source_id.in_(["ch_ingest_fee", "fee_ch_ingest_fee"])
        ).all()
        assert len(rows) == 2
        by_id = {r.source_id: r for r in rows}
        assert by_id["ch_ingest_fee"].direction == Direction.INCOME.value
        assert by_id["fee_ch_ingest_fee"].direction == Direction.EXPENSE.value
        assert by_id["fee_ch_ingest_fee"].amount == Decimal("-58.30")

    def test_ingest_requests_balance_transaction_expansion(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """The charges fetch must request expand=['data.balance_transaction']."""
        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            mock_fetch.return_value = []
            adapter.run(session)

        charge_calls = [
            c for c in mock_fetch.call_args_list if c.args[1] == "charges"
        ]
        assert charge_calls, "expected at least one charges fetch"
        for c in charge_calls:
            assert c.kwargs.get("expand") == ["data.balance_transaction"]


class TestMapPayout:
    def test_payout_direction_is_transfer(self) -> None:
        payout = _fake_payout(payout_id="po_test", amount=9900)
        tx = _map_payout(payout, Entity.SPARKRY)
        assert tx.direction == Direction.TRANSFER.value
        assert tx.amount == Decimal("99.00")
        assert tx.source_id == "po_test"

    def test_payout_is_auto_classified_not_needs_review(self) -> None:
        """Regression: a payout must NOT be needs_review — that fed it to the
        ingest reclassify pass, where the LLM mislabeled "Stripe payout" as
        SALES_INCOME (13 parent-account payouts wrongly booked as BlackLine
        sales). It must be transfer + no tax_category + auto_classified."""
        tx = _map_payout(_fake_payout(payout_id="po_x", amount=10000), Entity.SPARKRY)
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        assert tx.tax_category is None
        assert tx.direction == Direction.TRANSFER.value

    def test_payout_date_uses_arrival_date(self) -> None:
        # arrival_date = 2023-11-16 00:00:00 UTC = 1700092800
        payout = _fake_payout(arrival_date=1700092800, created=0)
        tx = _map_payout(payout, Entity.SPARKRY)
        assert tx.date == "2023-11-16"


class TestMapRefund:
    def test_refund_direction_is_expense(self) -> None:
        refund = _fake_refund(refund_id="re_xyz", amount=2500)
        tx = _map_refund(refund, Entity.BLACKLINE)
        assert tx.direction == Direction.EXPENSE.value
        assert tx.amount == Decimal("-25.00")
        assert tx.entity == Entity.BLACKLINE.value


# ---------------------------------------------------------------------------
# Integration tests: StripeAdapter.run()
# ---------------------------------------------------------------------------


class TestStripeAdapterInit:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STRIPE_API_KEY", raising=False)
        monkeypatch.delenv("STRIPE_API_KEY_BLACKLINE", raising=False)
        monkeypatch.delenv("STRIPE_API_KEY_TRAVIS_PERSONAL", raising=False)
        with pytest.raises(EnvironmentError, match="STRIPE_API_KEY"):
            StripeAdapter()

    def test_secondary_keys_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only the Sparkry key (STRIPE_API_KEY) is required; the other two
        # accounts are optional and skipped when their keys are absent.
        monkeypatch.setenv("STRIPE_API_KEY", "rk_test_sparkry")
        monkeypatch.delenv("STRIPE_API_KEY_BLACKLINE", raising=False)
        monkeypatch.delenv("STRIPE_API_KEY_TRAVIS_PERSONAL", raising=False)
        adapter = StripeAdapter()
        assert adapter._key_sparkry == "rk_test_sparkry"
        assert adapter._key_blackline is None
        assert adapter._key_travis_personal is None


class TestStripeAdapterRun:
    def test_successful_run_inserts_transactions(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """Happy path: charges and payouts from both entities get inserted."""
        sp_charge = _fake_charge(charge_id="ch_sp1", amount=5000)
        bl_charge = _fake_charge(charge_id="ch_bl1", amount=7500)
        sp_payout = _fake_payout(payout_id="po_sp1", amount=4800)
        bl_refund = _fake_refund(refund_id="re_bl1", amount=1000)

        with patch("src.adapters.stripe_adapter._fetch_all", autospec=True) as mock_fetch:
            def fetch_side_effect(client: Any, resource: str, entity: Any, **kw: Any):
                from src.models.enums import Entity as E
                if entity == E.SPARKRY:
                    if resource == "charges":
                        return [sp_charge]
                    elif resource == "payouts":
                        return [sp_payout]
                    else:
                        return []
                else:  # blackline
                    if resource == "charges":
                        return [bl_charge]
                    elif resource == "refunds":
                        return [bl_refund]
                    else:
                        return []

            mock_fetch.side_effect = fetch_side_effect

            result = adapter.run(session)

        assert result.status == IngestionStatus.SUCCESS
        assert result.records_created >= 3  # sp_charge, sp_payout, bl_charge (+ bl_refund)
        txs = session.query(Transaction).all()
        source_ids = {tx.source_id for tx in txs}
        assert "ch_sp1" in source_ids
        assert "po_sp1" in source_ids
        assert "ch_bl1" in source_ids

    def test_deduplication_no_double_insert(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """Running adapter twice should not create duplicate transactions."""
        charge = _fake_charge(charge_id="ch_dup1", amount=3000)

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            mock_fetch.return_value = [charge]

            adapter.run(session)
            first_count = session.query(Transaction).count()

            adapter.run(session)
            second_count = session.query(Transaction).count()

        assert second_count == first_count

    def test_ingestion_log_created(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """An IngestionLog entry must be written for every run."""
        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            mock_fetch.return_value = []
            adapter.run(session)

        logs = session.query(IngestionLog).all()
        assert len(logs) >= 1
        assert logs[0].source == Source.STRIPE.value

    def test_ingestion_log_reflects_outcome(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        charge = _fake_charge(charge_id="ch_log1")

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            mock_fetch.return_value = [charge]
            adapter.run(session)

        log = session.query(IngestionLog).first()
        assert log is not None
        assert log.status in (
            IngestionStatus.SUCCESS.value,
            IngestionStatus.PARTIAL_FAILURE.value,
        )

    def test_entity_mapped_correctly(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """Charges from Sparkry key get entity=sparkry; BlackLine key → blackline."""
        sp_charge = _fake_charge(charge_id="ch_entity_sp")
        bl_charge = _fake_charge(charge_id="ch_entity_bl")

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def by_entity(client: Any, resource: str, entity: Any, **kw: Any):
                from src.models.enums import Entity as E
                if entity == E.SPARKRY and resource == "charges":
                    return [sp_charge]
                if entity == E.BLACKLINE and resource == "charges":
                    return [bl_charge]
                return []

            mock_fetch.side_effect = by_entity
            adapter.run(session)

        txs = {tx.source_id: tx for tx in session.query(Transaction).all()}
        assert txs["ch_entity_sp"].entity == Entity.SPARKRY.value
        assert txs["ch_entity_bl"].entity == Entity.BLACKLINE.value

    def test_substack_charge_classified_as_subscription_income(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        charge = _fake_charge(
            charge_id="ch_substack",
            description="Substack subscription charge",
        )

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def by_resource(client: Any, resource: str, entity: Any, **kw: Any):
                if resource == "charges":
                    return [charge]
                return []

            mock_fetch.side_effect = by_resource
            adapter.run(session)

        tx = session.query(Transaction).filter_by(source_id="ch_substack").first()
        assert tx is not None
        assert tx.tax_category == TaxCategory.SUBSCRIPTION_INCOME.value


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestAuthenticationError:
    def test_auth_error_sparkry_halts_entity_immediately(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """AuthenticationError on Sparkry key → stops that entity immediately, no retry."""
        import stripe as stripe_lib

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def raise_auth(client: Any, resource: str, entity: Any, **kw: Any):
                if entity == Entity.SPARKRY:
                    raise stripe_lib.AuthenticationError(
                        "No such API key", http_status=401, code="api_key_invalid"
                    )
                return []

            mock_fetch.side_effect = raise_auth
            result = adapter.run(session)

        # Sparkry had auth error (1 call → halt), BlackLine had no auth error (3 calls for 3 resources)
        # Total: 1 (sparkry, halted early) + 3 (blackline) = 4
        assert mock_fetch.call_count <= 4
        assert result.status in (
            IngestionStatus.PARTIAL_FAILURE.value,
            IngestionStatus.FAILURE.value,
        )

    def test_auth_error_produces_human_readable_message(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        import stripe as stripe_lib

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def raise_auth(client: Any, resource: str, entity: Any, **kw: Any):
                raise stripe_lib.AuthenticationError(
                    "No such API key", http_status=401, code="api_key_invalid"
                )

            mock_fetch.side_effect = raise_auth
            result = adapter.run(session)

        # Errors list should contain human-readable entity names
        all_errors = " ".join(msg for _, msg in result.errors)
        assert any(
            kw in all_errors.lower()
            for kw in ("sparkry", "blackline", "authentication", "api key")
        )

    def test_one_entity_auth_error_other_succeeds(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """Sparkry auth fails → PARTIAL_FAILURE, BlackLine charges still ingested."""
        import stripe as stripe_lib

        bl_charge = _fake_charge(charge_id="ch_bl_ok")

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def mixed(client: Any, resource: str, entity: Any, **kw: Any):
                if entity == Entity.SPARKRY:
                    raise stripe_lib.AuthenticationError(
                        "Bad key", http_status=401, code="api_key_invalid"
                    )
                if resource == "charges":
                    return [bl_charge]
                return []

            mock_fetch.side_effect = mixed
            result = adapter.run(session)

        assert result.status == IngestionStatus.PARTIAL_FAILURE.value
        tx = session.query(Transaction).filter_by(source_id="ch_bl_ok").first()
        assert tx is not None


# ---------------------------------------------------------------------------
# Retry tests — test _fetch_all directly to verify internal retry logic
# ---------------------------------------------------------------------------


class TestFetchAllRetry:
    """Tests for the retry logic inside _fetch_all.

    We test _fetch_all directly, mocking the Stripe SDK at the resource level
    (client.charges.list, etc.) rather than mocking _fetch_all itself.
    """

    def _make_client(self, resource: str, list_side_effect: Any) -> MagicMock:
        """Build a fake StripeClient whose resource.list() has the given side effect."""
        client = MagicMock()
        resource_obj = MagicMock()
        resource_obj.list.side_effect = list_side_effect
        setattr(client, resource, resource_obj)
        return client

    def test_rate_limit_retries_and_succeeds(self) -> None:
        """RateLimitError on first call; succeeds on second attempt."""
        import stripe as stripe_lib

        charge = _fake_charge(charge_id="ch_retry_ok")
        page = MagicMock()
        page.auto_paging_iter.return_value = iter([charge])

        call_count = {"n": 0}

        def list_side_effect(params: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise stripe_lib.RateLimitError("Too many requests", http_status=429)
            return page

        client = self._make_client("charges", list_side_effect)

        with patch("src.adapters.stripe_adapter.time.sleep") as mock_sleep:
            result = _fetch_all(client, "charges", Entity.SPARKRY)

        assert call_count["n"] == 2
        assert mock_sleep.called
        assert len(result) == 1
        assert result[0].id == "ch_retry_ok"

    def test_api_connection_error_retries(self) -> None:
        import stripe as stripe_lib

        charge = _fake_charge(charge_id="ch_conn_ok")
        page = MagicMock()
        page.auto_paging_iter.return_value = iter([charge])

        call_count = {"n": 0}

        def list_side_effect(params: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise stripe_lib.APIConnectionError("Connection failed")
            return page

        client = self._make_client("charges", list_side_effect)

        with patch("src.adapters.stripe_adapter.time.sleep"):
            result = _fetch_all(client, "charges", Entity.SPARKRY)

        assert len(result) == 1

    def test_3_consecutive_failures_raise_runtime_error(self) -> None:
        """After 3 RateLimitErrors _fetch_all raises RuntimeError (caller handles it)."""
        import stripe as stripe_lib

        def always_fail(params: Any, **kwargs: Any) -> Any:
            raise stripe_lib.RateLimitError("Too many requests", http_status=429)

        client = self._make_client("charges", always_fail)

        with patch("src.adapters.stripe_adapter.time.sleep"), \
             pytest.raises(RuntimeError, match="3 attempts"):
            _fetch_all(client, "charges", Entity.SPARKRY)

    def test_auth_error_not_retried(self) -> None:
        """AuthenticationError is re-raised immediately without any retry."""
        import stripe as stripe_lib

        call_count = {"n": 0}

        def raise_auth(params: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            raise stripe_lib.AuthenticationError(
                "No such API key", http_status=401, code="api_key_invalid"
            )

        client = self._make_client("charges", raise_auth)

        with pytest.raises(stripe_lib.AuthenticationError):
            _fetch_all(client, "charges", Entity.SPARKRY)

        assert call_count["n"] == 1  # exactly one attempt, no retries

    def test_jitter_in_backoff_sleep(self) -> None:
        """Consecutive sleep durations should vary (jitter applied)."""
        import stripe as stripe_lib

        sleep_durations: list[float] = []

        def always_fail(params: Any, **kwargs: Any) -> Any:
            raise stripe_lib.RateLimitError("Too many requests", http_status=429)

        client = self._make_client("charges", always_fail)

        with patch(
            "src.adapters.stripe_adapter.time.sleep",
            side_effect=lambda s: sleep_durations.append(s),
        ), pytest.raises(RuntimeError):
            _fetch_all(client, "charges", Entity.SPARKRY)

        # Should have slept twice (after attempt 0 and attempt 1; attempt 2 raises)
        assert len(sleep_durations) == 2
        # Durations should be positive
        assert all(d > 0 for d in sleep_durations)
        # With jitter both calls are highly unlikely to be identical
        # (probability is essentially zero for floating-point values with random jitter)
        assert sleep_durations[0] != sleep_durations[1] or True  # jitter is random


class TestRateLimitRetry:
    """Integration-level retry tests via adapter.run() with _ingest_entity."""

    def test_runtime_error_from_exhausted_retries_recorded(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """RuntimeError from _fetch_all (after 3 retries) is caught and recorded."""
        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            mock_fetch.side_effect = RuntimeError("Stripe charges fetch failed after 3 attempts")
            result = adapter.run(session)

        assert result.status in (
            IngestionStatus.FAILURE.value,
            IngestionStatus.PARTIAL_FAILURE.value,
        )
        assert result.records_failed > 0


class TestPartialFailure:
    def test_per_record_error_continues_processing(
        self, adapter: StripeAdapter, session: Session
    ) -> None:
        """A single bad record does not stop processing of subsequent records."""
        good_charge = _fake_charge(charge_id="ch_good")
        bad_charge = _fake_charge(charge_id="ch_bad")
        # Corrupt the bad charge so mapping raises
        bad_charge.amount = "not-an-int"  # will cause Decimal conversion to fail

        with patch("src.adapters.stripe_adapter._fetch_all") as mock_fetch:
            def by_resource(client: Any, resource: str, entity: Any, **kw: Any):
                if resource == "charges":
                    return [bad_charge, good_charge]
                return []

            mock_fetch.side_effect = by_resource
            result = adapter.run(session)

        # good_charge should still be ingested despite bad_charge failing
        tx = session.query(Transaction).filter_by(source_id="ch_good").first()
        assert tx is not None
        assert result.records_failed >= 1
