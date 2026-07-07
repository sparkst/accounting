"""Cloud-mode tests for the Franklin Templeton PDF adapter (IC-T02).

All network calls are mocked — no real Workers endpoint is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.ft_pdf import (
    _CLOUD_INGEST_SOURCE,
    FT_RAW_ACCOUNT_NAME,
    SOURCE_TAG,
    _default_target,
    import_statements_cloud,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_PDF_TEXT_A = (
    "Franklin Templeton\n"
    "PORTFOLIO OVERVIEW   $85,000.00\n"
    "Some other content\n"
)

SAMPLE_PDF_TEXT_B = (
    "Franklin Templeton\n"
    "PORTFOLIO OVERVIEW   $90,500.50\n"
)

BAD_PDF_TEXT = "No portfolio overview here."


def _make_pdf_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a temp directory with PDF files having given names."""
    d = tmp_path / "ft_statements"
    d.mkdir()
    for name in files:
        (d / name).write_bytes(b"%PDF-1.4 fake")
    return d


@pytest.fixture()
def pdf_dir_two_valid(tmp_path):
    return _make_pdf_dir(tmp_path, {
        "2025-12-31.pdf": "",
        "2024-12-31.pdf": "",
    })


@pytest.fixture()
def mock_pdftotext_two_valid(monkeypatch, pdf_dir_two_valid):
    """Return distinct balances per filename."""
    texts = {
        "2025-12-31.pdf": SAMPLE_PDF_TEXT_A,
        "2024-12-31.pdf": SAMPLE_PDF_TEXT_B,
    }

    def _layout(path: Path) -> str:
        return texts.get(path.name, BAD_PDF_TEXT)

    monkeypatch.setattr("src.adapters.ft_pdf.pdftotext_layout", _layout)
    return pdf_dir_two_valid


@pytest.fixture()
def mock_post(monkeypatch):
    mock = MagicMock(return_value={"accepted": 2, "rejected": 0})
    monkeypatch.setattr("src.adapters.ft_pdf.post_to_wealth", mock)
    return mock


# ── _default_target tests ─────────────────────────────────────────────────────

def test_default_target_reads_env(monkeypatch):
    monkeypatch.setenv("WEALTH_TARGET_DEFAULT", "cloud")
    assert _default_target() == "cloud"


def test_default_target_falls_back_to_local(monkeypatch):
    monkeypatch.delenv("WEALTH_TARGET_DEFAULT", raising=False)
    assert _default_target() == "local"


# ── import_statements_cloud happy path ────────────────────────────────────────

def test_cloud_posts_to_correct_slug(mock_pdftotext_two_valid, mock_post):
    result = import_statements_cloud(mock_pdftotext_two_valid)

    assert result.errors == []
    mock_post.assert_called_once()
    _, source = mock_post.call_args[0]
    assert source == _CLOUD_INGEST_SOURCE
    assert source == "xlsx-snapshot"


def test_cloud_payload_shape(mock_pdftotext_two_valid, mock_post):
    import_statements_cloud(mock_pdftotext_two_valid)

    payload = mock_post.call_args[0][0]
    assert "rows" in payload
    assert len(payload["rows"]) == 2

    row = payload["rows"][0]
    assert row["raw_account_name"] == FT_RAW_ACCOUNT_NAME
    assert row["source"] == SOURCE_TAG
    assert "as_of" in row
    assert "balance" in row
    assert "source_row_hash" in row
    assert len(row["source_row_hash"]) == 64


def test_cloud_balance_is_decimal_string(mock_pdftotext_two_valid, mock_post):
    import_statements_cloud(mock_pdftotext_two_valid)

    payload = mock_post.call_args[0][0]
    # Rows sorted by filename (alphabetically, 2024 first, then 2025)
    rows_by_date = {r["as_of"]: r for r in payload["rows"]}
    assert rows_by_date["2025-12-31"]["balance"] == "85000.00"
    assert rows_by_date["2024-12-31"]["balance"] == "90500.50"


def test_cloud_result_counts(mock_pdftotext_two_valid, mock_post):
    result = import_statements_cloud(mock_pdftotext_two_valid)

    assert result.files_seen == 2
    assert result.imported == 2
    assert result.matched == 2
    assert result.errors == []


def test_cloud_empty_directory(tmp_path, mock_post):
    d = tmp_path / "empty"
    d.mkdir()
    result = import_statements_cloud(d)

    assert result.files_seen == 0
    assert result.imported == 0
    mock_post.assert_not_called()


# ── Per-file error isolation ──────────────────────────────────────────────────

def test_cloud_bad_filename_skipped(tmp_path, monkeypatch, mock_post):
    """A PDF with a non-date filename yields a per-file error; others continue."""
    d = _make_pdf_dir(tmp_path, {
        "2025-12-31.pdf": "",
        "notastatement.pdf": "",
    })
    monkeypatch.setattr(
        "src.adapters.ft_pdf.pdftotext_layout",
        lambda path: SAMPLE_PDF_TEXT_A,
    )
    result = import_statements_cloud(d)

    assert len(result.errors) == 1
    assert "notastatement.pdf" in result.errors[0]
    # The valid file should still be posted
    assert result.imported == 1


def test_cloud_missing_overview_yields_error(tmp_path, monkeypatch, mock_post):
    """A PDF with no PORTFOLIO OVERVIEW line records a per-file error."""
    d = _make_pdf_dir(tmp_path, {"2025-06-30.pdf": ""})
    monkeypatch.setattr(
        "src.adapters.ft_pdf.pdftotext_layout",
        lambda path: BAD_PDF_TEXT,
    )
    result = import_statements_cloud(d)

    assert len(result.errors) == 1
    assert "2025-06-30.pdf" in result.errors[0]
    mock_post.assert_not_called()


def test_cloud_post_failure_yields_error_no_raise(
    mock_pdftotext_two_valid, monkeypatch
):
    """WealthClientError on POST is caught; no re-raise."""
    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr(
        "src.adapters.ft_pdf.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad")),
    )
    result = import_statements_cloud(mock_pdftotext_two_valid)

    assert len(result.errors) > 0
    assert all("cloud POST failed" in e for e in result.errors)
    assert result.imported == 0


# ── CLI tests ─────────────────────────────────────────────────────────────────

def test_cli_dry_run_does_not_post(mock_pdftotext_two_valid, mock_post):
    from src.adapters.ft_pdf import main

    rc = main(["import-statements", "--dir", str(mock_pdftotext_two_valid)])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_target_cloud_calls_cloud_function(mock_pdftotext_two_valid, mock_post):
    from src.adapters.ft_pdf import main

    rc = main([
        "import-statements", "--dir", str(mock_pdftotext_two_valid),
        "--apply", "--target", "cloud",
    ])
    assert rc == 0
    mock_post.assert_called_once()


def test_cli_target_local_does_not_post(mock_pdftotext_two_valid, mock_post):
    """Dry-run (no --apply) never calls post_to_wealth."""
    from src.adapters.ft_pdf import main

    rc = main([
        "import-statements", "--dir", str(mock_pdftotext_two_valid),
        "--target", "local",
    ])
    assert rc == 0
    mock_post.assert_not_called()
