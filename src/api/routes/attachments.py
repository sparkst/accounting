"""Attachment file serving and upload endpoint.

Serves local attachment files (PDFs, images) so the dashboard can display
them inline. Only serves files from the known n8n accounting directories.

Also provides a receipt upload endpoint that saves files to
data/receipts/{transaction_id}/ and updates the transaction's attachments
JSON array.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from src.db.connection import SessionLocal
from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attachments"])

# Only serve files from these trusted directories
_ALLOWED_ROOTS = [
    Path("/Users/travis/SGDrive/LIVE_SYSTEM/accounting"),
    Path("/Users/travis/SGDrive/dev/accounting/data"),
]

# Where uploaded receipts are saved
_RECEIPTS_ROOT = Path("/Users/travis/SGDrive/dev/accounting/data/receipts")

_ALLOWED_UPLOAD_MIME = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "application/pdf",
}

_MIME_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}

_MIME_MAP = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".json": "application/json",
}

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


def _is_safe_path(path: Path) -> bool:
    """Verify path is under an allowed root and doesn't escape via symlinks."""
    resolved = path.resolve()
    return any(
        str(resolved).startswith(str(root.resolve()))
        for root in _ALLOWED_ROOTS
    )


@router.get("/attachments/serve")
async def serve_attachment(path: str) -> FileResponse:
    """Serve a local attachment file by its absolute path.

    Only files under the allowed accounting directories are served.
    """
    file_path = Path(path)

    if not file_path.is_absolute():
        raise HTTPException(status_code=400, detail="Path must be absolute")

    if not _is_safe_path(file_path):
        raise HTTPException(status_code=403, detail="Path not in allowed directory")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ext = file_path.suffix.lower()
    media_type = _MIME_MAP.get(ext, "application/octet-stream")

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
    )


@router.post("/transactions/{transaction_id}/upload-receipt")
async def upload_receipt(
    transaction_id: str,
    file: UploadFile = File(...),
) -> JSONResponse:
    """Upload a receipt file and attach it to a transaction.

    Saves the file to data/receipts/{transaction_id}/{filename} and appends
    the absolute path to the transaction's attachments JSON array.

    Accepts: image/jpeg, image/png, image/gif, image/webp, image/heic, application/pdf
    Max size: 20 MB
    """
    # Validate content type
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in _ALLOWED_UPLOAD_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {content_type!r}. Allowed: image/*, application/pdf",
        )

    # Read and size-check
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(data) // 1024} KB). Max is 20 MB.",
        )

    # Determine safe filename
    original_name = Path(file.filename or "receipt").name
    # Strip dangerous characters; keep extension
    safe_stem = "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in Path(original_name).stem
    )
    # Fall back to extension from MIME type if filename has none
    orig_ext = Path(original_name).suffix.lower()
    safe_ext = orig_ext if orig_ext in _MIME_MAP else _MIME_EXT_MAP.get(content_type, "")
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe_filename = f"{timestamp}_{safe_stem}{safe_ext}"

    # Create destination directory and write file
    dest_dir = _RECEIPTS_ROOT / transaction_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_filename
    dest_path.write_bytes(data)

    logger.info(
        "Receipt uploaded: transaction=%s file=%s size=%d bytes",
        transaction_id,
        safe_filename,
        len(data),
    )

    # Update transaction's attachments array in the DB
    with SessionLocal() as session:
        tx: Transaction | None = (
            session.query(Transaction)
            .filter(Transaction.id == transaction_id)
            .first()
        )
        if tx is None:
            # File was written; clean up before returning 404
            dest_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Transaction not found")

        existing: list[str] = tx.attachments or []
        abs_path = str(dest_path.resolve())
        if abs_path not in existing:
            tx.attachments = existing + [abs_path]
        tx.updated_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
        session.refresh(tx)
        updated_attachments: list[str] = tx.attachments or []

    return JSONResponse(
        status_code=200,
        content={
            "path": abs_path,
            "filename": safe_filename,
            "attachments": updated_attachments,
        },
    )
