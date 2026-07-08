"""Promotion ledger + qdecide-gated flip (REQ-VIS-003).

``record_cycle`` maintains the per-institution consecutive-clean counter: an
*equal-or-better* run (``DiffReport.clean`` — zero mismatches, ``vision_only``
extras allowed, ``legacy_only`` misses NOT) increments it; any dirty run resets
it to 0. At ``consecutive_clean >= 3`` the CLI declares the institution eligible
— **but the flip is never automatic.** ``record_cycle`` NEVER sets
``promoted=True``.

``promote`` is the manual, qdecide-gated flip (``--decision-ref <id>``): it sets
``promoted`` (or clears it with ``--revoke``), stamps ``promoted_at`` +
``decision_ref``, and writes an entity-mode ``AuditEvent``
(``entity_type="vision_promotion"``, field ``promoted``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.models.audit_event import AuditEvent
from src.models.vision_promotion import VisionPromotion
from src.vision.extract import INSTITUTIONS

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.vision.diff import DiffReport

ENTITY_TYPE_VISION_PROMOTION = "vision_promotion"
PROMOTION_THRESHOLD = 3


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _get_or_create(session: Session, institution: str) -> VisionPromotion:
    row = session.get(VisionPromotion, institution)
    if row is None:
        row = VisionPromotion(institution=institution)
        session.add(row)
        session.flush()
    return row


def record_cycle(
    session: Session,
    institution: str,
    diff_report: DiffReport,
    report_path: str | None,
) -> VisionPromotion:
    """Record one shadow cycle's outcome and update the clean counter.

    Equal-or-better (``diff_report.clean``) → increment ``consecutive_clean``;
    any dirty run → reset to 0. Always updates ``last_cycle_at`` /
    ``last_report_path``. NEVER touches ``promoted`` (no auto-flip).
    """
    row = _get_or_create(session, institution)
    if diff_report.clean:
        row.consecutive_clean = (row.consecutive_clean or 0) + 1
    else:
        row.consecutive_clean = 0
    row.last_cycle_at = _now()
    row.last_report_path = report_path
    session.flush()
    return row


def is_eligible(row: VisionPromotion) -> bool:
    """True once the institution has 3+ consecutive equal-or-better cycles."""
    return (row.consecutive_clean or 0) >= PROMOTION_THRESHOLD and not row.promoted


def promote(
    session: Session,
    institution: str,
    *,
    decision_ref: str,
    revoke: bool = False,
    changed_by: str = "human",
) -> VisionPromotion:
    """Manually flip (or revoke) the promotion for *institution*.

    Sets ``promoted = not revoke``, stamps ``promoted_at`` + ``decision_ref``,
    and writes an entity-mode ``AuditEvent`` for the ``promoted`` field. This is
    the ONLY code path that changes ``promoted``.
    """
    row = _get_or_create(session, institution)
    old_value = bool(row.promoted)
    new_value = not revoke

    row.promoted = new_value
    row.promoted_at = _now()
    row.decision_ref = decision_ref

    session.add(
        AuditEvent(
            entity_id=institution,
            entity_type=ENTITY_TYPE_VISION_PROMOTION,
            field_changed="promoted",
            old_value=str(old_value),
            new_value=str(new_value),
            changed_by=changed_by,
        )
    )
    session.flush()
    return row


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.vision.promote",
        description="Manually promote/revoke a vision institution (qdecide-gated).",
    )
    p.add_argument("institution", choices=list(INSTITUTIONS))
    p.add_argument(
        "--decision-ref",
        required=True,
        help="qdecide decision reference recorded on the promotion.",
    )
    p.add_argument(
        "--revoke",
        action="store_true",
        help="Demote (clear promoted) instead of promoting.",
    )
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — CLI wiring
    args = _build_arg_parser().parse_args(argv)
    try:
        from src.db.connection import get_session
    except ImportError as exc:
        print(f"cannot import DB session factory: {exc}", file=sys.stderr)
        return 1

    with get_session() as session:
        row = promote(
            session,
            args.institution,
            decision_ref=args.decision_ref,
            revoke=args.revoke,
        )
        session.commit()
        verb = "REVOKED" if args.revoke else "PROMOTED"
        print(f"{verb} {row.institution}: promoted={row.promoted} ref={row.decision_ref}")
    return 0


__all__ = [
    "ENTITY_TYPE_VISION_PROMOTION",
    "PROMOTION_THRESHOLD",
    "is_eligible",
    "promote",
    "record_cycle",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
