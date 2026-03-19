"""Deduction email adapter — ingests personal tax deduction emails into the register.

REQ-ID: ADAPTER-DEDUCTION-001  Reads JSON files from the deductions/ folder.
REQ-ID: ADAPTER-DEDUCTION-002  Auto-classifies as personal entity with deduction categories.
REQ-ID: ADAPTER-DEDUCTION-003  Keyword pattern matching selects the specific TaxCategory.
REQ-ID: ADAPTER-DEDUCTION-004  Deduplicates via IngestedFile.file_hash (file-level) and
                                Transaction.source_hash (record-level).
REQ-ID: ADAPTER-DEDUCTION-005  Stores raw_data as the verbatim original JSON object.
REQ-ID: ADAPTER-DEDUCTION-006  Creates IngestionLog entry for every adapter run.

File format: same JSON array format as gmail_n8n — each .json file is an array
containing exactly one object:
    [{
        "id":        "19578f6fd72939df",
        "filename":  "2025-03-15_Charity_XYZ_19578f6fd72939df",
        "date":      "2025-03-15T10:00:00.000Z",
        "from":      "Charity XYZ <receipts@charityxyz.org>",
        "subject":   "Thank you for your donation",
        "body_text": "...",
        "body_html": "..."
    }]

Deduction category keyword rules (checked in priority order):
    CHARITABLE_CASH     — "donation receipt", "tax-deductible", "charitable contribution",
                          "charitable gift", "donate", "donation"
    CHARITABLE_STOCK    — "stock donation", "stock transfer", "donated shares",
                          "noncash contribution"
    MEDICAL             — "medical expense", "explanation of benefits", "eob",
                          "health insurance", "medical bill", "prescription",
                          "dental", "vision", "physician"
    STATE_LOCAL_TAX     — "property tax", "state tax", "local tax", "tax statement",
                          "tax payment confirmation", "excise tax"
    MORTGAGE_INTEREST   — "mortgage interest", "1098", "form 1098", "home loan",
                          "escrow statement"

All deduction transactions are:
    entity    = personal
    direction = expense
    confidence = 0.85 (keyword match — high confidence, still warrants review)
    status    = auto_classified

When no deduction category can be matched, status stays needs_review.

Design spec: §Gmail/n8n Adapter, §Deduplication Strategy, §Tax Categories
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.adapters.gmail_n8n import (
    extract_amount,
    extract_vendor,
    find_attachments,
    normalise_date,
)
from src.models.enums import (
    Direction,
    Entity,
    FileStatus,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.ingested_file import IngestedFile
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.utils.dedup import compute_file_hash, compute_source_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deduction category keyword matching
# ---------------------------------------------------------------------------

# Each entry is (TaxCategory, list_of_patterns).  Patterns are checked against
# the combined subject + body_text of the email (case-insensitive).
# Rules are tried in priority order — first match wins.
_DEDUCTION_RULES: list[tuple[TaxCategory, list[re.Pattern[str]]]] = [
    # MORTGAGE_INTEREST — check before CHARITABLE to avoid "1098" false-positives
    (
        TaxCategory.MORTGAGE_INTEREST,
        [
            re.compile(r"mortgage\s+interest", re.IGNORECASE),
            re.compile(r"\bform\s+1098\b", re.IGNORECASE),
            re.compile(r"\b1098\b", re.IGNORECASE),
            re.compile(r"home\s+loan", re.IGNORECASE),
            re.compile(r"escrow\s+statement", re.IGNORECASE),
        ],
    ),
    # CHARITABLE_STOCK — before CHARITABLE_CASH so stock keywords take priority
    (
        TaxCategory.CHARITABLE_STOCK,
        [
            re.compile(r"stock\s+donation", re.IGNORECASE),
            re.compile(r"stock\s+transfer", re.IGNORECASE),
            re.compile(r"donated\s+shares", re.IGNORECASE),
            re.compile(r"noncash\s+contribution", re.IGNORECASE),
        ],
    ),
    # CHARITABLE_CASH — broad charitable keywords
    (
        TaxCategory.CHARITABLE_CASH,
        [
            re.compile(r"donation\s+receipt", re.IGNORECASE),
            re.compile(r"tax[-\s]deductible", re.IGNORECASE),
            re.compile(r"charitable\s+contribution", re.IGNORECASE),
            re.compile(r"charitable\s+gift", re.IGNORECASE),
            re.compile(r"\bdonate\b", re.IGNORECASE),
            re.compile(r"\bdonation\b", re.IGNORECASE),
        ],
    ),
    # MEDICAL
    (
        TaxCategory.MEDICAL,
        [
            re.compile(r"medical\s+expense", re.IGNORECASE),
            re.compile(r"explanation\s+of\s+benefits", re.IGNORECASE),
            re.compile(r"\beob\b", re.IGNORECASE),
            re.compile(r"health\s+insurance", re.IGNORECASE),
            re.compile(r"medical\s+bill", re.IGNORECASE),
            re.compile(r"\bprescription\b", re.IGNORECASE),
            re.compile(r"\bdental\b", re.IGNORECASE),
            re.compile(r"\bvision\b", re.IGNORECASE),
            re.compile(r"\bphysician\b", re.IGNORECASE),
        ],
    ),
    # STATE_LOCAL_TAX
    (
        TaxCategory.STATE_LOCAL_TAX,
        [
            re.compile(r"property\s+tax", re.IGNORECASE),
            re.compile(r"state\s+tax", re.IGNORECASE),
            re.compile(r"local\s+tax", re.IGNORECASE),
            re.compile(r"tax\s+statement", re.IGNORECASE),
            re.compile(r"tax\s+payment\s+confirmation", re.IGNORECASE),
            re.compile(r"excise\s+tax", re.IGNORECASE),
        ],
    ),
]

# Confidence assigned to keyword-matched transactions.
_KEYWORD_CONFIDENCE: float = 0.85


def classify_deduction(subject: str, body_text: str) -> TaxCategory | None:
    """Return the best-matching TaxCategory for this deduction email.

    Checks subject and body_text together against each rule set in priority
    order.  Returns the first category whose *any* pattern matches, or
    ``None`` when no rule fires.

    Args:
        subject:   Email subject line.
        body_text: Plain-text body of the email.

    Returns:
        Matched :class:`TaxCategory` or ``None``.
    """
    combined = f"{subject}\n{body_text}"
    for category, patterns in _DEDUCTION_RULES:
        if any(p.search(combined) for p in patterns):
            return category
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class DeductionEmailAdapter(BaseAdapter):
    """Ingests personal tax deduction email JSON files.

    Reads the same JSON array format as :class:`GmailN8nAdapter` but
    auto-classifies every transaction as ``entity=personal`` with the
    appropriate :class:`TaxCategory` determined by keyword matching against
    the email subject and body.

    Args:
        source_dirs: Directories to scan for ``*.json`` files.  Defaults to
                     the production ``deductions/`` folder on SGDrive.  Pass
                     alternative paths in tests.
    """

    _DEFAULT_DIRS: tuple[str, ...] = (
        "/Users/travis/SGDrive/LIVE_SYSTEM/accounting/deductions",
    )

    def __init__(self, source_dirs: list[str] | None = None) -> None:
        self._dirs: list[Path] = [
            Path(d) for d in (source_dirs or list(self._DEFAULT_DIRS))
        ]

    @property
    def source(self) -> str:
        return Source.DEDUCTION_EMAIL.value

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, session: Session) -> AdapterResult:
        """Scan configured directories, ingest new deduction JSON files.

        Per-file and per-record errors are isolated: a failure on one file
        does not halt processing of subsequent files.

        An :class:`IngestionLog` row is committed at the end of every run
        regardless of outcome.
        """
        result = AdapterResult(source=self.source)

        for directory in self._dirs:
            if not directory.exists():
                logger.debug("Directory does not exist, skipping: %s", directory)
                continue

            json_files = sorted(directory.glob("*.json"))
            logger.info(
                "DeductionEmailAdapter scanning %s: %d JSON files found",
                directory,
                len(json_files),
            )

            for json_path in json_files:
                try:
                    self._process_file(json_path, session, result)
                except Exception as exc:
                    result.record_error(str(json_path), exc)

        self._write_ingestion_log(session, result)
        return result

    # ------------------------------------------------------------------
    # Per-file processing
    # ------------------------------------------------------------------

    def _process_file(
        self,
        json_path: Path,
        session: Session,
        result: AdapterResult,
    ) -> None:
        """Load a single JSON file and insert a Transaction if not already seen.

        Dedup strategy:
        1. Compute SHA-256 of file bytes → check ``ingested_files.file_hash``.
           If match found → skip entire file.
        2. Compute SHA-256(source, source_id) → check
           ``transactions.source_hash``.  If match found → skip record (file
           may have been moved/renamed but content is identical).
        """
        file_hash = compute_file_hash(json_path)

        # ── Pass 1a: file-level dedup ──────────────────────────────────────
        existing_file = (
            session.query(IngestedFile)
            .filter(IngestedFile.file_hash == file_hash)
            .first()
        )
        if existing_file is not None:
            logger.debug("Skipping already-ingested file: %s", json_path.name)
            result.records_skipped += 1
            result.records_processed += 1
            return

        # ── Load JSON ─────────────────────────────────────────────────────
        try:
            raw_json = json_path.read_text(encoding="utf-8")
            payload = json.loads(raw_json)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Failed to parse {json_path.name}: {exc}") from exc

        if not isinstance(payload, list) or len(payload) == 0:
            raise ValueError(
                f"{json_path.name}: expected a non-empty JSON array, "
                f"got {type(payload).__name__}"
            )

        record = payload[0]

        # ── Extract fields ────────────────────────────────────────────────
        source_id: str = str(record.get("id", ""))
        if not source_id:
            raise ValueError(f"{json_path.name}: missing 'id' field")

        source_hash = compute_source_hash(self.source, source_id)

        # ── Pass 1b: record-level dedup ───────────────────────────────────
        existing_tx = (
            session.query(Transaction)
            .filter(Transaction.source_hash == source_hash)
            .first()
        )
        if existing_tx is not None:
            logger.debug(
                "Skipping already-ingested source_id=%s (duplicate file contents "
                "or re-named file): %s",
                source_id,
                json_path.name,
            )
            self._record_ingested_file(
                session,
                json_path,
                file_hash,
                FileStatus.SKIPPED,
                [existing_tx.id],
            )
            result.records_skipped += 1
            result.records_processed += 1
            return

        # ── Build transaction ─────────────────────────────────────────────
        raw_date: str = record.get("date", "")
        try:
            date_str = normalise_date(raw_date)
        except ValueError as exc:
            raise ValueError(
                f"{json_path.name}: invalid date {raw_date!r}: {exc}"
            ) from exc

        from_field: str = record.get("from", "")
        body_text: str = record.get("body_text", "")
        body_html: str = record.get("body_html", "")
        subject: str = record.get("subject", "")

        vendor = extract_vendor(from_field, body_text)

        # Amount extraction — same logic as gmail_n8n: try body_text first,
        # fall back to HTML, store None when unavailable.
        amount = extract_amount(body_text, subject)
        if amount is None and body_html:
            amount = extract_amount(body_html, subject)

        # ── Deduction classification ───────────────────────────────────────
        tax_category = classify_deduction(subject, body_text)

        if tax_category is not None:
            status = TransactionStatus.AUTO_CLASSIFIED.value
            confidence = _KEYWORD_CONFIDENCE
            review_reason = None
        else:
            status = TransactionStatus.NEEDS_REVIEW.value
            confidence = 0.0
            review_reason = (
                "Could not match a deduction category from subject/body. "
                "Please classify manually."
            )

        # Amount unknown overrides status to needs_review regardless of category match.
        if amount is None:
            status = TransactionStatus.NEEDS_REVIEW.value
            if review_reason:
                review_reason = (
                    "Amount could not be extracted from body_text; "
                    "and could not match a deduction category. "
                    "Manual review required."
                )
            else:
                review_reason = (
                    "Amount could not be extracted from body_text; "
                    "manual review required."
                )
            signed_amount: Decimal | None = None
        else:
            # Deduction payments are expenses (negative).
            signed_amount = -abs(amount)

        # Discover co-located attachments (same convention as gmail_n8n).
        attachments = find_attachments(json_path.parent, source_id)
        attachments = [str(json_path.resolve())] + attachments

        tx = Transaction(
            source=self.source,
            source_id=source_id,
            source_hash=source_hash,
            date=date_str,
            description=vendor,
            amount=signed_amount,
            currency="USD",
            entity=Entity.PERSONAL.value,
            direction=Direction.EXPENSE.value,
            tax_category=tax_category.value if tax_category else None,
            status=status,
            confidence=confidence,
            review_reason=review_reason,
            attachments=attachments,
            raw_data=record,
        )

        session.add(tx)
        session.flush()

        self._record_ingested_file(
            session,
            json_path,
            file_hash,
            FileStatus.SUCCESS,
            [tx.id],
        )

        session.commit()

        result.records_created += 1
        result.records_processed += 1
        logger.info(
            "Ingested %s  vendor=%r  date=%s  amount=%s  tax_category=%s",
            json_path.name,
            vendor,
            date_str,
            signed_amount,
            tax_category,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_ingested_file(
        session: Session,
        json_path: Path,
        file_hash: str,
        status: FileStatus,
        transaction_ids: list[str],
    ) -> None:
        ingested = IngestedFile(
            file_path=str(json_path.resolve()),
            file_hash=file_hash,
            adapter=Source.DEDUCTION_EMAIL.value,
            status=status.value,
            transaction_ids=transaction_ids,
        )
        session.add(ingested)

    @staticmethod
    def _write_ingestion_log(session: Session, result: AdapterResult) -> None:
        """Commit an IngestionLog row summarising this adapter run."""
        error_detail: str | None = None
        if result.errors:
            error_detail = "\n\n".join(
                f"Record {record_id}:\n{tb}" for record_id, tb in result.errors
            )

        log = IngestionLog(
            source=result.source,
            run_at=result.run_at,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=error_detail,
            retryable=False,
        )
        session.add(log)
        session.commit()
