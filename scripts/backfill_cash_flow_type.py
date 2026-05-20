"""REQ-PERF-003: Idempotent backfill of ``BrokerageTransaction.cash_flow_type``.

Walks every ``BrokerageTransaction`` row, calls
``src.analytics.classify.classify(tx, PortfolioScope)`` to derive the
portfolio-scope cash-flow type, and writes it to the row.

DRY-RUN by default. Use ``--apply`` to write.

Per-row error isolation (CLAUDE.md): one bad row never halts the batch.
Idempotent: re-running with ``--apply`` is a no-op if the column is already
correct.

Usage::

    python -m scripts.backfill_cash_flow_type             # dry-run
    python -m scripts.backfill_cash_flow_type --apply     # write
    python -m scripts.backfill_cash_flow_type --apply --batch-size 500
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.analytics.classify import ClassifyError, PortfolioScope, classify  # noqa: E402
from src.db.connection import SessionLocal  # noqa: E402
from src.models.brokerage import BrokerageTransaction  # noqa: E402
from src.models.enums import CashFlowType  # noqa: E402

logger = logging.getLogger(__name__)


def _classify_one(tx: BrokerageTransaction) -> CashFlowType | None:
    """Return the desired classification, or None if classification failed."""
    try:
        return classify(tx, PortfolioScope())
    except ClassifyError as exc:
        logger.warning(
            "classify failed tx=%s action=%s: %s",
            tx.id,
            tx.canonical_action,
            exc,
        )
        return None


BackfillResult = dict[str, int | dict[str, int]]


def backfill(
    session: Session, *, apply: bool, batch_size: int = 1000
) -> BackfillResult:
    """Backfill ``cash_flow_type``. Returns dict of counts and per-action breakdowns."""
    changed: Counter[str] = Counter()
    unchanged: Counter[str] = Counter()
    errors = 0
    examined = 0

    # Stream rows so we don't load the whole table into memory.
    stmt = select(BrokerageTransaction).execution_options(yield_per=batch_size)
    for tx in session.execute(stmt).scalars():
        examined += 1
        desired = _classify_one(tx)
        if desired is None:
            errors += 1
            continue

        current_raw = tx.cash_flow_type
        if current_raw == desired.value:
            unchanged[tx.canonical_action] += 1
            continue

        changed[tx.canonical_action] += 1
        if apply:
            tx.cash_flow_type = desired.value

    if apply:
        session.commit()

    return {
        "examined": examined,
        "changed_total": sum(changed.values()),
        "unchanged_total": sum(unchanged.values()),
        "errors": errors,
        "by_action_changed": dict(changed),
        "by_action_unchanged": dict(unchanged),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="REQ-PERF-003 cash_flow_type backfill"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes. Default is dry-run (read-only).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Rows per yield_per batch (memory tuning only).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Starting cash_flow_type backfill (%s)", mode)

    session = SessionLocal()
    try:
        result = backfill(session, apply=args.apply, batch_size=args.batch_size)
    finally:
        session.close()

    logger.info(
        "Done. examined=%d changed=%d unchanged=%d errors=%d",
        result["examined"],
        result["changed_total"],
        result["unchanged_total"],
        result["errors"],
    )
    by_changed = result["by_action_changed"]
    assert isinstance(by_changed, dict)  # narrow for mypy
    for action, count in sorted(by_changed.items()):
        logger.info("  changed %s: %d", action, count)
    changed_total = result["changed_total"]
    if not args.apply and isinstance(changed_total, int) and changed_total:
        logger.warning(
            "DRY-RUN: %d rows would change. Re-run with --apply.", changed_total
        )

    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
