"""Tests for the brokerage ingest CLI dispatcher (TASK-10).

Covers folder-discovery dispatch, unknown-broker filtering, and exit codes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Load scripts/ingest-brokerage.py as a module despite the hyphenated filename.
_THIS_DIR = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "_ingest_brokerage_cli", _THIS_DIR / "ingest-brokerage.py"
)
assert _SPEC is not None and _SPEC.loader is not None
ingest_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ingest_cli)


def test_find_broker_folders_recognizes_known_names(tmp_path: Path) -> None:
    """REQ-005: dispatcher matches subfolder names case-insensitively."""
    (tmp_path / "Fidelity").mkdir()
    (tmp_path / "schwab").mkdir()
    (tmp_path / "ETRADE").mkdir()
    (tmp_path / "vanguard").mkdir()

    found = ingest_cli._find_broker_folders(tmp_path)

    assert set(found.keys()) == {"fidelity", "schwab", "etrade", "vanguard"}


def test_find_broker_folders_ignores_unknown_subdirs(tmp_path: Path) -> None:
    """Unknown subfolder names (e.g., 'robinhood') are silently skipped."""
    (tmp_path / "Fidelity").mkdir()
    (tmp_path / "robinhood").mkdir()
    (tmp_path / "Notes").mkdir()

    found = ingest_cli._find_broker_folders(tmp_path)

    assert set(found.keys()) == {"fidelity"}


def test_main_exits_2_when_root_is_not_a_directory(tmp_path: Path) -> None:
    """ERROR exit code 2 when target path is missing."""
    bogus = tmp_path / "nonexistent"
    rc = ingest_cli.main([str(bogus)])
    assert rc == 2


def test_main_exits_2_when_no_recognized_subfolders(tmp_path: Path) -> None:
    """ERROR exit code 2 when no broker subfolders are found."""
    (tmp_path / "robinhood").mkdir()
    rc = ingest_cli.main([str(tmp_path)])
    assert rc == 2


def test_main_exits_2_on_unknown_broker_filter(tmp_path: Path) -> None:
    """--brokers with an unknown name exits 2."""
    (tmp_path / "Fidelity").mkdir()
    rc = ingest_cli.main([str(tmp_path), "--brokers", "robinhood"])
    assert rc == 2


def test_adapters_dispatch_table_keys() -> None:
    """REQ-005: ADAPTERS dispatch table has exactly the four broker subfolder names."""
    assert set(ingest_cli.ADAPTERS.keys()) == {
        "fidelity",
        "schwab",
        "etrade",
        "vanguard",
    }


def test_adapters_dispatch_table_classes() -> None:
    """ADAPTERS values are BaseAdapter subclasses."""
    from src.adapters.base import BaseAdapter

    for cls in ingest_cli.ADAPTERS.values():
        assert issubclass(cls, BaseAdapter)


@pytest.mark.parametrize(
    "broker,expected_class_name",
    [
        ("fidelity", "FidelityCsvAdapter"),
        ("schwab", "SchwabCsvAdapter"),
        ("etrade", "EtradeCsvAdapter"),
        ("vanguard", "VanguardCsvAdapter"),
    ],
)
def test_adapters_dispatch_correct_class(broker: str, expected_class_name: str) -> None:
    """Each broker name maps to its expected adapter class."""
    assert ingest_cli.ADAPTERS[broker].__name__ == expected_class_name
