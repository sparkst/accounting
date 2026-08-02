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

REQ-DFB-001: the 2026-07-27 Plaid consolidation stopped local snapshot rows
for wealth-scope Items (their balances go to the wealth D1 only), freezing the
pulse's Investment section at the last local row. The Investment section now
renders from the wealth Worker's freshness payload (which carries
latest/previous balances) whenever it is reachable; the frozen local lines are
kept only as a degraded fallback when it is not.

REQ-DFB-002: every line with a previous-snapshot baseline renders a signed
day-change amount; a baseline older than yesterday is labeled
`since <date>` so a multi-day move is never mis-read as one day.
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
    # REQ-DFB-002: baseline for the day-change amount — the snapshot
    # immediately before the latest one (local rows or wealth-D1 payload).
    previous_balance: Decimal | None = None
    previous_date: date | None = None

    @property
    def day_change(self) -> Decimal | None:
        if self.previous_balance is None:
            return None
        return self.balance - self.previous_balance

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
            previous = session.scalars(
                select(Snap)
                .where(
                    Snap.account_id == account_id,
                    Snap.snapshot_date < latest.snapshot_date,
                )
                .order_by(Snap.snapshot_date.desc())
                .limit(1)
            ).first()
            lines.append(
                PulseLine(
                    name,
                    kind,
                    bal,
                    _breached(kind, bal),
                    latest.snapshot_date,
                    cache_last_updated(latest.raw_data),
                    previous.current_balance if previous is not None else None,
                    previous.snapshot_date if previous is not None else None,
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


def _render_day_change(ln: PulseLine, today: date) -> str:
    """REQ-DFB-002: ` (+$1,135.00)` — signed vs the previous snapshot.

    A baseline that is not exactly yesterday gets ` since <date>` so a
    multi-day (or stale-window) move is never presented as a one-day change.
    """
    delta = ln.day_change
    if delta is None:
        return ""
    sign = "+" if delta >= 0 else "-"
    out = f" ({sign}${abs(delta):,.2f}"
    if ln.previous_date is not None and ln.previous_date != today - timedelta(days=1):
        out += f" since {ln.previous_date.isoformat()}"
    return out + ")"


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
            chg = _render_day_change(ln, today)
            as_of = f" (as of {ln.effective_date.isoformat()}) ⏳" if ln.stale(today) else ""
            warn = " ⚠️" if ln.breached else ""
            rows.append(f"  {ln.account_name} — ${ln.balance:,.2f}{chg}{as_of}{warn}")
        blocks.append(_PULSE_KIND_LABEL[kind] + "\n" + "\n".join(rows))
    breached = sum(1 for x in lines if x.breached)
    stale = sum(1 for x in lines if x.stale(today))
    n = len(lines)
    footer = f"{n} account{'s' if n != 1 else ''} · {breached} flagged · {stale} stale"
    return "\n\n".join(blocks) + "\n\n" + footer


# ── Wealth-D1-sourced investment lines (REQ-DFB-001) ────────────────────────

#: Plaid account types that belong in the pulse's Investment section.
WEALTH_INVESTMENT_TYPES = frozenset({"investment", "brokerage"})


def fetch_wealth_freshness() -> tuple[str, dict[str, object] | None]:
    """Fetch the wealth Worker's freshness payload (latest/previous balances).

    Same endpoint + env gating the sentinel uses: ('unconfigured', None) when
    WEALTH_API_BASE/WEALTH_INTERNAL_KEY are absent, ('error', None) on any
    network/HTTP failure.
    """
    from src.monitoring.sentinel import fetch_d1_freshness

    return fetch_d1_freshness()


def build_wealth_investment_lines(payload: dict[str, object]) -> list[PulseLine]:
    """PulseLines for every investment/brokerage account in the payload.

    Per-row isolation (house pattern): one malformed account row is skipped
    with a log line, never blanking the whole Investment section. Rows with no
    snapshot (null balance/date) have nothing to render and are skipped.
    """
    accounts_raw = payload.get("accounts", []) if isinstance(payload, dict) else []
    accounts = accounts_raw if isinstance(accounts_raw, list) else []
    lines: list[PulseLine] = []
    for acct in accounts:
        try:
            if not isinstance(acct, dict):
                continue
            if acct.get("plaid_account_type") not in WEALTH_INVESTMENT_TYPES:
                continue
            raw_balance = acct.get("latest_balance")
            raw_date = acct.get("latest_snapshot_date")
            if raw_balance is None or raw_date is None:
                continue
            name = str(acct.get("account_name") or acct.get("account_id") or "?")
            name = name[:80]  # institution-controlled — cap before display
            raw_prev_balance = acct.get("previous_balance")
            raw_prev_date = acct.get("previous_snapshot_date")
            lines.append(
                PulseLine(
                    name,
                    "investment",
                    Decimal(str(raw_balance)),
                    False,
                    date.fromisoformat(str(raw_date)),
                    None,
                    Decimal(str(raw_prev_balance)) if raw_prev_balance is not None else None,
                    date.fromisoformat(str(raw_prev_date)) if raw_prev_date else None,
                )
            )
        except Exception:  # noqa: BLE001 — per-row isolation
            logger.exception(
                "build_wealth_investment_lines: skipping malformed row %r",
                acct.get("account_id") if isinstance(acct, dict) else acct,
            )
    return lines


def merge_wealth_lines(
    local: list[PulseLine], wealth: list[PulseLine]
) -> list[PulseLine]:
    """Replace local investment lines with wealth-D1-sourced ones.

    Local investment lines are the frozen pre-consolidation leftovers — when
    the wealth surface produced lines, they are superseded wholesale. With no
    wealth lines (fetch failed / unconfigured / empty), keep the local ones:
    a stale-marked value beats a silently missing section.
    """
    if not wealth:
        return local
    return [ln for ln in local if ln.kind != "investment"] + wealth


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
    # REQ-DFB-001: swap the frozen local investment lines for current wealth-D1
    # values. Any failure here degrades to the local (stale-marked) lines — the
    # day's pulse must still send.
    try:
        wealth_status, wealth_payload = fetch_wealth_freshness()
        wealth_lines = (
            build_wealth_investment_lines(wealth_payload)
            if wealth_status == "ok" and wealth_payload is not None
            else []
        )
    except Exception:  # noqa: BLE001 — degraded pulse beats a lost pulse
        logger.exception("wealth freshness fetch failed; keeping local investment lines")
        wealth_lines = []
    lines = merge_wealth_lines(lines, wealth_lines)
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
