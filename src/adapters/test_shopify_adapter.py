"""Tests for the Shopify adapter.

REQ-ID: ADAPTER-SHOPIFY-001  Pulls orders from Shopify Admin REST API.
REQ-ID: ADAPTER-SHOPIFY-002  Pulls payouts from Shopify Payments API.
REQ-ID: ADAPTER-SHOPIFY-003  All transactions auto-tagged as BlackLine entity.
REQ-ID: ADAPTER-SHOPIFY-004  Deduplicates by source_hash — safe to re-run.
REQ-ID: ADAPTER-SHOPIFY-005  Auth failures (401/403) halt immediately.
REQ-ID: ADAPTER-SHOPIFY-006  Transient failures (429/5xx) retry with backoff.
REQ-ID: ADAPTER-SHOPIFY-007  Minimum 500 ms delay between API calls.
REQ-ID: ADAPTER-SHOPIFY-008  Creates IngestionLog entry for every run.
REQ-ID: ADAPTER-SHOPIFY-009  Per-record error isolation — one bad record never halts a batch.
REQ-ID: ADAPTER-SHOPIFY-010  raw_data preserved verbatim from source.

All tests use in-memory SQLite and mock httpx calls.
"""

from __future__ import annotations

import decimal
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.shopify_adapter import (
    ShopifyAdapter,
    ShopifyAuthError,
    _parse_order,
    _parse_payout,
)
from src.models.base import Base
from src.models.enums import Direction, Entity, Source, TaxCategory
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test function."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def adapter() -> ShopifyAdapter:
    """Adapter configured with test credentials."""
    return ShopifyAdapter(
        api_key="test-api-key",
        store_url="test-store.myshopify.com",
        min_delay_s=0.0,  # disable rate-limiting in tests
    )


# ---------------------------------------------------------------------------
# Sample API payloads
# ---------------------------------------------------------------------------

SAMPLE_ORDER: dict = {
    "id": 5001234567890,
    "name": "#1042",
    "created_at": "2025-03-01T14:30:00-08:00",
    "processed_at": "2025-03-01T14:30:00-08:00",
    "financial_status": "paid",
    "total_price": "85.00",
    "subtotal_price": "75.00",
    "total_shipping_price_set": {
        "shop_money": {"amount": "10.00", "currency_code": "USD"}
    },
    "total_discounts": "0.00",
    "currency": "USD",
    "customer": {"first_name": "Jane", "last_name": "Doe"},
    "line_items": [
        {
            "title": "BlackLine Jersey",
            "quantity": 1,
            "price": "75.00",
        }
    ],
    "refunds": [],
}

SAMPLE_ORDER_WITH_REFUND: dict = {
    "id": 5001234567891,
    "name": "#1043",
    "created_at": "2025-03-05T10:00:00-08:00",
    "processed_at": "2025-03-05T10:00:00-08:00",
    "financial_status": "refunded",
    "total_price": "85.00",
    "subtotal_price": "75.00",
    "total_shipping_price_set": {
        "shop_money": {"amount": "10.00", "currency_code": "USD"}
    },
    "total_discounts": "0.00",
    "currency": "USD",
    "customer": {"first_name": "John", "last_name": "Smith"},
    "line_items": [
        {"title": "BlackLine Hat", "quantity": 1, "price": "75.00"}
    ],
    "refunds": [
        {
            "id": 9001,
            "created_at": "2025-03-06T09:00:00-08:00",
            "transactions": [
                {
                    "id": 9001001,
                    "kind": "refund",
                    "status": "success",
                    "amount": "85.00",
                    "currency": "USD",
                }
            ],
        }
    ],
}

SAMPLE_PAYOUT: dict = {
    "id": 7000001,
    "status": "paid",
    "date": "2025-03-07",
    "currency": "usd",
    "amount": "210.50",
    "summary": {
        "charges_gross": "250.00",
        "refunds_gross": "-20.00",
        "adjustments_gross": "0.00",
        "charges_fee_gross": "-15.50",
        "refunds_fee_gross": "2.00",
        "adjustments_fee_gross": "0.00",
        "payout_fee_gross": "0.00",
        "reserved_funds_gross": "0.00",
        "total": "216.50",
    },
}


# ---------------------------------------------------------------------------
# Unit tests for pure parsing helpers
# ---------------------------------------------------------------------------


class TestParseOrder:
    def test_basic_order_fields(self) -> None:
        tx = _parse_order(SAMPLE_ORDER)
        assert tx["source"] == Source.SHOPIFY.value
        assert tx["source_id"] == "order_5001234567890"
        assert tx["date"] == "2025-03-01"
        assert tx["amount"] == decimal.Decimal("85.00")
        assert tx["currency"] == "USD"
        assert tx["entity"] == Entity.BLACKLINE.value
        assert tx["direction"] == Direction.INCOME.value
        assert tx["tax_category"] == TaxCategory.SALES_INCOME.value
        assert "#1042" in tx["description"]

    def test_description_includes_customer(self) -> None:
        tx = _parse_order(SAMPLE_ORDER)
        assert "Jane Doe" in tx["description"]

    def test_raw_data_preserved(self) -> None:
        tx = _parse_order(SAMPLE_ORDER)
        assert tx["raw_data"] == SAMPLE_ORDER

    def test_order_with_no_customer(self) -> None:
        order = dict(SAMPLE_ORDER)
        order = {**SAMPLE_ORDER, "customer": None, "id": 9999}
        tx = _parse_order(order)
        assert tx["amount"] == decimal.Decimal("85.00")


class TestParseRefund:
    def test_refund_is_negative(self) -> None:
        refund = SAMPLE_ORDER_WITH_REFUND["refunds"][0]
        order = SAMPLE_ORDER_WITH_REFUND
        from src.adapters.shopify_adapter import _parse_refund
        tx = _parse_refund(refund, order)
        assert tx["amount"] == decimal.Decimal("-85.00")
        assert tx["direction"] == Direction.EXPENSE.value
        assert tx["entity"] == Entity.BLACKLINE.value
        assert tx["source_id"] == "refund_9001"

    def test_refund_date_from_refund_not_order(self) -> None:
        refund = SAMPLE_ORDER_WITH_REFUND["refunds"][0]
        from src.adapters.shopify_adapter import _parse_refund
        tx = _parse_refund(refund, SAMPLE_ORDER_WITH_REFUND)
        assert tx["date"] == "2025-03-06"


class TestParsePayout:
    def test_payout_is_income(self) -> None:
        tx = _parse_payout(SAMPLE_PAYOUT)
        assert tx["amount"] == decimal.Decimal("210.50")
        assert tx["direction"] == Direction.INCOME.value
        assert tx["entity"] == Entity.BLACKLINE.value
        assert tx["source_id"] == "payout_7000001"
        assert tx["date"] == "2025-03-07"

    def test_payout_tax_category(self) -> None:
        tx = _parse_payout(SAMPLE_PAYOUT)
        assert tx["tax_category"] == TaxCategory.SALES_INCOME.value

    def test_payout_raw_data_preserved(self) -> None:
        tx = _parse_payout(SAMPLE_PAYOUT)
        assert tx["raw_data"] == SAMPLE_PAYOUT


# ---------------------------------------------------------------------------
# Integration tests — mocked httpx
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_body: dict) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    return resp


class TestShopifyAdapterRun:
    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_run_inserts_orders_and_payouts(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """Happy path: one order page + one payout page → two Transaction rows.

        When the API response has no Link header, the adapter terminates
        pagination after the first page.  Two GET calls total: one for orders,
        one for payouts.
        """
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        # Single orders page (no Link header → pagination ends)
        orders_resp = _make_response(200, {"orders": [SAMPLE_ORDER]})
        orders_resp.headers = {}

        # Single payouts page (no Link header → pagination ends)
        payouts_resp = _make_response(200, {"payouts": [SAMPLE_PAYOUT]})
        payouts_resp.headers = {}

        client_instance.get.side_effect = [orders_resp, payouts_resp]

        result = adapter.run(session)

        assert result.records_created == 2  # 1 order + 1 payout
        assert result.records_failed == 0
        txs = session.query(Transaction).all()
        assert len(txs) == 2

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_run_creates_ingestion_log(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """An IngestionLog row is written for every run."""
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        empty_orders = _make_response(200, {"orders": []})
        empty_orders.headers = {}
        empty_payouts = _make_response(200, {"payouts": []})
        empty_payouts.headers = {}
        client_instance.get.side_effect = [empty_orders, empty_payouts]

        adapter.run(session)

        logs = session.query(IngestionLog).all()
        assert len(logs) == 1
        assert logs[0].source == Source.SHOPIFY.value

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_dedup_second_run_skips(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """Running the adapter twice does not insert duplicate rows."""
        def make_side_effects():
            o1 = _make_response(200, {"orders": [SAMPLE_ORDER]})
            o1.headers = {}
            p1 = _make_response(200, {"payouts": [SAMPLE_PAYOUT]})
            p1.headers = {}
            return [o1, p1]

        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        client_instance.get.side_effect = make_side_effects()

        adapter.run(session)
        first_count = session.query(Transaction).count()

        # Reset side effects for second run
        client_instance.get.side_effect = make_side_effects()
        result2 = adapter.run(session)

        assert session.query(Transaction).count() == first_count
        assert result2.records_created == 0
        assert result2.records_skipped == 2

    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_auth_failure_raises(
        self, mock_client_cls: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """401/403 responses raise ShopifyAuthError immediately."""
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        auth_fail = _make_response(401, {"errors": "Unauthorized"})
        auth_fail.headers = {}
        client_instance.get.return_value = auth_fail

        with pytest.raises(ShopifyAuthError):
            adapter.run(session)

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_refund_ingested_as_negative(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """Orders with refunds produce a negative-amount refund transaction."""
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        o1 = _make_response(200, {"orders": [SAMPLE_ORDER_WITH_REFUND]})
        o1.headers = {}
        p1 = _make_response(200, {"payouts": []})
        p1.headers = {}
        client_instance.get.side_effect = [o1, p1]

        adapter.run(session)

        txs = session.query(Transaction).order_by(Transaction.amount).all()
        amounts = [t.amount for t in txs]
        # One positive (order) + one negative (refund)
        assert any(a < 0 for a in amounts)
        assert any(a > 0 for a in amounts)

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_all_transactions_tagged_blackline(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """Every transaction has entity == blackline."""
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        o1 = _make_response(200, {"orders": [SAMPLE_ORDER]})
        o1.headers = {}
        p1 = _make_response(200, {"payouts": [SAMPLE_PAYOUT]})
        p1.headers = {}
        client_instance.get.side_effect = [o1, p1]

        adapter.run(session)

        txs = session.query(Transaction).all()
        assert all(t.entity == Entity.BLACKLINE.value for t in txs)

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_per_record_error_isolation(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """A malformed order does not prevent a good order from being inserted."""
        bad_order = {"id": 9999, "total_price": "not-a-number", "name": "#bad"}
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        o1 = _make_response(200, {"orders": [bad_order, SAMPLE_ORDER]})
        o1.headers = {}
        p1 = _make_response(200, {"payouts": []})
        p1.headers = {}
        client_instance.get.side_effect = [o1, p1]

        result = adapter.run(session)

        assert result.records_failed == 1
        assert result.records_created == 1  # good order inserted

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_transient_error_retries(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """429 responses are retried up to 3 times before failing."""
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        rate_limited = _make_response(429, {"errors": "Too Many Requests"})
        rate_limited.headers = {"Retry-After": "1"}
        good_resp = _make_response(200, {"orders": []})
        good_resp.headers = {}
        payouts_resp = _make_response(200, {"payouts": []})
        payouts_resp.headers = {}

        # First call → 429, second call → 200, then payouts
        client_instance.get.side_effect = [rate_limited, good_resp, payouts_resp]

        result = adapter.run(session)
        # Should succeed after retry
        assert result.records_failed == 0

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_source_property(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, adapter: ShopifyAdapter
    ) -> None:
        assert adapter.source == Source.SHOPIFY.value

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_raw_data_preserved(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session, adapter: ShopifyAdapter
    ) -> None:
        """raw_data contains the original Shopify API object verbatim."""
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        o1 = _make_response(200, {"orders": [SAMPLE_ORDER]})
        o1.headers = {}
        p1 = _make_response(200, {"payouts": []})
        p1.headers = {}
        client_instance.get.side_effect = [o1, p1]

        adapter.run(session)

        tx = session.query(Transaction).filter(
            Transaction.source_id == "order_5001234567890"
        ).one()
        assert tx.raw_data["id"] == SAMPLE_ORDER["id"]
        assert tx.raw_data["name"] == "#1042"

    @patch("src.adapters.shopify_adapter.time.sleep")
    @patch("src.adapters.shopify_adapter.httpx.Client")
    def test_rate_limit_delay_called(
        self, mock_client_cls: MagicMock, mock_sleep: MagicMock, session: Session
    ) -> None:
        """Adapter with min_delay_s > 0 calls time.sleep between API requests."""
        adapter_with_delay = ShopifyAdapter(
            api_key="key",
            store_url="test.myshopify.com",
            min_delay_s=0.5,
        )
        client_instance = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        o1 = _make_response(200, {"orders": []})
        o1.headers = {}
        p1 = _make_response(200, {"payouts": []})
        p1.headers = {}
        client_instance.get.side_effect = [o1, p1]

        adapter_with_delay.run(session)

        assert mock_sleep.called
        # At least one call with a value >= 0.5
        sleep_vals = [c.args[0] for c in mock_sleep.call_args_list]
        assert any(v >= 0.5 for v in sleep_vals)
