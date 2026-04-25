"""Create Stripe payment links for invoices.

Idempotent: if the invoice already has a payment link, the existing one is returned.
Uses Decimal arithmetic for cent conversion to avoid float rounding errors.
"""

import os
from dataclasses import dataclass
from decimal import Decimal

import stripe


@dataclass
class PaymentLinkResult:
    url: str
    link_id: str


def create_payment_link(invoice) -> PaymentLinkResult:
    """Create a Stripe payment link for an invoice, or reuse existing one."""
    # Idempotent: reuse if already created
    if invoice.payment_link_url and invoice.payment_link_id:
        return PaymentLinkResult(url=invoice.payment_link_url, link_id=invoice.payment_link_id)

    if invoice.total <= 0:
        raise ValueError(f"Invoice total must be positive, got {invoice.total}")

    stripe.api_key = os.environ.get("STRIPE_RESTRICTED_KEY", "")

    metadata = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "customer_id": invoice.customer_id,
    }

    product = stripe.Product.create(
        name=f"Sparkry AI LLC Invoice {invoice.invoice_number}",
        metadata=metadata,
    )

    unit_amount = int(invoice.total * Decimal("100"))

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

    return PaymentLinkResult(url=payment_link.url, link_id=payment_link.id)
