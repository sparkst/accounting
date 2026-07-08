"""Tests for Stripe payment link creation.

Covers: happy path, decimal→cents conversion, idempotent reuse,
validation (zero/negative), Stripe errors, metadata, and restrictions.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe as stripe_lib

from src.invoicing.payment_link import PaymentLinkResult, create_payment_link


def _make_invoice(
    *,
    id: str = "inv-uuid-001",
    invoice_number: str = "202604-001",
    customer_id: str = "cust-uuid-001",
    total: Decimal = Decimal("3300.00"),
    payment_link_url: str | None = None,
    payment_link_id: str | None = None,
    payment_link_amount: Decimal | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        invoice_number=invoice_number,
        customer_id=customer_id,
        total=total,
        payment_link_url=payment_link_url,
        payment_link_id=payment_link_id,
        payment_link_amount=payment_link_amount,
    )


@patch("src.invoicing.payment_link.stripe")
class TestCreatePaymentLink:
    """All tests patch the stripe module used by payment_link."""

    def test_happy_path(self, mock_stripe):
        """Creates Product, Price, PaymentLink and returns PaymentLinkResult."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_abc")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_xyz")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/test_link",
            id="plink_123",
        )

        inv = _make_invoice(total=Decimal("3300.00"))
        result = create_payment_link(inv)

        assert isinstance(result, PaymentLinkResult)
        assert result.url == "https://buy.stripe.com/test_link"
        assert result.link_id == "plink_123"

        # Product created with correct name
        mock_stripe.Product.create.assert_called_once()
        product_kwargs = mock_stripe.Product.create.call_args
        assert product_kwargs.kwargs["name"] == "Sparkry LLC Invoice 202604-001"

        # Price created with correct unit_amount and currency
        mock_stripe.Price.create.assert_called_once()
        price_kwargs = mock_stripe.Price.create.call_args
        assert price_kwargs.kwargs["product"] == "prod_abc"
        assert price_kwargs.kwargs["unit_amount"] == 330000
        assert price_kwargs.kwargs["currency"] == "usd"

        # PaymentLink created with line_items and restrictions
        mock_stripe.PaymentLink.create.assert_called_once()
        link_kwargs = mock_stripe.PaymentLink.create.call_args
        assert link_kwargs.kwargs["line_items"] == [{"price": "price_xyz", "quantity": 1}]
        assert link_kwargs.kwargs["restrictions"] == {"completed_sessions": {"limit": 1}}

    def test_decimal_cents_conversion(self, mock_stripe):
        """Decimal amounts convert to integer cents without float rounding."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_1")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_1")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/link", id="plink_1"
        )

        # $199.99 -> 19999 cents
        inv = _make_invoice(total=Decimal("199.99"))
        create_payment_link(inv)
        assert mock_stripe.Price.create.call_args.kwargs["unit_amount"] == 19999

        mock_stripe.reset_mock()
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_2")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_2")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/link2", id="plink_2"
        )

        # $33,000.00 -> 3300000 cents
        inv = _make_invoice(total=Decimal("33000.00"))
        create_payment_link(inv)
        assert mock_stripe.Price.create.call_args.kwargs["unit_amount"] == 3300000

    def test_idempotent_reuse(self, mock_stripe):
        """If payment link already exists AND its recorded amount still
        matches invoice.total, return it without calling Stripe."""
        inv = _make_invoice(
            total=Decimal("3300.00"),
            payment_link_url="https://buy.stripe.com/existing",
            payment_link_id="plink_existing",
            payment_link_amount=Decimal("3300.00"),
        )
        result = create_payment_link(inv)

        assert result.url == "https://buy.stripe.com/existing"
        assert result.link_id == "plink_existing"
        assert result.amount == Decimal("3300.00")
        mock_stripe.Product.create.assert_not_called()
        mock_stripe.Price.create.assert_not_called()
        mock_stripe.PaymentLink.create.assert_not_called()

    def test_reuse_with_mismatched_amount_recreates(self, mock_stripe):
        """REQ-FIX-INV-002: reuse is only valid when payment_link_amount ==
        invoice.total. If the total changed since the link was created (e.g.
        a PATCH that didn't clear stale fields), a fresh link is minted
        instead of blindly reusing a link for the wrong amount."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_new")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_new")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/fresh", id="plink_fresh"
        )

        inv = _make_invoice(
            total=Decimal("5000.00"),
            payment_link_url="https://buy.stripe.com/existing",
            payment_link_id="plink_existing",
            payment_link_amount=Decimal("3300.00"),  # stale — total changed
        )
        result = create_payment_link(inv)

        assert result.url == "https://buy.stripe.com/fresh"
        assert result.link_id == "plink_fresh"
        assert result.amount == Decimal("5000.00")
        mock_stripe.Product.create.assert_called_once()
        mock_stripe.PaymentLink.create.assert_called_once()

    def test_create_returns_amount(self, mock_stripe):
        """A freshly created link's result carries the invoice total so the
        route can persist all three fields atomically."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_a")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_a")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/a", id="plink_a"
        )
        inv = _make_invoice(total=Decimal("777.00"))
        result = create_payment_link(inv)
        assert result.amount == Decimal("777.00")

    def test_rejects_zero_amount(self, mock_stripe):
        """Zero total raises ValueError."""
        inv = _make_invoice(total=Decimal("0.00"))
        with pytest.raises(ValueError, match="positive"):
            create_payment_link(inv)
        mock_stripe.Product.create.assert_not_called()

    def test_rejects_negative_amount(self, mock_stripe):
        """Negative total raises ValueError."""
        inv = _make_invoice(total=Decimal("-100.00"))
        with pytest.raises(ValueError, match="positive"):
            create_payment_link(inv)
        mock_stripe.Product.create.assert_not_called()

    def test_stripe_error(self, mock_stripe):
        """Stripe API errors propagate as StripeError."""
        mock_stripe.error.StripeError = stripe_lib.error.StripeError
        mock_stripe.Product.create.side_effect = stripe_lib.error.StripeError(
            "Something went wrong"
        )

        inv = _make_invoice()
        with pytest.raises(stripe_lib.error.StripeError):
            create_payment_link(inv)

    def test_metadata_includes_ids(self, mock_stripe):
        """Both Product.create and PaymentLink.create receive metadata with all IDs."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_m")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_m")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/meta", id="plink_m"
        )

        inv = _make_invoice(
            id="uuid-invoice-42",
            invoice_number="HTF-2026-004",
            customer_id="uuid-customer-99",
        )
        create_payment_link(inv)

        expected_metadata = {
            "invoice_id": "uuid-invoice-42",
            "invoice_number": "HTF-2026-004",
            "customer_id": "uuid-customer-99",
        }

        product_meta = mock_stripe.Product.create.call_args.kwargs["metadata"]
        assert product_meta == expected_metadata

        link_meta = mock_stripe.PaymentLink.create.call_args.kwargs["metadata"]
        assert link_meta == expected_metadata

    def test_unit_amount_quantizes_never_truncates(self, mock_stripe):
        """REQ-FIX-INV-005: unit_amount is derived via Decimal quantization
        (ROUND_HALF_UP), never int() truncation. Regression value: a total
        whose cents*100 lands exactly on .5 due to upstream float drift must
        round, not truncate."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_q")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_q")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/q", id="plink_q"
        )
        # 199.995 * 100 = 19999.5 -> ROUND_HALF_UP -> 20000, never int()-truncated to 19999.
        inv = _make_invoice(total=Decimal("199.995"))
        create_payment_link(inv)
        assert mock_stripe.Price.create.call_args.kwargs["unit_amount"] == 20000

    def test_payment_link_restrictions(self, mock_stripe):
        """PaymentLink.create is called with single-use restrictions."""
        mock_stripe.Product.create.return_value = SimpleNamespace(id="prod_r")
        mock_stripe.Price.create.return_value = SimpleNamespace(id="price_r")
        mock_stripe.PaymentLink.create.return_value = SimpleNamespace(
            url="https://buy.stripe.com/restrict", id="plink_r"
        )

        inv = _make_invoice()
        create_payment_link(inv)

        restrictions = mock_stripe.PaymentLink.create.call_args.kwargs["restrictions"]
        assert restrictions == {"completed_sessions": {"limit": 1}}
