"""Cloud-mode tests for the F&G PDF adapter (IC-T02).

All network calls are mocked — no real Workers endpoint is required.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.fg_pdf import (
    _CLOUD_INGEST_SOURCE,
    SOURCE_TAG,
    _default_target,
    import_pdf_cloud,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

ANNUAL_PDF_TEXT = """\
F&G Life Insurance and Annuity Company
Annual Statement
Contract Number: 12345678
As of December 31, 2025
Accumulated Value: $125,000.00
"""

PORTAL_PDF_TEXT = """\
F&G Life Insurance
Contract: 87654321
Closing Balance $98,500.50
"""


def _make_fake_pdf(tmp_path: Path, name: str = "fg_statement.pdf") -> Path:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.4 fake")
    return pdf


@pytest.fixture()
def mock_post(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value={"accepted": 1, "rejected": 0})
    monkeypatch.setattr("src.adapters.fg_pdf.post_to_wealth", mock)
    return mock


# ── _default_target tests ─────────────────────────────────────────────────────

def test_default_target_reads_env(monkeypatch):
    monkeypatch.setenv("WEALTH_TARGET_DEFAULT", "cloud")
    assert _default_target() == "cloud"


def test_default_target_falls_back_to_local(monkeypatch):
    monkeypatch.delenv("WEALTH_TARGET_DEFAULT", raising=False)
    assert _default_target() == "local"


def test_default_target_ignores_empty_string(monkeypatch):
    monkeypatch.setenv("WEALTH_TARGET_DEFAULT", "")
    assert _default_target() == "local"


# ── import_pdf_cloud happy path ───────────────────────────────────────────────

def test_cloud_posts_to_correct_slug(tmp_path, monkeypatch, mock_post):
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    # Also patch extract helpers to return known values
    monkeypatch.setattr(
        "src.adapters.fg_pdf.detect_template",
        lambda text: "annual",
    )
    from decimal import Decimal
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert result.errors == []
    mock_post.assert_called_once()
    _, source = mock_post.call_args[0]
    assert source == _CLOUD_INGEST_SOURCE
    assert source == "xlsx-snapshot"


def test_cloud_payload_shape(tmp_path, monkeypatch, mock_post):
    from decimal import Decimal
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    pdf = _make_fake_pdf(tmp_path)
    import_pdf_cloud(pdf)

    payload = mock_post.call_args[0][0]
    assert "rows" in payload
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert "raw_account_name" in row
    assert "12345678" in row["raw_account_name"]
    assert row["as_of"] == "2025-12-31"
    assert row["source"] == SOURCE_TAG
    assert "source_row_hash" in row
    assert len(row["source_row_hash"]) == 64


def test_cloud_balance_is_decimal_string(tmp_path, monkeypatch, mock_post):
    from decimal import Decimal
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.50")),
    )
    pdf = _make_fake_pdf(tmp_path)
    import_pdf_cloud(pdf)

    row = mock_post.call_args[0][0]["rows"][0]
    assert isinstance(row["balance"], str)
    assert row["balance"] == "125000.50"


def test_cloud_as_of_override(tmp_path, monkeypatch, mock_post):
    from decimal import Decimal
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    pdf = _make_fake_pdf(tmp_path)
    override_date = date(2025, 3, 31)
    import_pdf_cloud(pdf, as_of=override_date)

    row = mock_post.call_args[0][0]["rows"][0]
    assert row["as_of"] == "2025-03-31"


def test_cloud_result_counts(tmp_path, monkeypatch, mock_post):
    from decimal import Decimal
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert result.imported == 1
    assert result.errors == []


# ── Per-record error isolation ────────────────────────────────────────────────

def test_cloud_pdftotext_failure_yields_error_no_post(tmp_path, monkeypatch, mock_post):
    monkeypatch.setattr(
        "src.adapters.fg_pdf.pdftotext_layout",
        lambda p: (_ for _ in ()).throw(RuntimeError("pdftotext not found")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert len(result.errors) == 1
    assert "pdftotext" in result.errors[0]
    mock_post.assert_not_called()


def test_cloud_parse_failure_yields_error_no_post(tmp_path, monkeypatch, mock_post):
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: "garbage text")
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: (_ for _ in ()).throw(ValueError("no contract number found")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert len(result.errors) == 1
    mock_post.assert_not_called()


def test_cloud_post_failure_yields_error_no_raise(tmp_path, monkeypatch):
    from decimal import Decimal

    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    monkeypatch.setattr(
        "src.adapters.fg_pdf.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert len(result.errors) == 1
    assert "cloud POST" in result.errors[0]
    assert result.imported == 0


# ── CLI tests ─────────────────────────────────────────────────────────────────

def test_cli_dry_run_does_not_post(tmp_path, monkeypatch, mock_post):
    from decimal import Decimal
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    from src.adapters.fg_pdf import main
    pdf = _make_fake_pdf(tmp_path)
    rc = main(["import-pdf", "--file", str(pdf)])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_target_cloud_calls_cloud_function(tmp_path, monkeypatch, mock_post):
    from decimal import Decimal
    monkeypatch.setattr("src.adapters.fg_pdf.pdftotext_layout", lambda p: ANNUAL_PDF_TEXT)
    monkeypatch.setattr("src.adapters.fg_pdf.detect_template", lambda text: "annual")
    monkeypatch.setattr(
        "src.adapters.fg_pdf.extract_annual_statement",
        lambda text: ("12345678", date(2025, 12, 31), Decimal("125000.00")),
    )
    from src.adapters.fg_pdf import main
    pdf = _make_fake_pdf(tmp_path)
    rc = main(["import-pdf", "--file", str(pdf), "--apply", "--target", "cloud"])
    assert rc == 0
    mock_post.assert_called_once()
