"""Tests for src/utils/reclassify.py.

accounting#85 review round 3 (REQ-GMOBJ-01): the reclassify path re-extracts a
vendor from the forwarded body and writes it over the description. That write
bypassed the adapter's ``[object Object]`` sanitisation, so a corrupted payload
that was cleaned at ingest got the literal written back on the next
``reclassify_all`` run.
"""

from __future__ import annotations

from decimal import Decimal

from src.adapters.gmail_n8n import UNKNOWN_VENDOR
from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.utils.reclassify import _update_forwarded_vendor

_SELF = "Travis Sparks <travis@sparkry.com>"


def _tx(body_text: str, description: str) -> Transaction:
    return Transaction(
        id="0" * 32,
        source=Source.GMAIL_N8N.value,
        source_id="msg-1",
        source_hash="hash-1",
        date="2026-01-10",
        description=description,
        amount=Decimal("-25.00"),
        currency="USD",
        entity=Entity.SPARKRY.value,
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
        status=TransactionStatus.NEEDS_REVIEW.value,
        confidence=0.0,
        raw_data={"from": _SELF, "body_text": body_text},
    )


class TestReclassifyNeverStoresObjectObject:
    def test_object_object_forwarded_vendor_is_not_written(self) -> None:
        tx = _tx(
            "---------- Forwarded message ---------\n"
            "From: [object Object] <billing@vendor.com>\n",
            description="vendor.com",
        )
        changed = _update_forwarded_vendor(tx)
        assert "[object Object]" not in (tx.description or "")
        assert tx.description != "[object Object]"
        # The sanitised ingest description must survive.
        assert tx.description == "vendor.com"
        assert changed is False

    def test_nested_object_object_variant_is_not_written(self) -> None:
        tx = _tx(
            "---------- Forwarded message ---------\n"
            "From: [object object],[object Object] <ap@acme.com>\n",
            description=UNKNOWN_VENDOR,
        )
        _update_forwarded_vendor(tx)
        assert "object" not in (tx.description or "").lower() or (
            tx.description == UNKNOWN_VENDOR
        )
        assert "[object" not in (tx.description or "").lower()

    def test_clean_forwarded_vendor_still_updates(self) -> None:
        tx = _tx(
            "---------- Forwarded message ---------\n"
            "From: Acme Billing <ap@acme.com>\n",
            description="gmail.com",
        )
        changed = _update_forwarded_vendor(tx)
        assert changed is True
        assert tx.description == "Acme Billing"
