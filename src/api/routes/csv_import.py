"""Bank CSV import endpoints.

POST /api/import/bank-csv/preview  — Upload a CSV, return decoded preview rows
                                     and detected headers without persisting anything.
POST /api/import/bank-csv/configs  — Create or update a bank CSV column mapping config.
GET  /api/import/bank-csv/configs  — List all saved bank CSV configs.
GET  /api/import/bank-csv/configs/{bank_name}  — Fetch one config.
POST /api/import/bank-csv/commit   — Commit a previously previewed CSV upload.

Upload flow:
  1. POST /preview with multipart file → returns preview rows + config options.
  2. POST /configs to save/update the column mapping for this bank.
  3. POST /commit with multipart file + bank_name → inserts rows, returns summary.

REQ-ID: BANK-CSV-001  Configurable column mapping per bank format.
REQ-ID: BANK-CSV-002  Encoding detection via chardet.
REQ-ID: BANK-CSV-003  Amount parsing (parenthetical negatives, comma thousands, currency).
REQ-ID: BANK-CSV-004  Date format validated; unparseable dates rejected per-record.
REQ-ID: BANK-CSV-005  Cross-reference against existing transactions.
REQ-ID: BANK-CSV-006  Preview (first 5 rows) returned before user confirms.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.adapters.bank_csv import (
    BankCsvAdapter,
    BankCsvConfig,
    parse_csv_bytes,
)
from src.db.connection import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/import", tags=["csv_import"])

# Max upload size: 50 MB
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class BankCsvConfigRequest(BaseModel):
    """Payload for creating or updating a bank CSV column mapping config."""

    bank_name: str = Field(..., description="Unique identifier for this bank format")
    date_column: str = Field("Date", description="CSV column header for the transaction date")
    date_format: str = Field("%m/%d/%Y", description="strptime format string for dates")
    description_column: str = Field("Description", description="CSV column header for payee/memo")
    amount_column: str = Field(
        "Amount",
        description="Signed single-column amount. Leave blank if using debit/credit columns.",
    )
    debit_column: str = Field(
        "",
        description="CSV column for debit amounts (money out). Alternative to amount_column.",
    )
    credit_column: str = Field(
        "",
        description="CSV column for credit amounts (money in). Alternative to amount_column.",
    )
    balance_column: str = Field("", description="Optional running balance column")
    entity: str | None = Field(None, description="Default entity: sparkry | blackline | personal")
    payment_method: str | None = Field(
        None, description="Default payment_method label (e.g. 'Chase ****1234')"
    )


class BankCsvConfigResponse(BaseModel):
    """Saved bank CSV config as returned by the API."""

    bank_name: str
    date_column: str
    date_format: str
    description_column: str
    amount_column: str
    debit_column: str
    credit_column: str
    balance_column: str
    entity: str | None
    payment_method: str | None


class PreviewRow(BaseModel):
    """One row from the CSV preview."""

    row_number: int
    date: str
    description: str
    amount: float
    raw: dict[str, str]


class RowErrorOut(BaseModel):
    """A row that could not be parsed."""

    row_number: int
    reason: str
    raw: dict[str, str]


class PreviewResponse(BaseModel):
    """Response from POST /api/import/bank-csv/preview."""

    headers: list[str]
    preview_rows: list[PreviewRow]
    total_rows: int
    error_rows: list[RowErrorOut]
    encoding_detected: str


class CommitResponse(BaseModel):
    """Response from POST /api/import/bank-csv/commit."""

    records_created: int
    records_skipped: int
    records_failed: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_upload(file: UploadFile) -> bytes:
    """Read upload bytes, enforcing the 50 MB size limit."""
    raw = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum upload size is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    return raw


def _config_to_response(config: BankCsvConfig) -> BankCsvConfigResponse:
    return BankCsvConfigResponse(
        bank_name=config.bank_name,
        date_column=config.date_column,
        date_format=config.date_format,
        description_column=config.description_column,
        amount_column=config.amount_column,
        debit_column=config.debit_column,
        credit_column=config.credit_column,
        balance_column=config.balance_column,
        entity=config.entity,
        payment_method=config.payment_method,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/bank-csv/preview", response_model=PreviewResponse)
def preview_bank_csv(
    file: UploadFile,
    bank_name: str = "default",
) -> PreviewResponse:
    """Upload a bank CSV and return the first 5 rows for review.

    Does not write anything to the database.

    If a saved config exists for *bank_name*, it is used for parsing.
    Otherwise a default config is applied.

    Args:
        file:      Multipart CSV file upload (max 50 MB).
        bank_name: Name of a saved bank config to use for column mapping.
                   Pass "default" or omit to use generic defaults.

    Returns:
        :class:`PreviewResponse` with detected headers, preview rows, and any
        per-record parse errors.
    """
    raw_bytes = _read_upload(file)

    # Load saved config or use default
    config = BankCsvConfig.load(bank_name)
    if config is None:
        config = BankCsvConfig(bank_name=bank_name)

    from src.adapters.bank_csv import detect_encoding
    encoding = detect_encoding(raw_bytes)

    parse_result = parse_csv_bytes(raw_bytes, config)

    preview_rows = [
        PreviewRow(
            row_number=r.row_number,
            date=r.date,
            description=r.description,
            amount=float(r.amount),
            raw=r.raw,
        )
        for r in parse_result.preview
    ]

    error_rows = [
        RowErrorOut(row_number=e.row_number, reason=e.reason, raw=e.raw)
        for e in parse_result.errors
    ]

    return PreviewResponse(
        headers=parse_result.headers,
        preview_rows=preview_rows,
        total_rows=len(parse_result.rows),
        error_rows=error_rows,
        encoding_detected=encoding,
    )


@router.post("/bank-csv/commit", response_model=CommitResponse)
def commit_bank_csv(
    file: UploadFile,
    bank_name: str = "default",
) -> CommitResponse:
    """Import a bank CSV file into the register.

    Parses the file using the saved config for *bank_name* (or defaults if none
    saved), then inserts each valid row as a Transaction with status
    ``needs_review``.  Per-record errors are isolated — a bad row does not halt
    the batch.

    Args:
        file:      Multipart CSV file upload (max 50 MB).
        bank_name: Name of the saved bank config to use for column mapping.

    Returns:
        :class:`CommitResponse` with counts and any errors.
    """
    raw_bytes = _read_upload(file)
    filename = file.filename or "bank_statement.csv"

    config = BankCsvConfig.load(bank_name)
    if config is None:
        config = BankCsvConfig(bank_name=bank_name)

    adapter = BankCsvAdapter(
        raw_bytes,
        config,
        filename=filename,
        dry_run=False,
    )

    with SessionLocal() as session:
        result = adapter.run(session)

    errors = [f"row {rid}: {msg}" for rid, msg in result.errors]

    return CommitResponse(
        records_created=result.records_created,
        records_skipped=result.records_skipped,
        records_failed=result.records_failed,
        errors=errors,
    )


@router.post("/bank-csv/configs", response_model=BankCsvConfigResponse, status_code=201)
def create_or_update_config(payload: BankCsvConfigRequest) -> BankCsvConfigResponse:
    """Create or update a bank CSV column mapping config.

    Configs are persisted to ``data/bank_csv_configs/<bank_name>.json`` and
    reused automatically on subsequent uploads with the same *bank_name*.
    """
    config = BankCsvConfig(
        bank_name=payload.bank_name,
        date_column=payload.date_column,
        date_format=payload.date_format,
        description_column=payload.description_column,
        amount_column=payload.amount_column,
        debit_column=payload.debit_column,
        credit_column=payload.credit_column,
        balance_column=payload.balance_column,
        entity=payload.entity,
        payment_method=payload.payment_method,
    )
    config.save()
    logger.info("Saved bank CSV config for %r", payload.bank_name)
    return _config_to_response(config)


@router.get("/bank-csv/configs", response_model=list[str])
def list_configs() -> list[str]:
    """Return a list of saved bank CSV config names."""
    return BankCsvConfig.list_saved()


@router.get("/bank-csv/configs/{bank_name}", response_model=BankCsvConfigResponse)
def get_config(bank_name: str) -> BankCsvConfigResponse:
    """Fetch a saved bank CSV config by name.

    Returns 404 if no config exists for *bank_name*.
    """
    config = BankCsvConfig.load(bank_name)
    if config is None:
        raise HTTPException(status_code=404, detail=f"No config found for bank: {bank_name!r}")
    return _config_to_response(config)
