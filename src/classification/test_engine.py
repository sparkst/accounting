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
    source_id: str = "test-001",
) -> Transaction:
    """Factory for minimal Transaction instances (not persisted to DB)."""
    return Transaction(
        source=source,
        source_id=source_id,
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

    def test_shopify_refund_is_other_expense_not_supplies(self) -> None:
        """REQ-FIX-TAX-003 (issue #58): a Shopify refund is contra-revenue,
        not a purchased-supplies expense. Rule 1 must not lump refunds in
        with real negative-amount Shopify fees — refunds keep OTHER_EXPENSE
        so a later reclassify pass can't clobber the adapter's category
        back to SUPPLIES.
        """
        txn = _make_transaction(
            description="Shopify Refund for #1024",
            source=Source.SHOPIFY.value,
            amount=Decimal("-32.76"),
            source_id="refund_999",
        )
        result = match_structural_pattern(txn)

        assert result is not None
        assert result.entity == Entity.BLACKLINE
        assert result.tax_category == TaxCategory.OTHER_EXPENSE
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
    """Build a mock Gemini client that returns *response_json* as text."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(response_json)
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 50
    mock_response.model_version = "gemini-2.5-flash-lite"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

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
        mock_response = MagicMock()
        mock_response.text = "This is not JSON at all."
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        txn = _make_transaction(description="Mystery vendor")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence == 0.0
        assert "JSON parse error" in result.reasoning

    def test_markdown_fenced_json_is_parsed(self) -> None:
        """Gemini sometimes wraps output in ```json ... ``` fences — _parse_response strips them."""
        mock_response = MagicMock()
        mock_response.text = (
            "```json\n"
            '{"entity": "sparkry", "tax_category": "OFFICE_EXPENSE", '
            '"direction": "expense", "confidence": 0.82, '
            '"reasoning": "Office supply purchase."}\n'
            "```"
        )
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        txn = _make_transaction(description="Staples office supplies")
        result = llm_classify(txn, _client=mock_client)

        assert result.entity == Entity.SPARKRY
        assert result.tax_category == TaxCategory.OFFICE_EXPENSE
        assert result.confidence == pytest.approx(0.82)

    def test_api_error_returns_low_confidence(self) -> None:
        from google.genai import errors as genai_errors

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = genai_errors.ClientError(
            429, {"error": {"message": "rate limit", "status": "RESOURCE_EXHAUSTED"}}
        )
        txn = _make_transaction(description="Any vendor")
        result = llm_classify(txn, _client=mock_client)

        assert result.confidence == 0.0
        assert "Gemini API error" in result.reasoning

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

    def test_plaid_outflow_never_classified_income(self, seeded_session: Session) -> None:
        """Guard: an authoritative-signed outflow (Plaid, amount < 0) that a tier
        labels as income is overridden to OTHER_EXPENSE + needs_review.

        Regression for the Amex 'CLAUDE.AI SUBSCRIPTION' -220.60 charges that
        keyword-matched SUBSCRIPTION_INCOME and inflated Sparkry B&O gross via
        the abs(amount) tax aggregation.
        """
        txn = _make_transaction(
            description="Mystery Vendor QQQ no-rule-match",
            source=Source.PLAID.value,
            amount=Decimal("-220.60"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.SUBSCRIPTION_INCOME,
                direction=Direction.INCOME,
                confidence=0.95,
                tier_used=3,
                reasoning="keyword 'subscription' matched",
            )
            result = classify(txn, seeded_session)

        assert result.direction == Direction.EXPENSE
        assert result.tax_category not in {
            TaxCategory.CONSULTING_INCOME,
            TaxCategory.SUBSCRIPTION_INCOME,
            TaxCategory.SALES_INCOME,
            TaxCategory.WHOLESALE_INCOME,
        }
        assert result.status == TransactionStatus.NEEDS_REVIEW
        assert "outflow" in (result.review_reason or "").lower()

    def test_plaid_inflow_classified_expense_routes_to_needs_review(
        self, seeded_session: Session
    ) -> None:
        """REQ-FIX-ING-008: mirror veto — an authoritative-signed INFLOW
        (Plaid, amount > 0) that a tier labels as expense routes to
        needs_review, but — unlike the income-on-outflow veto — direction and
        tax_category are NOT overridden (a positive-amount "expense" is
        usually a refund; the human decides refund-income vs category
        reversal)."""
        txn = _make_transaction(
            description="Mystery Refund QQQ no-rule-match",
            source=Source.PLAID.value,
            amount=Decimal("220.60"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.OFFICE_EXPENSE,
                direction=Direction.EXPENSE,
                confidence=0.95,
                tier_used=3,
                reasoning="keyword matched an expense category",
            )
            result = classify(txn, seeded_session)

        # Category/direction preserved — NOT overridden.
        assert result.direction == Direction.EXPENSE
        assert result.tax_category == TaxCategory.OFFICE_EXPENSE
        assert result.status == TransactionStatus.NEEDS_REVIEW
        assert "inflow" in (result.review_reason or "").lower()

    def test_plaid_inflow_transfer_direction_not_vetoed(
        self, seeded_session: Session
    ) -> None:
        """transfer/reimbursable directions are exempt from the mirror veto —
        the condition requires direction == EXPENSE."""
        txn = _make_transaction(
            description="Internal transfer QQQ no-rule-match",
            source=Source.PLAID.value,
            amount=Decimal("500.00"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.OFFICE_EXPENSE,
                direction=Direction.TRANSFER,
                confidence=0.95,
                tier_used=3,
                reasoning="looks like a transfer",
            )
            result = classify(txn, seeded_session)

        assert result.direction == Direction.TRANSFER
        assert result.status == TransactionStatus.AUTO_CLASSIFIED

    def test_gmail_negative_income_not_overridden(self, seeded_session: Session) -> None:
        """The guard must NOT touch Gmail: that adapter stores income as
        -abs(amount) *before* classification sets direction=income, so a
        negative Gmail amount classified as income is correct, not a mismatch.
        """
        txn = _make_transaction(
            description="Stripe payout deposit receipt",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-500.00"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.SALES_INCOME,
                direction=Direction.INCOME,
                confidence=0.95,
                tier_used=3,
                reasoning="receipt body indicates income",
            )
            result = classify(txn, seeded_session)

        assert result.direction == Direction.INCOME
        assert result.tax_category == TaxCategory.SALES_INCOME

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

    def test_apply_result_preserves_entity_for_stripe(self) -> None:
        """Stripe entity is set authoritatively by the adapter (per-account key);
        classification must NOT reassign it. Regression for parent-account
        Substack charges/fees/payouts being relabeled BlackLine by the LLM."""
        txn = _make_transaction(description="STRIPE charge", source=Source.STRIPE.value)
        txn.entity = Entity.SPARKRY.value  # adapter set this from the account key
        result = ClassificationResult(
            entity=Entity.BLACKLINE,  # the LLM's (wrong) guess
            tax_category=TaxCategory.SALES_INCOME,
            direction=Direction.INCOME,
            confidence=0.9,
            tier_used=3,
            reasoning="LLM guess",
        )
        apply_result(txn, result)
        assert txn.entity == Entity.SPARKRY.value  # preserved, not overwritten
        assert txn.tax_category == TaxCategory.SALES_INCOME.value  # category still applied

    def test_apply_result_sets_entity_for_non_authoritative_source(self) -> None:
        """For gmail/bank the adapter can't know the entity, so classification
        legitimately assigns it."""
        txn = _make_transaction(description="bank row", source=Source.BANK_CSV.value)
        txn.entity = None
        result = ClassificationResult(
            entity=Entity.BLACKLINE,
            tax_category=TaxCategory.SALES_INCOME,
            direction=Direction.INCOME,
            confidence=0.9,
            tier_used=1,
            reasoning="rule",
        )
        apply_result(txn, result)
        assert txn.entity == Entity.BLACKLINE.value

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
            "src.classification.llm_classifier.genai.Client",
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
# REQ-GMOBJ-02: gmail rows never auto-book as income
# ---------------------------------------------------------------------------


class TestGmailIncomeVeto:
    """Issue #85: gmail_n8n's adapter contract is signed_amount = -abs()
    (always an expense). A tier that mis-guesses an income tax_category for a
    gmail-sourced row (e.g. a broken `[object Object]` description defeating
    vendor/pattern rules) must be downgraded to NEEDS_REVIEW rather than
    silently AUTO_CLASSIFIED, mirroring the existing _reconcile_sign() outflow
    veto but keyed on source == GMAIL_N8N rather than on amount sign (gmail is
    deliberately excluded from _AUTHORITATIVE_SIGN_SOURCES)."""

    def test_gmail_income_veto_downgrades_subscription_income_to_needs_review(
        self, seeded_session: Session
    ) -> None:
        """Regression for accounting#85: the ElevenLabs charge
        (4ed8f5dd-f936-4357-8a58-7e47b9f3af8f, 2026-08-18, +$24.27) was
        mis-booked SUBSCRIPTION_INCOME, inflating Sparkry B&O gross."""
        txn = _make_transaction(
            description="[object Object]",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-24.27"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.SUBSCRIPTION_INCOME,
                direction=Direction.INCOME,
                confidence=0.95,
                tier_used=3,
                reasoning="keyword 'subscription' matched",
            )
            result = classify(txn, seeded_session)

        assert result.status == TransactionStatus.NEEDS_REVIEW
        assert "gmail" in (result.review_reason or "").lower()

    def test_gmail_income_veto_ignores_non_gmail_sources(
        self, seeded_session: Session
    ) -> None:
        """The gmail-keyed veto must not fire for a non-gmail source."""
        txn = _make_transaction(
            description="Some subscription income vendor",
            source=Source.STRIPE.value,
            amount=Decimal("24.27"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.SUBSCRIPTION_INCOME,
                direction=Direction.INCOME,
                confidence=0.95,
                tier_used=3,
                reasoning="keyword 'subscription' matched",
            )
            result = classify(txn, seeded_session)

        assert result.status == TransactionStatus.AUTO_CLASSIFIED
        assert result.tax_category == TaxCategory.SUBSCRIPTION_INCOME

    def test_gmail_income_veto_leaves_expense_classification_alone(
        self, seeded_session: Session
    ) -> None:
        """A correctly-classified gmail expense row (e.g.
        517ecbb1-a4cd-45b2-9f66-87c875f6884a, OFFICE_EXPENSE -$27.58) must
        not be touched by the income veto."""
        txn = _make_transaction(
            description="[object Object]",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-27.58"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.OFFICE_EXPENSE,
                direction=Direction.EXPENSE,
                confidence=0.95,
                tier_used=3,
                reasoning="office supplies keyword matched",
            )
            result = classify(txn, seeded_session)

        assert result.status == TransactionStatus.AUTO_CLASSIFIED
        assert result.tax_category == TaxCategory.OFFICE_EXPENSE


class TestGmailIncomeVetoEdges:
    """Round-1 review fixes for accounting#85."""

    def test_reimbursable_direction_is_not_exempt(
        self, seeded_session: Session
    ) -> None:
        """An income tax_category must be vetoed even when direction is
        REIMBURSABLE — gross receipts aggregate on tax_category alone."""
        txn = _make_transaction(
            description="[object Object]",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-24.27"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.CONSULTING_INCOME,
                direction=Direction.REIMBURSABLE,
                confidence=0.95,
                tier_used=3,
                reasoning="reimbursable guess",
            )
            result = classify(txn, seeded_session)

        assert result.status == TransactionStatus.NEEDS_REVIEW

    def test_low_confidence_reason_is_preserved(
        self, seeded_session: Session
    ) -> None:
        """The veto appends to, never replaces, the tier's own explanation."""
        txn = _make_transaction(
            description="[object Object]",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-24.27"),
        )
        with patch("src.classification.llm_classifier.llm_classify") as mock_llm:
            mock_llm.return_value = ClassificationResult(
                entity=Entity.SPARKRY,
                tax_category=TaxCategory.SUBSCRIPTION_INCOME,
                direction=Direction.INCOME,
                confidence=0.10,
                tier_used=3,
                reasoning="unsure",
            )
            result = classify(txn, seeded_session)

        reason = result.review_reason or ""
        assert "Low confidence" in reason
        assert "gmail" in reason.lower()

    def test_corrupted_payload_marker_survives_classification(
        self, seeded_session: Session
    ) -> None:
        """A corrupted-payload flag set at ingest must not be erased by a
        confident expense classification."""
        txn = _make_transaction(
            description="elevenlabs.io",
            source=Source.GMAIL_N8N.value,
            amount=Decimal("-24.27"),
        )
        txn.review_reason = (
            "Corrupted upstream payload: the sender header contained the "
            "literal '[object Object]'."
        )
        result = ClassificationResult(
            entity=Entity.SPARKRY,
            tax_category=TaxCategory.OFFICE_EXPENSE,
            direction=Direction.EXPENSE,
            confidence=0.95,
            tier_used=3,
            status=TransactionStatus.AUTO_CLASSIFIED,
            reasoning="office expense",
        )
        apply_result(txn, result)

        assert txn.status == TransactionStatus.NEEDS_REVIEW.value
        assert "Corrupted upstream payload" in (txn.review_reason or "")


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
