"""Institution registry + legacy-extractor wrappers (REQ-VIS-001).

Maps each of the five in-scope institutions to a
``(statement_type, prompt, schema)`` :class:`InstitutionSpec`, and provides
``legacy_extract`` — the *legacy side* of the shadow diff. Because each legacy
adapter's ``ImportResult`` captures COUNTS (not field values), we wrap each
adapter's PURE extraction helper into the normalized schema dict instead.

The wrappers accept already-extracted inputs (layout ``text`` for the PDF
adapters, a filename for FT's date, or a ``values`` dict for the value-fed
carriers) so tests never need real PDF/XLSX bytes. Per-institution failures are
caught by the caller (the shadow harness) — these wrappers raise ``ValueError``
on bad input, matching the underlying legacy helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from src.adapters import fg_pdf, ft_pdf, gsk_pdf
from src.adapters.north_american_iul import _DEFAULT_ACCOUNT_NAME
from src.vision import schemas

# ── Institution keys ─────────────────────────────────────────────────────────

FG: Final = "fg"
GSK: Final = "gsk"
NW_MUTUAL: Final = "nw_mutual"
FT: Final = "ft"
NA_IUL: Final = "na_iul"

INSTITUTIONS: Final[tuple[str, ...]] = (FG, GSK, NW_MUTUAL, FT, NA_IUL)


@dataclass(frozen=True)
class InstitutionSpec:
    """Everything the vision path needs to extract one institution's statement."""

    institution: str
    statement_type: str
    prompt: str
    schema: dict[str, Any]


_BALANCES_PROMPT = (
    "Extract the account balance statement into JSON with exactly these string "
    "fields: institution, account_number_mask (mask all but the last 4 chars), "
    "as_of (ISO YYYY-MM-DD), balance (two decimals). Return money as a string."
)
_POLICY_PROMPT = (
    "Extract the life-insurance policy values into JSON with exactly these "
    "string fields: policy_number, as_of (ISO YYYY-MM-DD), cash_value, "
    "surrender_value, death_benefit, premium_paid. Return money as strings with "
    "two decimals; use \"0.00\" for any value not present."
)

REGISTRY: Final[dict[str, InstitutionSpec]] = {
    FG: InstitutionSpec(FG, schemas.BALANCES, _BALANCES_PROMPT, schemas.BALANCES_SCHEMA),
    GSK: InstitutionSpec(GSK, schemas.BALANCES, _BALANCES_PROMPT, schemas.BALANCES_SCHEMA),
    NW_MUTUAL: InstitutionSpec(
        NW_MUTUAL, schemas.BALANCES, _BALANCES_PROMPT, schemas.BALANCES_SCHEMA
    ),
    FT: InstitutionSpec(FT, schemas.BALANCES, _BALANCES_PROMPT, schemas.BALANCES_SCHEMA),
    NA_IUL: InstitutionSpec(
        NA_IUL, schemas.POLICY_VALUES, _POLICY_PROMPT, schemas.POLICY_VALUES_SCHEMA
    ),
}

# Display names used on the legacy side of the balances diff.
_DISPLAY_NAME: Final[dict[str, str]] = {
    FG: "F&G",
    GSK: "GSK",
    NW_MUTUAL: "Northwestern Mutual",
    FT: "Franklin Templeton",
}


def spec_for(institution: str) -> InstitutionSpec:
    """Return the :class:`InstitutionSpec` for *institution* (KeyError if unknown)."""
    try:
        return REGISTRY[institution]
    except KeyError as exc:
        raise ValueError(f"unknown institution: {institution!r}") from exc


def mask_account(number: str) -> str:
    """Mask all but the last 4 characters of an account/policy number."""
    number = str(number)
    if len(number) <= 4:
        return number
    return "*" * (len(number) - 4) + number[-4:]


# ── Legacy-side wrappers ─────────────────────────────────────────────────────


def _legacy_fg(text: str, fallback_as_of: date | None) -> dict[str, Any]:
    flavor = fg_pdf.detect_template(text)
    if flavor == "annual":
        contract, as_of, balance = fg_pdf.extract_annual_statement(text)
    else:
        contract, as_of, balance = fg_pdf.extract_portal_screen(
            text, fallback_as_of or date.today()
        )
    return {
        "institution": _DISPLAY_NAME[FG],
        "account_number_mask": mask_account(contract),
        "as_of": as_of,
        "balance": balance,
    }


def _legacy_gsk(text: str) -> dict[str, Any]:
    as_of, balance = gsk_pdf.extract_closing_balance(text)
    return {
        "institution": _DISPLAY_NAME[GSK],
        "account_number_mask": gsk_pdf.GSK_ACCOUNT_NUMBER,
        "as_of": as_of,
        "balance": balance,
    }


def _legacy_ft(text: str, filename: str) -> dict[str, Any]:
    as_of = ft_pdf.parse_statement_filename(filename)
    balance = ft_pdf.extract_portfolio_overview(text)
    return {
        "institution": _DISPLAY_NAME[FT],
        "account_number_mask": ft_pdf.FT_ACCOUNT_NUMBER,
        "as_of": as_of,
        "balance": balance,
    }


def _require(values: dict[str, Any] | None, *keys: str) -> dict[str, Any]:
    if not values:
        raise ValueError("legacy_extract requires a 'values' dict for this institution")
    missing = [k for k in keys if k not in values]
    if missing:
        raise ValueError(f"legacy 'values' missing keys: {missing}")
    return values


def _legacy_nw_mutual(values: dict[str, Any] | None) -> dict[str, Any]:
    v = _require(values, "policy_number", "net_accum_value", "as_of")
    return {
        "institution": _DISPLAY_NAME[NW_MUTUAL],
        "account_number_mask": mask_account(v["policy_number"]),
        "as_of": v["as_of"],
        "balance": v["net_accum_value"],
    }


def _legacy_na_iul(values: dict[str, Any] | None) -> dict[str, Any]:
    v = _require(values, "policy_number", "as_of")
    booked = v.get("surrender_value")
    if booked is None:
        booked = v.get("accumulation_value")
    return {
        "policy_number": v["policy_number"],
        "as_of": v["as_of"],
        "cash_value": v.get("accumulation_value", booked),
        "surrender_value": v.get("surrender_value", "0.00"),
        "death_benefit": v.get("death_benefit", "0.00"),
        "premium_paid": v.get("premium_paid", "0.00"),
    }


def legacy_extract(
    institution: str,
    *,
    text: str | None = None,
    filename: str | None = None,
    values: dict[str, Any] | None = None,
    fallback_as_of: date | None = None,
) -> dict[str, Any]:
    """Return the legacy adapter's fields for *institution*, normalized.

    Inputs vary by institution:

    * ``fg``/``gsk`` — ``text`` (pdftotext layout dump).
    * ``ft`` — ``text`` + ``filename`` (the ``YYYY-MM-DD.pdf`` basename → as_of).
    * ``nw_mutual`` — ``values`` with ``policy_number``, ``net_accum_value``, ``as_of``.
    * ``na_iul`` — ``values`` with ``policy_number``, ``as_of`` and any of
      ``surrender_value``/``accumulation_value``/``death_benefit``/``premium_paid``.

    The returned dict is passed through :func:`schemas.normalize_fields` so it
    diffs cleanly against a normalized vision extraction. Raises ``ValueError``
    on missing/unparseable input (the caller isolates per-file).
    """
    spec = spec_for(institution)
    if institution == FG:
        if text is None:
            raise ValueError("fg legacy_extract requires 'text'")
        raw = _legacy_fg(text, fallback_as_of)
    elif institution == GSK:
        if text is None:
            raise ValueError("gsk legacy_extract requires 'text'")
        raw = _legacy_gsk(text)
    elif institution == FT:
        if text is None or filename is None:
            raise ValueError("ft legacy_extract requires 'text' and 'filename'")
        raw = _legacy_ft(text, filename)
    elif institution == NW_MUTUAL:
        raw = _legacy_nw_mutual(values)
    elif institution == NA_IUL:
        raw = _legacy_na_iul(values)
    else:  # pragma: no cover — spec_for already guards
        raise ValueError(f"unknown institution: {institution!r}")

    return schemas.normalize_fields(spec.statement_type, raw)


# Referenced only to keep the na_iul default name discoverable to callers.
DEFAULT_IUL_ACCOUNT_NAME: Final = _DEFAULT_ACCOUNT_NAME


__all__ = [
    "FG",
    "FT",
    "GSK",
    "INSTITUTIONS",
    "NA_IUL",
    "NW_MUTUAL",
    "REGISTRY",
    "InstitutionSpec",
    "legacy_extract",
    "mask_account",
    "spec_for",
]
