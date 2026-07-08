"""Tests for the vision shadow-run harness.

REQ-VIS-002: shadow diff report; never writes the register while in shadow.
REQ-VIS-004: per-run cost logged AND capped (projection abort, mid-run stop,
--max-files), documents never leave the configured providers, raw response
preserved in the diff report.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.models import brokerage as _brokerage  # noqa: F401 — register tables
from src.models import history as _history  # noqa: F401 — register tables
from src.models import transaction as _transaction  # noqa: F401 — register tables
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot, RealizedGainLoss
from src.models.history import (
    AccountAlias,
    AccountBalanceSnapshot,
    AccountTag,
    CostBasisLot,
    ExpectedAccount,
    HistoricalPrice,
    StockSplit,
)
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.vision import extract as extract_mod
from src.vision import shadow as shadow_mod
from src.vision.client import VisionError, VisionExtraction
from src.vision.shadow import (
    OK,
    SKIPPED_COST_CAP,
    ShadowFile,
    VisionConfig,
    run_shadow_batch,
)

_FIX_DIR = Path(__file__).parent / "fixtures"

_FG_ANNUAL_TEXT = (
    "Contract #:        MZ152585\n"
    "Total Account Value as of 05/07/2026      $   660,218.55\n"
)


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_FIX_DIR / f"{name}.json").read_text())
    return data


class FakeVisionProvider:
    """Recorded-fixture fake provider — zero network, zero SDK import."""

    def __init__(
        self,
        fields: dict[str, Any],
        *,
        cost: float = 0.005,
        name: str = "gemini",
        fail_calls: frozenset[int] = frozenset(),
        input_tokens: int = 4000,
        output_tokens: int = 1500,
        model: str = "gemini-2.5-flash",
    ) -> None:
        self.name = name
        self._fields = fields
        self._cost = cost
        self._fail_calls = fail_calls
        self._in = input_tokens
        self._out = output_tokens
        self._model = model
        self.calls = 0

    def extract(
        self,
        file_bytes: bytes,
        mime: str,
        schema: dict[str, Any],
        prompt: str,
        *,
        session: Session | None = None,
    ) -> VisionExtraction:
        self.calls += 1
        if self.calls in self._fail_calls:
            raise VisionError(f"poisoned file on call {self.calls}")
        return VisionExtraction(
            fields=dict(self._fields),
            raw_response=json.dumps(self._fields),
            model=self._model,
            input_tokens=self._in,
            output_tokens=self._out,
            cost_estimate=self._cost,
            duration_ms=3,
        )


def _cfg(*, cap: float = 2.00, per_file_tokens: tuple[int, int] = (4000, 1500)) -> VisionConfig:
    return VisionConfig(
        run_cost_cap_usd=cap,
        max_files=10,
        est_input_tokens=per_file_tokens[0],
        est_output_tokens=per_file_tokens[1],
        provider_rates={"gemini": (0.30, 2.50), "openai": (0.15, 0.60)},
    )


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    event.listen(eng, "connect", _configure_sqlite)
    Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _fg_file() -> ShadowFile:
    legacy = extract_mod.legacy_extract("fg", text=_FG_ANNUAL_TEXT)
    return ShadowFile(
        name="fg-2026.pdf", file_bytes=b"%PDF-1.4 fake", mime="application/pdf",
        legacy_fields=legacy,
    )


# ── Clean-diff happy path + report file (REQ-VIS-002/004) ────────────────────


def test_clean_shadow_run_writes_report_and_log(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-002: a matching statement produces a clean diff report + IngestionLog."""
    provider = FakeVisionProvider(_fixture("fg")["fields"], cost=0.0049)
    batch = run_shadow_batch(
        "fg", [_fg_file()], provider=provider, session=session,
        config=_cfg(), report_dir=tmp_path,
    )
    assert len(batch.files) == 1
    fr = batch.files[0]
    assert fr.status == OK
    assert fr.diff is not None
    assert fr.diff.clean is True
    assert fr.diff.n_match == 4
    assert fr.diff.n_mismatch == 0

    # Report JSON on disk includes the raw provider response (REQ-VIS-004).
    report = json.loads(Path(fr.report_path).read_text())  # type: ignore[arg-type]
    assert report["institution"] == "fg"
    assert report["raw_response"]  # provenance preserved
    assert report["diff"]["clean"] is True

    # One vision_shadow IngestionLog row with the summary line.
    logs = session.query(IngestionLog).filter_by(source="vision_shadow").all()
    assert len(logs) == 1
    assert logs[0].error_detail is not None
    assert "4 match / 0 mismatch / clean=True" in logs[0].error_detail
    assert "provider=gemini" in logs[0].error_detail


def test_mismatch_run_is_dirty(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-002: a divergent balance yields a mismatch (clean=False)."""
    provider = FakeVisionProvider(_fixture("fg_mismatch")["fields"], cost=0.0049)
    batch = run_shadow_batch(
        "fg", [_fg_file()], provider=provider, session=session,
        config=_cfg(), report_dir=tmp_path,
    )
    fr = batch.files[0]
    assert fr.diff is not None
    assert fr.diff.n_mismatch == 1
    assert fr.diff.clean is False


# ── Never writes register / brokerage / history (REQ-VIS-002) ────────────────


# Every register/brokerage/history table shadow.py claims (in its module
# docstring) to leave untouched — P1-101: the row-count invariant must be
# proven for all of them, not just Transaction and AccountBalanceSnapshot.
_PROTECTED_MODELS = (
    Transaction,
    AccountBalanceSnapshot,
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
    HistoricalPrice,
    ExpectedAccount,
    AccountTag,
    CostBasisLot,
    StockSplit,
    AccountAlias,
)


def test_shadow_never_writes_register(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-002: register/brokerage/history row counts are unchanged by a shadow run."""
    before_counts = {model: session.query(model).count() for model in _PROTECTED_MODELS}

    provider = FakeVisionProvider(_fixture("fg")["fields"])
    run_shadow_batch(
        "fg", [_fg_file()], provider=provider, session=session,
        config=_cfg(), report_dir=tmp_path,
    )

    for model, before in before_counts.items():
        assert session.query(model).count() == before, (
            f"{model.__name__} row count changed by shadow run"
        )


# ── Cost ceiling (REQ-VIS-004) ───────────────────────────────────────────────


def test_projection_abort_before_any_call(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-004: a pre-run projection over the cap aborts with zero provider calls."""
    # per-file projection = (4000*0.30 + 1500*2.50)/1e6 = 0.00495; 3 files = 0.01485.
    provider = FakeVisionProvider(_fixture("fg")["fields"])
    files = [_fg_file() for _ in range(3)]
    batch = run_shadow_batch(
        "fg", files, provider=provider, session=session,
        config=_cfg(cap=0.01), report_dir=tmp_path,
    )
    assert batch.aborted_projection is True
    assert provider.calls == 0
    assert all(f.status == SKIPPED_COST_CAP for f in batch.files)


def test_mid_run_stop_when_actual_cost_crosses_cap(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-004: cumulative actual cost stops the batch; remaining skipped_cost_cap."""
    # Tiny per-file projection (passes) but each file's ACTUAL cost is $0.10.
    cfg = _cfg(cap=0.25, per_file_tokens=(10, 10))  # per_file projection ~2.8e-5
    provider = FakeVisionProvider(_fixture("fg")["fields"], cost=0.10)
    files = [_fg_file() for _ in range(5)]
    batch = run_shadow_batch(
        "fg", files, provider=provider, session=session,
        config=cfg, report_dir=tmp_path,
    )
    processed = [f for f in batch.files if f.status == OK]
    skipped = [f for f in batch.files if f.status == SKIPPED_COST_CAP]
    assert len(processed) == 3          # 0.10*3 = 0.30 would exceed 0.25 at the 4th
    assert len(skipped) == 2
    assert provider.calls == 3          # never called for the skipped files


def test_max_files_truncation(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-004: --max-files bounds the batch to the first N files."""
    provider = FakeVisionProvider(_fixture("fg")["fields"])
    files = [_fg_file() for _ in range(5)]
    batch = run_shadow_batch(
        "fg", files, provider=provider, session=session,
        config=_cfg(), max_files=2, report_dir=tmp_path,
    )
    assert provider.calls == 2
    assert len(batch.files) == 2


# ── Per-file isolation (REQ-VIS-001/002) ─────────────────────────────────────


def test_poisoned_file_does_not_halt_batch(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-002: a failing file is isolated; other files still process."""
    provider = FakeVisionProvider(_fixture("fg")["fields"], fail_calls=frozenset({2}))
    files = [_fg_file() for _ in range(3)]
    batch = run_shadow_batch(
        "fg", files, provider=provider, session=session,
        config=_cfg(), report_dir=tmp_path,
    )
    statuses = [f.status for f in batch.files]
    assert statuses == [OK, "error", OK]
    # Both a success and a failure IngestionLog row were written.
    logs = session.query(IngestionLog).filter_by(source="vision_shadow").all()
    assert len(logs) == 3
    failed = [x for x in logs if x.records_failed == 1]
    assert len(failed) == 1


def test_config_loads_from_yaml() -> None:
    """REQ-VIS-004: config/vision.yaml loads with the documented defaults."""
    cfg = shadow_mod.load_config()
    assert cfg.run_cost_cap_usd == 2.00
    assert cfg.max_files == 10
    assert "gemini" in cfg.provider_rates
    assert "openai" in cfg.provider_rates


def test_env_overrides_cost_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-VIS-004: VISION_RUN_COST_CAP_USD overrides the config default."""
    monkeypatch.setenv("VISION_RUN_COST_CAP_USD", "0.50")
    cfg = _cfg(cap=2.00)
    assert shadow_mod.resolve_cost_cap(cfg) == 0.50


def test_na_iul_policy_values_end_to_end(session: Session, tmp_path: Path) -> None:
    """REQ-VIS-001/002: policy_values institution shadow-runs clean from fixtures."""
    legacy = extract_mod.legacy_extract(
        "na_iul",
        values={
            "policy_number": "IUL0099",
            "as_of": date(2026, 6, 1),
            "surrender_value": "45000",
            "accumulation_value": "52000",
            "death_benefit": "500000",
            "premium_paid": "60000",
        },
    )
    f = ShadowFile("iul.txt", b"data", "application/pdf", legacy)
    provider = FakeVisionProvider(_fixture("na_iul")["fields"])
    batch = run_shadow_batch(
        "na_iul", [f], provider=provider, session=session,
        config=_cfg(), report_dir=tmp_path,
    )
    assert batch.files[0].diff is not None
    assert batch.files[0].diff.clean is True
