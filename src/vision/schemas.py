"""Statement-type JSON Schemas + boundary normalization (REQ-VIS-001).

Three normalized schemas the vision extractor targets, one per statement type:

* ``balances``       — ``{institution, account_number_mask, as_of, balance}``
* ``positions``      — ``{account, as_of, positions:[{symbol, quantity, price, value}]}``
* ``policy_values``  — ``{policy_number, as_of, cash_value, surrender_value,
                          death_benefit, premium_paid}``

All monetary fields are STRINGS in the schema. At the boundary they are
converted with ``Decimal(str(v)).quantize(Decimal("0.01"))`` so ``10.5`` and
``10.50`` normalize to the same value; date fields are coerced to ISO
(``YYYY-MM-DD``). ``normalize_fields`` is the single boundary both the legacy
and vision sides pass through before diffing.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

# ── Statement types ──────────────────────────────────────────────────────────

BALANCES: Final = "balances"
POSITIONS: Final = "positions"
POLICY_VALUES: Final = "policy_values"

STATEMENT_TYPES: Final[frozenset[str]] = frozenset({BALANCES, POSITIONS, POLICY_VALUES})

# ── Quantization quanta ──────────────────────────────────────────────────────

_CENTS: Final[Decimal] = Decimal("0.01")
_SHARES: Final[Decimal] = Decimal("0.00000001")


# ── JSON Schemas (passed to the provider as structured-output config) ─────────

BALANCES_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "institution": {"type": "string"},
        "account_number_mask": {"type": "string"},
        "as_of": {"type": "string"},
        "balance": {"type": "string"},
    },
    "required": ["institution", "account_number_mask", "as_of", "balance"],
}

POSITIONS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "account": {"type": "string"},
        "as_of": {"type": "string"},
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "quantity": {"type": "string"},
                    "price": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["symbol", "quantity", "price", "value"],
            },
        },
    },
    "required": ["account", "as_of", "positions"],
}

POLICY_VALUES_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "policy_number": {"type": "string"},
        "as_of": {"type": "string"},
        "cash_value": {"type": "string"},
        "surrender_value": {"type": "string"},
        "death_benefit": {"type": "string"},
        "premium_paid": {"type": "string"},
    },
    "required": [
        "policy_number",
        "as_of",
        "cash_value",
        "surrender_value",
        "death_benefit",
        "premium_paid",
    ],
}

SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    BALANCES: BALANCES_SCHEMA,
    POSITIONS: POSITIONS_SCHEMA,
    POLICY_VALUES: POLICY_VALUES_SCHEMA,
}

# Which top-level fields are monetary / dates, per statement type.
_MONEY_FIELDS: Final[dict[str, frozenset[str]]] = {
    BALANCES: frozenset({"balance"}),
    POSITIONS: frozenset(),
    POLICY_VALUES: frozenset(
        {"cash_value", "surrender_value", "death_benefit", "premium_paid"}
    ),
}
_DATE_FIELDS: Final[frozenset[str]] = frozenset({"as_of"})


# ── Boundary normalizers ─────────────────────────────────────────────────────


def normalize_money(value: Any) -> str:
    """Quantize a monetary value to cents and return it as a canonical string.

    ``Decimal(str(v)).quantize(Decimal("0.01"))`` — so ``10.5`` and ``"10.50"``
    return ``"10.50"``. ``None``/empty → ``"0.00"``. Unparseable input raises
    ``ValueError`` (callers isolate per-record).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return str(Decimal("0").quantize(_CENTS))
    raw = value.strip() if isinstance(value, str) else value
    if isinstance(raw, str):
        raw = raw.replace("$", "").replace(",", "").strip()
    try:
        return str(Decimal(str(raw)).quantize(_CENTS))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unparseable money value: {value!r}") from exc


def normalize_shares(value: Any) -> str:
    """Quantize a share quantity to 8 dp and return it as a canonical string."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return str(Decimal("0").quantize(_SHARES))
    raw = value.strip() if isinstance(value, str) else value
    if isinstance(raw, str):
        raw = raw.replace(",", "").strip()
    try:
        return str(Decimal(str(raw)).quantize(_SHARES))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unparseable share quantity: {value!r}") from exc


def normalize_date(value: Any) -> str:
    """Coerce a date/datetime/date-string to an ISO ``YYYY-MM-DD`` string.

    Accepts ``date``, ``datetime``, ISO strings, and US ``MM/DD/YYYY``. If the
    value cannot be parsed it is returned stringified verbatim (the diff engine
    then compares it exactly, surfacing the divergence as a mismatch).
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return str(value)
    s = value.strip()
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _normalize_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Normalize one position row (symbol str, quantity 8dp, price/value cents)."""
    out: dict[str, Any] = dict(pos)
    if "symbol" in out and out["symbol"] is not None:
        out["symbol"] = str(out["symbol"]).strip().upper()
    if "quantity" in out:
        out["quantity"] = normalize_shares(out["quantity"])
    for money_key in ("price", "value"):
        if money_key in out:
            out[money_key] = normalize_money(out[money_key])
    return out


def normalize_fields(statement_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *fields* with money/date/share fields normalized.

    Idempotent: normalizing an already-normalized dict yields the same dict.
    Unknown ``statement_type`` values pass money-normalization by-name is skipped
    but dates are still coerced.
    """
    money = _MONEY_FIELDS.get(statement_type, frozenset())
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _DATE_FIELDS:
            out[key] = normalize_date(value)
        elif key in money:
            out[key] = normalize_money(value)
        elif statement_type == POSITIONS and key == "positions":
            out[key] = [
                _normalize_position(p) if isinstance(p, dict) else p
                for p in (value or [])
            ]
        else:
            out[key] = value
    return out


__all__ = [
    "BALANCES",
    "BALANCES_SCHEMA",
    "POLICY_VALUES",
    "POLICY_VALUES_SCHEMA",
    "POSITIONS",
    "POSITIONS_SCHEMA",
    "SCHEMAS",
    "STATEMENT_TYPES",
    "normalize_date",
    "normalize_fields",
    "normalize_money",
    "normalize_shares",
]
