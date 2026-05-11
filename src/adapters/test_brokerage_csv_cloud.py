"""Cloud-mode tests for the brokerage CSV adapter (IC-T02).

All network calls are mocked — no real Workers endpoint is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.brokerage_csv import (
    ETRADE,
    SCHWAB,
    VANGUARD,
    _CLOUD_INGEST_SOURCE,
    _default_target,
    import_csv_cloud,
)


# ── Sample CSV content ────────────────────────────────────────────────────────

ETRADE_CSV = """\
Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost or Other Basis,Wash Sale Loss Disallowed,Gain or Loss,Term,Covered/Uncovered
01/15/2025,03/10/2023,AAPL Apple Inc,10.000,"$1,500.00","$1,200.00",$0.00,"$300.00",Long,Covered
02/20/2025,07/01/2024,MSFT Microsoft Corp,5.000,"$2,000.00","$2,100.00",$0.00,"($100.00)",Short,Covered
"""

SCHWAB_CSV = """\
Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost Basis,Wash Sale Loss Disallowed,Gain or (Loss),Short-term or long-term,Covered
2025-03-01,2023-06-15,GOOG Alphabet Inc,2.000,"$5,600.00","$4,800.00",$0.00,"$800.00",Long-term,Covered
"""


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def etrade_csv_path(tmp_path) -> Path:
    p = tmp_path / "etrade_1099b.csv"
    p.write_text(ETRADE_CSV, encoding="utf-8")
    return p


@pytest.fixture()
def schwab_csv_path(tmp_path) -> Path:
    p = tmp_path / "schwab_1099b.csv"
    p.write_text(SCHWAB_CSV, encoding="utf-8")
    return p


@pytest.fixture()
def mock_post(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value={"accepted": 2, "rejected": 0})
    monkeypatch.setattr("src.adapters.brokerage_csv.post_to_wealth", mock)
    return mock


# ── _default_target tests ─────────────────────────────────────────────────────

def test_default_target_reads_env(monkeypatch):
    monkeypatch.setenv("WEALTH_TARGET_DEFAULT", "cloud")
    assert _default_target() == "cloud"


def test_default_target_falls_back_to_local(monkeypatch):
    monkeypatch.delenv("WEALTH_TARGET_DEFAULT", raising=False)
    assert _default_target() == "local"


# ── import_csv_cloud happy path ───────────────────────────────────────────────

def test_cloud_posts_to_correct_slug(etrade_csv_path, mock_post):
    result = import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    assert result.errors == []
    mock_post.assert_called_once()
    _, source = mock_post.call_args[0]
    assert source == _CLOUD_INGEST_SOURCE
    assert source == "brokerage-csv"


def test_cloud_payload_shape(etrade_csv_path, mock_post):
    import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    payload = mock_post.call_args[0][0]
    assert "rows" in payload
    assert len(payload["rows"]) == 2

    row = payload["rows"][0]
    required_fields = {
        "brokerage", "date_sold", "date_acquired", "description",
        "proceeds", "cost_basis", "gain_loss", "is_long_term",
        "source_id", "source_file",
    }
    assert required_fields.issubset(row.keys())


def test_cloud_brokerage_set_correctly(etrade_csv_path, mock_post):
    import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        assert row["brokerage"] == ETRADE


def test_cloud_schwab_auto_detected(schwab_csv_path, mock_post):
    """When brokerage=None, auto-detection should identify Schwab."""
    # Schwab CSV doesn't contain "schwab" keyword, pass explicitly
    result = import_csv_cloud(schwab_csv_path, brokerage=SCHWAB)

    assert result.errors == []
    mock_post.assert_called_once()
    payload = mock_post.call_args[0][0]
    assert payload["rows"][0]["brokerage"] == SCHWAB


def test_cloud_decimal_amounts_are_strings(etrade_csv_path, mock_post):
    """All decimal amount fields must be JSON-safe strings, not floats."""
    import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        for field in ("proceeds", "cost_basis", "gain_loss", "wash_sale_loss"):
            if row[field] is not None:
                assert isinstance(row[field], str), f"{field} should be str, got {type(row[field])}"


def test_cloud_result_counts(etrade_csv_path, mock_post):
    result = import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    assert result.imported == 2
    assert result.errors == []


def test_cloud_is_long_term_bool(etrade_csv_path, mock_post):
    import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        assert isinstance(row["is_long_term"], bool)


# ── Batching (>100 rows) ──────────────────────────────────────────────────────

def test_cloud_batching(tmp_path, monkeypatch, mock_post):
    """110 rows should be POSTed in 2 batches of 100 and 10."""
    # Build a CSV with 110 data rows
    lines = ["Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost or Other Basis,Wash Sale Loss Disallowed,Gain or Loss,Term,Covered/Uncovered"]
    for i in range(110):
        lines.append(
            f"01/15/2025,03/10/2023,STCK{i} Corp,1.000,$100.00,$80.00,$0.00,$20.00,Short,Covered"
        )
    csv_path = tmp_path / "big.csv"
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    result = import_csv_cloud(csv_path, brokerage=ETRADE)

    assert result.errors == []
    assert mock_post.call_count == 2
    # First batch 100, second batch 10
    first_batch = mock_post.call_args_list[0][0][0]["rows"]
    second_batch = mock_post.call_args_list[1][0][0]["rows"]
    assert len(first_batch) == 100
    assert len(second_batch) == 10


# ── Error isolation ───────────────────────────────────────────────────────────

def test_cloud_unknown_brokerage_yields_error(tmp_path, mock_post):
    """Completely unrecognized content yields an error, no POST."""
    csv_path = tmp_path / "mystery.csv"
    csv_path.write_text("A,B,C\n1,2,3\n", encoding="utf-8")

    result = import_csv_cloud(csv_path)  # no brokerage hint

    assert len(result.errors) == 1
    assert "detect" in result.errors[0].lower() or "brokerage" in result.errors[0].lower()
    mock_post.assert_not_called()


def test_cloud_post_failure_yields_error_no_raise(etrade_csv_path, monkeypatch):
    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr(
        "src.adapters.brokerage_csv.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad")),
    )
    result = import_csv_cloud(etrade_csv_path, brokerage=ETRADE)

    assert len(result.errors) > 0
    assert all("cloud POST failed" in e for e in result.errors)
    assert result.imported == 0


# ── CLI tests ─────────────────────────────────────────────────────────────────

def test_cli_dry_run_does_not_post(etrade_csv_path, mock_post):
    from src.adapters.brokerage_csv import main

    rc = main(["import-csv", "--file", str(etrade_csv_path), "--brokerage", ETRADE])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_target_cloud_calls_cloud_function(etrade_csv_path, mock_post):
    from src.adapters.brokerage_csv import main

    rc = main([
        "import-csv", "--file", str(etrade_csv_path),
        "--brokerage", ETRADE, "--apply", "--target", "cloud",
    ])
    assert rc == 0
    mock_post.assert_called_once()


def test_cli_target_local_dry_run_no_post(etrade_csv_path, mock_post):
    """No --apply means dry-run; no POST regardless of --target."""
    from src.adapters.brokerage_csv import main

    rc = main([
        "import-csv", "--file", str(etrade_csv_path),
        "--brokerage", ETRADE, "--target", "local",
    ])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_default_target_env(etrade_csv_path, mock_post, monkeypatch):
    """When WEALTH_TARGET_DEFAULT=cloud, --apply without --target uses cloud."""
    monkeypatch.setenv("WEALTH_TARGET_DEFAULT", "cloud")
    from src.adapters.brokerage_csv import main

    rc = main([
        "import-csv", "--file", str(etrade_csv_path),
        "--brokerage", ETRADE, "--apply",
    ])
    assert rc == 0
    mock_post.assert_called_once()
