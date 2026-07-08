"""Concentration-drift alert dispatcher (REQ-IPD-004).

When the AMZN+MSFT combined concentration closes more than
``drift_alert_threshold_pts`` (default 3) above the glide line, POST one
``info``-severity payload to the shared n8n severity webhook, deduped to one per
calendar month via the ``alert_dispatch`` ledger (key ``policy_drift:<YYYY-MM>``).

Per the plaid-alert-reliability spec §8 (REQ-FIX-ALR-002) this is a webhook
emitter, so every ledger write persists ``payload_json`` and sets
``delivery_channel="n8n_webhook"`` — a transient n8n failure is retriable by that
spec's failed-row sweep rather than lost.

DRY-RUN by default; ``--apply`` to POST + record.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.alerts.models import AlertDispatch  # noqa: E402
from src.analytics.policy import compute_policy  # noqa: E402
from src.analytics.policy_config import load_policy_config  # noqa: E402
from src.balance_alerts.webhook import build_payload_dict, post_payload  # noqa: E402

logger = logging.getLogger(__name__)

ALERT_TYPE = "policy_drift"


@dataclass
class DriftSummary:
    fired: bool = False
    drift_pts: str | None = None
    status: str = "no_drift"  # no_drift | dry_run | sent | failed | skipped


def _month_key(today: date) -> tuple[str, str]:
    """Return (alert_key, occurrence_date) — one dedup slot per calendar month."""
    return f"policy_drift:{today:%Y-%m}", today.replace(day=1).isoformat()


def dispatch_policy_drift(
    today: date, session: Session, *, apply: bool
) -> DriftSummary:
    """Compute drift; if over threshold, POST one info alert (deduped per month)."""
    cfg = load_policy_config()
    result = compute_policy(session, cfg, today)
    drift = result.current_pct - result.glide_pct
    threshold = result.drift_alert_threshold_pts
    summary = DriftSummary(drift_pts=str(drift))

    if drift <= threshold:
        return summary
    summary.fired = True

    alert_key, occurrence_date = _month_key(today)
    existing = (
        session.query(AlertDispatch)
        .filter_by(alert_key=alert_key, occurrence_date=occurrence_date)
        .one_or_none()
    )
    if existing is not None and existing.status == "sent":
        summary.status = "skipped"
        return summary

    title = "Concentration drift above glide"
    message = (
        f"AMZN+MSFT at {result.current_pct:.1f}% vs glide {result.glide_pct:.1f}% "
        f"(+{drift:.1f} pts, threshold {threshold:.0f}). "
        f"Investable base ${result.investable_base:,.0f}."
    )
    payload = build_payload_dict(
        severity="info", title=title, message=message, alert_key=alert_key
    )

    if not apply:
        summary.status = "dry_run"
        return summary

    result_post = post_payload(payload, key=alert_key, apply=True)
    payload_json = json.dumps(payload)
    if existing is not None:
        existing.status = result_post.status
        existing.http_status = result_post.http_status
        existing.error_detail = result_post.error
        existing.delivery_channel = "n8n_webhook"
        existing.payload_json = payload_json
        session.commit()
    else:
        row = AlertDispatch(
            alert_key=alert_key,
            occurrence_date=occurrence_date,
            alert_type=ALERT_TYPE,
            entity="personal",
            subject=title,
            status=result_post.status,
            http_status=result_post.http_status,
            error_detail=result_post.error,
            delivery_channel="n8n_webhook",
            payload_json=payload_json,
        )
        try:
            with session.begin_nested():
                session.add(row)
            session.commit()
        except IntegrityError:
            session.rollback()
    summary.status = result_post.status
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="POST + record (default: dry-run).")
    parser.add_argument("--date", help="Override 'today' (YYYY-MM-DD).")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date()
    from src.db.connection import get_session

    with get_session() as session:
        summary = dispatch_policy_drift(today, session, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[policy_drift] {mode} drift={summary.drift_pts}pts status={summary.status}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
