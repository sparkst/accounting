"""REQ-HM-018: ingestion dirs read from env (Hetzner-local), default to Mac paths."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.adapters.deduction_email import DeductionEmailAdapter
from src.adapters.gmail_n8n import GmailN8nAdapter


def test_gmail_dirs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_N8N_DIRS", "/home/travis/accounting/data/inbox:/home/travis/accounting/data/review")
    a = GmailN8nAdapter()
    assert Path("/home/travis/accounting/data/inbox") in a._dirs
    assert Path("/home/travis/accounting/data/review") in a._dirs


def test_gmail_dirs_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GMAIL_N8N_DIRS", raising=False)
    a = GmailN8nAdapter()
    assert any("SGDrive" in str(p) for p in a._dirs)


def test_deduction_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUCTION_DIR", "/home/travis/accounting/data/deductions")
    a = DeductionEmailAdapter()
    assert Path("/home/travis/accounting/data/deductions") in a._dirs


def test_deduction_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEDUCTION_DIR", raising=False)
    a = DeductionEmailAdapter()
    assert any("SGDrive" in str(p) for p in a._dirs)
