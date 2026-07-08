"""Auto-confirm policy (REQ-MCA-002/003, spec §2).

``auto_confirm_if_eligible`` promotes an ``auto_classified`` transaction to
``confirmed`` iff it was classified by a Tier-1 vendor rule of confidence
>= 0.90. This is the ONLY sanctioned automated confirm. It never touches
``vendor_rules`` — no confidence bump, no examples bump, no upsert (spec §2.3):
only human confirms feed the learning loop, so a rule can only ever *lose*
auto-confirm eligibility (via the REQ-FIX-ING-004 corrected-learning reset),
never gain it from its own auto-confirms.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.models.audit_event import AuditEvent
from src.models.enums import TransactionStatus
from src.models.vendor_rule import VendorRule

if TYPE_CHECKING:
    from src.classification.engine import ClassificationResult
    from src.models.transaction import Transaction

# Minimum VendorRule.confidence for auto-confirm (spec §2.1). This is the
# *rule* confidence, not the transaction confidence — the distinction the old
# scripts/auto-confirm-high-confidence.py got wrong.
AUTO_CONFIRM_RULE_THRESHOLD = 0.90


def _changed_by(rule_id: str) -> str:
    """The ``changed_by`` / ``confirmed_by`` marker for an auto-confirm.

    Fits ``AuditEvent.changed_by`` / ``Transaction.confirmed_by`` String(64).
    """
    return f"auto:rule:{rule_id}"


def auto_confirm_if_eligible(
    session: Session,
    tx: Transaction,
    result: ClassificationResult,
) -> bool:
    """Confirm *tx* iff it meets every §2.1 conjunct. Returns whether it did.

    Eligible iff ALL hold:
      - ``result.tier_used == 1`` (Tier-2/3 never auto-confirm)
      - ``result.rule_id is not None`` and the VendorRule exists
      - that VendorRule's ``confidence >= 0.90``
      - ``result.status == AUTO_CLASSIFIED`` (a sign-veto → NEEDS_REVIEW
        disqualifies even at high confidence)
      - ``tx.amount is not None``
      - ``tx.parent_id is None`` (split children never auto-confirm)
      - ``tx.status == "auto_classified"``
      - ``tx.entity`` / ``tx.tax_category`` / ``tx.direction`` all set

    On eligible: sets ``status=confirmed`` and ``confirmed_by=auto:rule:<id>``,
    and appends two transaction-mode AuditEvent rows (``status`` and
    ``confirmed_by``). Does NOT commit — the caller owns the transaction
    boundary. Ineligible → returns ``False`` and mutates nothing.
    """
    if result.tier_used != 1:
        return False
    if result.rule_id is None:
        return False
    if result.status != TransactionStatus.AUTO_CLASSIFIED:
        return False
    if tx.amount is None:
        return False
    if tx.parent_id is not None:
        return False
    if tx.status != TransactionStatus.AUTO_CLASSIFIED.value:
        return False
    if not (tx.entity and tx.tax_category and tx.direction):
        return False

    rule = session.get(VendorRule, result.rule_id)
    if rule is None:
        return False
    if rule.confidence < AUTO_CONFIRM_RULE_THRESHOLD:
        return False

    marker = _changed_by(rule.id)
    prior_confirmed_by = tx.confirmed_by

    # A new (unflushed) Transaction has no PK yet — the id default fires only at
    # INSERT. Assign it now so the two AuditEvent FKs point at a real id (the
    # value survives flush because the column default only fills a NULL id).
    if tx.id is None:
        tx.id = str(uuid.uuid4())

    tx.status = TransactionStatus.CONFIRMED.value
    tx.confirmed_by = marker

    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed="status",
            old_value=TransactionStatus.AUTO_CLASSIFIED.value,
            new_value=TransactionStatus.CONFIRMED.value,
            changed_by=marker,
        )
    )
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed="confirmed_by",
            old_value=prior_confirmed_by,
            new_value=marker,
            changed_by=marker,
        )
    )
    return True
