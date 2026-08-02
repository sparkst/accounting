"""Per-account display config for the daily Wealth flash (REQ-DFB-006).

Travis's 2026-08-02 template: short aliases (PF-=PenFed, CH-=Chase), custom
sections (STOCKS / RETIREMENT / 529s split; annuity + whole-life under LIFE
INSURANCE; the NA Builder IUL deliberately under RETIREMENT), and a hide list.

Edit freely — keys are wealth-D1 `account.id` values (stable). An account
missing from this map falls back to its D1 name and the account-type default
section in digest.py. Hidden accounts (explicit `hide=True` or balance under
``AUTO_HIDE_BELOW``) still count toward section totals and net worth; they
just don't render a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Dust threshold — anything below this auto-hides (REQ-DFB-006).
AUTO_HIDE_BELOW = Decimal("100")


@dataclass(frozen=True)
class FlashAccount:
    alias: str
    section: str  # cash | credit | stocks | retirement | 529 | loans | life | other
    hide: bool = False


FLASH_ACCOUNTS: dict[str, FlashAccount] = {
    # ── CASH ────────────────────────────────────────────────────────────────
    "db7bd804-8ad1-4969-bfef-54fd03cd70fe": FlashAccount("PF-Checking", "cash"),
    "ad0159d4-f0df-43ed-b45e-f6742dad69e7": FlashAccount("CH-Checking", "cash"),
    # Template said CH-, but this account's broker is PenFed → PF- (confirmed
    # inconsistency, flagged 2026-08-02).
    "113a1462-94a3-40ed-8199-79ebbf24d814": FlashAccount("PF-Savings", "cash"),
    "e287c52d-3b1f-47a3-b748-d1595f3d56ea": FlashAccount("PF-MM", "cash"),
    # ── CREDIT ──────────────────────────────────────────────────────────────
    "aec8a7e1-a5d4-486f-b75f-52e8f5069235": FlashAccount("Alaska Ascent", "credit"),
    "8e804849-8dee-45a6-9a3e-0eb6c97b0d40": FlashAccount("Costco Visa", "credit"),
    "cb0e984f-edb3-4961-8714-690224a3d9c7": FlashAccount("Prime Visa", "credit"),
    # ── STOCKS ──────────────────────────────────────────────────────────────
    "5658ff22-8f2c-4929-82d0-d819affb9d85": FlashAccount("E-Trade", "stocks"),
    "d442821f-6c5d-446e-88f0-f9a02a69e5a9": FlashAccount("Schwab", "stocks"),
    "ab2d72b2-d45b-437a-9ba1-95dd73902834": FlashAccount("Schw. AMZN", "stocks"),
    "4ea987d2-ft-8291": FlashAccount("Templ. Growth", "stocks"),
    # ── RETIREMENT ──────────────────────────────────────────────────────────
    "0c9cf072-e60c-4b19-acf1-7e0b7d2ea3de": FlashAccount("Travis IRA", "retirement"),
    "428ffa4d-baa0-405b-851b-38c2fee31bd8": FlashAccount("Amy IRA", "retirement"),
    "6617f786-d7d3-4684-b4a3-68f74ee39cd4": FlashAccount("Travis Roth IRA", "retirement"),
    "011e987e-df2d-424f-9f47-8d62900589de": FlashAccount("Amy Roth IRA", "retirement"),
    "3c12c098-30b9-4c44-85dd-e273f5b97482": FlashAccount("Fidelity 401k", "retirement"),
    "458ee1fd-9154-4ba1-9da9-437943f9bdeb": FlashAccount("HSA", "retirement"),
    "795bd503-gsk-pen": FlashAccount("GSK Pension", "retirement"),
    # F&G is an annuity (not life insurance) — its death benefit == account
    # value, so it belongs with retirement assets, not the LIFE payout section.
    "6dd9fd2c-fg-2585": FlashAccount("F&G Annuity", "retirement"),
    # ── 529s ────────────────────────────────────────────────────────────────
    "2c2fb215-f5fa-49be-9e08-3475b41bc9b4": FlashAccount("Emerson 529", "529"),
    "ab434ae1-86b9-4c3e-a43e-ed147d2cd0b9": FlashAccount("Aiden 529", "529"),
    # ── LOANS ───────────────────────────────────────────────────────────────
    "b7ac6407-ede9-4704-b851-f17053bfc4c3": FlashAccount("CH Mortgage", "loans"),
    # ── LIFE INSURANCE (cash values) ────────────────────────────────────────
    # NA Builder IUL lives here (not RETIREMENT) to match the WBR — broker
    # `north_american` classifies as insurance — so its death benefit + policy
    # loan roll into the LIFE INSURANCE summary.
    "0a5af9b7-a41f-4694-ab24-759ff8079957": FlashAccount("NA Builder", "life"),
    "f334e6e7-nwm-amy": FlashAccount("NWM Life — Amy", "life"),
    "0eda5238-nwm-9215": FlashAccount("NWM Life — Travis", "life"),
    "ac2e3142-nwm-5148": FlashAccount("NWM Life — Travis", "life"),
    # ── Hidden (template 2026-08-02) — still counted in totals/net worth ────
    "1ab67844-nwm-7277": FlashAccount("NWM Life — Aiden", "life", hide=True),
    "e7ad8c04-nwm-eme": FlashAccount("NWM Life — Emerson", "life", hide=True),
    # Dust rows (also caught by AUTO_HIDE_BELOW):
    "bf62db6a-a5e6-4e9d-8239-509f92b4538c": FlashAccount("Individual - TOD", "stocks", hide=True),
    "54e18d7d-132d-42a0-a2ec-6f753462d924": FlashAccount("unnamed · vanguard", "stocks", hide=True),
}
