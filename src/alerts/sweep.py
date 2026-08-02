"""Shared failed-webhook-row sweep — REQ-FIX-ALR-002.

`alert_dispatch` is shared by every alert/report emitter in this program's
n8n_webhook channel (EA alerts, balance alerts, and — added by later
workstreams — `policy_drift_dispatch.py` and the AR-chaser draft-notification
POST). Each emitter's write path persists `payload_json` (the exact dict
handed to `httpx.post`) and `delivery_channel="n8n_webhook"` on every row it
writes. At the top of each `--apply` dispatch run, the emitter calls
`sweep_failed_rows` so a transient webhook failure is retried on the next run
rather than lost forever.

This function is intentionally alert_type-agnostic — the predicate below is
the full contract (channel/status/date-window/payload-not-null). Callers MAY
narrow further with `alert_types` as a practical safety net so one dispatcher
doesn't attempt to replay another emitter's row through its own webhook
target (this repo currently has two disjoint n8n webhook targets: EA's
`N8N_ALERTS_WEBHOOK_URL` and balance's `N8N_SEVERITY_WEBHOOK_URL`).
`resend_email`-channel rows are never swept — Resend emitters regenerate
fresh content on their own timer rather than replaying stale copy.

REQ-FIX-ALR-007 (2026-08-02 incident): replaying `payload_json` VERBATIM is
only safe for durable, date-keyed content (tax deadlines, milestone
crossings). Point-in-time digests (e.g. `balance_pulse`) go stale within
hours — a replay re-delivers yesterday's balances as if current. Callers
declare such types via `same_day_only_types`: a failed row of a listed type
is only replayed on the SAME `occurrence_date` as the run; older rows are
flipped to the terminal status `'superseded'` (never re-examined — the sweep
predicate matches `status='failed'` only) instead of being re-POSTed. The
next scheduled run of the emitter regenerates fresh content anyway, so
nothing is lost.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 7


@dataclass
class SweepSummary:
    resent: int = 0
    still_failed: int = 0
    # DRY-RUN only: rows that WOULD be re-POSTed (query runs, nothing sent/written).
    candidates: int = 0
    # REQ-FIX-ALR-007: stale same-day-only rows retired instead of replayed
    # (apply: rows flipped to status='superseded'; DRY-RUN: rows that would be).
    superseded: int = 0


_SUPERSEDED_DETAIL = (
    "stale digest — superseded, not replayed (same-day-only alert type)"
)


def sweep_failed_rows(
    session: Session,
    today: date,
    *,
    post: Callable[[dict[str, object]], WebhookResult],
    apply: bool,
    alert_types: Sequence[str] | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    same_day_only_types: Sequence[str] = (),
) -> SweepSummary:
    """Re-POST failed n8n_webhook rows from the last `lookback_days` days.

    Predicate: `(delivery_channel='n8n_webhook' OR delivery_channel IS NULL)
    AND status='failed' AND occurrence_date >= today-lookback_days AND
    payload_json IS NOT NULL` [`AND alert_type IN alert_types` when provided].
    The explicit IS NULL arm captures pre-migration legacy rows (every
    pre-migration emitter was webhook-only) once a payload exists for them.

    REQ-FIX-ALR-007: rows whose `alert_type` is in `same_day_only_types` are
    only replayed when `occurrence_date == today` — their stored payload is a
    point-in-time digest that goes stale within hours. Older rows are flipped
    to the terminal status `'superseded'` (with `error_detail` explaining why)
    so no later sweep ever replays them.

    DRY-RUN (`apply=False`) makes no POST and no write, but DOES run the sweep
    query and reports the would-be-resent rows in `summary.candidates` and the
    would-be-superseded rows in `summary.superseded` (and a log line per row),
    so an operator can see what a real run would replay.
    Per-row isolation: one raising re-POST never halts the sweep.
    """
    summary = SweepSummary()
    cutoff = (today - timedelta(days=lookback_days)).isoformat()
    query = session.query(AlertDispatch).filter(
        or_(
            AlertDispatch.delivery_channel == "n8n_webhook",
            AlertDispatch.delivery_channel.is_(None),
        ),
        AlertDispatch.status == "failed",
        AlertDispatch.occurrence_date >= cutoff,
        AlertDispatch.payload_json.is_not(None),
    )
    if alert_types is not None:
        query = query.filter(AlertDispatch.alert_type.in_(alert_types))

    def _is_stale_same_day_only(row: AlertDispatch) -> bool:
        return (
            row.alert_type in same_day_only_types
            and row.occurrence_date != today.isoformat()
        )

    if not apply:
        for row in query.all():
            if _is_stale_same_day_only(row):
                summary.superseded += 1
                logger.info(
                    "sweep DRY-RUN would supersede stale %s@%s",
                    row.alert_key,
                    row.occurrence_date,
                )
                continue
            summary.candidates += 1
            logger.info(
                "sweep DRY-RUN would resend %s@%s", row.alert_key, row.occurrence_date
            )
        return summary

    for row in query.all():
        if _is_stale_same_day_only(row):
            row.status = "superseded"
            row.error_detail = _SUPERSEDED_DETAIL
            session.commit()
            summary.superseded += 1
            logger.info(
                "sweep: superseded stale %s@%s (same-day-only type %s)",
                row.alert_key,
                row.occurrence_date,
                row.alert_type,
            )
            continue
        try:
            assert row.payload_json is not None  # guaranteed by the query filter
            payload = json.loads(row.payload_json)
            result = post(payload)
            row.status = result.status
            row.http_status = result.http_status
            row.error_detail = result.error
            session.commit()
            if result.status == "sent":
                summary.resent += 1
            else:
                summary.still_failed += 1
        except Exception:  # noqa: BLE001 — per-row isolation, one bad re-POST
            # never halts the sweep or the caller's main dispatch run.
            logger.exception("sweep: row %s raised during re-POST", row.alert_key)
            summary.still_failed += 1
            session.rollback()
    return summary
