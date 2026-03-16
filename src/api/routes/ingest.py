"""Ingestion trigger endpoints.

POST /api/ingest/run — Runs the Gmail/n8n adapter, then runs the classification
engine on all unclassified (needs_review) transactions, and returns a summary.

POST /api/ingest/reclassify — Re-extracts vendors for forwarded emails and
re-runs classification on all needs_review transactions using current vendor rules.
"""

from __future__ import annotations

import logging
import os
import traceback

from fastapi import APIRouter
from pydantic import BaseModel

from src.adapters.gmail_n8n import GmailN8nAdapter
from src.classification.engine import apply_result, classify
from src.db.connection import SessionLocal
from src.models.enums import TransactionStatus
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.utils.reclassify import reclassify_all

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class IngestSummary(BaseModel):
    """Result of a POST /api/ingest/run call."""

    ingested_count: int
    classified_count: int
    needs_review_count: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/ingest/run", response_model=IngestSummary)
def run_ingest() -> IngestSummary:
    """Trigger a full ingestion + classification pass.

    Steps:
      1. Run GmailN8nAdapter to pull new email receipts.
      2. Run the classification engine on every transaction whose status is
         ``needs_review`` (this includes newly ingested items *and* any
         previously stuck items).
      3. Return counts and errors.

    Errors are collected per-step and included in the response rather than
    raising an HTTP 500, so the dashboard can show partial results.
    """
    errors: list[str] = []
    ingested_count = 0

    # ── Step 1: Run Gmail adapter ─────────────────────────────────────────────
    session = SessionLocal()
    try:
        adapter = GmailN8nAdapter()
        result = adapter.run(session)
        ingested_count = result.records_created

        # Persist an IngestionLog entry for this run.
        log = IngestionLog(
            source=adapter.source,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=(
                "\n".join(err for _, err in result.errors) if result.errors else None
            ),
        )
        session.add(log)
        session.commit()

        for _rec_id, err_text in result.errors:
            errors.append(f"[gmail_n8n] {err_text}")
    except Exception:
        errors.append(f"[gmail_n8n] Adapter halted: {traceback.format_exc()}")
    finally:
        session.close()

    # ── Step 2: Classify unclassified transactions ────────────────────────────
    classified_count = 0
    classify_session = SessionLocal()
    try:
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")

        # Fetch all needs_review transactions for the classification pass.
        pending: list[Transaction] = (
            classify_session.query(Transaction)
            .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
            .all()
        )

        for tx in pending:
            try:
                classification = classify(
                    tx,
                    classify_session,
                    anthropic_api_key=anthropic_api_key,
                )
                apply_result(tx, classification)
                classify_session.commit()
                classified_count += 1
            except Exception:
                errors.append(
                    f"[classify] tx={tx.id} failed: {traceback.format_exc()}"
                )
                classify_session.rollback()
    finally:
        classify_session.close()

    # ── Step 3: Count remaining needs_review ────────────────────────────────
    count_session = SessionLocal()
    try:
        needs_review_count: int = (
            count_session.query(Transaction)
            .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
            .count()
        )
    finally:
        count_session.close()

    return IngestSummary(
        ingested_count=ingested_count,
        classified_count=classified_count,
        needs_review_count=needs_review_count,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Reclassify endpoint
# ---------------------------------------------------------------------------


class ReclassifySummary(BaseModel):
    """Result of a POST /api/ingest/reclassify call."""

    vendor_updated: int
    classified: int
    still_needs_review: int
    errors: list[str]


@router.post("/ingest/reclassify", response_model=ReclassifySummary)
def run_reclassify() -> ReclassifySummary:
    """Re-extract forwarded-email vendors and reclassify all needs_review transactions.

    Steps:
      1. Seed any missing vendor rules.
      2. Re-extract the real vendor for any self-forwarded Gmail emails.
      3. Run the classification engine on every ``needs_review`` transaction.
      4. Return counts and errors.
    """
    session = SessionLocal()
    try:
        result = reclassify_all(
            session,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            seed_rules=True,
        )
    except Exception:
        session.close()
        return ReclassifySummary(
            vendor_updated=0,
            classified=0,
            still_needs_review=0,
            errors=[f"[reclassify] Fatal error: {traceback.format_exc()}"],
        )
    finally:
        session.close()

    return ReclassifySummary(
        vendor_updated=result.vendor_updated,
        classified=result.classified,
        still_needs_review=result.still_needs_review,
        errors=result.errors,
    )
