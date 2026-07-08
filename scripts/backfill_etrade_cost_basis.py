"""REQ-FIX-WLT-003 (spec §3.3): one-shot backfill of E*TRADE ``cost_basis``.

Legacy E*TRADE ``PositionSnapshot`` rows were written with ``cost_basis=None``
even though ``avg_cost_basis`` and ``quantity`` were present, so holdings views
computed unrealized gain against a $0 basis (phantom ~100% gains). This script
backfills ``cost_basis = (avg_cost_basis × quantity).quantize(0.01)`` **in
place** — a pure derivation of already-stored fields, so an audited UPDATE is
safe (see the spec's "annotate, don't supersede" decision).

Guarantees (CLAUDE.md invariants):

* **DRY-RUN by default** — pass ``--apply`` to write.
* **E*TRADE only** (``Account.broker == 'etrade'``).
* **Never mutates ``raw_data``** and never touches ``as_of`` (no history rewrite).
* **One AuditEvent per changed row** (entity mode): ``entity_id=<snapshot.id>``,
  ``entity_type='position_snapshot'``, ``field_changed='cost_basis'``,
  ``changed_by='script:etrade_cost_basis_backfill'``, ``old_value=None``,
  ``new_value=str(cost_basis)``.
* **Idempotent** — rows that already carry a ``cost_basis`` are skipped by the
  query predicate, so re-running writes nothing.
* **Per-record error isolation** — one bad row never halts the batch.

Usage::

    python -m scripts.backfill_etrade_cost_basis            # dry-run
    python -m scripts.backfill_etrade_cost_basis --apply    # write
"""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.brokerage import Account, PositionSnapshot  # noqa: E402
from src.models.enums import Broker  # noqa: E402

logger = logging.getLogger(__name__)

_CHANGED_BY = "script:etrade_cost_basis_backfill"
# Entity-mode discriminator for the audited snapshot (a PositionSnapshot is not
# a transaction, so the audit row uses entity_id + entity_type).
_ENTITY_TYPE_POSITION_SNAPSHOT = "position_snapshot"
_CENTS = Decimal("0.01")


def backfill(session: Session, *, apply: bool) -> dict[str, int]:
    """Backfill E*TRADE ``cost_basis`` in place. Returns count summary.

    Selects only rows where ``cost_basis IS NULL AND avg_cost_basis IS NOT NULL
    AND quantity IS NOT NULL`` on E*TRADE accounts — so a re-run is a no-op.
    """
    examined = 0
    changed = 0
    errors = 0

    stmt = (
        select(PositionSnapshot)
        .join(Account, PositionSnapshot.account_id == Account.id)
        .where(
            Account.broker == Broker.ETRADE.value,
            PositionSnapshot.cost_basis.is_(None),
            PositionSnapshot.avg_cost_basis.is_not(None),
            PositionSnapshot.quantity.is_not(None),
        )
    )

    for snap in session.execute(stmt).scalars():
        examined += 1
        try:
            # Values already round-trip through Numeric → Decimal; guard the
            # multiply defensively so one odd row can't halt the batch.
            cost_basis = (
                Decimal(str(snap.avg_cost_basis)) * Decimal(str(snap.quantity))
            ).quantize(_CENTS)
        except (InvalidOperation, ValueError, TypeError) as exc:
            errors += 1
            logger.warning(
                "skip snapshot=%s: cost_basis derivation failed: %s", snap.id, exc
            )
            continue

        changed += 1
        if apply:
            snap.cost_basis = cost_basis
            # raw_data is intentionally NOT touched.
            session.add(
                AuditEvent(
                    entity_id=snap.id,
                    entity_type=_ENTITY_TYPE_POSITION_SNAPSHOT,
                    field_changed="cost_basis",
                    old_value=None,
                    new_value=str(cost_basis),
                    changed_by=_CHANGED_BY,
                )
            )

    if apply:
        session.commit()

    return {"examined": examined, "changed": changed, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="REQ-FIX-WLT-003 E*TRADE cost_basis backfill (DRY-RUN default)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes + AuditEvents. Default is dry-run (read-only).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Starting E*TRADE cost_basis backfill (%s)", mode)

    from src.db.connection import SessionLocal  # late import keeps tests light

    session = SessionLocal()
    try:
        result = backfill(session, apply=args.apply)
    finally:
        session.close()

    logger.info(
        "Done. examined=%d changed=%d errors=%d",
        result["examined"],
        result["changed"],
        result["errors"],
    )
    if not args.apply and result["changed"]:
        logger.warning(
            "DRY-RUN: %d rows would change. Re-run with --apply.", result["changed"]
        )
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
