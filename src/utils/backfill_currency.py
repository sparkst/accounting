"""One-time backfill: detect foreign currency in existing transactions.

Scans all transactions for foreign currency amounts in their raw_data body text.
- If amount is None and foreign currency found: convert and set amount.
- If amount exists and foreign currency found: store foreign amount for reference.

Usage::

    python -m src.utils.backfill_currency

Safe to run multiple times (skips transactions that already have currency_code set).
"""

from __future__ import annotations

import json
import logging
import sys
from decimal import Decimal

from sqlalchemy import select

from src.db.connection import SessionLocal, init_db
from src.models.transaction import Transaction
from src.utils.currency import convert_to_usd, detect_currency

logger = logging.getLogger(__name__)


def backfill(dry_run: bool = False) -> dict[str, int]:
    """Scan and update existing transactions with foreign currency info.

    Returns a summary dict with counts of updated/skipped transactions.
    """
    init_db()
    session = SessionLocal()

    stats = {"scanned": 0, "updated_amount": 0, "updated_reference": 0, "skipped": 0, "errors": 0}

    try:
        stmt = select(Transaction).where(Transaction.source == "gmail_n8n")
        transactions = list(session.scalars(stmt).all())

        for tx in transactions:
            stats["scanned"] += 1

            # Skip if already has currency_code set
            if tx.currency_code is not None:
                stats["skipped"] += 1
                continue

            # Extract body text from raw_data
            body_text = ""
            body_html = ""
            if isinstance(tx.raw_data, dict):
                body_text = tx.raw_data.get("body_text", "") or ""
                body_html = tx.raw_data.get("body_html", "") or ""
            elif isinstance(tx.raw_data, str):
                try:
                    raw = json.loads(tx.raw_data)
                    if isinstance(raw, dict):
                        body_text = raw.get("body_text", "") or ""
                        body_html = raw.get("body_html", "") or ""
                except (json.JSONDecodeError, TypeError):
                    pass

            search_text = body_text or body_html
            if not search_text:
                stats["skipped"] += 1
                continue

            # Detect foreign currency
            hits = detect_currency(search_text)
            if not hits and body_html and search_text == body_text:
                hits = detect_currency(body_html)

            if not hits:
                stats["skipped"] += 1
                continue

            best = hits[0]

            try:
                if tx.amount is None:
                    # No USD amount — convert foreign amount
                    conversion = convert_to_usd(best.amount, best.currency_code, tx.date)
                    if conversion is not None:
                        if not dry_run:
                            tx.currency_code = best.currency_code
                            tx.amount_foreign = best.amount
                            tx.amount = -Decimal(str(conversion.usd_amount))
                            tx.exchange_rate = conversion.rate
                            tx.exchange_rate_source = conversion.source
                            if tx.review_reason and "Amount could not be extracted" in tx.review_reason:
                                tx.review_reason = (
                                    f"Amount converted from {best.currency_code} "
                                    f"{best.amount:.2f} via Frankfurter API. "
                                    "Please verify."
                                )
                        logger.info(
                            "%s tx %s: %s %.2f → USD %.2f (date=%s)",
                            "Would update" if dry_run else "Updated",
                            tx.id[:8], best.currency_code, best.amount,
                            conversion.usd_amount, tx.date,
                        )
                        stats["updated_amount"] += 1
                    else:
                        logger.warning(
                            "Could not convert %s %.2f for tx %s (API error)",
                            best.currency_code, best.amount, tx.id[:8],
                        )
                        stats["errors"] += 1
                else:
                    # USD amount exists — store foreign amount for reference
                    if not dry_run:
                        tx.currency_code = best.currency_code
                        tx.amount_foreign = best.amount
                        if best.amount > 0:
                            tx.exchange_rate = float(abs(tx.amount)) / best.amount
                            tx.exchange_rate_source = "email_extracted"
                    logger.info(
                        "%s tx %s: stored reference %s %.2f (USD amount=%.2f)",
                        "Would update" if dry_run else "Updated",
                        tx.id[:8], best.currency_code, best.amount,
                        float(abs(tx.amount)),
                    )
                    stats["updated_reference"] += 1
            except Exception as exc:
                logger.error("Error processing tx %s: %s", tx.id[:8], exc)
                stats["errors"] += 1

        if not dry_run:
            session.commit()
            logger.info("Committed changes.")
        else:
            logger.info("Dry run — no changes committed.")

    finally:
        session.close()

    return stats


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("DRY RUN mode — no changes will be written.")

    stats = backfill(dry_run=dry_run)

    logger.info(
        "Backfill complete: scanned=%d updated_amount=%d updated_reference=%d "
        "skipped=%d errors=%d",
        stats["scanned"], stats["updated_amount"], stats["updated_reference"],
        stats["skipped"], stats["errors"],
    )


if __name__ == "__main__":
    main()
