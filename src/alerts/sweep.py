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


def sweep_failed_rows(
    session: Session,
    today: date,
    *,
    post: Callable[[dict[str, object]], WebhookResult],
    apply: bool,
    alert_types: Sequence[str] | None = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> SweepSummary:
    """Re-POST failed n8n_webhook rows from the last `lookback_days` days.

    Predicate: `(delivery_channel='n8n_webhook' OR delivery_channel IS NULL)
    AND status='failed' AND occurrence_date >= today-lookback_days AND
    payload_json IS NOT NULL` [`AND alert_type IN alert_types` when provided].
    The explicit IS NULL arm captures pre-migration legacy rows (every
    pre-migration emitter was webhook-only) once a payload exists for them.

    DRY-RUN (`apply=False`) makes no POST and no write, but DOES run the sweep
    query and reports the would-be-resent rows in `summary.candidates` (and a
    log line per row), so an operator can see what a real run would replay.
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

    if not apply:
        for row in query.all():
            summary.candidates += 1
            logger.info(
                "sweep DRY-RUN would resend %s@%s", row.alert_key, row.occurrence_date
            )
        return summary

    for row in query.all():
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
