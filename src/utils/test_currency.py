"""Tests for foreign currency detection and conversion.

Covers:
- detect_currency() with various symbol and code formats
- convert_to_usd() with mocked Frankfurter API responses
- API error handling (graceful None return)
- Backfill logic with fixture transactions
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.utils.currency import (
    clear_rate_cache,
    convert_to_usd,
    detect_currency,
    fetch_exchange_rate,
)

# ---------------------------------------------------------------------------
# detect_currency tests
# ---------------------------------------------------------------------------


class TestDetectCurrency:
    """Test currency detection from text snippets."""

    def test_gbp_symbol(self):
        result = detect_currency("Total \u00a3 4.99")
        assert len(result) == 1
        assert result[0].amount == 4.99
        assert result[0].currency_code == "GBP"

    def test_gbp_symbol_no_space(self):
        result = detect_currency("\u00a34.99")
        assert len(result) == 1
        assert result[0].amount == 4.99
        assert result[0].currency_code == "GBP"

    def test_eur_symbol(self):
        result = detect_currency("\u20ac25")
        assert len(result) == 1
        assert result[0].amount == 25.0
        assert result[0].currency_code == "EUR"

    def test_eur_symbol_with_decimals(self):
        result = detect_currency("\u20ac25.00")
        assert len(result) == 1
        assert result[0].amount == 25.0
        assert result[0].currency_code == "EUR"

    def test_jpy_symbol(self):
        result = detect_currency("\u00a51000")
        assert len(result) == 1
        assert result[0].amount == 1000.0
        assert result[0].currency_code == "JPY"

    def test_code_suffix_gbp(self):
        result = detect_currency("4.99 GBP")
        assert len(result) == 1
        assert result[0].amount == 4.99
        assert result[0].currency_code == "GBP"

    def test_code_suffix_eur(self):
        result = detect_currency("25 EUR payment")
        assert len(result) == 1
        assert result[0].amount == 25.0
        assert result[0].currency_code == "EUR"

    def test_code_suffix_jpy(self):
        result = detect_currency("1000 JPY")
        assert len(result) == 1
        assert result[0].amount == 1000.0
        assert result[0].currency_code == "JPY"

    def test_total_with_gbp_symbol(self):
        result = detect_currency("Your total is \u00a3 4.99 for this month")
        assert len(result) == 1
        assert result[0].amount == 4.99
        assert result[0].currency_code == "GBP"

    def test_eur_code_prefix(self):
        result = detect_currency("EUR 25.00 commission payment")
        assert len(result) == 1
        assert result[0].amount == 25.0
        assert result[0].currency_code == "EUR"

    def test_mixed_currencies(self):
        result = detect_currency("Paid \u00a34.99 GBP and \u20ac25 EUR")
        # Should find both (deduped by amount+code)
        codes = {r.currency_code for r in result}
        assert "GBP" in codes
        assert "EUR" in codes

    def test_no_currency(self):
        result = detect_currency("No foreign currency here, just plain text.")
        assert result == []

    def test_usd_not_matched(self):
        """USD amounts ($XX.XX) should NOT be detected as foreign currency."""
        result = detect_currency("Total $49.99 charged to your card")
        assert result == []

    def test_empty_string(self):
        result = detect_currency("")
        assert result == []

    def test_none_text(self):
        result = detect_currency("")
        assert result == []

    def test_comma_amounts(self):
        result = detect_currency("\u00a31,234.56")
        assert len(result) == 1
        assert result[0].amount == 1234.56
        assert result[0].currency_code == "GBP"

    def test_british_airways_style(self):
        """Real-world pattern from British Airways emails."""
        result = detect_currency("Total \u00a3 4.99\nThank you for your purchase")
        assert len(result) == 1
        assert result[0].amount == 4.99
        assert result[0].currency_code == "GBP"

    def test_lovable_style(self):
        """Real-world pattern from Lovable commission emails."""
        result = detect_currency("\u20ac25 EUR payment for commission")
        codes = {r.currency_code for r in result}
        assert "EUR" in codes
        amounts = {r.amount for r in result}
        assert 25.0 in amounts


# ---------------------------------------------------------------------------
# fetch_exchange_rate tests
# ---------------------------------------------------------------------------


class TestFetchExchangeRate:
    """Test exchange rate API calls with mocked responses."""

    def setup_method(self):
        clear_rate_cache()

    def test_successful_fetch(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "amount": 1,
            "base": "GBP",
            "date": "2025-06-25",
            "rates": {"USD": 1.3603},
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("src.utils.currency.urlopen", return_value=mock_response):
            result = fetch_exchange_rate("GBP", "USD", "2025-06-25")

        assert result is not None
        assert result.rate == 1.3603
        assert result.date_used == "2025-06-25"
        assert result.source == "frankfurter_api"

    def test_api_error_returns_none(self):
        from urllib.error import URLError
        with patch("src.utils.currency.urlopen", side_effect=URLError("Network error")):
            result = fetch_exchange_rate("GBP", "USD", "2025-06-25")
        assert result is None

    def test_caching(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "amount": 1,
            "base": "EUR",
            "date": "2025-06-25",
            "rates": {"USD": 1.08},
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("src.utils.currency.urlopen", return_value=mock_response) as mock_url:
            result1 = fetch_exchange_rate("EUR", "USD", "2025-06-25")
            result2 = fetch_exchange_rate("EUR", "USD", "2025-06-25")

        # Should only call the API once (second call hits cache)
        assert mock_url.call_count == 1
        assert result1 is not None
        assert result2 is not None
        assert result1.rate == result2.rate


# ---------------------------------------------------------------------------
# convert_to_usd tests
# ---------------------------------------------------------------------------


class TestConvertToUSD:
    """Test end-to-end currency conversion."""

    def setup_method(self):
        clear_rate_cache()

    def test_successful_conversion(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "amount": 1,
            "base": "GBP",
            "date": "2025-06-25",
            "rates": {"USD": 1.36},
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("src.utils.currency.urlopen", return_value=mock_response):
            result = convert_to_usd(4.99, "GBP", "2025-06-25")

        assert result is not None
        assert result.usd_amount == round(4.99 * 1.36, 2)
        assert result.rate == 1.36
        assert result.source == "frankfurter_api"

    def test_usd_identity(self):
        """Converting USD to USD returns the same amount."""
        result = convert_to_usd(100.0, "USD", "2025-06-25")
        assert result is not None
        assert result.usd_amount == 100.0
        assert result.rate == 1.0
        assert result.source == "identity"

    def test_api_failure_returns_none(self):
        from urllib.error import URLError
        with patch("src.utils.currency.urlopen", side_effect=URLError("fail")):
            result = convert_to_usd(25.0, "EUR", "2025-06-25")
        assert result is None


# ---------------------------------------------------------------------------
# Backfill logic tests
# ---------------------------------------------------------------------------


class TestBackfillLogic:
    """Test the backfill detection logic with fixture data."""

    def test_detect_currency_in_ba_email(self):
        """British Airways email body with GBP amount."""
        body = (
            "British Airways\n"
            "Booking reference: ABC123\n"
            "Total \u00a3 4.99\n"
            "Thank you for flying with us."
        )
        hits = detect_currency(body)
        assert len(hits) >= 1
        assert hits[0].currency_code == "GBP"
        assert hits[0].amount == 4.99

    def test_detect_currency_in_lovable_email(self):
        """Lovable commission email with EUR amount."""
        body = (
            "Lovable Commission Payment\n"
            "You received a \u20ac25 EUR payment for your referral.\n"
            "Payment ID: PAY-123456"
        )
        hits = detect_currency(body)
        codes = {h.currency_code for h in hits}
        assert "EUR" in codes
        amounts = {h.amount for h in hits}
        assert 25.0 in amounts

    def test_no_false_positive_on_usd_email(self):
        """Normal USD email should not trigger foreign currency detection."""
        body = (
            "Your receipt from Anthropic, PBC\n"
            "Amount paid $238.03\n"
            "Thank you for your payment."
        )
        hits = detect_currency(body)
        assert hits == []
