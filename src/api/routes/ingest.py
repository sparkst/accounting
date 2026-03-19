"""Ingestion trigger endpoints.

POST /api/ingest/run — Runs one or all registered adapters, then runs the
classification engine on all unclassified (needs_review) transactions, and
returns a summary.

Optional query parameter:
    source=<source_value>  Run only the specified adapter (e.g. ?source=stripe).
    When omitted, all registered adapters are run sequentially.

POST /api/ingest/reclassify — Re-extracts vendors for forwarded emails and
re-runs classification on all needs_review transactions using current vendor rules.

POST /api/import/brokerage-csv — Upload a brokerage 1099-B CSV (E*Trade, Schwab,
or Vanguard) for immediate ingestion. Max 50 MB.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from src.adapters import INGEST_SOURCES, get_adapter
from src.adapters.brokerage_csv import (
    SUPPORTED_BROKERAGES,
    BrokerageCsvAdapter,
    detect_brokerage,
)
from src.classification.engine import apply_result, classify
from src.db.connection import SessionLocal
from src.models.enums import Source, TransactionStatus
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.utils.reclassify import reclassify_all

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

# ---------------------------------------------------------------------------
# Concurrency guard — only one ingest run at a time
# ---------------------------------------------------------------------------

_ingest_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class AdapterRunSummary(BaseModel):
    """Per-adapter result within an IngestSummary."""

    source: str
    records_processed: int
    records_created: int
    records_skipped: int
    records_failed: int


class IngestSummary(BaseModel):
    """Result of a POST /api/ingest/run call."""

    ingested_count: int
    classified_count: int
    needs_review_count: int
    adapter_results: list[AdapterRunSummary]
    warnings: list[str]
    errors: list[str]


# ---------------------------------------------------------------------------
# Query parameter singletons (avoids B008 — no function call in default arg)
# ---------------------------------------------------------------------------

_SOURCE_QUERY = Query(
    default=None,
    description=(
        "Run only this adapter (e.g. 'stripe'). "
        "Omit to run all registered adapters."
    ),
)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/ingest/run", response_model=IngestSummary)
def run_ingest(
    source: Source | None = _SOURCE_QUERY,
) -> IngestSummary:
    """Trigger a full ingestion + classification pass.

    Steps:
      1. Acquire concurrency lock — return HTTP 409 if already running.
      2. Run one or all registered adapters, collecting results.
         Sources with missing API keys are skipped (warning included in response).
      3. Run the classification engine on every transaction whose status is
         ``needs_review`` (includes newly ingested items *and* any previously
         stuck items).
      4. Return counts, per-adapter breakdown, warnings, and errors.

    Errors are collected per-step and included in the response rather than
    raising an HTTP 500, so the dashboard can show partial results.
    """
    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                "An ingestion run is already in progress. "
                "Please wait for it to complete before starting another."
            ),
        )

    try:
        return _run_ingest_locked(source)
    finally:
        _ingest_lock.release()


def _run_ingest_locked(source: Source | None) -> IngestSummary:
    """Execute the ingest pass (called only while _ingest_lock is held)."""
    errors: list[str] = []
    warnings: list[str] = []
    ingested_count = 0
    adapter_results: list[AdapterRunSummary] = []

    # Determine which sources to run
    sources_to_run: list[Source] = [source] if source is not None else INGEST_SOURCES

    # ── Step 1: Run adapters ──────────────────────────────────────────────────
    for src in sources_to_run:
        adapter = get_adapter(src)
        if adapter is None:
            msg = (
                f"Adapter for source '{src.value}' is unavailable — "
                "check that required API keys are set."
            )
            warnings.append(msg)
            logger.warning(msg)
            continue

        session = SessionLocal()
        try:
            result = adapter.run(session)
            ingested_count += result.records_created

            # Persist IngestionLog entry for this adapter run.
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

            adapter_results.append(
                AdapterRunSummary(
                    source=adapter.source,
                    records_processed=result.records_processed,
                    records_created=result.records_created,
                    records_skipped=result.records_skipped,
                    records_failed=result.records_failed,
                )
            )

            for _rec_id, err_text in result.errors:
                errors.append(f"[{adapter.source}] {err_text}")

        except Exception:
            tb = traceback.format_exc()
            errors.append(f"[{src.value}] Adapter halted: {tb}")
            logger.error("Adapter %r halted with exception:\n%s", src.value, tb)
        finally:
            session.close()

    # ── Step 2: Classify unclassified transactions ────────────────────────────
    classified_count = 0
    classify_session = SessionLocal()
    try:
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")

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

    # ── Step 3: Count remaining needs_review ─────────────────────────────────
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
        adapter_results=adapter_results,
        warnings=warnings,
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


# ---------------------------------------------------------------------------
# Brokerage CSV import
# ---------------------------------------------------------------------------

_MAX_BROKERAGE_CSV_BYTES = 50 * 1024 * 1024  # 50 MB


class BrokerageCsvImportSummary(BaseModel):
    """Result of POST /api/import/brokerage-csv."""

    brokerage: str
    filename: str
    records_created: int
    records_skipped: int
    records_failed: int
    errors: list[str]


_BROKERAGE_FILE_FIELD = File(..., description="1099-B CSV export from E*Trade, Schwab, or Vanguard")
_BROKERAGE_FORM_FIELD = Form(
    default=None,
    description=(
        "Brokerage format: 'etrade', 'schwab', or 'vanguard'. "
        "Omit to auto-detect from file contents."
    ),
)


@router.post("/import/brokerage-csv", response_model=BrokerageCsvImportSummary)
async def import_brokerage_csv(
    file: UploadFile = _BROKERAGE_FILE_FIELD,
    brokerage: str | None = _BROKERAGE_FORM_FIELD,
) -> BrokerageCsvImportSummary:
    """Import a brokerage 1099-B CSV file.

    Accepts CSV exports from E*Trade, Schwab, and Vanguard.  The brokerage
    format can be specified explicitly or auto-detected from the file header.

    Each row is imported as a Transaction with:
      - entity = personal
      - tax_category = INVESTMENT_INCOME
      - tax_subcategory = CAPITAL_GAIN_SHORT or CAPITAL_GAIN_LONG
      - raw_data preserving cost basis, wash sale adjustment, and all original columns

    Duplicate rows (same security + date + index) are silently skipped via
    source_hash dedup.  A bad row never halts the batch (per-record isolation).

    Args:
        file:      Multipart CSV upload. Maximum 50 MB.
        brokerage: Optional brokerage identifier. Auto-detected when omitted.

    Returns:
        Import summary with counts and any per-row errors.
    """
    # Validate brokerage if provided
    if brokerage is not None and brokerage not in SUPPORTED_BROKERAGES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported brokerage {brokerage!r}. "
                f"Supported values: {list(SUPPORTED_BROKERAGES)}"
            ),
        )

    # Read upload (enforce size limit)
    raw_bytes = await file.read(_MAX_BROKERAGE_CSV_BYTES + 1)
    if len(raw_bytes) > _MAX_BROKERAGE_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File exceeds maximum allowed size of 50 MB.",
        )

    try:
        csv_content = raw_bytes.decode("utf-8-sig")  # utf-8-sig strips BOM if present
    except UnicodeDecodeError:
        try:
            csv_content = raw_bytes.decode("latin-1")
        except UnicodeDecodeError as err:
            raise HTTPException(
                status_code=422,
                detail="Could not decode CSV file. Expected UTF-8 or Latin-1 encoding.",
            ) from err

    filename = file.filename or "upload.csv"

    adapter = BrokerageCsvAdapter(
        csv_content=csv_content,
        brokerage=brokerage,
        filename=filename,
    )

    session = SessionLocal()
    try:
        result = adapter.run(session)
    except Exception as err:
        session.close()
        raise HTTPException(
            status_code=500,
            detail=f"Adapter error: {traceback.format_exc()}",
        ) from err
    finally:
        session.close()

    # Determine the actual brokerage used (may have been auto-detected)
    resolved_brokerage = brokerage or detect_brokerage(csv_content) or "unknown"

    return BrokerageCsvImportSummary(
        brokerage=resolved_brokerage,
        filename=filename,
        records_created=result.records_created,
        records_skipped=result.records_skipped,
        records_failed=result.records_failed,
        errors=[f"{rec_id}: {err}" for rec_id, err in result.errors],
    )
