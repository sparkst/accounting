"""Create Stripe payment links for invoices."""

import os
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import stripe

CENT = Decimal("0.01")


@dataclass
class PaymentLinkResult:
    url: str
    link_id: str
    amount: Decimal


def create_payment_link(invoice) -> PaymentLinkResult:
    """Create a Stripe payment link for an invoice, or reuse an existing one.

    REQ-FIX-INV-002: reuse is only valid when the invoice's three persisted
    link fields are all set AND ``payment_link_amount`` equals the current
    ``total`` — a link created for a since-changed total is stale and must
    not be handed to a new send. The caller (route) is responsible for
    clearing all three fields whenever the total changes (PATCH) or the
    email send fails after creation (INV-001); this function only decides
    reuse-vs-create from what's currently persisted.

    REQ-FIX-INV-005: cent conversion goes through Decimal quantization
    (ROUND_HALF_UP), never ``int()`` truncation, so the Stripe amount always
    equals the stored, already-quantized invoice total.
    """
    reuse_valid = (
        invoice.payment_link_url
        and invoice.payment_link_id
        and invoice.payment_link_amount is not None
        and Decimal(str(invoice.payment_link_amount)) == Decimal(str(invoice.total))
    )
    if reuse_valid:
        return PaymentLinkResult(
            url=invoice.payment_link_url,
            link_id=invoice.payment_link_id,
            amount=Decimal(str(invoice.payment_link_amount)),
        )

    if invoice.total <= 0:
        raise ValueError(f"Invoice total must be positive, got {invoice.total}")

    stripe.api_key = os.environ.get("STRIPE_RESTRICTED_KEY", "")

    metadata = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "customer_id": invoice.customer_id,
    }

    product = stripe.Product.create(
        name=f"Sparkry LLC Invoice {invoice.invoice_number}",
        metadata=metadata,
    )

    total = Decimal(str(invoice.total))
    unit_amount = int((total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    price = stripe.Price.create(
        product=product.id,
        unit_amount=unit_amount,
        currency="usd",
    )

    payment_link = stripe.PaymentLink.create(
        line_items=[{"price": price.id, "quantity": 1}],
        metadata=metadata,
        restrictions={"completed_sessions": {"limit": 1}},
    )

    return PaymentLinkResult(url=payment_link.url, link_id=payment_link.id, amount=total)
