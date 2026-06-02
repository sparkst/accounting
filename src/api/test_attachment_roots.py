"""REQ-HM-018: attachment roots + receipts root read from env."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_roots_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTACHMENT_ROOTS", "/home/travis/accounting/data")
    monkeypatch.setenv("RECEIPTS_ROOT", "/home/travis/accounting/data/receipts")
    import src.api.routes.attachments as att
    importlib.reload(att)
    assert Path("/home/travis/accounting/data") in att._ALLOWED_ROOTS
    assert Path("/home/travis/accounting/data/receipts") == att._RECEIPTS_ROOT
    importlib.reload(att)  # restore for other tests


def test_roots_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTACHMENT_ROOTS", raising=False)
    monkeypatch.delenv("RECEIPTS_ROOT", raising=False)
    import src.api.routes.attachments as att
    importlib.reload(att)
    assert any("SGDrive" in str(p) for p in att._ALLOWED_ROOTS)
    assert "SGDrive" in str(att._RECEIPTS_ROOT)
    importlib.reload(att)  # restore for other tests
