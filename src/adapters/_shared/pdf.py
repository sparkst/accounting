"""Subprocess wrapper for Poppler's ``pdftotext`` binary.

Phase-4 PDF adapters (F&G, GSK, FT statements) all start by extracting layout
text from a PDF; this helper isolates the binary dependency in one place so the
adapters stay testable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Final

_PDFTOTEXT_BINARY: Final[str] = "pdftotext"


def pdftotext_layout(path: Path) -> str:
    """Return the layout-preserving text extraction of ``path``.

    Raises ``FileNotFoundError`` if the binary or input PDF is missing,
    ``RuntimeError`` if the binary returns non-zero.
    """
    binary = shutil.which(_PDFTOTEXT_BINARY)
    if binary is None:
        raise FileNotFoundError(
            f"{_PDFTOTEXT_BINARY!r} not on PATH; install Poppler"
            " (`brew install poppler`)"
        )
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    proc = subprocess.run(
        [binary, "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pdftotext failed (rc={proc.returncode}) on {path}: {proc.stderr.strip()}"
        )
    return proc.stdout
