"""Shopify payout reclassification backfill (REQ-FIX-TAX-001).

Revision ID: tax001_shopify_payout_backfill
Revises: inv002_payment_link_amount
Create Date: 2026-07-07 00:00:03.000000

WS2 backfill for the forward fix in ``shopify_adapter._parse_payout`` (which now
writes ``direction=transfer``/``tax_category=None``). Historical Shopify payout
rows were booked ``direction=income``/``tax_category=SALES_INCOME`` — double-
counting every dollar once as the order and again as the settling payout, which
inflated WA B&O gross and Form 1065 L1a. This migration reclassifies the existing
rows to match the fixed adapter.

Selection
    ``source='shopify' AND source_id LIKE 'payout_%' AND direction='income'``.
    The ``direction='income'`` predicate is what makes the upgrade idempotent:
    after the flip the same query selects zero rows, so a re-run is a no-op. It
    also means natively-``transfer`` payouts written by the fixed adapter after
    the deploy are never touched.

Upgrade (status-preserving)
    Per row: ``direction -> 'transfer'``, ``tax_category -> NULL``, bump
    ``updated_at``. ``status``, ``confirmed_by`` and ``raw_data`` are left intact
    — a human-``rejected`` payout stays rejected, ``needs_review`` stays
    ``needs_review``. Two transaction-mode ``AuditEvent`` rows are written per
    transaction (``direction`` income->transfer, ``tax_category`` <old>->NULL),
    ``changed_by='migration:tax001_shopify_payout_backfill'``. Inserts use
    ``transaction_id`` with NULL ``entity_id``/``entity_type`` so
    ``ck_audit_events_exactly_one_target`` is satisfied. The affected row count is
    printed for the runbook.

Downgrade (append-only — compensating events, NEVER delete)
    Audit is append-only in *both* directions (design spec §2.2; the
    ``alembic-migration`` skill checker flags any raw ``DELETE FROM`` on the audit
    trail as P0; CLAUDE.md forbids deleting audit rows). So the downgrade does
    NOT delete the upgrade's events. Instead it locates each transaction this
    migration flipped via its own upgrade audit events, restores
    ``direction``/``tax_category`` from the recorded ``old_value``, and appends
    *reversal* AuditEvents tagged
    ``changed_by='migration:tax001_shopify_payout_backfill:downgrade'``. Guarded
    on the row still being ``direction='transfer'`` so a re-run restores nothing.

Cross-workstream migration ledger: this revision chains onto the current head
``inv002_payment_link_amount`` (verified with ``alembic heads``), keeping the
chain linear per the ledger note in that revision's docstring.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "tax001_shopify_payout_backfill"
down_revision: str | None = "inv002_payment_link_amount"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# changed_by tags — distinct so the downgrade join never mistakes a compensating
# reversal event for an upgrade event.
_UPGRADE_ACTOR = "migration:tax001_shopify_payout_backfill"
_DOWNGRADE_ACTOR = "migration:tax001_shopify_payout_backfill:downgrade"

_SELECT_PAYOUTS = sa.text(
    "SELECT id, tax_category FROM transactions "
    "WHERE source = 'shopify' AND source_id LIKE 'payout_%' "
    "AND direction = 'income'"
)

_FLIP_TO_TRANSFER = sa.text(
    "UPDATE transactions SET direction = 'transfer', tax_category = NULL, "
    "updated_at = :now WHERE id = :id"
)

_RESTORE_TO_INCOME = sa.text(
    "UPDATE transactions SET direction = 'income', tax_category = :tax_category, "
    "updated_at = :now WHERE id = :id"
)

# Transaction-mode audit row: entity_id/entity_type NULL satisfies
# ck_audit_events_exactly_one_target.
_INSERT_AUDIT = sa.text(
    "INSERT INTO audit_events "
    "(id, transaction_id, entity_id, entity_type, field_changed, "
    " old_value, new_value, changed_by, changed_at) "
    "VALUES (:id, :tx, NULL, NULL, :field, :old, :new, :by, :at)"
)

# Rows this migration flipped, recovered from its own upgrade audit events. The
# tax_category upgrade event's old_value is the original category to restore. The
# join on direction='transfer' keeps the downgrade idempotent and avoids touching
# payouts not flipped by this migration.
_SELECT_FLIPPED = sa.text(
    "SELECT DISTINCT ae.transaction_id, ae.old_value "
    "FROM audit_events ae "
    "JOIN transactions t ON t.id = ae.transaction_id "
    "WHERE ae.changed_by = :by AND ae.field_changed = 'tax_category' "
    "AND t.direction = 'transfer'"
)


def _now_str() -> str:
    """Naive UTC timestamp in SQLAlchemy's SQLite DateTime string format."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")


def _insert_audit(
    bind: sa.engine.Connection,
    *,
    tx: str,
    field: str,
    old: str | None,
    new: str | None,
    by: str,
    at: str,
) -> None:
    bind.execute(
        _INSERT_AUDIT,
        {
            "id": str(uuid.uuid4()),
            "tx": tx,
            "field": field,
            "old": old,
            "new": new,
            "by": by,
            "at": at,
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    now = _now_str()
    rows = bind.execute(_SELECT_PAYOUTS).fetchall()
    for tx_id, old_tax_category in rows:
        bind.execute(_FLIP_TO_TRANSFER, {"now": now, "id": tx_id})
        _insert_audit(
            bind,
            tx=tx_id,
            field="direction",
            old="income",
            new="transfer",
            by=_UPGRADE_ACTOR,
            at=now,
        )
        _insert_audit(
            bind,
            tx=tx_id,
            field="tax_category",
            old=old_tax_category,
            new=None,
            by=_UPGRADE_ACTOR,
            at=now,
        )
    print(
        f"[{revision}] reclassified {len(rows)} shopify payout(s) "
        "income/SALES_INCOME -> transfer/NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    now = _now_str()
    rows = bind.execute(_SELECT_FLIPPED, {"by": _UPGRADE_ACTOR}).fetchall()
    for tx_id, old_tax_category in rows:
        bind.execute(
            _RESTORE_TO_INCOME,
            {"now": now, "id": tx_id, "tax_category": old_tax_category},
        )
        # Append reversal events — the upgrade's own events are left in place.
        _insert_audit(
            bind,
            tx=tx_id,
            field="direction",
            old="transfer",
            new="income",
            by=_DOWNGRADE_ACTOR,
            at=now,
        )
        _insert_audit(
            bind,
            tx=tx_id,
            field="tax_category",
            old=None,
            new=old_tax_category,
            by=_DOWNGRADE_ACTOR,
            at=now,
        )
    print(
        f"[{revision}] downgrade restored {len(rows)} shopify payout(s) "
        "transfer/NULL -> income/<category> (upgrade audit events preserved)"
    )
