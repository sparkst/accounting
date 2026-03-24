"""Foreign currency detection, conversion, and exchange rate lookup.

Detects non-USD currency amounts in email body text (GBP, EUR, JPY, etc.)
and converts to USD using the Frankfurter API (free, no key required).

Usage::

    from src.utils.currency import detect_currency, convert_to_usd

    hits = detect_currency("Total £ 4.99")
    # [CurrencyAmount(amount=4.99, currency_code='GBP')]

    result = convert_to_usd(4.99, 'GBP', '2025-06-25')
    # ConversionResult(usd_amount=6.79, rate=1.3603, date_used='2025-06-25', source='frankfurter_api')
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CurrencyAmount:
    """A detected currency amount from text."""
    amount: Decimal
    currency_code: str  # ISO 4217


@dataclass
class ExchangeRateResult:
    """Result from an exchange rate lookup."""
    rate: Decimal
    date_used: str  # actual date used (may differ if weekend/holiday)
    source: str  # "frankfurter_api"


@dataclass
class ConversionResult:
    """Result of converting a foreign amount to USD."""
    usd_amount: Decimal
    rate: Decimal
    date_used: str
    source: str


# ---------------------------------------------------------------------------
# Currency symbol / code mappings
# ---------------------------------------------------------------------------

_SYMBOL_TO_CODE: dict[str, str] = {
    "\u00a3": "GBP",  # £
    "\u20ac": "EUR",  # €
    "\u00a5": "JPY",  # ¥
    "\u20a3": "FRF",  # ₣ (historical)
    "\u20b9": "INR",  # ₹
    "\u20a9": "KRW",  # ₩
    "R$": "BRL",
    "C$": "CAD",
    "A$": "AUD",
}

# ISO 4217 codes we recognize (excludes USD — we don't detect USD as "foreign")
_KNOWN_CODES: set[str] = {
    "GBP", "EUR", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK",
    "PLN", "CZK", "HUF", "RON", "BGN", "HRK", "ISK", "TRY", "BRL", "MXN",
    "INR", "KRW", "SGD", "HKD", "TWD", "THB", "MYR", "PHP", "IDR", "ZAR",
    "ILS", "AED", "SAR", "CNY", "RUB",
}

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Pattern 1: Symbol prefix — £4.99, €25, ¥1000, € 25.00
_SYMBOL_PREFIX_RE = re.compile(
    r"(" + "|".join(re.escape(s) for s in _SYMBOL_TO_CODE) + r")"
    r"\s*([\d,]+(?:\.\d{1,2})?)",
)

# Pattern 2: Code suffix — 4.99 GBP, 25 EUR, 1000 JPY
_CODE_SUFFIX_RE = re.compile(
    r"([\d,]+(?:\.\d{1,2})?)\s*(" + "|".join(_KNOWN_CODES) + r")\b",
)

# Pattern 3: Code prefix — GBP 4.99, EUR 25
_CODE_PREFIX_RE = re.compile(
    r"\b(" + "|".join(_KNOWN_CODES) + r")\s*([\d,]+(?:\.\d{1,2})?)",
)


def detect_currency(text: str) -> list[CurrencyAmount]:
    """Scan text for non-USD currency amounts.

    Returns a list of CurrencyAmount objects found, ordered by position in text.
    USD amounts ($XX.XX) are intentionally excluded — they are handled by
    the normal amount extraction pipeline.
    """
    if not text:
        return []

    results: list[tuple[int, CurrencyAmount]] = []
    seen: set[tuple[float, str]] = set()

    def _add(pos: int, amount_str: str, code: str) -> None:
        raw = amount_str.replace(",", "")
        try:
            amount = Decimal(raw)
        except Exception:
            return
        if amount <= 0:
            return
        key = (amount, code)
        if key not in seen:
            seen.add(key)
            results.append((pos, CurrencyAmount(amount=amount, currency_code=code)))

    # Symbol prefix: £4.99, €25
    for m in _SYMBOL_PREFIX_RE.finditer(text):
        symbol = m.group(1)
        code = _SYMBOL_TO_CODE.get(symbol)
        if code:
            _add(m.start(), m.group(2), code)

    # Code suffix: 4.99 GBP, 25 EUR
    for m in _CODE_SUFFIX_RE.finditer(text):
        _add(m.start(), m.group(1), m.group(2))

    # Code prefix: GBP 4.99, EUR 25
    for m in _CODE_PREFIX_RE.finditer(text):
        _add(m.start(), m.group(2), m.group(1))

    # Sort by position and return just the CurrencyAmount objects
    results.sort(key=lambda x: x[0])
    return [ca for _, ca in results]


# ---------------------------------------------------------------------------
# Exchange rate cache & API
# ---------------------------------------------------------------------------

# Simple in-memory cache: (from_currency, to_currency, date) -> ExchangeRateResult
_rate_cache: dict[tuple[str, str, str], ExchangeRateResult] = {}


def fetch_exchange_rate(
    from_currency: str,
    to_currency: str = "USD",
    date: str = "latest",
) -> ExchangeRateResult | None:
    """Fetch an exchange rate from the Frankfurter API.

    Args:
        from_currency: ISO 4217 code (e.g. "GBP").
        to_currency:   Target currency (default "USD").
        date:          ISO date string "YYYY-MM-DD" or "latest".

    Returns:
        ExchangeRateResult on success, None on failure.
        Results are cached in memory for repeated calls with same parameters.
    """
    cache_key = (from_currency.upper(), to_currency.upper(), date)
    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    url = (
        f"https://api.frankfurter.dev/v1/{date}"
        f"?from={from_currency.upper()}&to={to_currency.upper()}&amount=1"
    )

    try:
        req = Request(url, headers={"User-Agent": "SparkryAccounting/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        rate = data.get("rates", {}).get(to_currency.upper())
        if rate is None:
            logger.warning("No rate returned for %s->%s on %s", from_currency, to_currency, date)
            return None

        result = ExchangeRateResult(
            rate=Decimal(str(rate)),
            date_used=data.get("date", date),
            source="frankfurter_api",
        )
        _rate_cache[cache_key] = result
        return result

    except (URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("Frankfurter API error for %s->%s on %s: %s", from_currency, to_currency, date, exc)
        return None


def convert_to_usd(
    amount: Decimal | float,
    currency_code: str,
    date: str = "latest",
) -> ConversionResult | None:
    """Convert a foreign currency amount to USD.

    Args:
        amount:        The amount in foreign currency (positive).
        currency_code: ISO 4217 code (e.g. "GBP").
        date:          Transaction date "YYYY-MM-DD" for historical rate.

    Returns:
        ConversionResult on success, None if the API call fails.
    """
    amount = Decimal(str(amount))

    if currency_code.upper() == "USD":
        return ConversionResult(
            usd_amount=amount,
            rate=Decimal("1"),
            date_used=date,
            source="identity",
        )

    rate_result = fetch_exchange_rate(currency_code, "USD", date)
    if rate_result is None:
        return None

    usd_amount = (amount * rate_result.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return ConversionResult(
        usd_amount=usd_amount,
        rate=rate_result.rate,
        date_used=rate_result.date_used,
        source=rate_result.source,
    )


def clear_rate_cache() -> None:
    """Clear the in-memory exchange rate cache (useful for testing)."""
    _rate_cache.clear()
