#!/usr/bin/env python3
"""Auto-confirm operations CLI (REQ-MCA-002/003, spec §2.4–2.5).

Subcommands:
  sweep   — re-run Tier-1 lookup over the auto_classified backlog and confirm the
            eligible rows (DRY-RUN default; --apply writes with per-row savepoint).
  digest  — email the trailing-7-day auto-confirm activity, grouped by vendor,
            with per-row undo command lines (DRY-RUN default).
  undo    — revert one auto-confirmed row to needs_review (guarded; never deletes,
            never touches the rule).

Usage:
    python -m scripts.autoconfirm sweep                 # dry-run preview
    python -m scripts.autoconfirm sweep --apply         # confirm eligible rows
    python -m scripts.autoconfirm digest --apply        # email weekly digest
    python -m scripts.autoconfirm undo <tx-id> --apply  # revert one row
"""

from __future__ import annotations

import argparse
import html as _html
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

# Add project root to path when invoked as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.alerts.models import AlertDispatch  # noqa: E402
from src.classification.rules import lookup_vendor_rule  # noqa: E402
from src.close.autoconfirm import auto_confirm_if_eligible  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402
from src.invoicing.email_sender import _FONT_STACK, _format_currency, _validate_email  # noqa: E402
from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.enums import ConfirmedBy, TransactionStatus  # noqa: E402
from src.models.transaction import Transaction  # noqa: E402
from src.models.vendor_rule import VendorRule  # noqa: E402
from src.utils.constants import INVOICE_FROM_ADDRESS  # noqa: E402

logger = logging.getLogger("autoconfirm")

DIGEST_ALERT_TYPE = "autoconfirm_digest"
DELIVERY_CHANNEL = "resend_email"
_AUTO_RULE_PREFIX = "auto:rule:"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _abs(amount: Any) -> Decimal:
    return abs(Decimal(str(amount))) if amount is not None else Decimal("0")


# ── sweep (§2.4) ──────────────────────────────────────────────────────────


@dataclass
class SweepRow:
    tx_id: str
    date: str
    vendor: str
    amount: Decimal
    rule_id: str
    rule_confidence: float


@dataclass
class SweepResult:
    applied: bool
    scanned: int = 0
    confirmed: int = 0
    failed: int = 0
    candidates: list[SweepRow] = field(default_factory=list)


def sweep(session: Session, *, apply: bool = False, limit: int | None = None) -> SweepResult:
    """Confirm eligible auto_classified rows. DRY-RUN rolls back everything.

    P3-002: per-row isolation — one raising row is caught, logged, and
    skipped; it never aborts the rest of the backlog run (mirrors
    src/alerts/sweep.py's per-record-error-isolation pattern). Each confirmed
    row is committed immediately (when ``apply``) rather than batched, so a
    later row's failure-triggered ``session.rollback()`` can never undo an
    earlier row's already-applied confirm.
    """
    query = (
        session.query(Transaction)
        .filter(Transaction.status == TransactionStatus.AUTO_CLASSIFIED.value)
        .order_by(Transaction.date)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = query.all()

    result = SweepResult(applied=apply)
    for tx in rows:
        result.scanned += 1
        try:
            # touch_last_matched=False: the sweep is a read for eligibility
            # only — REQ-MCA-002 forbids auto-confirm from mutating
            # vendor_rules, so the matcher used here must never dirty
            # last_matched (P1-a1c).
            cls = lookup_vendor_rule(tx.description, session, touch_last_matched=False)
            if cls is None:
                continue
            # P2-b2e: confirmed_by=auto:rule:<id> must stay faithful to what
            # got confirmed. auto_confirm_if_eligible only confirms the row's
            # EXISTING stored classification, not cls's — if the current
            # best-matching rule has since diverged from that stored
            # entity/tax_category/direction (edited rule, or the row was
            # originally Tier-2/3), skip rather than attribute the confirm to
            # a rule whose classification doesn't match the recorded row.
            # Leaves the row for the next sweep / human review.
            if (
                cls.entity.value != tx.entity
                or cls.tax_category.value != tx.tax_category
                or cls.direction.value != tx.direction
            ):
                continue
            savepoint = session.begin_nested()
            confirmed = auto_confirm_if_eligible(session, tx, cls)
            if confirmed and cls.rule_id is not None:
                rule = session.get(VendorRule, cls.rule_id)
                result.candidates.append(
                    SweepRow(
                        tx_id=tx.id,
                        date=tx.date,
                        vendor=tx.description,
                        amount=_abs(tx.amount),
                        rule_id=cls.rule_id,
                        rule_confidence=rule.confidence if rule else 0.0,
                    )
                )
                result.confirmed += 1
                if apply:
                    savepoint.commit()
                    # Commit each confirmed row immediately rather than
                    # batching to one commit at the end: a later row's
                    # exception handler calls session.rollback(), which would
                    # otherwise discard every prior row's uncommitted
                    # savepoint-merged change from this same run.
                    session.commit()
                else:
                    savepoint.rollback()
            else:
                savepoint.rollback()
        except Exception:  # noqa: BLE001 — per-record isolation: one bad row
            # never aborts the backlog sweep.
            logger.exception("autoconfirm sweep: row %s raised, skipping", tx.id)
            result.failed += 1
            session.rollback()
            continue

    if apply:
        session.commit()
    else:
        # DRY-RUN: roll back any confirm mutations staged by
        # auto_confirm_if_eligible (lookup_vendor_rule itself no longer
        # touches vendor_rules — touch_last_matched=False above).
        session.rollback()
    return result


def _print_sweep(result: SweepResult) -> None:
    mode = "APPLIED" if result.applied else "DRY-RUN"
    print(
        f"autoconfirm sweep {mode}: scanned={result.scanned} "
        f"confirmed={result.confirmed} failed={result.failed}"
    )
    if result.candidates:
        print(f"{'tx_id':38}  {'date':10}  {'vendor':28}  {'amount':>10}  {'rule':38}  conf")
        for c in result.candidates:
            print(
                f"{c.tx_id:38}  {c.date:10}  {c.vendor[:28]:28}  "
                f"{_format_currency(c.amount):>10}  {c.rule_id:38}  {c.rule_confidence:.2f}"
            )


# ── digest (§2.4) ─────────────────────────────────────────────────────────


@dataclass
class DigestVendor:
    vendor: str
    count: int
    total: Decimal
    tx_ids: list[str]


def collect_digest(session: Session, *, now: datetime | None = None) -> list[DigestVendor]:
    """Group auto-confirms of the trailing 7 days (by AuditEvent status flip) by vendor."""
    now = now or _utcnow()
    cutoff = now - timedelta(days=7)
    events = (
        session.query(AuditEvent.transaction_id)
        .filter(
            AuditEvent.field_changed == "status",
            AuditEvent.new_value == TransactionStatus.CONFIRMED.value,
            AuditEvent.changed_by.like(f"{_AUTO_RULE_PREFIX}%"),
            AuditEvent.changed_at >= cutoff,
        )
        .all()
    )
    tx_ids = {e[0] for e in events if e[0]}
    if not tx_ids:
        return []
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.id.in_(tx_ids),
            Transaction.confirmed_by.like(f"{_AUTO_RULE_PREFIX}%"),
        )
        .all()
    )
    grouped: dict[str, list[Transaction]] = {}
    for r in rows:
        grouped.setdefault(r.description, []).append(r)
    return [
        DigestVendor(
            vendor=vendor,
            count=len(group),
            total=sum((_abs(g.amount) for g in group), Decimal("0")).quantize(Decimal("0.01")),
            tx_ids=[g.id for g in group],
        )
        for vendor, group in sorted(grouped.items())
    ]


def render_digest_html(vendors: list[DigestVendor]) -> str:
    """Pure render of the weekly auto-confirm digest with per-row undo commands."""
    f = _FONT_STACK
    total = sum((v.count for v in vendors), 0)
    parts = [
        f'<tr><td style="padding:16px 12px;font-family:{f};font-size:15px;font-weight:600;'
        f'color:#1d1d1f;">Auto-confirm digest — {total} row(s) in the last 7 days</td></tr>'
    ]
    for v in vendors:
        parts.append(
            f'<tr><td style="padding:6px 12px;font-family:{f};font-size:13px;color:#1d1d1f;">'
            f"<strong>{_html.escape(v.vendor)}</strong>: {v.count}× "
            f"{_format_currency(v.total)}</td></tr>"
        )
        for tx_id in v.tx_ids:
            cmd = f"python -m scripts.autoconfirm undo {tx_id} --apply"
            parts.append(
                f'<tr><td style="padding:0 12px 6px 12px;font-family:monospace;font-size:11px;'
                f'color:#86868b;">{_html.escape(cmd)}</td></tr>'
            )
    if not vendors:
        parts.append(
            f'<tr><td style="padding:6px 12px;font-family:{f};font-size:13px;color:#86868b;">'
            "No auto-confirms in the last 7 days.</td></tr>"
        )
    return (
        '<body style="margin:0;padding:0;background-color:#f5f5f7;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" '
        'style="background-color:#ffffff;border-radius:8px;overflow:hidden;">'
        f'{"".join(parts)}'
        "</table></td></tr></table></body>"
    )


def send_digest(
    session: Session,
    *,
    apply: bool = False,
    now: datetime | None = None,
    to_email: str | None = None,
) -> AlertDispatch | None:
    """Send the weekly digest (DRY-RUN default). Records the ledger row on apply."""
    now = now or _utcnow()
    vendors = collect_digest(session, now=now)
    if not apply:
        return None

    # P1-201: pre-send dedup mirroring src/alerts/dispatcher._already_sent —
    # the UNIQUE constraint only prevents a duplicate ledger ROW; without this
    # check a same-day re-run (systemd retry, operator re-run) would SEND a
    # second real email before colliding on the constraint.
    existing = (
        session.query(AlertDispatch)
        .filter_by(
            alert_key=f"autoconfirm_digest:{now.date().isoformat()}",
            occurrence_date=now.date().isoformat(),
        )
        .one_or_none()
    )
    if existing is not None and existing.status == "sent":
        return existing

    subject = f"Auto-confirm digest — week of {now.date().isoformat()}"
    html_body = render_digest_html(vendors)
    recipient = to_email or os.environ["ALERT_TO_EMAIL"]

    status = "sent"
    http_status: int | None = None
    error_detail: str | None = None
    try:
        _validate_email(recipient)
        import resend

        resend.api_key = os.environ.get("RESEND_API_KEY", "")
        if not resend.api_key:
            raise ValueError("RESEND_API_KEY is not configured")
        params: resend.Emails.SendParams = {
            "from": INVOICE_FROM_ADDRESS,
            "to": [recipient],
            "subject": subject,
            "html": html_body,
        }
        resend.Emails.send(params)
        http_status = 200
    except Exception as exc:  # noqa: BLE001 — a send failure is recorded, not raised
        status = "failed"
        error_detail = f"{type(exc).__name__}: {exc}"

    # A prior FAILED row for today flips in place on retry (mirrors _record);
    # only a fresh day inserts a new row.
    if existing is not None:
        existing.status = status
        existing.http_status = http_status
        existing.error_detail = error_detail
        existing.subject = subject
        session.commit()
        return existing

    row = AlertDispatch(
        alert_key=f"autoconfirm_digest:{now.date().isoformat()}",
        occurrence_date=now.date().isoformat(),
        alert_type=DIGEST_ALERT_TYPE,
        entity="all",
        subject=subject,
        status=status,
        http_status=http_status,
        error_detail=error_detail,
        payload_json=None,
        delivery_channel=DELIVERY_CHANNEL,
    )
    # Mirrors src/alerts/dispatcher.py:_record — savepoint + commit so the
    # ledger row is durable on its own; a concurrent same-day insert collides
    # on UNIQUE(alert_key, occurrence_date) and is treated as already recorded.
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        session.rollback()
    return row


# ── undo (§2.5) ───────────────────────────────────────────────────────────


class UndoError(Exception):
    """A guard on ``undo`` failed (row missing / not an auto-confirm)."""


@dataclass
class UndoResult:
    tx_id: str
    applied: bool
    old_status: str
    old_confirmed_by: str


_UNDO_REASON = "auto-confirm undone by operator"


def undo(session: Session, tx_id: str, *, apply: bool = False) -> UndoResult:
    """Revert one auto-confirmed row to needs_review. Guarded; never deletes."""
    from src.api.routes.tax_year_locks import check_lock

    tx = session.get(Transaction, tx_id)
    if tx is None:
        raise UndoError(f"transaction not found: {tx_id}")
    if not (tx.confirmed_by or "").startswith(_AUTO_RULE_PREFIX):
        raise UndoError(
            f"not an auto-confirm (confirmed_by={tx.confirmed_by!r}); refusing to undo"
        )
    # Raises HTTPException(403) if the transaction's tax year is locked.
    check_lock(session, tx.entity, tx.date)

    old_status = tx.status
    old_reason = tx.review_reason
    old_confirmed_by = tx.confirmed_by

    if apply:
        tx.status = TransactionStatus.NEEDS_REVIEW.value
        tx.review_reason = _UNDO_REASON
        tx.confirmed_by = ConfirmedBy.AUTO.value
        for field_name, old, new in (
            ("status", old_status, TransactionStatus.NEEDS_REVIEW.value),
            ("review_reason", old_reason, _UNDO_REASON),
            ("confirmed_by", old_confirmed_by, ConfirmedBy.AUTO.value),
        ):
            session.add(
                AuditEvent(
                    transaction_id=tx.id,
                    field_changed=field_name,
                    old_value=old,
                    new_value=new,
                    changed_by=ConfirmedBy.HUMAN.value,
                )
            )
        session.commit()

    return UndoResult(
        tx_id=tx_id,
        applied=apply,
        old_status=old_status,
        old_confirmed_by=old_confirmed_by,
    )


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sweep = sub.add_parser("sweep", help="Confirm eligible auto_classified rows.")
    p_sweep.add_argument("--apply", action="store_true")
    p_sweep.add_argument("--limit", type=int, default=None)

    p_digest = sub.add_parser("digest", help="Email the weekly auto-confirm digest.")
    p_digest.add_argument("--apply", action="store_true")

    p_undo = sub.add_parser("undo", help="Revert one auto-confirmed row.")
    p_undo.add_argument("transaction_id")
    p_undo.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    init_db()
    with SessionLocal() as session:
        if args.command == "sweep":
            result = sweep(session, apply=args.apply, limit=args.limit)
            _print_sweep(result)
            return 0
        if args.command == "digest":
            row = send_digest(session, apply=args.apply)
            if row is None:
                vendors = collect_digest(session)
                print(f"autoconfirm digest DRY-RUN: {sum(v.count for v in vendors)} row(s)")
                return 0
            print(f"autoconfirm digest APPLIED: status={row.status}")
            return 1 if row.status == "failed" else 0
        if args.command == "undo":
            try:
                res = undo(session, args.transaction_id, apply=args.apply)
            except UndoError as exc:
                print(f"undo refused: {exc}")
                return 1
            except Exception as exc:  # noqa: BLE001 — e.g. locked tax year (HTTP 403)
                print(f"undo blocked: {exc}")
                return 1
            mode = "APPLIED" if res.applied else "DRY-RUN"
            print(f"undo {mode}: {res.tx_id} ({res.old_status} → needs_review)")
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
