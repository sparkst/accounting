"""Attachment file serving endpoint.

Serves local attachment files (PDFs, images) so the dashboard can display
them inline. Only serves files from the known n8n accounting directories.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["attachments"])

# Only serve files from these trusted directories
_ALLOWED_ROOTS = [
    Path("/Users/travis/SGDrive/LIVE_SYSTEM/accounting"),
    Path("/Users/travis/SGDrive/dev/accounting/data"),
]

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
