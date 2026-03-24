"""Tests for Tier 3 LLM classifier — including LLMUsageLog persistence.

REQ-ID: LLM-USAGE-004  LLMUsageLog is written after every successful Claude API call.
REQ-ID: LLM-USAGE-005  LLMUsageLog records correct model, tokens, cost, duration, and transaction_id.
REQ-ID: LLM-USAGE-006  No LLMUsageLog row is written when _session is None (opt-in behaviour).
REQ-ID: LLM-USAGE-007  estimate_cost_for_model returns correct amounts for Haiku vs Sonnet.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.classification.llm_classifier import llm_classify
from src.db.connection import _configure_sqlite
from src.models.base import Base
from src.models.enums import Source
from src.models.llm_usage import LLMUsageLog, estimate_cost_for_model
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def in_memory_engine() -> Generator[Any, None, None]:
    """SQLite in-memory engine with the full schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _configure_sqlite)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(in_memory_engine: Any) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=in_memory_engine, autocommit=False, autoflush=False)
    with factory() as s:
        yield s


def _make_transaction(
    description: str = "Mystery Vendor Co",
    amount: Decimal = Decimal("-99.00"),
    date: str = "2026-03-01",
    transaction_id: str | None = "txn-test-uuid-1234",
) -> Transaction:
    txn = Transaction(
        source=Source.BANK_CSV.value,
        source_id="test-source-1",
        source_hash="deadbeef01",
        date=date,
        description=description,
        amount=amount,
        raw_data={"note": "test"},
    )
    txn.id = transaction_id  # type: ignore[assignment]
    return txn


def _make_mock_client(
    model: str = "claude-3-5-haiku-20241022",
    input_tokens: int = 300,
    output_tokens: int = 80,
    response_json: dict[str, Any] | None = None,
) -> MagicMock:
    """Return a mock Anthropic client whose messages.create() returns a realistic response."""
    if response_json is None:
        response_json = {
            "entity": "sparkry",
            "tax_category": "OFFICE_EXPENSE",
            "direction": "expense",
            "confidence": 0.85,
            "reasoning": "Mock classified as office expense.",
        }

    text_block = MagicMock()
    text_block.text = json.dumps(response_json)

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.model = model
    response.content = [text_block]
    response.usage = usage

    client = MagicMock()
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# estimate_cost_for_model
# ---------------------------------------------------------------------------


def test_estimate_cost_haiku_input_only() -> None:
    """REQ-ID: LLM-USAGE-007 — Haiku: $0.25/1M input."""
    cost = estimate_cost_for_model("claude-3-5-haiku-20241022", 1_000_000, 0)
    assert abs(cost - 0.25) < 1e-9


def test_estimate_cost_haiku_output_only() -> None:
    """REQ-ID: LLM-USAGE-007 — Haiku: $1.25/1M output."""
    cost = estimate_cost_for_model("claude-3-5-haiku-20241022", 0, 1_000_000)
    assert abs(cost - 1.25) < 1e-9


def test_estimate_cost_sonnet() -> None:
    """REQ-ID: LLM-USAGE-007 — Sonnet: $3/1M input, $15/1M output."""
    cost = estimate_cost_for_model("claude-3-5-sonnet-20241022", 1_000_000, 1_000_000)
    assert abs(cost - 18.0) < 1e-9


def test_estimate_cost_unknown_model_falls_back_to_sonnet() -> None:
    """Unknown model names fall back to Sonnet pricing."""
    cost = estimate_cost_for_model("claude-future-model-xyz", 1_000_000, 0)
    assert abs(cost - 3.0) < 1e-9


def test_estimate_cost_zero_tokens() -> None:
    cost = estimate_cost_for_model("claude-3-5-haiku-20241022", 0, 0)
    assert cost == 0.0


# ---------------------------------------------------------------------------
# LLMUsageLog written after successful API call
# ---------------------------------------------------------------------------


def test_llm_usage_log_written_after_classification(session: Session) -> None:
    """REQ-ID: LLM-USAGE-004 — A log row is created when _session is provided."""
    txn = _make_transaction()
    client = _make_mock_client(
        model="claude-3-5-haiku-20241022",
        input_tokens=350,
        output_tokens=90,
    )

    result = llm_classify(txn, _client=client, _session=session)
    session.commit()

    logs = session.query(LLMUsageLog).all()
    assert len(logs) == 1, "Expected exactly one LLMUsageLog row"
    log = logs[0]

    # Classification still succeeds
    assert result.confidence > 0.0

    # Log fields are correct
    assert log.model == "claude-3-5-haiku-20241022"
    assert log.input_tokens == 350
    assert log.output_tokens == 90
    assert log.cost_estimate > 0.0
    assert log.duration_ms >= 0
    assert log.id is not None


def test_llm_usage_log_contains_correct_cost(session: Session) -> None:
    """REQ-ID: LLM-USAGE-005 — Cost is computed using model-specific pricing."""
    txn = _make_transaction()
    client = _make_mock_client(
        model="claude-3-5-haiku-20241022",
        input_tokens=1_000_000,
        output_tokens=0,
    )

    llm_classify(txn, _client=client, _session=session)
    session.commit()

    log = session.query(LLMUsageLog).one()
    # Haiku input pricing: $0.25 / 1M
    assert abs(log.cost_estimate - 0.25) < 1e-6


def test_llm_usage_log_stores_transaction_id(session: Session) -> None:
    """REQ-ID: LLM-USAGE-005 — transaction_id is stored when available."""
    txn = _make_transaction(transaction_id="my-txn-id-5678")
    client = _make_mock_client()

    llm_classify(txn, _client=client, _session=session)
    session.commit()

    log = session.query(LLMUsageLog).one()
    assert log.transaction_id == "my-txn-id-5678"


def test_llm_usage_log_transaction_id_none_when_missing(session: Session) -> None:
    """REQ-ID: LLM-USAGE-005 — transaction_id is None when transaction has no id."""
    txn = _make_transaction(transaction_id=None)
    client = _make_mock_client()

    llm_classify(txn, _client=client, _session=session)
    session.commit()

    log = session.query(LLMUsageLog).one()
    assert log.transaction_id is None


def test_no_log_written_when_session_is_none() -> None:
    """REQ-ID: LLM-USAGE-006 — No DB write when _session is not provided."""
    txn = _make_transaction()
    client = _make_mock_client()

    # No session — must not raise, must still return a result
    result = llm_classify(txn, _client=client)
    assert result.confidence > 0.0
    # No assertion on DB — just confirm it doesn't crash


def test_multiple_calls_write_multiple_log_rows(session: Session) -> None:
    """Each API call produces its own LLMUsageLog row."""
    txn = _make_transaction()
    client = _make_mock_client()

    llm_classify(txn, _client=client, _session=session)
    llm_classify(txn, _client=client, _session=session)
    session.commit()

    logs = session.query(LLMUsageLog).all()
    assert len(logs) == 2


def test_usage_log_duration_ms_is_non_negative(session: Session) -> None:
    """duration_ms must always be >= 0."""
    txn = _make_transaction()
    client = _make_mock_client()

    llm_classify(txn, _client=client, _session=session)
    session.commit()

    log = session.query(LLMUsageLog).one()
    assert log.duration_ms >= 0


def test_usage_log_written_even_on_low_confidence(session: Session) -> None:
    """Usage is still logged when Claude returns low-confidence classification."""
    txn = _make_transaction()
    client = _make_mock_client(
        response_json={
            "entity": "personal",
            "tax_category": "PERSONAL_NON_DEDUCTIBLE",
            "direction": "expense",
            "confidence": 0.3,
            "reasoning": "Low confidence guess.",
        }
    )

    result = llm_classify(txn, _client=client, _session=session)
    session.commit()

    assert result.confidence == pytest.approx(0.3)
    logs = session.query(LLMUsageLog).all()
    assert len(logs) == 1
