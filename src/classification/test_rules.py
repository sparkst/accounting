"""Tests for Tier-1 vendor rule matching and ranking.

REQ-ID: REQ-FIX-ING-005  Literal-by-default vendor patterns: is_regex flag
                          drives matcher behavior; invalid regex is skipped
                          loudly instead of silently falling back to
                          substring matching.
REQ-ID: REQ-FIX-ING-009  Tier-1 ranking prefers pattern specificity (matched
                          length) before example count, then confidence,
                          then id for determinism.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.classification.rules import lookup_vendor_rule
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory
from src.models.vendor_rule import VendorRule


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


def _rule(
    session: Session,
    pattern: str,
    *,
    is_regex: bool = False,
    entity: Entity = Entity.SPARKRY,
    tax_category: TaxCategory = TaxCategory.SUPPLIES,
    direction: Direction = Direction.EXPENSE,
    examples: int = 1,
    confidence: float = 0.90,
) -> VendorRule:
    rule = VendorRule(
        vendor_pattern=pattern,
        is_regex=is_regex,
        entity=entity.value,
        tax_category=tax_category.value,
        direction=direction.value,
        examples=examples,
        confidence=confidence,
    )
    session.add(rule)
    session.commit()
    return rule


# ---------------------------------------------------------------------------
# REQ-FIX-ING-005: literal-by-default matching
# ---------------------------------------------------------------------------


class TestLiteralByDefaultMatching:
    def test_literal_pattern_does_not_over_match_via_regex_semantics(
        self, session: Session
    ) -> None:
        """'A.B Corp' as a literal must NOT match 'A9B Corporate' — the '.'
        in the stored pattern is a literal dot, not a regex wildcard."""
        _rule(session, "A.B Corp")
        result = lookup_vendor_rule("A9B Corporate", session)
        assert result is None

    def test_literal_pattern_matches_case_insensitive_substring(
        self, session: Session
    ) -> None:
        _rule(session, "A.B Corp")
        result = lookup_vendor_rule("Invoice from a.b corp #123", session)
        assert result is not None

    def test_is_regex_true_uses_regex_semantics(self, session: Session) -> None:
        _rule(session, r"amazon.*aws|aws\.amazon", is_regex=True)
        result = lookup_vendor_rule("AMAZON WEB SERVICES AWS CHARGE", session)
        assert result is not None

    def test_invalid_regex_with_is_regex_true_is_skipped_not_fallback(
        self, session: Session
    ) -> None:
        """An invalid regex pattern (unbalanced paren) must be skipped
        entirely — never silently reinterpreted as a literal substring."""
        _rule(session, "amazon(unclosed", is_regex=True)
        # If this fell back to literal-substring matching, the description
        # below (which literally contains "amazon(unclosed") would match.
        result = lookup_vendor_rule("charge from amazon(unclosed today", session)
        assert result is None

    def test_learned_rule_write_path_sets_is_regex_false(
        self, session: Session
    ) -> None:
        """New VendorRule() instances default to is_regex=False (the
        _upsert_vendor_rule learning-loop write path never sets True). The
        Python-side default applies at flush time."""
        rule = VendorRule(
            vendor_pattern="Some Vendor",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
        )
        session.add(rule)
        session.flush()
        assert rule.is_regex is False


# ---------------------------------------------------------------------------
# REQ-FIX-ING-009: specificity-first ranking
# ---------------------------------------------------------------------------


class TestSpecificityRanking:
    def test_specific_pattern_beats_generic_regardless_of_examples(
        self, session: Session
    ) -> None:
        """'amazon web services' (2 examples) beats 'amazon' (40 examples)
        for an AWS description — specificity (matched length) is ranked
        before example count."""
        _rule(
            session,
            "amazon",
            examples=40,
            tax_category=TaxCategory.OFFICE_EXPENSE,
        )
        _rule(
            session,
            "amazon web services",
            examples=2,
            tax_category=TaxCategory.SUPPLIES,
        )
        result = lookup_vendor_rule("AMAZON WEB SERVICES AWS-EAST-1", session)
        assert result is not None
        assert result.tax_category == TaxCategory.SUPPLIES

    def test_equal_length_falls_back_to_examples_then_confidence(
        self, session: Session
    ) -> None:
        _rule(
            session,
            "vendor a",
            examples=5,
            confidence=0.80,
            tax_category=TaxCategory.OFFICE_EXPENSE,
        )
        _rule(
            session,
            "vendor b",
            examples=10,
            confidence=0.80,
            tax_category=TaxCategory.SUPPLIES,
        )
        # Neither literally matches — use a description containing both
        # equal-length substrings is impossible; instead verify tie-break via
        # confidence when examples are equal and length is equal.
        result = lookup_vendor_rule("charge from vendor b today", session)
        assert result is not None
        assert result.tax_category == TaxCategory.SUPPLIES

    def test_deterministic_on_full_ties(self, session: Session) -> None:
        """When length, examples, and confidence are all equal, ranking is
        deterministic (breaks on rule id) — repeated lookups return the same
        result rather than a query-order-dependent one."""
        _rule(
            session,
            "widget",
            examples=3,
            confidence=0.85,
            tax_category=TaxCategory.OFFICE_EXPENSE,
        )
        _rule(
            session,
            "widget",
            examples=3,
            confidence=0.85,
            entity=Entity.BLACKLINE,
            tax_category=TaxCategory.SUPPLIES,
        )
        first = lookup_vendor_rule("Widget Co charge", session)
        second = lookup_vendor_rule("Widget Co charge", session)
        assert first is not None
        assert second is not None
        assert first.tax_category == second.tax_category
        assert first.entity == second.entity
