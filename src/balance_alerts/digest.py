"""Daily account-pulse digest (REQ-BAL-008).

A single `info` message listing every monitored account, its current balance, and
a flag on anything currently in a breached state (below a cash floor / above a
credit ceiling). Sent via the shared n8n severity webhook path, and recorded in
the `alert_dispatch` ledger so a timer flap can't double-send it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.balance_alerts.rules import (
    CHECKING_MILESTONES,
    CREDIT_STEP,
    SAVINGS_FLOOR,
    classify,
)
from src.balance_alerts.webhook import build_payload_dict, post_payload

logger = logging.getLogger(__name__)

# A checking account is "breached" in the pulse when it has dropped into sev3
# territory (≤ $1k) — not merely below the top $10k milestone, which would flag
# almost every account and make the flag meaningless.
_CHECKING_BREACH_FLOOR = CHECKING_MILESTONES[-2]  # $1,000


@dataclass(frozen=True)
class PulseLine:
    account_name: str
    kind: str
    balance: Decimal
    breached: bool


def _breached(kind: str, balance: Decimal) -> bool:
    if kind == "checking":
        return balance <= _CHECKING_BREACH_FLOOR
    if kind == "savings":
        return balance <= SAVINGS_FLOOR
    if kind == "credit":
        return balance >= CREDIT_STEP
    return False


def build_pulse(today: date, session: Session) -> list[PulseLine]:
    from src.models.brokerage import Account
    from src.models.plaid import PlaidAccountBalanceSnapshot as Snap

    lines: list[PulseLine] = []
    account_ids = session.scalars(select(Snap.account_id).distinct()).all()
    for account_id in account_ids:
        latest = session.scalars(
            select(Snap)
            .where(Snap.account_id == account_id, Snap.snapshot_date <= today)
            .order_by(Snap.snapshot_date.desc())
            .limit(1)
        ).first()
        if latest is None:
            continue
        kind = classify(latest.plaid_account_type, latest.plaid_account_subtype)
        if kind is None:
            continue
        account = session.get(Account, account_id)
        name = account.account_name if account and account.account_name else account_id
        name = name[:80]  # Plaid/institution-controlled — cap before display
        bal = latest.current_balance  # already a Decimal (Numeric asdecimal=True)
        lines.append(PulseLine(name, kind, bal, _breached(kind, bal)))
    return sorted(lines, key=lambda x: (not x.breached, x.account_name))


def render_pulse(lines: list[PulseLine]) -> str:
    if not lines:
        return "No monitored accounts."
    rows = []
    for ln in lines:
        flag = " ⚠️" if ln.breached else ""
        rows.append(f"  {ln.account_name} ({ln.kind}): ${ln.balance:,.2f}{flag}")
    breached = sum(1 for x in lines if x.breached)
    head = f"Account pulse — {len(lines)} accounts, {breached} flagged."
    return head + "\n" + "\n".join(rows)


def _pulse_already_sent(session: Session, key: str, occ: str) -> bool:
    row = (
        session.query(AlertDispatch)
        .filter_by(alert_key=key, occurrence_date=occ)
        .one_or_none()
    )
    return row is not None and row.status == "sent"


def _record_pulse(session: Session, key: str, occ: str, result: WebhookResult) -> None:
    # Update an existing same-day row in place (e.g. a prior `failed` flips to
    # `sent` on retry) so the audit trail stays accurate — mirrors dispatcher._record.
    existing = (
        session.query(AlertDispatch)
        .filter_by(alert_key=key, occurrence_date=occ)
        .one_or_none()
    )
    if existing is not None:
        existing.status = result.status
        existing.http_status = result.http_status
        existing.error_detail = result.error
        session.commit()
        return
    row = AlertDispatch(
        alert_key=key,
        occurrence_date=occ,
        alert_type="balance_pulse",
        entity="all",
        subject=f"Daily account pulse — {occ}",
        status=result.status,
        http_status=result.http_status,
        error_detail=result.error,
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        session.rollback()


def post_pulse(today: date, session: Session, *, apply: bool) -> WebhookResult:
    """Build and (when apply) POST the daily pulse as an `info` alert.

    Deduped + audited via `alert_dispatch` so a same-day re-run never double-sends.
    """
    occ = today.isoformat()
    key = f"balance:pulse:{occ}"
    lines = build_pulse(today, session)
    payload = build_payload_dict(
        severity="info",
        title=f"Daily account pulse — {occ}",
        message=render_pulse(lines),
        alert_key=key,
    )
    if apply and _pulse_already_sent(session, key, occ):
        return WebhookResult("skipped", None, None)
    result = post_payload(payload, key=key, apply=apply, timeout=10.0)
    if apply:
        _record_pulse(session, key, occ, result)
    return result
