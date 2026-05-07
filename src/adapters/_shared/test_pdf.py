"""Tests for src.adapters._shared.pdf — uses an existing tiny fixture PDF."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.adapters._shared.pdf import pdftotext_layout

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_GSK_FIXTURE = Path("/Users/travis/Downloads/accounts/gsk/GSK Cash Balance Account Activity.pdf")


def _have_pdftotext() -> bool:
    import shutil

    return shutil.which("pdftotext") is not None


@pytest.mark.skipif(not _have_pdftotext(), reason="pdftotext binary not installed")
@pytest.mark.skipif(
    not _GSK_FIXTURE.exists(), reason="GSK fixture PDF not present"
)
def test_pdftotext_layout_extracts_known_phrase() -> None:
    text = pdftotext_layout(_GSK_FIXTURE)
    assert "Closing Balance" in text
    assert "GSK Cash Balance Pension Plan" in text


def test_missing_file_raises_filenotfound() -> None:
    with pytest.raises(FileNotFoundError):
        pdftotext_layout(_FIXTURE_DIR / "definitely-not-here.pdf")


def test_missing_binary_raises_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("src.adapters._shared.pdf.shutil.which", return_value=None),
        pytest.raises(FileNotFoundError, match="pdftotext"),
    ):
        pdftotext_layout(Path("/tmp/anything.pdf"))


@pytest.mark.skipif(not _have_pdftotext(), reason="pdftotext binary not installed")
def test_nonzero_returncode_raises_runtimeerror(tmp_path: Path) -> None:
    # Feed pdftotext a non-PDF file → it returns non-zero.
    bad = tmp_path / "not-a-pdf.pdf"
    bad.write_bytes(b"this is plain text, not PDF")
    with pytest.raises(RuntimeError, match="pdftotext failed"):
        pdftotext_layout(bad)
