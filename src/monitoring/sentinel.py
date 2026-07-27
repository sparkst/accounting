"""Data-level freshness/invariant sentinel (REQ-SEN-001..008).

Process-level monitoring (systemd exit codes + OnFailure alerts) has repeatedly
stayed green while data silently went stale or wrong: the 30-day frozen wealth
balances (INVALID_PRODUCT while cron "succeeded"), Stripe/Shopify adapters not
running for six weeks (ingest endpoint returned 200 on failure), and two
consecutive wrong-scope Plaid links that a human had to notice. This module
asserts *data* invariants against the box DB every day, independent of whether
the producing processes claim success.

Checks (each returns a list of :class:`Violation`):

- ``check_item_staleness``       REQ-SEN-002  active Plaid items synced recently, status ok
- ``check_ingestion_staleness``  REQ-SEN-003  every expected ingestion source has a
                                              recent *success* row (expectations are
                                              derived from active items, so a new link
                                              is monitored automatically)
- ``check_register_snapshot_staleness`` REQ-SEN-004  register-mapped accounts have a
                                              recent balance snapshot (the balance-alert
                                              baseline would silently die without it)
- ``check_scope_anomalies``      REQ-SEN-005  the mislink signature: an active
                                              register-scope item with zero mapped
                                              accounts, or a wealth-scope item that
                                              still has register mappings
- ``check_register_tx_staleness`` REQ-SEN-006  plaid transactions keep flowing into
                                              the register
- ``check_report_freshness``     REQ-SEN-007  the weekly P&L artifact exists and is
                                              recent (a deleted reports/ dir failed
                                              this exact way on 2026-07-27)

``run_sentinel`` composes them; ``build_sentinel_payload`` aggregates violations
into a single n8n severity-webhook payload (type = worst severity present).

All functions take an explicit ``now`` so tests never depend on the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.alerts.webhook import WebhookResult
from src.balance_alerts.webhook import post_payload
from src.models.brokerage import Account
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.models.transaction import Transaction

SEV2 = "sev2"
SEV3 = "sev3"

# Ordered worst-first for payload aggregation.
_SEVERITY_ORDER = (SEV2, SEV3)

ITEM_MAX_AGE_HOURS = 26
INGEST_MAX_AGE_HOURS = 26
SNAPSHOT_MAX_AGE_DAYS = 2
REGISTER_TX_MAX_AGE_DAYS = 10
REPORT_MAX_AGE_DAYS = 8

# Sources expected regardless of Plaid item state (daily timers). The
# wealth-D1 push source is NOT here: its producer only writes a log row when
# wealth-scope items exist (src/adapters/plaid_balance.py P0-r3a guard), so it
# is derived in _expected_sources — an unconditional expectation would cry a
# false sev2 daily forever after the last wealth item is disconnected.
STATIC_EXPECTED_SOURCES = ("stripe", "shopify")
WEALTH_PUSH_SOURCE = "wealth_cloud:plaid_balance"


@dataclass(frozen=True)
class Violation:
    """One failed data assertion."""

    check: str
    severity: str
    subject: str
    detail: str


def _active_items(session: Session) -> list[PlaidItem]:
    return list(
        session.scalars(select(PlaidItem).where(PlaidItem.status == "active"))
    )


def check_item_staleness(
    session: Session, now: datetime, *, max_age_hours: int = ITEM_MAX_AGE_HOURS
) -> list[Violation]:
    """REQ-SEN-002: every active item synced within ``max_age_hours`` with status ok."""
    cutoff = now - timedelta(hours=max_age_hours)
    violations: list[Violation] = []
    for item in _active_items(session):
        if item.last_sync_at is None or item.last_sync_at < cutoff:
            age = (
                "never"
                if item.last_sync_at is None
                else f"{(now - item.last_sync_at).total_seconds() / 3600:.0f}h ago"
            )
            violations.append(
                Violation(
                    "item_stale",
                    SEV2,
                    f"{item.institution_name} ({item.scope})",
                    f"last sync {age} (limit {max_age_hours}h)",
                )
            )
        elif item.last_sync_status != "ok":
            violations.append(
                Violation(
                    "item_stale",
                    SEV2,
                    f"{item.institution_name} ({item.scope})",
                    f"last_sync_status={item.last_sync_status!r}"
                    + (f" last_error={item.last_error}" if item.last_error else ""),
                )
            )
    return violations


def _expected_sources(session: Session) -> set[str]:
    expected = set(STATIC_EXPECTED_SOURCES)
    for item in _active_items(session):
        expected.add(f"plaid_balance:{item.institution_name}")
        if item.scope == "register":
            expected.add(f"plaid_tx:{item.institution_name}")
        elif item.scope == "wealth":
            expected.add(f"plaid_investments:{item.institution_name}")
            expected.add(WEALTH_PUSH_SOURCE)
    return expected


def _masked_ambiguous_institutions(
    session: Session, now: datetime, max_age_hours: int
) -> list[tuple[str, int]]:
    """Institutions where source-key masking is actually happening.

    >1 active item sharing an institution_name collapses their ingestion_log
    source keys (the log carries institution_name, not item id), so one item's
    ingest failure hides behind a sibling's success. Production has duplicate
    names PERMANENTLY (Chase register+wealth; Travis's and Amy's Vanguard
    logins), so flagging mere duplication would cry daily forever — the marker
    fires only while a name-sharing item is itself sync-stale, i.e. exactly
    when the shared key may be lying."""
    cutoff = now - timedelta(hours=max_age_hours)
    by_name: dict[str, list[PlaidItem]] = {}
    for item in _active_items(session):
        by_name.setdefault(item.institution_name, []).append(item)
    flagged: list[tuple[str, int]] = []
    for name, items in sorted(by_name.items()):
        if len(items) < 2:
            continue
        if any(
            i.last_sync_at is None or i.last_sync_at < cutoff or i.last_sync_status != "ok"
            for i in items
        ):
            flagged.append((name, len(items)))
    return flagged


def check_ingestion_staleness(
    session: Session, now: datetime, *, max_age_hours: int = INGEST_MAX_AGE_HOURS
) -> list[Violation]:
    """REQ-SEN-003: every expected source has a *success* row within the window.

    Expectations derive from active Plaid items plus the static daily sources,
    so a newly linked institution is asserted automatically — no config edit,
    no way to forget it.
    """
    cutoff = now - timedelta(hours=max_age_hours)
    rows = session.execute(
        select(IngestionLog.source, func.max(IngestionLog.run_at))
        .where(IngestionLog.status == "success")
        .group_by(IngestionLog.source)
    ).all()
    latest_success = {source: run_at for source, run_at in rows}

    violations: list[Violation] = []
    for source in sorted(_expected_sources(session)):
        last = latest_success.get(source)
        if last is None:
            violations.append(
                Violation(
                    "ingest_stale",
                    SEV2,
                    source,
                    "no successful run on record",
                )
            )
        elif last < cutoff:
            violations.append(
                Violation(
                    "ingest_stale",
                    SEV2,
                    source,
                    f"last success {(now - last).total_seconds() / 3600:.0f}h ago"
                    f" (limit {max_age_hours}h)",
                )
            )
    # Degraded-coverage marker: duplicate institution names share one source
    # key, so per-item ingest failures can hide behind a sibling's success
    # (REQ-SEN-002's last_sync_at check still catches the item itself). Fires
    # only while one of the name-sharers is actually stale — see helper.
    for name, n in _masked_ambiguous_institutions(session, now, max_age_hours):
        violations.append(
            Violation(
                "ingest_source_ambiguous",
                SEV3,
                name,
                f"{n} active items share this institution name — per-item "
                "ingest staleness cannot be distinguished for them",
            )
        )
    return violations


def check_register_snapshot_staleness(
    session: Session, now: datetime, *, max_age_days: int = SNAPSHOT_MAX_AGE_DAYS
) -> list[Violation]:
    """REQ-SEN-004: register-mapped accounts have a balance snapshot within the window.

    These snapshots are the prior-day baseline the balance-milestone alerts
    cross against — if they stop, milestone alerting silently dies with them.
    """
    cutoff_date = (now - timedelta(days=max_age_days)).date()
    rows = session.execute(
        select(
            Account.id,
            Account.account_name,
            func.max(PlaidAccountBalanceSnapshot.snapshot_date),
        )
        .join(PlaidItem, PlaidItem.id == Account.plaid_item_id)
        .outerjoin(
            PlaidAccountBalanceSnapshot,
            PlaidAccountBalanceSnapshot.account_id == Account.id,
        )
        .where(
            PlaidItem.status == "active",
            PlaidItem.scope == "register",
            Account.plaid_account_id.is_not(None),
        )
        .group_by(Account.id, Account.account_name)
    ).all()

    violations: list[Violation] = []
    for _acct_id, name, latest in rows:
        if latest is None:
            violations.append(
                Violation("snapshot_stale", SEV3, name or _acct_id, "no snapshot rows")
            )
        else:
            # SQLite may hand back a str for a Date aggregate.
            latest_date = (
                latest if not isinstance(latest, str) else datetime.fromisoformat(latest).date()
            )
            if latest_date < cutoff_date:
                violations.append(
                    Violation(
                        "snapshot_stale",
                        SEV3,
                        name or _acct_id,
                        f"latest snapshot {latest_date} (limit {max_age_days}d)",
                    )
                )
    return violations


def check_scope_anomalies(session: Session) -> list[Violation]:
    """REQ-SEN-005: the wrong-scope-link signature, both directions.

    A register-scope item with zero mapped register accounts is exactly what
    the Schwab and Vanguard mislinks looked like (2026-07-27) — data landing
    nowhere. The inverse (a wealth item still holding register mappings) means
    a scope repair was left half-done.
    """
    violations: list[Violation] = []
    mapped_counts: dict[str, int] = {
        item_id: count
        for item_id, count in session.execute(
            select(Account.plaid_item_id, func.count(Account.id))
            .where(Account.plaid_item_id.is_not(None))
            .group_by(Account.plaid_item_id)
        ).all()
        if item_id is not None
    }
    for item in _active_items(session):
        mapped = mapped_counts.get(item.id, 0)
        if item.scope == "register" and mapped == 0:
            violations.append(
                Violation(
                    "scope_anomaly",
                    SEV2,
                    item.institution_name,
                    "register-scope item with zero mapped accounts (mislink signature)",
                )
            )
        elif item.scope == "wealth" and mapped > 0:
            violations.append(
                Violation(
                    "scope_anomaly",
                    SEV3,
                    item.institution_name,
                    f"wealth-scope item still has {mapped} register mapping(s)",
                )
            )
    return violations


def check_register_tx_staleness(
    session: Session, now: datetime, *, max_age_days: int = REGISTER_TX_MAX_AGE_DAYS
) -> list[Violation]:
    """REQ-SEN-006: plaid transactions keep arriving while register items exist."""
    has_register_item = any(
        i.scope == "register" for i in _active_items(session)
    )
    if not has_register_item:
        return []
    latest: str | None = session.execute(
        select(func.max(Transaction.date)).where(
            Transaction.source == "plaid", Transaction.status != "rejected"
        )
    ).scalar_one()
    cutoff = (now - timedelta(days=max_age_days)).date().isoformat()
    if latest is None or latest < cutoff:
        return [
            Violation(
                "register_tx_stale",
                SEV3,
                "plaid transactions",
                f"newest non-rejected plaid txn {latest or 'none'}"
                f" (limit {max_age_days}d)",
            )
        ]
    return []


def check_report_freshness(
    report_path: Path, now: datetime, *, max_age_days: int = REPORT_MAX_AGE_DAYS
) -> list[Violation]:
    """REQ-SEN-007: the weekly P&L artifact exists and is recent.

    Catches the runtime-dir-deleted class of failure at the data level (the
    reports/ dir was wiped by a deploy on 2026-07-26 and Monday's report died
    on mount namespacing).
    """
    if not report_path.exists():
        return [
            Violation(
                "report_stale", SEV3, report_path.name, "report file missing"
            )
        ]
    # UTC-naive on both sides: `now` is UTC-naive by repo convention, and a
    # bare fromtimestamp() would read the mtime in SYSTEM-LOCAL time, skewing
    # the staleness axis by the TZ offset on any non-UTC box.
    mtime_utc = datetime.fromtimestamp(report_path.stat().st_mtime, UTC).replace(
        tzinfo=None
    )
    age = now - mtime_utc
    if age > timedelta(days=max_age_days):
        return [
            Violation(
                "report_stale",
                SEV3,
                report_path.name,
                f"last written {age.days}d ago (limit {max_age_days}d)",
            )
        ]
    return []


def run_sentinel(
    session: Session, now: datetime, *, report_path: Path
) -> list[Violation]:
    """REQ-SEN-001: run every check; return all violations (worst first)."""
    violations = [
        *check_item_staleness(session, now),
        *check_ingestion_staleness(session, now),
        *check_register_snapshot_staleness(session, now),
        *check_scope_anomalies(session),
        *check_register_tx_staleness(session, now),
        *check_report_freshness(report_path, now),
    ]
    violations.sort(key=lambda v: _SEVERITY_ORDER.index(v.severity))
    return violations


def build_sentinel_payload(
    violations: list[Violation], now: datetime
) -> dict[str, str | None] | None:
    """REQ-SEN-008: one severity-webhook payload for the day, or None when clean.

    ``type`` is the worst severity present so n8n routes the whole digest to
    the loudest channel it deserves.
    """
    if not violations:
        return None
    worst = min(violations, key=lambda v: _SEVERITY_ORDER.index(v.severity))
    lines = [
        f"[{v.severity}] {v.check}: {v.subject} — {v.detail}" for v in violations
    ]
    return {
        "source": "freshness_sentinel",
        "type": worst.severity,
        "title": f"Freshness sentinel: {len(violations)} violation(s)",
        "message": "\n".join(lines),
        "alert_key": f"sentinel:{now.date().isoformat()}",
    }


def dispatch_sentinel(
    session: Session,
    now: datetime,
    *,
    report_path: Path,
    apply: bool,
) -> tuple[list[Violation], WebhookResult | None]:
    """REQ-SEN-008: run all checks and POST one digest to the severity webhook.

    Returns ``(violations, webhook_result)``; ``webhook_result`` is None when
    there was nothing to send. ``post_payload`` owns DRY-RUN semantics, the
    HTTPS guard, and never-log-the-secret discipline — one POST path repo-wide.
    """
    violations = run_sentinel(session, now, report_path=report_path)
    payload = build_sentinel_payload(violations, now)
    if payload is None:
        return violations, None
    result = post_payload(payload, key=str(payload["alert_key"]), apply=apply)
    return violations, result
