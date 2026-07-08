"""Cloud-mode tests for the NW Mutual XLSX adapter (IC-T02).

All network calls are mocked — no real Workers endpoint is required.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.nw_mutual_xlsx import (
    _CLOUD_INGEST_SOURCE,
    _CLOUD_LOG_SOURCE,
    SOURCE_TAG,
    _default_target,
    import_balances_cloud,
)
from src.models.base import Base
from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog
from src.models.plaid import (
    PlaidItem,  # noqa: F401 — Account FKs plaid_item; register for create_all
)

# ── Sample parsed workbook data ───────────────────────────────────────────────

SAMPLE_ROWS = [
    {
        "policy_number": "12345678",
        "insured": "Travis Sparks",
        "net_accum_value": Decimal("45000.00"),
    },
    {
        "policy_number": "87654321",
        "insured": "Amy Sparks",
        "net_accum_value": Decimal("30000.50"),
    },
]

ROWS_WITH_NA = [
    {
        "policy_number": "11111111",
        "insured": "Travis Sparks",
        "net_accum_value": None,  # N/A — should be skipped with warning
    },
    {
        "policy_number": "22222222",
        "insured": "Amy Sparks",
        "net_accum_value": Decimal("20000.00"),
    },
]


def _fake_xlsx(tmp_path: Path) -> Path:
    p = tmp_path / "nw_mutual_balances.xlsx"
    p.write_bytes(b"PK fake xlsx")
    return p


@pytest.fixture()
def mock_post(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value={"accepted": 2, "rejected": 0})
    monkeypatch.setattr("src.adapters.nw_mutual_xlsx.post_to_wealth", mock)
    return mock


@pytest.fixture()
def mock_workbook(monkeypatch):
    monkeypatch.setattr(
        "src.adapters.nw_mutual_xlsx.parse_workbook",
        lambda path: SAMPLE_ROWS,
    )


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


# ── import_balances_cloud happy path ─────────────────────────────────────────

def test_cloud_posts_to_correct_slug(tmp_path, mock_workbook, mock_post):
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    assert result.errors == []
    mock_post.assert_called_once()
    _, source = mock_post.call_args[0]
    assert source == _CLOUD_INGEST_SOURCE
    assert source == "xlsx-snapshot"


def test_cloud_payload_shape(tmp_path, mock_workbook, mock_post):
    xlsx = _fake_xlsx(tmp_path)
    import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    payload = mock_post.call_args[0][0]
    assert "rows" in payload
    assert len(payload["rows"]) == 2

    row = payload["rows"][0]
    assert "raw_account_name" in row
    assert row["as_of"] == "2025-12-31"
    assert row["source"] == SOURCE_TAG
    assert "source_row_hash" in row
    assert len(row["source_row_hash"]) == 64


def test_cloud_balance_is_decimal_string(tmp_path, mock_workbook, mock_post):
    xlsx = _fake_xlsx(tmp_path)
    import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    payload = mock_post.call_args[0][0]
    for row in payload["rows"]:
        assert isinstance(row["balance"], str)
        # Must have exactly 2 decimal places
        parts = row["balance"].split(".")
        assert len(parts) == 2
        assert len(parts[1]) == 2


def test_cloud_result_counts(tmp_path, mock_workbook, mock_post):
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    assert result.parsed == 2
    assert result.imported == 2
    assert result.errors == []


def test_cloud_raw_account_name_contains_policy(tmp_path, mock_workbook, mock_post):
    xlsx = _fake_xlsx(tmp_path)
    import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    payload = mock_post.call_args[0][0]
    names = [r["raw_account_name"] for r in payload["rows"]]
    assert any("12345678" in n for n in names)
    assert any("87654321" in n for n in names)


# ── N/A handling ─────────────────────────────────────────────────────────────

def test_cloud_na_policy_skipped_with_warning(tmp_path, monkeypatch, mock_post):
    """N/A balance policies are skipped; a warning is recorded; other rows POST."""
    monkeypatch.setattr(
        "src.adapters.nw_mutual_xlsx.parse_workbook",
        lambda path: ROWS_WITH_NA,
    )
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    assert len(result.warnings) == 1
    assert "N/A" in result.warnings[0]
    assert result.imported == 1  # only the non-N/A row
    mock_post.assert_called_once()
    payload = mock_post.call_args[0][0]
    assert len(payload["rows"]) == 1


# ── Batching ──────────────────────────────────────────────────────────────────

def test_cloud_batching(tmp_path, monkeypatch, mock_post):
    """150 rows should be split into 2 batches (100 + 50)."""
    big_rows = [
        {
            "policy_number": f"{i:08d}",
            "insured": f"Person {i}",
            "net_accum_value": Decimal("10000.00"),
        }
        for i in range(150)
    ]
    monkeypatch.setattr(
        "src.adapters.nw_mutual_xlsx.parse_workbook",
        lambda path: big_rows,
    )
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    assert result.errors == []
    assert mock_post.call_count == 2
    assert len(mock_post.call_args_list[0][0][0]["rows"]) == 100
    assert len(mock_post.call_args_list[1][0][0]["rows"]) == 50


# ── Error isolation ───────────────────────────────────────────────────────────

def test_cloud_post_failure_yields_error_no_raise(tmp_path, mock_workbook, monkeypatch):
    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr(
        "src.adapters.nw_mutual_xlsx.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad")),
    )
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    assert len(result.errors) > 0
    assert all("cloud POST" in e for e in result.errors)
    assert result.imported == 0


def test_cloud_parse_failure_yields_error_no_post(tmp_path, monkeypatch, mock_post):
    monkeypatch.setattr(
        "src.adapters.nw_mutual_xlsx.parse_workbook",
        lambda path: (_ for _ in ()).throw(ValueError("bad xlsx")),
    )
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))

    assert len(result.errors) == 1
    mock_post.assert_not_called()


# ── REQ-FIX-WLT-007: cloud import writes a local IngestionLog row ─────────────


@pytest.fixture()
def log_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_cloud_import_writes_ingestion_log_on_success(
    tmp_path, mock_workbook, mock_post, log_session: Session
) -> None:
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31), session=log_session)
    assert result.errors == []

    logs = log_session.scalars(select(IngestionLog)).all()
    assert len(logs) == 1
    assert logs[0].source == _CLOUD_LOG_SOURCE
    assert logs[0].status == IngestionStatus.SUCCESS.value


def test_cloud_import_writes_ingestion_log_on_error(
    tmp_path, mock_workbook, log_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.adapters._shared.wealth_client import WealthHTTPError

    monkeypatch.setattr(
        "src.adapters.nw_mutual_xlsx.post_to_wealth",
        lambda payload, source, **kw: (_ for _ in ()).throw(WealthHTTPError(422, "bad")),
    )
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31), session=log_session)
    assert result.imported == 0
    assert result.errors

    logs = log_session.scalars(select(IngestionLog)).all()
    assert len(logs) == 1
    assert logs[0].source == _CLOUD_LOG_SOURCE
    assert logs[0].status == IngestionStatus.FAILURE.value


def test_cloud_import_without_session_skips_log(
    tmp_path, mock_workbook, mock_post
) -> None:
    """Backward-compat: no session → no IngestionLog attempted, still returns result."""
    xlsx = _fake_xlsx(tmp_path)
    result = import_balances_cloud(xlsx, as_of=date(2025, 12, 31))
    assert result.errors == []
    assert result.imported == 2


# ── CLI tests ─────────────────────────────────────────────────────────────────
#
# The apply+cloud CLI test stubs ``get_session`` — REQ-FIX-WLT-007's
# IngestionLog write happens through the same real session the local-write
# path uses, so an unmocked test here would silently write into
# ``data/accounting.db``.


def test_cli_dry_run_does_not_post(tmp_path, mock_workbook, mock_post):
    from src.adapters.nw_mutual_xlsx import main

    xlsx = _fake_xlsx(tmp_path)
    rc = main(["import-balances", "--file", str(xlsx)])
    assert rc == 0
    mock_post.assert_not_called()


def test_cli_target_cloud_calls_cloud_function(tmp_path, mock_workbook, mock_post):
    import unittest.mock as um
    from contextlib import contextmanager

    from src.adapters.nw_mutual_xlsx import main

    mock_session = um.MagicMock()

    @contextmanager
    def _fake_get_session():
        yield mock_session

    xlsx = _fake_xlsx(tmp_path)
    with um.patch("src.db.connection.get_session", _fake_get_session):
        rc = main([
            "import-balances", "--file", str(xlsx),
            "--apply", "--target", "cloud",
            "--as-of", "2025-12-31",
        ])
    assert rc == 0
    mock_post.assert_called_once()
    assert mock_session.add.called
    assert mock_session.commit.called


def test_cli_target_local_dry_run_no_post(tmp_path, mock_workbook, mock_post):
    from src.adapters.nw_mutual_xlsx import main

    xlsx = _fake_xlsx(tmp_path)
    rc = main([
        "import-balances", "--file", str(xlsx),
        "--target", "local",
    ])
    assert rc == 0
    mock_post.assert_not_called()
