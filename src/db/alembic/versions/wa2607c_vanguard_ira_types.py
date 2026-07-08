"""Vanguard IRA account_type / tax_sheltered data fix

Revision ID: wa2607c_vanguard_ira_types
Revises: wa2607b_account_alias
Create Date: 2026-07-07 00:00:06.000000

REQ-FIX-DAT-001 (wealth design §10). Audited data migration: the four Vanguard
retirement accounts were mis-typed ``taxable``/``tax_sheltered=0``. Keyed on
``broker='vanguard'`` + exact ``account_name`` (NOT hardcoded ids):

    Amy IRA             -> trad_ira, tax_sheltered=1
    Amy Roth IRA        -> roth_ira, tax_sheltered=1
    Travis Vanguard IRA -> trad_ira, tax_sheltered=1
    Travis Roth IRA     -> roth_ira, tax_sheltered=1

Fails loudly if any name matches 0 or >1 rows. Writes one entity-mode
``AuditEvent`` per *actually-changed* field (``entity_type='account'``,
``changed_by='migration:wa2607c'``), capturing the prior value in ``old_value``.

Downgrade (append-only audit): reads back each upgrade event and restores the
recorded ``old_value``, appending reversal events tagged
``changed_by='migration:wa2607c:downgrade'``. Guarded on the current value
differing so a re-run restores nothing. The upgrade's own events are never
deleted (audit is append-only in both directions).

Chains onto ``wa2607b_account_alias`` (in-branch predecessor) per the ledger.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "wa2607c_vanguard_ira_types"
down_revision: str | None = "wa2607b_account_alias"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE_ACTOR = "migration:wa2607c"
_DOWNGRADE_ACTOR = "migration:wa2607c:downgrade"

# (account_name, account_type, tax_sheltered) — matched by name + broker=vanguard.
_TARGETS: tuple[tuple[str, str, int], ...] = (
    ("Amy IRA", "trad_ira", 1),
    ("Amy Roth IRA", "roth_ira", 1),
    ("Travis Vanguard IRA", "trad_ira", 1),
    ("Travis Roth IRA", "roth_ira", 1),
)

_SELECT_ONE = sa.text(
    "SELECT id, account_type, tax_sheltered FROM account "
    "WHERE broker = 'vanguard' AND account_name = :name"
)
_UPDATE_TYPE = sa.text(
    "UPDATE account SET account_type = :val, updated_at = :now WHERE id = :id"
)
_UPDATE_SHELTERED = sa.text(
    "UPDATE account SET tax_sheltered = :val, updated_at = :now WHERE id = :id"
)
_INSERT_AUDIT = sa.text(
    "INSERT INTO audit_events "
    "(id, transaction_id, entity_id, entity_type, field_changed, "
    " old_value, new_value, changed_by, changed_at) "
    "VALUES (:id, NULL, :eid, 'account', :field, :old, :new, :by, :at)"
)
# Upgrade events for this migration, joined to the current account row so the
# downgrade can restore only fields still holding the migrated value.
_SELECT_UPGRADE_EVENTS = sa.text(
    "SELECT entity_id, field_changed, old_value, new_value FROM audit_events "
    "WHERE changed_by = :by AND entity_type = 'account'"
)


def _now_str() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")


def _insert_audit(
    bind: sa.engine.Connection,
    *,
    eid: str,
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
            "eid": eid,
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
    changed_fields = 0
    for name, target_type, target_sheltered in _TARGETS:
        rows = bind.execute(_SELECT_ONE, {"name": name}).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"[{revision}] expected exactly 1 vanguard account named {name!r}, "
                f"found {len(rows)} — refusing to guess"
            )
        acct_id, cur_type, cur_sheltered = rows[0]
        if cur_type != target_type:
            bind.execute(_UPDATE_TYPE, {"val": target_type, "now": now, "id": acct_id})
            _insert_audit(
                bind,
                eid=acct_id,
                field="account_type",
                old=str(cur_type),
                new=target_type,
                by=_UPGRADE_ACTOR,
                at=now,
            )
            changed_fields += 1
        # tax_sheltered stored as 0/1 in SQLite — normalise to int for comparison.
        if int(cur_sheltered or 0) != target_sheltered:
            bind.execute(
                _UPDATE_SHELTERED, {"val": target_sheltered, "now": now, "id": acct_id}
            )
            _insert_audit(
                bind,
                eid=acct_id,
                field="tax_sheltered",
                old=str(int(cur_sheltered or 0)),
                new=str(target_sheltered),
                by=_UPGRADE_ACTOR,
                at=now,
            )
            changed_fields += 1
    print(
        f"[{revision}] corrected {changed_fields} field(s) across "
        f"{len(_TARGETS)} vanguard retirement account(s)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    now = _now_str()
    restored = 0
    events = bind.execute(_SELECT_UPGRADE_EVENTS, {"by": _UPGRADE_ACTOR}).fetchall()
    for eid, field, old_value, new_value in events:
        # Idempotency: only restore a field still holding the migrated value.
        cur = bind.execute(
            sa.text(f"SELECT {field} FROM account WHERE id = :id"), {"id": eid}
        ).scalar()
        cur_norm = str(int(cur or 0)) if field == "tax_sheltered" else str(cur)
        if cur_norm != str(new_value):
            continue
        if field == "account_type":
            bind.execute(_UPDATE_TYPE, {"val": old_value, "now": now, "id": eid})
        else:  # tax_sheltered
            bind.execute(
                _UPDATE_SHELTERED,
                {"val": int(old_value or 0), "now": now, "id": eid},
            )
        _insert_audit(
            bind,
            eid=eid,
            field=field,
            old=str(new_value),
            new=str(old_value),
            by=_DOWNGRADE_ACTOR,
            at=now,
        )
        restored += 1
    print(f"[{revision}] downgrade restored {restored} field(s) (upgrade audit preserved)")
