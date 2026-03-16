"""SHA-256 deduplication utilities.

REQ-ID: DEDUP-001  Same-source dedup via SHA256(source_type, source_id).
REQ-ID: DEDUP-002  File-content dedup via SHA256 of raw file bytes.

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Deduplication Strategy
"""

import hashlib
from pathlib import Path


def compute_source_hash(source_type: str, source_id: str) -> str:
    """Return SHA-256 hex digest of a length-framed concatenation of
    ``source_type`` and ``source_id``.

    This is the primary same-source dedup key stored in
    ``transactions.source_hash``.  Two records from the same adapter with the
    same source_id will produce an identical hash and the second insert will be
    skipped by the UNIQUE constraint.

    The payload format is ``"<len(source_type)>:<source_type>:<source_id>"``
    (e.g. ``"9:gmail_n8n:19578f6fd72939df"``).  The length prefix prevents
    collisions when either field contains a colon — for example
    ``compute_source_hash("stripe", "abc:def")`` and
    ``compute_source_hash("stripe:abc", "def")`` are guaranteed to differ.

    Args:
        source_type: The ``Source`` enum value (e.g. ``"gmail_n8n"``).
        source_id:   The original identifier from the source system (e.g. the
                     n8n JSON ``id`` field like ``"19578f6fd72939df"``).

    Returns:
        64-character lower-case hex string.
    """
    payload = f"{len(source_type)}:{source_type}:{source_id}"
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_file_hash(file_path: Path) -> str:
    """Return SHA-256 hex digest of a file's raw bytes.

    Used to populate ``ingested_files.file_hash`` so that re-processing an
    identical file (same bytes, possibly moved) is detected and skipped.

    Args:
        file_path: Absolute path to the file on disk.

    Returns:
        64-character lower-case hex string.

    Raises:
        FileNotFoundError: If ``file_path`` does not exist.
        OSError: On any other I/O error.
    """
    h = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
