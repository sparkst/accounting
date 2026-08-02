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

REQ-DFB-004 (2026-08-02): the daily flash is TWO messages — `📊 Wealth
Snapshot` (every /wealth-page account from the D1 freshness surface,
including statement-fed rows via REQ-DFB-003) and `🏢 Business Accounts`
(local register, non-investment kinds). The delivery-health block no longer
rides the flash; `build_delivery_health`/`render_delivery_health` stay
exported for the alerting-consolidation workstream to re-home.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.balance_alerts.flash_config import AUTO_HIDE_BELOW, FLASH_ACCOUNTS
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

# REQ-DFB-007 (supersedes the REQ-FIX-ALR-005 window): any snapshot before
# today is stale — 🟡 1-5 days old, 🔴 older; footer counts both.
STALE_AFTER_DAYS = 0


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
    # REQ-DFB-006: hidden lines still count in totals/net worth but render no row.
    hidden: bool = False
    # REQ-DFB-009: WBR-sourced (death benefit from notes; borrow = margin or
    # policy-loan capacity). None when the account has neither.
    death_benefit: Decimal | None = None
    borrow_capacity: Decimal | None = None

    @property
    def day_change(self) -> Decimal | None:
        # Balance + date are an atomic pair: a delta without its baseline date
        # would render as a same-day change (REQ-DFB-002 misread).
        if self.previous_balance is None or self.previous_date is None:
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
    "checking": "💵 CHECKING",
    "savings": "🏦 SAVINGS",
    "credit": "💳 CREDIT",
    "investment": "📈 INVESTMENT",
}

# ── Phone-first rendering (REQ-DFB-005/006/007) ─────────────────────────────
# Travis's 2026-08-02 template, v3: the body ships as PLAIN TEXT with
# `pre: true` in the webhook payload — WH-Severity itself wraps the escaped
# message in a trusted <pre> (REQ-SEV-006; caller-embedded tags arrive
# entity-escaped and render literally, observed live 2026-08-02). Amounts
# right-align at a fixed DISPLAY-cell column (emoji are 2 cells in a
# monospace font — a 🟡-prefixed row must not shift the column). No `$`
# (less busy); staleness is a colored bullet: 🟡 1–5 days old, 🔴 older.

_SECTION_SEP = "━━━━━━━━━━━━━━"
#: Right-alignment column in DISPLAY CELLS. 30 verified on-device (iPhone,
#: Telegram <pre>) 2026-08-02 — 34 wrapped. The amount column is right-padded
#: to exactly this many display cells, so EVERY row is this wide; keep it under
#: the phone's monospace wrap point.
_ROW_WIDTH = 30

#: Chars rendered double-width in monospace fonts beyond east_asian_width
#: 'W'/'F'. ⚠ is EAW 'A'; ▲/▼ are EAW 'A' too but iOS gives them emoji
#: presentation and renders them 2 cells — counting them as 1 pushed delta
#: rows past the column and wrapped on-device (2026-08-02).
_EXTRA_WIDE = frozenset("⚠▲▼")
#: Zero-width: variation selectors + ZWJ.
_ZERO_WIDE = frozenset("️‍")


def _dwidth(s: str) -> int:
    import unicodedata

    w = 0
    for ch in s:
        if ch in _ZERO_WIDE:
            continue
        if ch in _EXTRA_WIDE or unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _fmt_money(v: Decimal) -> str:
    return f"-{abs(v):,.0f}" if v < 0 else f"{v:,.0f}"


def _stale_star(ln: PulseLine, today: date) -> str:
    """REQ-DFB-007 v2: staleness is a plain `*` after the colon — emoji
    bullets are variable-width in monospace fonts and skewed the column."""
    return "*" if ln.stale(today) else ""


def _expected_baseline(today: date) -> date:
    """REQ-DFB-008: the delta baseline is the previous BUSINESS day —
    Sat/Sun/Mon all diff against Friday, Tue–Fri against yesterday."""
    wd = today.weekday()  # Mon=0 … Sun=6
    if wd == 0:  # Monday → Friday
        return today - timedelta(days=3)
    if wd == 6:  # Sunday → Friday
        return today - timedelta(days=2)
    return today - timedelta(days=1)  # incl. Saturday → Friday


def _delta_tag(ln: PulseLine, today: date) -> str:
    """` ▲12,694` — strictly vs the previous business day (REQ-DFB-008).

    Any other baseline renders nothing: a multi-day statement move must not
    masquerade as a business-day change."""
    delta = ln.day_change
    if delta is None or abs(delta) < Decimal("0.5"):
        return ""
    if ln.previous_date != _expected_baseline(today):
        return ""
    arrow = "▲" if delta > 0 else "▼"
    return f" {arrow}{abs(delta):,.0f}"


def _aligned_row(left: str, amount: str) -> str:
    """`left……amount` with the amount right-aligned at _ROW_WIDTH cells.

    An over-long left segment is truncated with an ellipsis so the amount
    column never moves and no line soft-wraps on the phone.
    """
    pad = _ROW_WIDTH - _dwidth(left) - _dwidth(amount)
    if pad < 1:
        while left and _dwidth(left) > _ROW_WIDTH - _dwidth(amount) - 2:
            left = left[:-1]
        left += "…"
        pad = _ROW_WIDTH - _dwidth(left) - _dwidth(amount)
    return f"{left}{' ' * pad}{amount}"


def _account_row(ln: PulseLine, today: date, *, flags: bool = False) -> str:
    """One account line; a ▲/▼ tag drops to an indented continuation line
    when it would push past the amount column — the column never moves and
    nothing soft-wraps mid-number on the phone."""
    amount = _fmt_money(ln.balance)
    warn = " ⚠️" if flags and ln.breached else ""
    delta = _delta_tag(ln, today)
    base = f"• {ln.account_name}:{_stale_star(ln, today)}{warn}"
    left = f"{base}{delta}"
    if _dwidth(left) + _dwidth(amount) + 1 <= _ROW_WIDTH:
        return _aligned_row(left, amount)
    row = _aligned_row(base, amount)
    if delta:
        row += f"\n {delta}"
    return row


def _section_block(
    label: str, total: Decimal, rows: list[str], summary: list[str] | None = None
) -> str:
    """A framed section. When ``summary`` is given (REQ-DFB-009 — the STOCKS
    Borrowability / LIFE INSURANCE Death Benefit+Borrowability lines), it sits
    between the header and a second rule, then the account rows."""
    header = _aligned_row(f"{label} ·", _fmt_money(total))
    head = f"{_SECTION_SEP}\n{header}\n{_SECTION_SEP}\n"
    if summary:
        head += "\n".join(summary) + f"\n{_SECTION_SEP}\n"
    return head + "\n".join(rows)


def _wealth_summary(section: str, group: list[PulseLine]) -> list[str]:
    """REQ-DFB-009 summary sub-lines for an enriched section.

    STOCKS → Borrowability (Σ margin capacity). LIFE → Death Benefit
    (Σ death benefit) + Borrowability (Σ policy-loan capacity). Amounts come
    straight from the freshness payload, which computes them with the WBR's own
    constants — the flash never does the money math itself. A zero/empty
    aggregate omits its line."""
    out: list[str] = []
    if section == "life":
        db = sum((ln.death_benefit for ln in group if ln.death_benefit), Decimal(0))
        if db > 0:
            out.append(_aligned_row("• Death Benefit:", _fmt_money(db)))
    if section in ("stocks", "life"):
        borrow = sum(
            (ln.borrow_capacity for ln in group if ln.borrow_capacity), Decimal(0)
        )
        if borrow > 0:
            out.append(_aligned_row("• Borrowability:", _fmt_money(borrow)))
    return out


def render_pulse(lines: list[PulseLine], today: date) -> str:
    """Business flash body (REQ-DFB-006 template) — <pre>-wrapped monospace."""
    if not lines:
        return "No monitored accounts."
    blocks: list[str] = []
    for kind in _PULSE_KIND_ORDER:
        group = [ln for ln in lines if ln.kind == kind]
        if not group:
            continue
        group.sort(key=lambda x: (not x.breached, -x.balance))
        total = sum((ln.balance for ln in group), Decimal(0))
        rows = [_account_row(ln, today, flags=True) for ln in group]
        blocks.append(_section_block(_PULSE_KIND_LABEL[kind], total, rows))
    breached = sum(1 for x in lines if x.breached)
    stale = sum(1 for x in lines if x.stale(today))
    n = len(lines)
    footer = f"{n} account{'s' if n != 1 else ''} · {breached} flagged · {stale} stale"
    return "BUSINESS ACCOUNTS\n" + "\n\n".join(blocks) + f"\n{_SECTION_SEP}\n{footer}"


# ── Wealth-D1-sourced pulse lines (REQ-DFB-001/003/004) ─────────────────────

#: Default section per D1 account_type — used for accounts NOT in
#: flash_config.FLASH_ACCOUNTS (a newly-linked account lands somewhere
#: sensible instead of vanishing).
_WEALTH_SECTION_BY_ACCOUNT_TYPE = {
    "checking": "cash",
    "savings": "cash",
    "credit_card": "credit",
    "taxable": "stocks",
    "joint": "stocks",
    "tod": "stocks",
    "rsu": "stocks",
    "roth_ira": "retirement",
    "trad_ira": "retirement",
    "401k": "retirement",
    "403b": "retirement",
    "brokeragelink": "retirement",
    "hsa": "retirement",
    "529": "529",
    "other": "other",
}

#: Fallback for plaid-typed rows whose account_type is missing/unknown.
_WEALTH_SECTION_BY_PLAID_TYPE = {
    "depository": "cash",
    "credit": "credit",
    "investment": "stocks",
    "brokerage": "stocks",
    "loan": "loans",
    "other": "other",
}

_WEALTH_SECTION_ORDER = ("cash", "credit", "stocks", "retirement", "529", "loans", "life", "other")
_WEALTH_SECTION_LABEL = {
    "cash": "💵 CASH",
    "credit": "💳 CREDIT",
    "stocks": "📈 STOCKS",
    "retirement": "📈 RETIREMENT",
    "529": "📈 529s",
    "loans": "🏦 LOANS",
    "life": "📦 LIFE INSURANCE",
    "other": "📦 OTHER",
}

#: Net-worth breakdown label per section (template's 💰 block).
_WEALTH_SECTION_NW_LABEL = {
    "cash": "Cash",
    "credit": "Credit",
    "stocks": "Stocks",
    "retirement": "Retirement",
    "529": "529s",
    "loans": "Loans",
    "life": "Life Ins. CV",
    "other": "Other",
}

#: Net-worth sign per section: credit/loan balances are amounts OWED.
_WEALTH_SECTION_SIGN = {
    "cash": 1,
    "credit": -1,
    "stocks": 1,
    "retirement": 1,
    "529": 1,
    "loans": -1,
    "life": 1,
    "other": 1,
}


def fetch_wealth_freshness(today: date) -> tuple[str, dict[str, object] | None]:
    """Fetch the wealth Worker's freshness payload with statement rows.

    Same endpoint + env gating the sentinel uses, plus ``include_statement=1``
    (REQ-DFB-003, digest-only — the sentinel's own call must never receive
    statement rows) and ``baseline=<previous business day>`` (REQ-DFB-008) so
    the previous snapshot diffs Sat/Sun/Mon against Friday. ('unconfigured',
    None) when WEALTH_API_BASE / WEALTH_INTERNAL_KEY are absent, ('error',
    None) on any network/HTTP failure. The key travels only in the header and
    is never logged.
    """
    import os

    import httpx

    base = os.environ.get("WEALTH_API_BASE", "").rstrip("/")
    key = os.environ.get("WEALTH_INTERNAL_KEY", "")
    if not base or not key:
        return ("unconfigured", None)
    try:
        resp = httpx.get(
            f"{base}/wealth/api/internal/freshness",
            params={
                "include_statement": "1",
                "baseline": _expected_baseline(today).isoformat(),
            },
            headers={"X-Internal-Key": key},
            timeout=15.0,
        )
    except httpx.HTTPError:
        logger.warning("wealth freshness fetch failed (network)")
        return ("error", None)
    if resp.status_code != 200:
        logger.warning("wealth freshness fetch failed (HTTP %s)", resp.status_code)
        return ("error", None)
    try:
        return ("ok", resp.json())
    except ValueError:
        return ("error", None)


def _wealth_section(acct: dict[str, object]) -> str:
    """Default section for an account NOT in FLASH_ACCOUNTS."""
    section = _WEALTH_SECTION_BY_ACCOUNT_TYPE.get(str(acct.get("account_type")))
    if section is not None:
        return section
    plaid_type = acct.get("plaid_account_type")
    return _WEALTH_SECTION_BY_PLAID_TYPE.get(str(plaid_type), "other")


def build_wealth_lines(payload: dict[str, object]) -> tuple[list[PulseLine], int]:
    """(PulseLines, skipped_count) for EVERY account in the wealth payload.

    REQ-DFB-004: the daily flash mirrors the /wealth page — all personal
    accounts (cash, credit, investment, loan, statement-fed), never the
    business register.

    Per-row isolation (house pattern): one malformed account row is skipped
    with a log line + COUNT (surfaced to the flash) — never silently gone.
    Rows with no snapshot (null balance/date) have nothing to render and skip
    silently. The previous-snapshot pair is atomic: if either half is missing
    or unparseable, both are dropped — a delta with no baseline date would
    render as a same-day change (the exact misread REQ-DFB-002 prevents).
    """
    accounts_raw = payload.get("accounts", []) if isinstance(payload, dict) else []
    accounts = accounts_raw if isinstance(accounts_raw, list) else []
    lines: list[PulseLine] = []
    skipped = 0
    for acct in accounts:
        try:
            if not isinstance(acct, dict):
                continue
            raw_balance = acct.get("latest_balance")
            raw_date = acct.get("latest_snapshot_date")
            if raw_balance is None or raw_date is None:
                continue
            balance = Decimal(str(raw_balance))
            # REQ-DFB-006: per-account alias/section/hide from flash_config;
            # dust below AUTO_HIDE_BELOW auto-hides. Hidden lines still count
            # toward totals/net worth.
            cfg = FLASH_ACCOUNTS.get(str(acct.get("account_id")))
            if cfg is not None:
                name = cfg.alias
                section = cfg.section
                hidden = cfg.hide or abs(balance) < AUTO_HIDE_BELOW
            else:
                # A nameless account must never render its raw UUID (observed
                # live 2026-08-02 — a bare id took three phone lines).
                name = str(
                    acct.get("account_name") or f"unnamed · {acct.get('broker', '?')}"
                )
                section = _wealth_section(acct)
                hidden = abs(balance) < AUTO_HIDE_BELOW
            name = name[:80]  # institution-controlled — cap before display
            raw_prev_balance = acct.get("previous_balance")
            raw_prev_date = acct.get("previous_snapshot_date")
            prev_balance: Decimal | None = None
            prev_date: date | None = None
            if raw_prev_balance is not None and raw_prev_date:
                try:
                    prev_balance = Decimal(str(raw_prev_balance))
                    prev_date = date.fromisoformat(str(raw_prev_date))
                except Exception:  # noqa: BLE001 — baseline is optional
                    logger.warning(
                        "build_wealth_lines: unparseable previous snapshot "
                        "for %r; rendering without day change",
                        acct.get("account_id"),
                    )
                    prev_balance = None
                    prev_date = None
            db_raw = acct.get("death_benefit")
            bc_raw = acct.get("borrow_capacity")
            lines.append(
                PulseLine(
                    name,
                    section,
                    balance,
                    False,
                    date.fromisoformat(str(raw_date)),
                    None,
                    prev_balance,
                    prev_date,
                    hidden,
                    Decimal(str(db_raw)) if db_raw is not None else None,
                    Decimal(str(bc_raw)) if bc_raw is not None else None,
                )
            )
        except Exception:  # noqa: BLE001 — per-row isolation
            skipped += 1
            logger.exception(
                "build_wealth_lines: skipping malformed row %r",
                acct.get("account_id") if isinstance(acct, dict) else acct,
            )
    return lines, skipped


def render_wealth_pulse(
    lines: list[PulseLine], today: date, note: str | None = None
) -> str:
    """Wealth flash body — Travis's 2026-08-02 template (REQ-DFB-006).

    <pre>-wrapped monospace: framed section headers with totals, right-aligned
    amounts, ⏳M/D stale tags, ▲/▼ one-day deltas Mon–Fri, hidden rows counted
    but not rendered, and a 💰 NET WORTH block with per-section breakdown.

    ``note`` (degradation signal — unreachable source, skipped rows) renders
    as a trailing ⚠️ line so a wealth-source failure is never invisible.
    """
    if not lines:
        return "No wealth accounts." + (f"\n⚠️ wealth source: {note}" if note else "")

    blocks: list[str] = []
    section_totals: dict[str, Decimal] = {}
    for section in _WEALTH_SECTION_ORDER:
        group = sorted(
            (ln for ln in lines if ln.kind == section), key=lambda x: -x.balance
        )
        if not group:
            continue
        total = sum((ln.balance for ln in group), Decimal(0))
        section_totals[section] = total
        rows = [_account_row(ln, today) for ln in group if not ln.hidden]
        summary = _wealth_summary(section, group)
        blocks.append(
            _section_block(_WEALTH_SECTION_LABEL[section], total, rows, summary)
        )

    net = sum(
        (_WEALTH_SECTION_SIGN[s] * t for s, t in section_totals.items()), Decimal(0)
    )
    nw_header = _aligned_row("💰 NET WORTH ·", _fmt_money(net))
    baseline = _expected_baseline(today)  # REQ-DFB-008
    deltas = [
        _WEALTH_SECTION_SIGN[ln.kind] * ln.day_change
        for ln in lines
        # NW delta sums only business-day moves — a months-old statement
        # baseline would swamp today's change.
        if ln.day_change is not None and ln.previous_date == baseline
    ]
    net_delta = sum(deltas, Decimal(0))
    if deltas and abs(net_delta) >= Decimal("0.5"):
        arrow = "▲" if net_delta > 0 else "▼"
        # 8-digit net worth + delta overflows the phone column — the delta
        # rides an indented continuation line, like an account row.
        nw_header += f"\n  {arrow}{abs(net_delta):,.0f}"
    nw_rows = [
        _aligned_row(
            f"• {_WEALTH_SECTION_NW_LABEL[s]}:",
            _fmt_money(_WEALTH_SECTION_SIGN[s] * section_totals[s]),
        )
        for s in _WEALTH_SECTION_ORDER
        if s in section_totals
    ]
    nw_block = (
        f"{_SECTION_SEP}\n{nw_header}\n{_SECTION_SEP}\n" + "\n".join(nw_rows)
    )

    visible = [ln for ln in lines if not ln.hidden]
    stale = sum(1 for x in visible if x.stale(today))
    n = len(visible)
    footer = f"{n} account{'s' if n != 1 else ''} · {stale} stale"

    body = (
        "PERSONAL ACCOUNTS\n"
        + "\n\n".join(blocks)
        + f"\n\n{nw_block}\n{_SECTION_SEP}\n{footer}"
    )
    if note:
        body += f"\n⚠️ wealth source: {note}"
    return body


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
    # REQ-DFB-001: non-None when the wealth-D1 source degraded this run
    # (unreachable, or N malformed rows dropped) — a sustained wealth outage
    # must not hide behind stale markers indefinitely.
    wealth_note: str | None = None

    @property
    def healthy(self) -> bool:
        return (
            all(s.healthy for s in self.syncs)
            and self.failed == 0
            and not self.unmapped
            and not self.gaps
            and self.wealth_note is None
        )


_SYNC_SUCCESS_STATUSES = ("success", "partial_failure")


def build_delivery_health(
    today: date,
    session: Session,
    lines: list[PulseLine],
    wealth_note: str | None = None,
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
        syncs=syncs,
        sent=sent,
        failed=failed,
        skipped=skipped,
        unmapped=unmapped,
        gaps=gaps,
        wealth_note=wealth_note,
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
    if health.wealth_note:
        out.append(f"  wealth: {health.wealth_note}")
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
    payload: dict[str, str | bool | None],
    *,
    alert_type: str = "balance_pulse",
    subject: str = "Business Account Snapshot",
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
        alert_type=alert_type,
        entity="all",
        subject=f"{subject} — {occ}",
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


def _post_one_pulse(
    session: Session,
    *,
    key: str,
    occ: str,
    title: str,
    message: str,
    alert_type: str,
    subject: str,
    apply: bool,
) -> WebhookResult:
    """Dedup + POST + ledger-record one pulse message."""
    payload = build_payload_dict(
        severity="info", title=title, message=message, alert_key=key, pre=True
    )
    if apply and _pulse_already_sent(session, key, occ):
        return WebhookResult("skipped", None, None)
    result = post_payload(payload, key=key, apply=apply, timeout=10.0)
    if apply:
        _record_pulse(
            session, key, occ, result, payload, alert_type=alert_type, subject=subject
        )
    return result


def post_pulse(today: date, session: Session, *, apply: bool) -> WebhookResult:
    """Build and (when apply) POST the daily flash as TWO `info` alerts.

    REQ-DFB-004 (2026-08-02): the daily flash is the /wealth page — every
    personal account (cash, credit, investment, loan, statement-fed) from the
    wealth D1, with day changes. Business register accounts ride a SEPARATE
    `🏢 Business Accounts` message. No delivery-health block on either — the
    sentinel owns operational alerting.

    Each message is deduped + audited via its own `alert_dispatch` row
    (`wealth:pulse:<date>` / `balance:pulse:<date>`) so a same-day re-run
    never double-sends. Returns the worst of the two results (failed > sent >
    skipped/dry_run) so the timer's exit status reflects any lost message.
    """
    occ = today.isoformat()

    # ── Wealth flash (REQ-DFB-004) ──────────────────────────────────────────
    wealth_note: str | None = None
    wealth_lines: list[PulseLine] = []
    try:
        wealth_status, wealth_payload = fetch_wealth_freshness(today)
        if wealth_status == "ok" and wealth_payload is not None:
            wealth_lines, wealth_skipped = build_wealth_lines(wealth_payload)
            if wealth_skipped:
                wealth_note = f"{wealth_skipped} malformed row(s) skipped"
        elif wealth_status != "unconfigured":
            wealth_note = "unreachable — showing last local values"
    except Exception:  # noqa: BLE001 — degraded flash beats a lost flash
        logger.exception("wealth freshness fetch failed; degrading to local lines")
        wealth_note = "fetch crashed — showing last local values"

    local_lines: list[PulseLine] = []
    try:
        local_lines = build_pulse(today, session)
    except Exception:
        logger.exception("local pulse compute failed")
        session.rollback()
        if not wealth_lines:
            return WebhookResult("failed", None, "digest compute error")

    if not wealth_lines:
        # Degraded: last local investment rows (frozen at the 2026-07-27
        # consolidation) with their stale markers beat a missing flash.
        # Local rows carry the register kind "investment" — re-home them into
        # the v2 template's STOCKS section so they actually render.
        wealth_lines = [
            replace(ln, kind="stocks")
            for ln in local_lines
            if ln.kind == "investment"
        ]

    wealth_result: WebhookResult | None = None
    # An empty wealth flash with nothing to warn about (local dev without
    # wealth env) is noise — send only when there are lines or a note.
    if wealth_lines or wealth_note is not None:
        wealth_result = _post_one_pulse(
            session,
            key=f"wealth:pulse:{occ}",
            occ=occ,
            title=f"📊 Wealth Snapshot · {today:%b} {today.day}",
            message=render_wealth_pulse(wealth_lines, today, wealth_note),
            alert_type="wealth_pulse",
            subject="Wealth Snapshot",
            apply=apply,
        )

    # ── Business flash (register accounts, non-investment) ─────────────────
    business_lines = [ln for ln in local_lines if ln.kind != "investment"]
    business_result: WebhookResult | None = None
    if business_lines:
        business_result = _post_one_pulse(
            session,
            key=f"balance:pulse:{occ}",
            occ=occ,
            title=f"🏢 Business Accounts · {today:%b} {today.day}",
            message=render_pulse(business_lines, today),
            alert_type="balance_pulse",
            subject="Business Accounts",
            apply=apply,
        )

    results = [r for r in (wealth_result, business_result) if r is not None]
    if not results:
        return WebhookResult("skipped", None, None)
    for status in ("failed", "sent", "dry_run"):
        for r in results:
            if r.status == status:
                return r
    return results[0]
