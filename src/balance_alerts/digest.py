"""Daily account-pulse digest (REQ-BAL-008).

A single `info` message listing every monitored account, its current balance, and
a flag on anything currently in a breached state (below a cash floor / above a
credit ceiling). Sent via the shared n8n severity webhook path, and recorded in
the `alert_dispatch` ledger so a timer flap can't double-send it.

REQ-FIX-ALR-005: a balance line whose snapshot is older than yesterday renders
`(as of <date>) ⏳` and counts toward the footer's stale total — confidently
stale data is never presented as current.

REQ-FIX-PLD-001: `/accounts/get` returns Plaid's *cached* balance, refreshed
only by Transactions syncs — a snapshot written today can carry a value from
days ago. When the Plaid response carries `balances.last_updated_datetime`
(populated for some institutions, e.g. Capital One; typically null for
others), that date — not the write-time `snapshot_date` — drives staleness
and the `as of` marker, so a stale cached value is never shown as current and
a multi-day move isn't mis-attributed to the single day it was re-written.

REQ-DHL-001/002: a "Delivery" block surfaces the four silent-failure modes
from the 2026-07-07 audit — missed snapshot day, failed webhook POST, unmapped
account skip, dead item — derived from `ingestion_log` + `alert_dispatch` +
`expected_account`. Collapses to one line when everything is healthy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
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
    cache_last_updated,
    classify,
)
from src.balance_alerts.webhook import build_payload_dict, post_payload

logger = logging.getLogger(__name__)

# A checking account is "breached" in the pulse when it has dropped into sev3
# territory (≤ $1k) — not merely below the top $10k milestone, which would flag
# almost every account and make the flag meaningless.
_CHECKING_BREACH_FLOOR = CHECKING_MILESTONES[-2]  # $1,000

# REQ-FIX-ALR-005: a snapshot older than yesterday is stale.
STALE_AFTER_DAYS = 1


@dataclass(frozen=True)
class PulseLine:
    account_name: str
    kind: str
    balance: Decimal
    breached: bool
    snapshot_date: date
    cache_last_updated: date | None = None

    @property
    def effective_date(self) -> date:
        """The date staleness/`as of` should key off: the cache's own
        last-refresh date when Plaid supplies one and it's older than the
        write-time `snapshot_date`, otherwise `snapshot_date` itself."""
        if self.cache_last_updated is not None and self.cache_last_updated < self.snapshot_date:
            return self.cache_last_updated
        return self.snapshot_date

    def stale(self, today: date) -> bool:
        return self.effective_date < today - timedelta(days=STALE_AFTER_DAYS)


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
        try:
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
            lines.append(
                PulseLine(
                    name,
                    kind,
                    bal,
                    _breached(kind, bal),
                    latest.snapshot_date,
                    cache_last_updated(latest.raw_data),
                )
            )
        except Exception:  # noqa: BLE001 — one bad account snapshot must not
            # blank the daily pulse for every other monitored account.
            logger.exception("build_pulse: account %s raised; skipping", account_id)
            session.rollback()
    return sorted(lines, key=lambda x: (not x.breached, x.account_name))


# Pulse layout: accounts grouped under a type header, largest balance first
# (any flagged account still surfaces at the top of its group with a ⚠️).
_PULSE_KIND_ORDER = ("checking", "savings", "credit", "investment")
_PULSE_KIND_LABEL = {
    "checking": "Checking",
    "savings": "Savings",
    "credit": "Credit",
    "investment": "Investment",
}


def render_pulse(lines: list[PulseLine], today: date) -> str:
    if not lines:
        return "No monitored accounts."
    blocks: list[str] = []
    for kind in _PULSE_KIND_ORDER:
        group = [ln for ln in lines if ln.kind == kind]
        if not group:
            continue
        group.sort(key=lambda x: (not x.breached, -x.balance))
        rows = []
        for ln in group:
            as_of = f" (as of {ln.effective_date.isoformat()}) ⏳" if ln.stale(today) else ""
            warn = " ⚠️" if ln.breached else ""
            rows.append(f"  {ln.account_name} — ${ln.balance:,.2f}{as_of}{warn}")
        blocks.append(_PULSE_KIND_LABEL[kind] + "\n" + "\n".join(rows))
    breached = sum(1 for x in lines if x.breached)
    stale = sum(1 for x in lines if x.stale(today))
    n = len(lines)
    footer = f"{n} account{'s' if n != 1 else ''} · {breached} flagged · {stale} stale"
    return "\n\n".join(blocks) + "\n\n" + footer


# ── Delivery-health block (REQ-DHL-001/002) ─────────────────────────────────


@dataclass(frozen=True)
class SyncHealth:
    institution: str
    age_days: int  # days since the latest success/partial_failure run
    healthy: bool  # < 24h old


@dataclass(frozen=True)
class DeliveryHealth:
    syncs: list[SyncHealth]
    sent: int
    failed: int
    skipped: int
    unmapped: list[str]
    gaps: list[tuple[str, int]]  # (account_name, gap_days), gap_days >= 2

    @property
    def healthy(self) -> bool:
        return (
            all(s.healthy for s in self.syncs)
            and self.failed == 0
            and not self.unmapped
            and not self.gaps
        )


_SYNC_SUCCESS_STATUSES = ("success", "partial_failure")


def build_delivery_health(
    today: date, session: Session, lines: list[PulseLine]
) -> DeliveryHealth:
    """Derive the delivery-health block (REQ-DHL-001).

    - Per-item last-success age: latest `ingestion_log` row per institution
      across both `plaid_balance:%` and `plaid_tx:%` sources.
    - Yesterday's sent/failed/skipped: `alert_dispatch` grouped by status
      where `occurrence_date = yesterday`, run markers excluded.
    - Unmapped: `expected_account(source='plaid', status='unconfirmed')`.
    - Gaps: `today - snapshot_date` per pulse line, when >= 2 days (the same
      threshold `PulseLine.stale` uses, expressed as a day count).
    """
    from src.models.history import ExpectedAccount
    from src.models.ingestion_log import IngestionLog

    log_rows = (
        session.query(IngestionLog)
        .filter(
            (IngestionLog.source.like("plaid_balance:%"))
            | (IngestionLog.source.like("plaid_tx:%")),
            IngestionLog.status.in_(_SYNC_SUCCESS_STATUSES),
        )
        .all()
    )
    latest_by_institution: dict[str, date] = {}
    for row in log_rows:
        _, _, institution = row.source.partition(":")
        run_date = row.run_at.date()
        prev = latest_by_institution.get(institution)
        if prev is None or run_date > prev:
            latest_by_institution[institution] = run_date
    syncs = [
        SyncHealth(
            institution=institution.lower(),
            age_days=(today - run_date).days,
            healthy=(today - run_date).days < 1,
        )
        for institution, run_date in sorted(latest_by_institution.items())
    ]

    yesterday = (today - timedelta(days=1)).isoformat()
    dispatch_rows = (
        session.query(AlertDispatch)
        .filter(
            AlertDispatch.occurrence_date == yesterday,
            AlertDispatch.alert_type != "run_marker",
        )
        .all()
    )
    sent = sum(1 for r in dispatch_rows if r.status == "sent")
    failed = sum(1 for r in dispatch_rows if r.status == "failed")
    skipped = sum(1 for r in dispatch_rows if r.status not in ("sent", "failed"))

    unmapped_rows = (
        session.query(ExpectedAccount)
        .filter_by(source="plaid", status="unconfirmed")
        .all()
    )
    unmapped = [f"{r.account_name} ·{r.last_4 or '----'}·" for r in unmapped_rows]

    gaps = [
        (ln.account_name, (today - ln.effective_date).days)
        for ln in lines
        if (today - ln.effective_date).days >= 2
    ]

    return DeliveryHealth(
        syncs=syncs, sent=sent, failed=failed, skipped=skipped, unmapped=unmapped, gaps=gaps
    )


def render_delivery_health(health: DeliveryHealth) -> str:
    if health.healthy:
        return "Delivery ✓ syncs<24h · 0 failed · 0 unmapped"
    out = ["Delivery"]
    if health.syncs:
        sync_str = " · ".join(
            f"{s.institution} {'✓' if s.healthy else '⏳'}{s.age_days}d" for s in health.syncs
        )
        out.append(f"  sync: {sync_str}")
    out.append(f"  y'day: {health.sent} sent · {health.failed} failed · {health.skipped} skipped")
    if health.unmapped:
        out.append(f"  unmapped: {'; '.join(health.unmapped)}")
    if health.gaps:
        gap_str = " · ".join(f"{name} {days}d" for name, days in health.gaps)
        out.append(f"  gap: {gap_str}")
    return "\n".join(out)


def _pulse_already_sent(session: Session, key: str, occ: str) -> bool:
    row = (
        session.query(AlertDispatch)
        .filter_by(alert_key=key, occurrence_date=occ)
        .one_or_none()
    )
    return row is not None and row.status == "sent"


def _record_pulse(
    session: Session,
    key: str,
    occ: str,
    result: WebhookResult,
    payload: dict[str, str | None],
) -> None:
    # Update an existing same-day row in place (e.g. a prior `failed` flips to
    # `sent` on retry) so the audit trail stays accurate — mirrors dispatcher._record.
    payload_json = json.dumps(payload)
    existing = (
        session.query(AlertDispatch)
        .filter_by(alert_key=key, occurrence_date=occ)
        .one_or_none()
    )
    if existing is not None:
        existing.status = result.status
        existing.http_status = result.http_status
        existing.error_detail = result.error
        existing.delivery_channel = "n8n_webhook"
        existing.payload_json = payload_json
        session.commit()
        return
    row = AlertDispatch(
        alert_key=key,
        occurrence_date=occ,
        alert_type="balance_pulse",
        entity="all",
        subject=f"Business Account Snapshot — {occ}",
        status=result.status,
        http_status=result.http_status,
        error_detail=result.error,
        delivery_channel="n8n_webhook",
        payload_json=payload_json,
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
    try:
        lines = build_pulse(today, session)
    except Exception:
        logger.exception("pulse compute failed; skipping digest send")
        session.rollback()
        return WebhookResult("failed", None, "digest compute error")
    # The pulse must still go out even if the delivery-health block can't be
    # computed — degrade to a pulse without the block rather than losing the day.
    try:
        health = build_delivery_health(today, session, lines)
        health_text = "\n\n" + render_delivery_health(health)
    except Exception:
        logger.exception("delivery-health compute failed; sending degraded pulse")
        session.rollback()
        health_text = "\n\n⚠️ Delivery-health block unavailable (compute error — check logs)"
    message = render_pulse(lines, today) + health_text
    payload = build_payload_dict(
        severity="info",
        title=f"📊 Business Account Snapshot · {today:%b} {today.day}",
        message=message,
        alert_key=key,
    )
    if apply and _pulse_already_sent(session, key, occ):
        return WebhookResult("skipped", None, None)
    result = post_payload(payload, key=key, apply=apply, timeout=10.0)
    if apply:
        _record_pulse(session, key, occ, result, payload)
    return result
