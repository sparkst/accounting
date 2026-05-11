"""Cloud-mode tests for the Vanguard CSV adapter (IC-T02).

All network calls are mocked — no real Workers endpoint is required.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.vanguard_csv import (
    SOURCE_TAG,
    _CLOUD_INGEST_SOURCE,
    _default_target,
    import_positions_cloud,
)


# ── Sample CSV ────────────────────────────────────────────────────────────────

# Minimal 6-column brokerage CSV that passes detect_csv_flavor + parse
BROKERAGE_CSV = """\
Account Number,Investment Name,Symbol,Shares,Share Price,Total Value
123456789,Vanguard Total Stock Market Index Fund Admiral Shares,VTSAX,100.000,$120.50,"$12,050.00"
123456789,Vanguard Total Bond Market Index Fund Admiral Shares,VBTLX,50.000,$10.25,$512.50
"""

# Minimal 5-column 529 CSV
K529_CSV = """\
Fund Account Number,Fund Name,Shares,Share Price,Total Value
529111111,Vanguard Target Retirement 2045,25.000,$50.00,"$1,250.00"
"""


def _write_csv(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture()
def brokerage_csv(tmp_path) -> Path:
    return _write_csv(tmp_path, "vanguard_positions.csv", BROKERAGE_CSV)


@pytest.fixture()
def k529_csv(tmp_path) -> Path:
    return _write_csv(tmp_path, "vanguard_529.csv", K529_CSV)


@pytest.fixture()
def mock_post(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value={"accepted": 2, "rejected": 0})
    monkeypatch.setattr("src.adapters.vanguard_csv.post_to_wealth", mock)
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


# ── import_positions_cloud happy path ─────────────────────────────────────────

def test_cloud_posts_to_correct_slug(brokerage_csv, mock_post):
    result = import_positions_cloud(brokerage_csv, as_of=date(2025, 12, 31))

    assert result.errors == []
    mock_post.assert_called_once()
    _, source = mock_post.call_args[0]
    assert source == _CLOUD_INGEST_SOURCE
    assert source == "brokerage-csv"


def test_cloud_payload_shape(brokerage_csv, mock_post):
    import_positions_cloud(brokerage_csv, as_of=date(2025, 12, 31))

    payload = mock_post.call_args[0][0]
    assert "rows" in payload
    assert len(payload["rows"]) == 2

    row = payload["rows"][0]
    required = {"account_number", "symbol", "description", "shares", "price",
                "market_value", "as_of", "source_file", "source", "source_row_hash"}
    assert required.issubset(row.keys())


def test_cloud_decimal_fields_are_strings(brokerage_csv, mock_post):
    import_positions_cloud(brokerage_csv, as_of=date(2025, 12, 31))

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        for field in ("shares", "price", "market_value"):
            assert isinstance(row[field], str), f"{field} should be str, got {type(row[field])}"


def test_cloud_source_tag_correct(brokerage_csv, mock_post):
    import_positions_cloud(brokerage_csv, as_of=date(2025, 12, 31))

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        assert row["source"] == SOURCE_TAG


def test_cloud_as_of_correct(brokerage_csv, mock_post):
    import_positions_cloud(brokerage_csv, as_of=date(2025, 6, 30))

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        assert row["as_of"] == "2025-06-30"


def test_cloud_result_counts(brokerage_csv, mock_post):
    result = import_positions_cloud(brokerage_csv, as_of=date(2025, 12, 31))

    assert result.parsed == 2
    assert result.imported == 2
    assert result.errors == []


def test_cloud_529_csv(k529_csv, mock_post):
    result = import_positions_cloud(k529_csv, as_of=date(2025, 12, 31))

    assert result.errors == []
    mock_post.assert_called_once()
    payload = mock_post.call_args[0][0]
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert "529111111" in row["account_number"]


# ── Batching ──────────────────────────────────────────────────────────────────

def test_cloud_batching(tmp_path, mock_post):
    """110 position rows should be POSTed in 2 batches."""
    lines = ["Account Number,Investment Name,Symbol,Shares,Share Price,Total Value"]
    for i in range(110):
        lines.append(f"12345678{i % 10},Fund {i},FUND{i},10.000,$100.00,$1000.00")
    csv_path = _write_csv(tmp_path, "big.csv", "\n".join(lines))

    result = import_positions_cloud(csv_path, as_of=date(2025, 12, 31))

    assert result.errors == []
    assert mock_post.call_count == 2
    first = mock_post.call_args_list[0][0][0]["rows"]
    second = mock_post.call_args_list[1][0][0]["rows"]
    assert len(first) == 100
    assert len(second) == 10


# ── Error isolation ───────────────────────────────────────────────────────────

def test_cloud_empty_csv_yields_error_no_post(tmp_path, mock_post):
    csv_path = _write_csv(tmp_path, "empty.csv", "")
    result = import_positions_cloud(csv_path, as_of=date(2025, 12, 31))

    assert len(result.errors) >= 1
    mock_post.assert_not_called()


def test_cloud_post_failure_yields_error_no_raise(brokerage_csv, monkeypatch):
    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr(
        "src.adapters.vanguard_csv.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad")),
    )
    result = import_positions_cloud(brokerage_csv, as_of=date(2025, 12, 31))

    assert len(result.errors) > 0
    assert all("cloud POST" in e for e in result.errors)
    assert result.imported == 0


# ── CLI tests ─────────────────────────────────────────────────────────────────

def test_cli_target_cloud_calls_cloud_function(brokerage_csv, mock_post):
    from src.adapters.vanguard_csv import main

    rc = main([
        "import-positions", "--file", str(brokerage_csv),
        "--apply", "--target", "cloud",
        "--as-of", "2025-12-31",
    ])
    assert rc == 0
    mock_post.assert_called_once()


def test_cli_target_cloud_no_apply_no_post(brokerage_csv, mock_post):
    """Dry-run (no --apply) never calls post_to_wealth, even if --target cloud.

    Vanguard's dry-run path may open a DB session for unmapped-account detection;
    we stub get_session so the test doesn't require a real SQLite file.
    """
    import unittest.mock as um
    from contextlib import contextmanager

    # Return a mock session that supports the context-manager protocol and
    # produces no rows from .execute() (no accounts mapped).
    mock_session = um.MagicMock()
    mock_session.execute.return_value.first.return_value = None

    @contextmanager
    def _fake_get_session():
        yield mock_session

    with um.patch("src.db.connection.get_session", _fake_get_session):
        from src.adapters.vanguard_csv import main
        rc = main([
            "import-positions", "--file", str(brokerage_csv),
            "--target", "cloud",  # --apply not present → dry-run
        ])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_default_target_env_cloud(brokerage_csv, mock_post, monkeypatch):
    """WEALTH_TARGET_DEFAULT=cloud + --apply uses cloud without explicit --target."""
    monkeypatch.setenv("WEALTH_TARGET_DEFAULT", "cloud")
    from src.adapters.vanguard_csv import main

    rc = main([
        "import-positions", "--file", str(brokerage_csv),
        "--apply", "--as-of", "2025-12-31",
    ])
    assert rc == 0
    mock_post.assert_called_once()
