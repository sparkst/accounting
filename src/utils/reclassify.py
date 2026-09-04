"""Reclassification utility.

Re-extracts vendors for forwarded emails and re-runs the classification engine
on all ``needs_review`` transactions using the current vendor rules.

Usage (CLI)::

    python -m src.utils.reclassify

Usage (from Python)::

    from src.utils.reclassify import reclassify_all
    from src.db.connection import get_session

    with get_session() as session:
        counts = reclassify_all(session)
        print(counts)

REQ-ID: CLASSIFY-001  Re-run classification on all needs_review transactions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.adapters.gmail_n8n import (
    _extract_forwarded_vendor,
    _has_object_object,
    _is_self_forwarded,
)
from src.classification.engine import apply_result, classify
from src.classification.seed_rules import seed_vendor_rules
from src.db.connection import SessionLocal
from src.models.enums import Source, TransactionStatus
from src.models.transaction import Transaction

logger = logging.getLogger(__name__)


@dataclass
class ReclassifyResult:
    """Counts returned by :func:`reclassify_all`."""

    vendor_updated: int = 0
    classified: int = 0
    still_needs_review: int = 0
    errors: list[str] = field(default_factory=list)


def _update_forwarded_vendor(tx: Transaction) -> bool:
    """Re-extract the vendor for a self-forwarded Gmail email.

    Updates ``tx.description`` in-place if a better vendor is found.

    Returns:
        True when the description was changed.
    """
    raw_data = tx.raw_data
    if not raw_data or not isinstance(raw_data, dict):
        return False

    from_field: str = raw_data.get("from", "")
    body_text: str = raw_data.get("body_text", "")

    if not _is_self_forwarded(from_field):
        return False

    forwarded_vendor = _extract_forwarded_vendor(body_text)

    # accounting#85 (REQ-GMOBJ-01, review round 3): _extract_forwarded_vendor
    # returns the RAW forwarded display name, unlike the adapter's
    # extract_vendor(), which sanitises it. Writing it back here re-introduced
    # the literal "[object Object]" as a description on every reclassify_all
    # run — over the already-sanitised value, and on ALL gmail rows, not just
    # needs_review ones. A corrupted name is never an improvement on what
    # ingest already stored, so keep the stored description.
    if forwarded_vendor and _has_object_object(forwarded_vendor):
        logger.warning(
            "Ignoring corrupted forwarded vendor for tx=%s (kept %r)",
            tx.id[:8],
            tx.description,
        )
        return False

    if forwarded_vendor and forwarded_vendor != tx.description:
        logger.info(
            "Re-extracted vendor for tx=%s: %r -> %r",
            tx.id[:8],
            tx.description,
            forwarded_vendor,
        )
        tx.description = forwarded_vendor
        return True

    return False


def reclassify_all(
    session: Session,
    *,
    llm_api_key: str | None = None,
    seed_rules: bool = True,
) -> ReclassifyResult:
    """Re-extract vendors and re-run classification on all needs_review transactions.

    Steps:
        1. Optionally seed any missing vendor rules.
        2. For every transaction whose source is ``gmail_n8n`` and whose
           ``from`` field indicates a self-forward, re-extract the real vendor
           from the raw_data body_text.
        3. Run the classification engine on every ``needs_review`` transaction.

    Args:
        session: Open SQLAlchemy session.
        llm_api_key: API key for Tier 3 (Gemini) classifier (optional).
            REQ-FIX-ING-010: renamed from ``anthropic_api_key`` — passing an
            Anthropic key here clobbered the correct ``GEMINI_API_KEY`` env
            fallback inside ``genai.Client(api_key=...)``.
        seed_rules: When True, run seed_vendor_rules() to ensure the DB has
            the latest rules before classifying.

    Returns:
        :class:`ReclassifyResult` with summary counts.
    """
    result = ReclassifyResult()

    # ── Step 1: Seed missing vendor rules ────────────────────────────────────
    if seed_rules:
        inserted = seed_vendor_rules(session)
        if inserted:
            logger.info("Seeded %d new vendor rules before reclassification", inserted)

    # ── Step 2: Re-extract vendors for forwarded emails ───────────────────────
    gmail_txns: list[Transaction] = (
        session.query(Transaction)
        .filter(Transaction.source == Source.GMAIL_N8N.value)
        .all()
    )

    for tx in gmail_txns:
        try:
            changed = _update_forwarded_vendor(tx)
            if changed:
                result.vendor_updated += 1
        except Exception as exc:
            logger.warning("Failed to update vendor for tx=%s: %s", tx.id[:8], exc)
            result.errors.append(f"vendor_update tx={tx.id}: {exc}")

    if result.vendor_updated:
        session.flush()
        logger.info("Updated %d vendor descriptions", result.vendor_updated)

    # ── Step 3: Re-classify all needs_review transactions ─────────────────────
    pending: list[Transaction] = (
        session.query(Transaction)
        .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
        .all()
    )

    logger.info("Reclassifying %d needs_review transactions", len(pending))

    for tx in pending:
        try:
            classification = classify(
                tx,
                session,
                llm_api_key=llm_api_key,
            )
            apply_result(tx, classification)
            result.classified += 1
        except Exception as exc:
            logger.warning("Failed to classify tx=%s: %s", tx.id[:8], exc)
            result.errors.append(f"classify tx={tx.id}: {exc}")

    session.commit()

    result.still_needs_review = (
        session.query(Transaction)
        .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
        .count()
    )

    logger.info(
        "Reclassification complete: vendor_updated=%d classified=%d "
        "still_needs_review=%d errors=%d",
        result.vendor_updated,
        result.classified,
        result.still_needs_review,
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run reclassification against the production database and print a summary."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # REQ-FIX-ING-010: Tier 3 is Gemini — read GEMINI_API_KEY, not
    # ANTHROPIC_API_KEY (the old code injected a wrong-provider key into
    # genai.Client(api_key=...), clobbering the correct env fallback).
    api_key = os.getenv("GEMINI_API_KEY")
    session = SessionLocal()
    try:
        result = reclassify_all(session, llm_api_key=api_key)
    finally:
        session.close()

    print(f"Vendor descriptions updated : {result.vendor_updated}")
    print(f"Transactions classified     : {result.classified}")
    print(f"Still needs review          : {result.still_needs_review}")
    if result.errors:
        print(f"Errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
