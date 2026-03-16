"""Tests for the 3-tier classification engine.

Tests are co-located with the source per project conventions. Each test
module section covers one tier plus full-engine orchestration.

Test database uses SQLite in-memory so nothing touches ``data/accounting.db``.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.classification.engine import ClassificationResult, apply_result, classify
from src.classification.llm_classifier import _parse_response, llm_classify
from src.classification.patterns import match_structural_pattern
from src.classification.rules import lookup_vendor_rule
from src.classification.seed_rules import seed_vendor_rules
from src.db.connection import _configure_sqlite
from src.models.base import Base
from src.models.enums import (
    Direction,
    Entity,
    Source,
    TaxCategory,
    TaxSubcategory,
    TransactionStatus,
)
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def in_memory_engine() -> Generator[Any, None, None]:
    """SQLite in-memory engine with the full schema."""
    from sqlalchemy import event

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
    """Return a session backed by the in-memory engine."""
    factory = sessionmaker(bind=in_memory_engine, autocommit=False, autoflush=False)
    with factory() as s:
        yield s


@pytest.fixture()
def seeded_session(session: Session) -> Session:
    """Session with vendor rules pre-seeded."""
    count = seed_vendor_rules(session)
    assert count > 0, "Expected seed rules to be inserted"
    return session


def _make_transaction(
    description: str = "Test Vendor",
    source: str = Source.BANK_CSV.value,
    amount: Decimal = Decimal("-50.00"),
    date: str = "2026-03-01",
    raw_data: dict[str, Any] | None = None,
) -> Transaction:
    """Factory for minimal Transaction instances (not persisted to DB)."""
    return Transaction(
        source=source,
        source_id="test-001",
        source_hash="abc123",
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        raw_data=raw_data or {},
    )


# ---------------------------------------------------------------------------
# Tier 1: Vendor rules
# ---------------------------------------------------------------------------


class TestTier1VendorRules:
    def test_known_vendor_anthropic_matches(self, seeded_session: Session) -> None:
        """Anthropic description should hit Tier 1 rule with high confidence."""
        result = lookup_vendor_rule("Anthropic API usage charge", seeded_session)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUPPLIES
        assert result.direction == Direction.EXPENSE
        assert result.confidence > 0.8
        assert result.tier_used == 1
        # Now uses ai_services subcategory instead of software
        assert result.tax_subcategory == TaxSubcategory.AI_SERVICES.value

    def test_aws_pattern_matches(self, seeded_session: Session) -> None:
        """AWS description should match the amazon.*aws pattern."""
        result = lookup_vendor_rule("Amazon AWS monthly invoice", seeded_session)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUPPLIES
        assert result.confidence > 0.8

    def test_hiscox_insurance(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("HISCOX Insurance Payment", seeded_session)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.INSURANCE
        assert result.direction == Direction.EXPENSE

    def test_fiverr_contract_labor(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Fiverr freelancer payment", seeded_session)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.CONTRACT_LABOR

    def test_northwest_registered_agent(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Northwest Registered Agent LLC fee", seeded_session)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.LEGAL_AND_PROFESSIONAL

    def test_shopify_vendor_rule(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Shopify payout", seeded_session)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SALES_INCOME
        assert result.direction == Direction.INCOME

    def test_ecoenclose_packaging(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("EcoEnclose packaging order", seeded_session)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SUPPLIES

    def test_fedex_shipping(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("FedEx Ground shipping label", seeded_session)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SUPPLIES

    def test_render_matches_sparkry_software_tools(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Render monthly invoice", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUPPLIES
        assert result.tax_subcategory == TaxSubcategory.SOFTWARE_TOOLS.value

    def test_lovable_matches_ai_services(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Lovable Labs Incorporated receipt", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_subcategory == TaxSubcategory.AI_SERVICES.value

    def test_runpod_matches_ai_services(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("RunPod invoice", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_subcategory == TaxSubcategory.AI_SERVICES.value

    def test_eleven_labs_matches(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Eleven Labs Inc.", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_subcategory == TaxSubcategory.AI_SERVICES.value

    def test_vercel_matches_software_tools(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Vercel Inc. receipt", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_subcategory == TaxSubcategory.SOFTWARE_TOOLS.value

    def test_google_workspace_matches(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Google Payments invoice for sparkry.com", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_subcategory == TaxSubcategory.SOFTWARE_TOOLS.value

    def test_brist_mfg_matches_blackline_cogs(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Brist Mfg receipt of payment", seeded_session)
        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.COGS
        assert result.tax_subcategory == TaxSubcategory.MANUFACTURING.value

    def test_minuteman_press_print_marketing(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Minuteman Press invoice", seeded_session)
        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_subcategory == TaxSubcategory.PRINT_MARKETING.value

    def test_dhl_shipping(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("DHL shipment", seeded_session)
        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_subcategory == TaxSubcategory.SHIPPING_INBOUND.value

    def test_pinterest_advertising(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Pinterest ads statement", seeded_session)
        assert result is not None
        assert result.tax_category == TaxCategory.ADVERTISING
        assert result.direction == Direction.EXPENSE

    def test_wifi_onboard_travel(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Wi-Fi Onboard receipt", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.TRAVEL
        assert result.tax_subcategory == TaxSubcategory.WIFI.value

    def test_apple_personal(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Apple receipt for app purchase", seeded_session)
        assert result is not None
        assert result.entity == Entity.PERSONAL
        assert result.tax_category == TaxCategory.PERSONAL_NON_DEDUCTIBLE

    def test_blacklinemtb_sales_income(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("Black Line MTB order notification", seeded_session)
        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SALES_INCOME
        assert result.direction == Direction.INCOME

    def test_no_match_returns_none(self, seeded_session: Session) -> None:
        result = lookup_vendor_rule("totally unknown mystery vendor xyz", seeded_session)
        assert result is None

    def test_empty_rules_table_returns_none(self, session: Session) -> None:
        """Empty vendor_rules table should return None without error."""
        result = lookup_vendor_rule("Anthropic", session)
        assert result is None

    def test_case_insensitive_match(self, seeded_session: Session) -> None:
        """Pattern matching must be case-insensitive."""
        result = lookup_vendor_rule("ANTHROPIC API CHARGE", seeded_session)
        assert result is not None
        assert result.entity == Entity.SPARKRY

    def test_seed_idempotent(self, seeded_session: Session) -> None:
        """Re-running seed_vendor_rules should insert 0 new rows."""
        inserted_again = seed_vendor_rules(seeded_session)
        assert inserted_again == 0

    def test_highest_examples_wins(self, session: Session) -> None:
        """When multiple rules match, the one with more examples wins."""
        rule_low = VendorRule(
            vendor_pattern="acme",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
            confidence=0.95,
            examples=2,
        )
        rule_high = VendorRule(
            vendor_pattern="acme",
            entity=Entity.BLACKLINE.value,
            tax_category=TaxCategory.COGS.value,
            direction=Direction.EXPENSE.value,
            confidence=0.90,
            examples=20,
        )
        session.add_all([rule_low, rule_high])
        session.commit()

        result = lookup_vendor_rule("ACME Corp charge", session)
        assert result is not None
        # Rule with more examples should win even though confidence is lower.
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.COGS


# ---------------------------------------------------------------------------
# Tier 2: Structural patterns
# ---------------------------------------------------------------------------


class TestTier2Patterns:
    def test_shopify_source_income(self) -> None:
        txn = _make_transaction(
            description="Order #12345",
            source=Source.SHOPIFY.value,
            amount=Decimal("89.99"),
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SALES_INCOME
        assert result.direction == Direction.INCOME
        assert result.confidence >= 0.7
        assert result.tier_used == 2

    def test_shopify_negative_amount_is_expense(self) -> None:
        txn = _make_transaction(
            description="Shopify subscription fee",
            source=Source.SHOPIFY.value,
            amount=Decimal("-29.00"),
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.direction == Direction.EXPENSE

    def test_stripe_substack_subscription_income(self) -> None:
        txn = _make_transaction(
            description="Stripe payout substack subscribers",
            source=Source.STRIPE.value,
            amount=Decimal("450.00"),
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUBSCRIPTION_INCOME
        assert result.direction == Direction.INCOME

    def test_stripe_substack_in_subject(self) -> None:
        txn = _make_transaction(
            description="Stripe payout",
            source=Source.STRIPE.value,
            amount=Decimal("300.00"),
            raw_data={"subject": "Substack monthly payout ready"},
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUBSCRIPTION_INCOME

    def test_sap_ariba_in_from_address(self) -> None:
        txn = _make_transaction(
            description="Payment notification",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("8500.00"),
            raw_data={"from": "ariba-notifications@sap.com"},
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.CONSULTING_INCOME
        assert result.direction == Direction.INCOME

    def test_sap_in_description(self) -> None:
        txn = _make_transaction(
            description="SAP Ariba PO confirmation",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("12000.00"),
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.CONSULTING_INCOME

    def test_photo_receipt_returns_none(self) -> None:
        txn = _make_transaction(
            description="Receipt scan",
            source=Source.PHOTO_RECEIPT.value,
        )
        result = match_structural_pattern(txn)
        assert result is None

    def test_self_forwarded_email_returns_none(self) -> None:
        txn = _make_transaction(
            description="Hardware order",
            source=Source.GMAIL_N8N.value,
            raw_data={"from": "travis@sparkry.com"},
        )
        result = match_structural_pattern(txn)
        assert result is None

    def test_stripe_without_substack_returns_none(self) -> None:
        txn = _make_transaction(
            description="Stripe payout for consulting",
            source=Source.STRIPE.value,
            amount=Decimal("1000.00"),
        )
        result = match_structural_pattern(txn)
        assert result is None

    def test_bank_csv_unknown_returns_none(self) -> None:
        txn = _make_transaction(
            description="RANDOM VENDOR 1234",
            source=Source.BANK_CSV.value,
        )
        result = match_structural_pattern(txn)
        assert result is None


# ---------------------------------------------------------------------------
# Tier 3: LLM classifier
# ---------------------------------------------------------------------------


def _make_mock_client(response_json: dict[str, Any]) -> MagicMock:
    """Build a mock Anthropic client that returns *response_json* as text."""
    mock_content = MagicMock()
    mock_content.text = json.dumps(response_json)

    mock_response = MagicMock()
    mock_response.content = [mock_content]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    return mock_client


class TestTier3LLMClassifier:
    def test_successful_classification(self) -> None:
        """LLM returns valid JSON → ClassificationResult populated correctly."""
        mock_client = _make_mock_client(
            {
                "entity": "sparkry",
                "tax_category": "SUPPLIES",
                "direction": "expense",
                "confidence": 0.88,
                "reasoning": "GitHub Copilot is a SaaS dev tool for Sparkry.",
            }
        )
        txn = _make_transaction(description="GitHub Copilot monthly subscription")
        result = llm_classify(txn, _client=mock_client)

        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUPPLIES
        assert result.direction == Direction.EXPENSE
        assert result.confidence == pytest.approx(0.88)
        assert result.tier_used == 3
        assert "SaaS" in result.reasoning

    def test_income_classification(self) -> None:
        mock_client = _make_mock_client(
            {
                "entity": "blackline",
                "tax_category": "SALES_INCOME",
                "direction": "income",
                "confidence": 0.92,
                "reasoning": "Shopify order for BlackLine MTB LLC.",
            }
        )
        txn = _make_transaction(description="Order fulfillment payment", amount=Decimal("199.00"))
        result = llm_classify(txn, _client=mock_client)

        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SALES_INCOME
        assert result.direction == Direction.INCOME

    def test_low_confidence_result(self) -> None:
        mock_client = _make_mock_client(
            {
                "entity": "personal",
                "tax_category": "PERSONAL_NON_DEDUCTIBLE",
                "direction": "expense",
                "confidence": 0.45,
                "reasoning": "Cannot determine business purpose from description.",
            }
        )
        txn = _make_transaction(description="Unknown vendor 9999")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence < 0.7
        assert result.tier_used == 3

    def test_invalid_entity_falls_back_to_error_result(self) -> None:
        mock_client = _make_mock_client(
            {
                "entity": "not_a_real_entity",
                "tax_category": "SUPPLIES",
                "direction": "expense",
                "confidence": 0.9,
                "reasoning": "Bad entity value.",
            }
        )
        txn = _make_transaction(description="Some vendor")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence == 0.0
        assert "Invalid entity" in result.reasoning

    def test_invalid_tax_category_falls_back(self) -> None:
        mock_client = _make_mock_client(
            {
                "entity": "sparkry",
                "tax_category": "NOT_A_CATEGORY",
                "direction": "expense",
                "confidence": 0.9,
                "reasoning": "Bad category.",
            }
        )
        txn = _make_transaction(description="Some vendor")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence == 0.0
        assert "Invalid tax_category" in result.reasoning

    def test_malformed_json_falls_back(self) -> None:
        mock_content = MagicMock()
        mock_content.text = "This is not JSON at all."
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        txn = _make_transaction(description="Mystery vendor")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence == 0.0
        assert "JSON parse error" in result.reasoning

    def test_markdown_fenced_json_is_parsed(self) -> None:
        """Claude sometimes wraps output in ```json ... ``` fences."""
        mock_content = MagicMock()
        mock_content.text = (
            "```json\n"
            '{"entity": "sparkry", "tax_category": "OFFICE_EXPENSE", '
            '"direction": "expense", "confidence": 0.82, '
            '"reasoning": "Office supply purchase."}\n'
            "```"
        )
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        txn = _make_transaction(description="Staples office supplies")
        result = llm_classify(txn, _client=mock_client)

        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.OFFICE_EXPENSE
        assert result.confidence == pytest.approx(0.82)

    def test_api_error_returns_low_confidence(self) -> None:
        import anthropic as _anthropic

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _anthropic.APIStatusError(
            "rate limit",
            response=MagicMock(status_code=429),
            body={},
        )
        txn = _make_transaction(description="Any vendor")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence == 0.0
        assert "API error" in result.reasoning

    def test_parse_response_confidence_clamped(self) -> None:
        raw = json.dumps(
            {
                "entity": "sparkry",
                "tax_category": "SUPPLIES",
                "direction": "expense",
                "confidence": 9999.0,
                "reasoning": "Overconfident model.",
            }
        )
        result = _parse_response(raw)
        assert result.confidence <= 1.0


# ---------------------------------------------------------------------------
# Full engine orchestration
# ---------------------------------------------------------------------------


class TestClassificationEngine:
    def test_tier1_hit_does_not_escalate(self, seeded_session: Session) -> None:
        """Known vendor should be classified by Tier 1 without reaching LLM."""
        txn = _make_transaction(
            description="Anthropic usage invoice",
            source=Source.BANK_CSV.value,
        )
        # Patch llm_classify at its home module so that any accidental Tier 3
        # call is intercepted — patch() must target the attribute on the module
        # where the function is defined, not where it is imported.
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            result = classify(txn, seeded_session)

        assert result.tier_used == 1
        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.SUPPLIES
        assert result.status == TransactionStatus.AUTO_CLASSIFIED
        mock_llm.assert_not_called()

    def test_tier2_hit_skips_tier3(self, seeded_session: Session) -> None:
        """Shopify source should be classified by Tier 2 without reaching LLM."""
        txn = _make_transaction(
            description="Order #99999 from customer",
            source=Source.SHOPIFY.value,
            amount=Decimal("149.00"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            result = classify(txn, seeded_session)

        assert result.tier_used == 2
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.SALES_INCOME
        mock_llm.assert_not_called()

    def test_tier3_reached_for_unknown_vendor(self, seeded_session: Session) -> None:
        """Unknown vendor that matches no rule or pattern escalates to Tier 3."""
        txn = _make_transaction(
            description="Totally Unknown Vendor XYZ9",
            source=Source.BANK_CSV.value,
        )
        # The engine calls _llm_mod.llm_classify where _llm_mod is the
        # llm_classifier module.  Patch the function on that module.
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.PERSONAL,
                tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
                direction=Direction.EXPENSE,
                confidence=0.75,
                tier_used=3,
                reasoning="Cannot determine business purpose.",
            )
            result = classify(txn, seeded_session)

        assert result.tier_used == 3
        mock_llm.assert_called_once()

    def test_low_confidence_sets_needs_review(self, seeded_session: Session) -> None:
        """Confidence < 0.7 from all tiers should set status=needs_review."""
        txn = _make_transaction(
            description="Mysterious Vendor ZZZZZ",
            source=Source.BANK_CSV.value,
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.PERSONAL,
                tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
                direction=Direction.EXPENSE,
                confidence=0.40,  # below threshold
                tier_used=3,
                reasoning="Very uncertain about this transaction.",
            )
            result = classify(txn, seeded_session)

        assert result.status == TransactionStatus.NEEDS_REVIEW
        assert result.review_reason is not None
        assert "0.40" in result.review_reason

    def test_apply_result_writes_all_fields(self, in_memory_engine: Any) -> None:
        """apply_result must populate every classification field on the ORM model."""
        factory = sessionmaker(bind=in_memory_engine, autocommit=False, autoflush=False)
        with factory() as session:
            txn = _make_transaction(description="Anthropic charge")
            session.add(txn)
            session.flush()

            classification = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.SUPPLIES,
                direction=Direction.EXPENSE,
                confidence=0.97,
                tier_used=1,
                reasoning="Matched vendor rule.",
                status=TransactionStatus.AUTO_CLASSIFIED,
            )
            apply_result(txn, classification)

            assert txn.entity == Entity.SPARKRY.value
            assert txn.tax_category == TaxCategory.SUPPLIES.value
            assert txn.direction == Direction.EXPENSE.value
            assert txn.confidence == pytest.approx(0.97)
            assert txn.status == TransactionStatus.AUTO_CLASSIFIED.value
            assert txn.review_reason is None

    def test_apply_result_sets_review_reason_when_needed(self) -> None:
        txn = _make_transaction(description="Unknown")
        result = ClassificationResult(
            entity=Entity.PERSONAL,
            tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
            direction=Direction.EXPENSE,
            confidence=0.35,
            tier_used=3,
            reasoning="Uncertain.",
            status=TransactionStatus.NEEDS_REVIEW,
            review_reason="Low confidence (0.35): Uncertain.",
        )
        apply_result(txn, result)

        assert txn.status == TransactionStatus.NEEDS_REVIEW.value
        assert txn.review_reason == "Low confidence (0.35): Uncertain."

    def test_full_pipeline_with_real_llm_mock(self, seeded_session: Session) -> None:
        """End-to-end: unknown vendor → Tier 3 fires → result applied to transaction."""
        txn = _make_transaction(
            description="Totally Novel Software Vendor ABC",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-79.00"),
        )
        seeded_session.add(txn)
        seeded_session.flush()

        mock_client = _make_mock_client(
            {
                "entity": "sparkry",
                "tax_category": "SUPPLIES",
                "direction": "expense",
                "confidence": 0.80,
                "reasoning": "SaaS software tool for Sparkry based on amount and source.",
            }
        )

        with patch(
            "src.classification.llm_classifier.anthropic.Anthropic",
            return_value=mock_client,
        ):
            result = classify(txn, seeded_session)
            apply_result(txn, result)
            seeded_session.commit()

        assert result.tier_used == 3
        assert txn.entity == Entity.SPARKRY.value
        assert txn.tax_category == TaxCategory.SUPPLIES.value
        assert txn.status == TransactionStatus.AUTO_CLASSIFIED.value


# ---------------------------------------------------------------------------
# Seed rules
# ---------------------------------------------------------------------------


class TestSeedRules:
    def test_seed_inserts_expected_count(self, session: Session) -> None:
        inserted = seed_vendor_rules(session)
        assert inserted >= 20  # at least 20 known vendors (expanded set)

    def test_all_seeded_rules_have_valid_enums(self, seeded_session: Session) -> None:
        """Every seeded rule's field values must parse into valid enums."""
        rules = seeded_session.query(VendorRule).all()
        for rule in rules:
            Entity(rule.entity)
            TaxCategory(rule.tax_category)
            Direction(rule.direction)
            if rule.tax_subcategory:
                from src.models.enums import TaxSubcategory
                TaxSubcategory(rule.tax_subcategory)

    def test_seeded_rules_have_high_confidence(self, seeded_session: Session) -> None:
        """All seed rules must have confidence >= 0.8 (human-authored)."""
        rules = seeded_session.query(VendorRule).all()
        for rule in rules:
            assert rule.confidence >= 0.8, (
                f"Rule {rule.vendor_pattern!r} has low confidence {rule.confidence}"
            )
