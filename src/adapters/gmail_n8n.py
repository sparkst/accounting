"""Gmail/n8n adapter — ingests email receipt JSON files into the register.

REQ-ID: ADAPTER-GMAIL-001  Reads JSON files produced by the n8n Gmail workflow.
REQ-ID: ADAPTER-GMAIL-002  Extracts vendor, date, and amount from structured fields.
REQ-ID: ADAPTER-GMAIL-003  Deduplicates via IngestedFile.file_hash (file-level) and
                            Transaction.source_hash (record-level).
REQ-ID: ADAPTER-GMAIL-004  Links JSON files to co-located PDF/image attachments by
                            shared hex-ID prefix in the filename.
REQ-ID: ADAPTER-GMAIL-005  Stores raw_data as the verbatim original JSON object.

File format (each .json is an array containing exactly one object):
    [{
        "id":        "19578f6fd72939df",
        "filename":  "2025-03-09_Anthropic_PBC_19578f6fd72939df",
        "date":      "2025-03-09T03:33:26.000Z",
        "from":      "Anthropic, PBC <invoice+statements@mail.anthropic.com>",
        "subject":   "Your receipt from Anthropic, PBC #2355-2148",
        "body_text": "...",
        "body_html": "..."
    }]

Attachments live in the same directory and are named:
    <hex_id>_<original_filename>.<ext>

For example the JSON with id="19578f6fd72939df" is accompanied by:
    19578f6fd72939df_Invoice-3F3E740C-0001.pdf
    19578f6fd72939df_Receipt-2355-2148.pdf

Amount handling:
    When no dollar amount can be extracted from body_text (e.g. Google Workspace
    invoices where the amount is only in the PDF attachment, Wi-Fi Onboard receipts
    with no plain-text body, or any "No plain text body available" email), the amount
    is stored as NULL so the dashboard shows "Amount missing" rather than "$0.00".

Forwarded emails:
    When the ``from`` field resolves to Travis Sparks's own address the email is
    self-forwarded.  The real vendor is parsed from the forwarded message header
    inside body_text: ``From: VENDOR <email>`` or ``From: <email>``.

Design spec: §Gmail/n8n Adapter, §Deduplication Strategy
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.models.enums import FileStatus, Source, TransactionStatus
from src.models.ingested_file import IngestedFile
from src.models.transaction import Transaction
from src.utils.dedup import compute_file_hash, compute_source_hash
from src.utils.receipt_ocr import (
    OCRResult,
    extract_receipt,
    find_extractable_attachments,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Amount extraction
# ---------------------------------------------------------------------------

# Ordered list of regex patterns tried against body_text.  The first match
# wins.  All patterns capture a single group: the numeric string (with optional
# commas and an optional decimal part).
#
# Patterns are ordered from most-specific to least-specific so that the most
# reliable signal (e.g. "Amount paid $X") takes precedence over a bare "$X".
_AMOUNT_PATTERNS: list[re.Pattern[str]] = [
    # "Amount paid $238.03" / "Amount paid : $10.00" / "- Amount paid : $10.00"
    re.compile(
        r"amount\s+paid\s*[:\-–]?\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
    # "You paid $2,025.00"
    re.compile(
        r"you\s+paid\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
    # "Receipt from Anthropic, PBC $238.03 Paid …"
    re.compile(
        r"receipt\s+from\s+[^$\n]{1,80}\$\s*([\d,]+(?:\.\d{1,2})?)\s+paid",
        re.IGNORECASE,
    ),
    # "Invoice Amount: $11.51"
    re.compile(
        r"invoice\s+amount\s*[:\-–]?\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
    # "WE'VE RECEIVED YOUR PAYMENT OF $891.07 …"
    re.compile(
        r"payment\s+of\s+\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
    # "Your payment amount of $5.63 to Google …"
    re.compile(
        r"payment\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
    # "Total $2,025.00" (standalone line — avoid matching inside tables)
    re.compile(
        r"(?:^|\n)\s*total\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "Total: $83.73" or "Grand Total: $83.73"
    # Negative lookbehind prevents matching "Subtotal:" or "subtotal:".
    re.compile(
        r"(?<![a-zA-Z])(?:grand\s+)?total\s*[:\-–]\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
    # "Amount: $947.14"
    re.compile(
        r"(?:^|\n)\s*amount\s*[:\-–]\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "TOTAL AMOUNT PAID 3,567.60 USD" (no $ sign, amount before currency)
    re.compile(
        r"total\s+amount\s+(?:paid\s+)?([\d,]+\.\d{2})\s*USD",
        re.IGNORECASE,
    ),
    # Generic fallback: any standalone "$XX.XX" (at least $1.00 to avoid noise)
    # Only used as last resort — placed last so specific patterns win.
    re.compile(
        r"\$\s*([\d,]+\.\d{2})\b",
    ),
]


_PAYMENT_METHOD_PATTERNS = [
    # "Card Type: VISA" + "Acct #: ************5482"
    re.compile(
        r"Card\s+Type:\s*(\w+).*?Acct\s*#?:\s*\*+(\d{4})",
        re.IGNORECASE | re.DOTALL,
    ),
    # "VISA ****1277" or "Mastercard ****4321"
    re.compile(
        r"\b(VISA|Mastercard|AMEX|Discover)\s*\*{2,}(\d{4})\b",
        re.IGNORECASE,
    ),
    # "Payment method Visa ****1277" or "paid with Visa ****1277"
    re.compile(
        r"(?:payment\s+method|paid\s+with)\s+(VISA|Mastercard|AMEX|Discover)\s*\*{2,}(\d{4})",
        re.IGNORECASE,
    ),
    # "ending in 5482" or "card ending 5482"
    re.compile(
        r"(?:card\s+)?ending\s+(?:in\s+)?(\d{4})",
        re.IGNORECASE,
    ),
]


def extract_payment_method(text: str) -> str | None:
    """Extract payment card info like 'VISA ****5482' from text."""
    for pattern in _PAYMENT_METHOD_PATTERNS:
        m = pattern.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                card_type = groups[0].upper()
                last4 = groups[1]
                return f"{card_type} ****{last4}"
            elif len(groups) == 1:
                # "ending in XXXX" pattern — no card type
                return f"****{groups[0]}"
    return None


def extract_amount(body_text: str, subject: str = "") -> Decimal | None:
    """Try each pattern in order; return the first successful match.

    Searches *body_text* first, then falls back to *subject* line.
    Returns ``None`` when no amount can be extracted (triggers needs_review).
    Commas are stripped from the numeric string before conversion.
    """
    for text in (body_text, subject):
        if not text:
            continue
        for pattern in _AMOUNT_PATTERNS:
            m = pattern.search(text)
            if m:
                raw = m.group(1).replace(",", "")
                try:
                    return Decimal(raw)
                except Exception:
                    continue
    return None


# ---------------------------------------------------------------------------
# Shopify order email filter
# ---------------------------------------------------------------------------

_SHOPIFY_ORDER_PATTERNS = [
    re.compile(r"\[.*\]\s*Order\s*#\d+", re.IGNORECASE),
    re.compile(r"\[.*\].*You've got a new order", re.IGNORECASE),
    re.compile(r"\[.*\]\s*New order.*#\d+", re.IGNORECASE),
]


def _is_shopify_order_email(subject: str, from_field: str) -> bool:
    """Detect Shopify order notification emails that should be skipped.

    Sales data comes from the Shopify API integration, not email notifications.
    Only skips order notifications — expense/billing emails from Shopify pass through.
    """
    return any(p.search(subject) for p in _SHOPIFY_ORDER_PATTERNS)


# ---------------------------------------------------------------------------
# Vendor extraction
# ---------------------------------------------------------------------------

# Indicators that the email was forwarded by Travis Sparks to himself.
# When the ``from`` field matches any of these the real vendor is inside
# the forwarded message body.
_SELF_FORWARD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sparkst@gmail\.com", re.IGNORECASE),
    re.compile(r"travis@sparkry\.com", re.IGNORECASE),
    re.compile(r"sparkst@blacklinemtb\.com", re.IGNORECASE),
    re.compile(r"\btravis\s+sparks\b", re.IGNORECASE),
]


def _is_self_forwarded(from_field: str) -> bool:
    """Return True when ``from_field`` belongs to Travis Sparks's own accounts."""
    return any(pattern.search(from_field) for pattern in _SELF_FORWARD_PATTERNS)


def _vendor_from_domain(email: str) -> str:
    """Convert an email address to a humanised domain name.

    Examples::
        "no-reply@supplierpayments.com" -> "supplierpayments.com"
        "noreply@dhl.com"               -> "DHL"
        "noreply@wifionboard.com"       -> "wifionboard.com"
    """
    # Known domain -> display name mappings.
    _DOMAIN_MAP: dict[str, str] = {
        "dhl.com": "DHL",
        "fedex.com": "FedEx",
        "ups.com": "UPS",
        "usps.com": "USPS",
        "apple.com": "Apple",
        "google.com": "Google",
    }
    m = re.search(r"@([a-zA-Z0-9._-]+\.[a-zA-Z]{2,})", email)
    if m:
        domain = m.group(1).lower()
        # Strip leading subdomains (e.g. "mail.dhl.com" → "dhl.com")
        parts = domain.split(".")
        if len(parts) > 2:
            domain = ".".join(parts[-2:])
        return _DOMAIN_MAP.get(domain, domain)
    return email


def _extract_forwarded_vendor(body_text: str) -> str | None:
    """Parse the real vendor from a forwarded-message body.

    Looks for the ``From:`` header line inside the forwarded block:
        ``From: Cloudflare <noreply@notify.cloudflare.com>``
        ``From: <noreply@notify.cloudflare.com>``

    Returns the display name (or domain-derived name) of the original sender,
    or ``None`` when the forwarded header cannot be located.
    """
    # The forwarded block starts after "---------- Forwarded message"
    # but we search the entire body in case the separator is absent.
    # Pattern: "From: Display Name <email>" or "From: <email>"
    m = re.search(
        r"^From:\s+(.*?)\s*<([^>]+)>",
        body_text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        display_name = m.group(1).strip()
        email_addr = m.group(2).strip()
        # If display name is present and not obviously a self-address, use it.
        if display_name and not _is_self_forwarded(display_name + " <" + email_addr + ">"):
            return display_name
        # Fall back to domain-derived name from the email address.
        return _vendor_from_domain(email_addr)

    # Also handle "From: <email@example.com>" (no display name)
    m2 = re.search(
        r"^From:\s+<([^>]+)>",
        body_text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m2:
        domain_vendor = _vendor_from_domain(m2.group(1).strip())
        # If the domain name is generic (e.g. "supplierpayments.com"),
        # try to get a better name from the forwarded Subject line
        if domain_vendor and "payment" in domain_vendor.lower():
            subj = _extract_forwarded_subject(body_text)
            if subj:
                return subj
        return domain_vendor

    return None


def _extract_forwarded_subject(body_text: str) -> str | None:
    """Extract a vendor name from the forwarded Subject line.

    Looks for patterns like "Payment receipt from X" or "receipt from X".
    """
    m = re.search(
        r"Subject:\s+(?:Fwd:\s*)?(?:Payment\s+)?[Rr]eceipt\s+from\s+(.+?)$",
        body_text,
        re.MULTILINE,
    )
    if m:
        return m.group(1).strip()
    return None


def extract_vendor(from_field: str, body_text: str = "") -> str:
    """Extract the human-readable vendor name from a RFC 5322 ``From`` header.

    For self-forwarded emails (from Travis Sparks's own addresses) the real
    vendor is parsed from the forwarded message header inside ``body_text``.

    Examples::

        "Anthropic, PBC <invoice+statements@mail.anthropic.com>", ""
            -> "Anthropic, PBC"
        "Fiverr <noreply@e.fiverr.com>", ""
            -> "Fiverr"
        "payments-noreply@google.com", ""
            -> "payments-noreply@google.com"   (no display name — keep as-is)
        "Travis Sparks <sparkst@gmail.com>", "...From: Cloudflare <...>..."
            -> "Cloudflare"

    Args:
        from_field: Raw value of the ``from`` key in the n8n JSON.
        body_text:  Email body text, used when ``from_field`` is a self-forward.

    Returns:
        Stripped display name if present, otherwise the raw ``from_field``.
        For self-forwards, returns the real vendor extracted from the body.
    """
    from_field = from_field.strip()

    # ── Self-forwarded: look inside the body for the real vendor ──────────────
    if _is_self_forwarded(from_field):
        forwarded_vendor = _extract_forwarded_vendor(body_text)
        if forwarded_vendor:
            return forwarded_vendor
        # Could not find a forwarded From: line — fall through to the normal
        # extraction so we at least get "Travis Sparks" as a fallback.

    # ── Normal: "Display Name <email@example.com>" ────────────────────────────
    m = re.match(r"^(.*?)\s*<[^>]+>\s*$", from_field)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    return from_field


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

def normalise_date(iso_date: str) -> str:
    """Truncate an ISO-8601 datetime string to ``YYYY-MM-DD``.

    Args:
        iso_date: e.g. ``"2025-03-09T03:33:26.000Z"``

    Returns:
        ``"2025-03-09"``

    Raises:
        ValueError: If the string is shorter than 10 characters.
    """
    if len(iso_date) < 10:
        raise ValueError(f"Date string too short to parse: {iso_date!r}")
    return iso_date[:10]


# ---------------------------------------------------------------------------
# Attachment discovery
# ---------------------------------------------------------------------------

def find_attachments(directory: Path, hex_id: str) -> list[str]:
    """Return absolute paths for files in *directory* whose name starts with
    ``hex_id + "_"``.

    The n8n workflow saves attachments alongside the JSON using the naming
    convention ``<hex_id>_<original_filename>.<ext>``.  The JSON file itself
    is excluded (it is the source, not an attachment).

    Args:
        directory: Directory to scan (typically the ``keep/`` folder).
        hex_id:    The ``id`` value from the JSON payload.

    Returns:
        Sorted list of absolute path strings for matching files (may be empty).
    """
    prefix = f"{hex_id}_"
    return sorted(
        str(p.resolve())
        for p in directory.iterdir()
        if p.is_file() and p.name.startswith(prefix) and p.suffix != ".json"
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class GmailN8nAdapter(BaseAdapter):
    """Ingests email receipt JSON files written by the n8n Gmail workflow.

    Args:
        source_dirs: Directories to scan for ``*.json`` files.  Defaults to
                     the production ``keep/`` folder on SGDrive.  Pass
                     alternative paths in tests.
    """

    _DEFAULT_DIRS: tuple[str, ...] = (
        "/Users/travis/SGDrive/LIVE_SYSTEM/accounting/keep",
        "/Users/travis/SGDrive/LIVE_SYSTEM/accounting/for-review",
        "/Users/travis/SGDrive/LIVE_SYSTEM/accounting/manual",
    )

    def __init__(self, source_dirs: list[str] | None = None) -> None:
        self._dirs: list[Path] = [
            Path(d) for d in (source_dirs or list(self._DEFAULT_DIRS))
        ]

    @property
    def source(self) -> str:
        return Source.GMAIL_N8N.value

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, session: Session) -> AdapterResult:
        """Scan configured directories, ingest new JSON files.

        Per-file and per-record errors are isolated: a failure on one file
        does not halt processing of subsequent files.
        """
        result = AdapterResult(source=self.source)

        for directory in self._dirs:
            if not directory.exists():
                logger.debug("Directory does not exist, skipping: %s", directory)
                continue

            json_files = sorted(directory.glob("*.json"))
            logger.info(
                "GmailN8nAdapter scanning %s: %d JSON files found",
                directory,
                len(json_files),
            )

            for json_path in json_files:
                try:
                    self._process_file(json_path, session, result)
                except Exception as exc:
                    result.record_error(str(json_path), exc)

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

        Dedup strategy (Pass 1 — same-source):
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
            # Still record the IngestedFile so we don't re-examine this file path.
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
        subject: str = record.get("subject", "")

        # Skip Shopify order notification emails — sales data comes from
        # the Shopify API integration, not email notifications.
        if _is_shopify_order_email(subject, from_field):
            logger.debug("Skipping Shopify order email: %s", subject[:60])
            result.records_skipped += 1
            result.records_processed += 1
            return

        vendor = extract_vendor(from_field, body_text)

        amount = extract_amount(body_text, subject)
        # Fallback: try HTML body for amounts if body_text had nothing
        body_html: str = record.get("body_html", "")
        if amount is None and body_html:
            amount = extract_amount(body_html, subject)
        payment_method = extract_payment_method(body_text) or extract_payment_method(body_html)

        # Determine status: needs_review when amount could not be extracted.
        # Store None (NULL) for unknown amounts so the dashboard shows
        # "Amount missing" instead of "$0.00".
        if amount is None:
            status = TransactionStatus.NEEDS_REVIEW.value
            review_reason = (
                "Amount could not be extracted from body_text; "
                "manual review required."
            )
            signed_amount = None
            confidence = 0.0
        else:
            status = TransactionStatus.NEEDS_REVIEW.value  # classification pending
            review_reason = None
            confidence = 0.0
            # All gmail receipts are expenses (negative) unless classified otherwise.
            # The amount extracted from body_text is always positive (it's what was
            # charged); store it as negative per the sign convention.
            signed_amount = -abs(amount)

        # Discover co-located attachments.
        attachments = find_attachments(json_path.parent, source_id)
        # Also include the JSON file itself.
        attachments = [str(json_path.resolve())] + attachments

        tx = Transaction(
            source=self.source,
            source_id=source_id,
            source_hash=source_hash,
            date=date_str,
            description=vendor,
            amount=signed_amount,
            currency="USD",
            status=status,
            confidence=confidence,
            review_reason=review_reason,
            payment_method=payment_method,
            attachments=attachments,
            raw_data=record,
        )

        session.add(tx)
        session.flush()  # Assign tx.id before committing IngestedFile.

        # ── Auto-extract from attachments when body is empty ─────────────
        # If we couldn't get an amount or vendor from the email text,
        # try OCR on image/PDF attachments via Claude CLI.
        body_is_empty = (
            not body_text.strip()
            or body_text.strip() == "No plain text body available."
            or len(body_text.strip()) < 20
        )
        if body_is_empty and (signed_amount is None or vendor in ("Travis Sparks", "")):
            image_attachments = find_extractable_attachments(attachments)
            if image_attachments:
                try:
                    ocr: OCRResult = extract_receipt(image_attachments[0])
                    if ocr.success:
                        if ocr.vendor and vendor in ("Travis Sparks", ""):
                            tx.description = ocr.vendor
                        if ocr.amount is not None and tx.amount is None:
                            tx.amount = -abs(ocr.amount)
                        if ocr.date:
                            tx.date = ocr.date
                        if ocr.entity_hint:
                            tx.entity = ocr.entity_hint
                        # Mark as OCR-extracted, still needs human review
                        tx.review_reason = (
                            "Auto-extracted from attachment via Claude CLI. "
                            "Please verify vendor, amount, and entity."
                        )
                        import json as _json
                        tx.notes = (
                            f"[Auto-extracted from {Path(image_attachments[0]).name}]\n"
                            f"{_json.dumps(ocr.raw_response, indent=2)}"
                        )
                        logger.info(
                            "OCR extracted: vendor=%r amount=%s from %s",
                            ocr.vendor, ocr.amount, image_attachments[0],
                        )
                except Exception:
                    logger.warning(
                        "OCR extraction failed for %s, skipping",
                        json_path.name,
                        exc_info=True,
                    )

        # ── Record IngestedFile ────────────────────────────────────────────
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
            "Ingested %s  vendor=%r  date=%s  amount=%s",
            json_path.name,
            vendor,
            date_str,
            signed_amount,
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
            adapter=Source.GMAIL_N8N.value,
            status=status.value,
            transaction_ids=transaction_ids,
        )
        session.add(ingested)
