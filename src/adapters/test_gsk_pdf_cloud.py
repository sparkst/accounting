"""Cloud-mode tests for the GSK PDF adapter (IC-T02).

All network calls are mocked — no real Workers endpoint is required.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.adapters.gsk_pdf import (
    GSK_RAW_ACCOUNT_NAME,
    SOURCE_TAG,
    _CLOUD_INGEST_SOURCE,
    _default_target,
    import_pdf_cloud,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_PDF_TEXT = (
    "GSK Cash Balance Account Activity\n"
    "Closing Balance as of May 07, 2026 $42,000.00\n"
)


def _make_fake_pdf(tmp_path: Path, text: str = SAMPLE_PDF_TEXT) -> Path:
    """Create a dummy PDF path with mocked pdftotext output."""
    pdf = tmp_path / "gsk_statement.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    return pdf


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_pdftotext(monkeypatch):
    """Patch pdftotext_layout to return our sample text."""
    monkeypatch.setattr(
        "src.adapters.gsk_pdf.pdftotext_layout",
        lambda path: SAMPLE_PDF_TEXT,
    )


@pytest.fixture()
def mock_post(monkeypatch):
    """Patch post_to_wealth to return a success dict."""
    mock = MagicMock(return_value={"accepted": 1, "rejected": 0})
    monkeypatch.setattr("src.adapters.gsk_pdf.post_to_wealth", mock)
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

def test_cloud_posts_to_correct_slug(tmp_path, mock_pdftotext, mock_post):
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert result.errors == []
    mock_post.assert_called_once()
    _, source = mock_post.call_args[0]
    assert source == _CLOUD_INGEST_SOURCE
    assert source == "xlsx-snapshot"


def test_cloud_payload_shape(tmp_path, mock_pdftotext, mock_post):
    pdf = _make_fake_pdf(tmp_path)
    import_pdf_cloud(pdf)

    payload = mock_post.call_args[0][0]
    assert "rows" in payload
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["raw_account_name"] == GSK_RAW_ACCOUNT_NAME
    assert row["as_of"] == "2026-05-07"
    assert row["source"] == SOURCE_TAG
    assert "source_row_hash" in row
    assert len(row["source_row_hash"]) == 64  # SHA-256 hex


def test_cloud_balance_is_decimal_string(tmp_path, mock_pdftotext, mock_post):
    pdf = _make_fake_pdf(tmp_path)
    import_pdf_cloud(pdf)

    row = mock_post.call_args[0][0]["rows"][0]
    # Balance must be a string (not float) and carry 2 decimal places
    assert isinstance(row["balance"], str)
    assert row["balance"] == "42000.00"


def test_cloud_as_of_override(tmp_path, mock_pdftotext, mock_post):
    pdf = _make_fake_pdf(tmp_path)
    override_date = date(2026, 3, 31)
    import_pdf_cloud(pdf, as_of=override_date)

    row = mock_post.call_args[0][0]["rows"][0]
    assert row["as_of"] == "2026-03-31"


def test_cloud_result_counts(tmp_path, mock_pdftotext, mock_post):
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert result.parsed == 1
    assert result.imported == 1
    assert result.matched == 1
    assert result.errors == []


# ── Per-record error isolation ────────────────────────────────────────────────

def test_cloud_parse_failure_yields_error_no_post(tmp_path, monkeypatch, mock_post):
    """When PDF text doesn't have a Closing Balance line, no POST is made."""
    monkeypatch.setattr(
        "src.adapters.gsk_pdf.pdftotext_layout",
        lambda path: "some unrelated text",
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert len(result.errors) == 1
    assert "Closing Balance" in result.errors[0]
    mock_post.assert_not_called()


def test_cloud_post_failure_yields_error_no_raise(tmp_path, mock_pdftotext, monkeypatch):
    """WealthClientError on POST is caught; result.errors populated; no re-raise."""
    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr(
        "src.adapters.gsk_pdf.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad payload")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert len(result.errors) == 1
    assert "cloud POST failed" in result.errors[0]
    assert result.imported == 0


def test_cloud_post_401_caught(tmp_path, mock_pdftotext, monkeypatch):
    from src.adapters._shared.wealth_client import WealthUnauthorizedError

    monkeypatch.setattr(
        "src.adapters.gsk_pdf.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthUnauthorizedError(401, "Unauthorized")),
    )
    pdf = _make_fake_pdf(tmp_path)
    result = import_pdf_cloud(pdf)

    assert any("cloud POST failed" in e for e in result.errors)
    assert result.imported == 0


# ── CLI tests ─────────────────────────────────────────────────────────────────

def test_cli_dry_run_does_not_post(tmp_path, mock_pdftotext, mock_post):
    """Dry-run mode never calls post_to_wealth regardless of --target."""
    from src.adapters.gsk_pdf import main

    pdf = _make_fake_pdf(tmp_path)
    rc = main(["import-pdf", "--file", str(pdf)])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_target_cloud_calls_cloud_function(tmp_path, mock_pdftotext, mock_post):
    """--apply --target cloud routes to import_pdf_cloud."""
    from src.adapters.gsk_pdf import main

    pdf = _make_fake_pdf(tmp_path)
    rc = main(["import-pdf", "--file", str(pdf), "--apply", "--target", "cloud"])
    assert rc == 0
    mock_post.assert_called_once()


def test_cli_target_local_does_not_post(tmp_path, mock_pdftotext, mock_post):
    """Dry-run (no --apply) never calls post_to_wealth."""
    from src.adapters.gsk_pdf import main

    pdf = _make_fake_pdf(tmp_path)
    # Without --apply, it's dry-run regardless of --target
    rc = main(["import-pdf", "--file", str(pdf), "--target", "local"])
    assert rc == 0
    mock_post.assert_not_called()
