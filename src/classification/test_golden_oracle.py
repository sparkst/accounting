"""Classification regression oracle (reliability audit 2026-07-27, follow-up 8).

~10 misclassification bugs this month were human-spotted because nothing
re-asserts classifier behavior over a known input set. This oracle replays a
fixed fixture of representative transactions through the DETERMINISTIC tiers
(Tier 1 seeded vendor rules + Tier 2 structural patterns; Tier 3 LLM is
stubbed to a no-answer) and compares against a committed golden file.

A legitimate behavior change (new seed rule, pattern fix) updates the golden:

    python -m pytest src/classification/test_golden_oracle.py --golden-update

and the diff lands in review, where a tax-affecting change is visible instead
of silent. Fixture inputs live in tests/fixtures/classification-golden/.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.classification.engine import ClassificationResult, classify
from src.classification.seed_rules import seed_vendor_rules
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory
from src.models.transaction import Transaction

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "classification-golden"
INPUTS_PATH = FIXTURE_DIR / "transactions.json"
GOLDEN_PATH = FIXTURE_DIR / "expected.json"

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    seed_vendor_rules(s)
    s.commit()
    yield s
    s.rollback()
    from src.models.vendor_rule import VendorRule

    s.query(VendorRule).delete()
    s.commit()
    s.close()


def _tx(spec: dict[str, Any]) -> Transaction:
    return Transaction(
        id=str(uuid.uuid4()),
        source=spec.get("source", "gmail_n8n"),
        source_hash=str(uuid.uuid4()),
        date=spec.get("date", "2026-07-01"),
        description=spec["description"],
        amount=spec["amount"],
        currency="USD",
        direction=spec.get("direction"),
        status="needs_review",
        raw_data=spec.get("raw_data", {}),
    )


def _no_llm(*args: Any, **kwargs: Any) -> ClassificationResult:
    """Tier 3 stub: the deterministic oracle never calls the network.

    Mirrors llm_classifier._error_result so needs-review fallthrough behavior
    is byte-identical to a no-API-key production run.
    """
    return ClassificationResult(
        entity=Entity.PERSONAL,
        tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
        direction=Direction.EXPENSE,
        confidence=0.0,
        tier_used=3,
        reasoning="llm disabled in oracle",
    )


def _replay(session: Session) -> list[dict[str, Any]]:
    inputs = json.loads(INPUTS_PATH.read_text())
    outputs: list[dict[str, Any]] = []
    with patch("src.classification.llm_classifier.llm_classify", side_effect=_no_llm):
        for spec in inputs:
            result = classify(_tx(spec), session)
            outputs.append(
                {
                    "description": spec["description"],
                    "amount": spec["amount"],
                    "entity": result.entity.value if result.entity else None,
                    "tax_category": result.tax_category.value if result.tax_category else None,
                    "direction": result.direction.value if result.direction else None,
                    "deductible_pct": result.deductible_pct,
                    "confidence": round(result.confidence, 4),
                    "tier_used": result.tier_used,
                    "status": result.status.value if result.status else None,
                }
            )
    return outputs


def test_classifier_matches_golden(session: Session, request: pytest.FixtureRequest) -> None:
    actual = _replay(session)
    if request.config.getoption("--golden-update", default=False):
        GOLDEN_PATH.write_text(json.dumps(actual, indent=2) + "\n")
        pytest.skip("golden updated — review the diff and commit")
    golden = json.loads(GOLDEN_PATH.read_text())
    assert actual == golden, (
        "Classifier output diverged from the golden fixture. If the change is"
        " intended, regenerate with --golden-update and commit the diff."
    )
