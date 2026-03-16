"""Receipt OCR via Claude CLI.

Shells out to the `claude` CLI to extract structured data from receipt
images and PDFs. No API key needed — uses the CLI's existing auth.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXTRACTABLE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}

_PROMPT = (
    "Look at this receipt/invoice image. Extract and return ONLY a JSON object "
    "(no markdown, no explanation) with these fields:\n"
    '{"vendor": "store/company name", "amount": 123.45, "date": "YYYY-MM-DD", '
    '"description": "what was purchased", '
    '"entity_hint": "sparkry or blackline or personal"}\n'
    "For entity_hint: sparkry = Sparkry AI LLC (software, consulting, AI tools), "
    "blackline = BlackLine MTB LLC (mountain bike apparel, ecommerce), "
    "personal = personal expense. "
    "amount should be the total paid. date in ISO format."
)


@dataclass
class OCRResult:
    """Structured result from receipt OCR."""

    vendor: str | None = None
    amount: Decimal | None = None
    date: str | None = None
    description: str | None = None
    entity_hint: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    error: str | None = None


def find_extractable_attachments(attachments: list[str]) -> list[str]:
    """Filter attachment paths to only image/PDF files."""
    return [p for p in attachments if Path(p).suffix.lower() in EXTRACTABLE_EXTS]


def extract_receipt(file_path: str) -> OCRResult:
    """Run Claude CLI on a receipt image/PDF and return structured data.

    Returns an OCRResult with success=False if anything goes wrong,
    never raises.
    """
    path = Path(file_path)
    if not path.exists():
        return OCRResult(error=f"File not found: {file_path}")

    if path.suffix.lower() not in EXTRACTABLE_EXTS:
        return OCRResult(error=f"Unsupported file type: {path.suffix}")

    try:
        result = subprocess.run(
            ["claude", "-p", _PROMPT, "--output-format", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return OCRResult(error="Claude CLI not found. Install claude CLI.")
    except subprocess.TimeoutExpired:
        return OCRResult(error="Claude CLI timed out after 60s")

    if result.returncode != 0:
        return OCRResult(error=f"Claude CLI failed: {result.stderr[:300]}")

    try:
        cli_response = json.loads(result.stdout)
        response_text = cli_response.get("result", result.stdout)

        if isinstance(response_text, str):
            text = response_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(text)
        else:
            parsed = response_text

        amount = parsed.get("amount")
        return OCRResult(
            vendor=parsed.get("vendor"),
            amount=Decimal(str(amount)) if amount is not None else None,
            date=parsed.get("date"),
            description=parsed.get("description"),
            entity_hint=parsed.get("entity_hint"),
            raw_response=parsed,
            success=True,
        )
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        return OCRResult(
            error=f"Failed to parse Claude response: {exc}",
            raw_response={"stdout": result.stdout[:500]},
        )
