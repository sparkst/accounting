"""Enumerations for the accounting system.

All values match the design spec verbatim. Python enum names are UPPER_CASE;
the string values stored in SQLite use the same casing to keep queries readable.
"""

import enum


class Entity(enum.StrEnum):
    """Legal entity or personal."""

    SPARKRY = "sparkry"
    BLACKLINE = "blackline"
    PERSONAL = "personal"


class Direction(enum.StrEnum):
    """Cash flow direction for a transaction."""

    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    REIMBURSABLE = "reimbursable"


class TaxCategory(enum.StrEnum):
    """IRS / WA tax category codes.

    Business (Schedule C / Form 1065):
        ADVERTISING, CAR_AND_TRUCK, CONTRACT_LABOR, INSURANCE,
        LEGAL_AND_PROFESSIONAL, OFFICE_EXPENSE, SUPPLIES,
        TAXES_AND_LICENSES, TRAVEL, MEALS, COGS,
        CONSULTING_INCOME, SUBSCRIPTION_INCOME, SALES_INCOME, REIMBURSABLE

    Personal (Schedule A / Other):
        CHARITABLE_CASH, CHARITABLE_STOCK, MEDICAL, STATE_LOCAL_TAX,
        MORTGAGE_INTEREST, INVESTMENT_INCOME, PERSONAL_NON_DEDUCTIBLE
    """

    # ── Business ──────────────────────────────────────────────────────────────
    ADVERTISING = "ADVERTISING"
    CAR_AND_TRUCK = "CAR_AND_TRUCK"
    CONTRACT_LABOR = "CONTRACT_LABOR"
    INSURANCE = "INSURANCE"
    LEGAL_AND_PROFESSIONAL = "LEGAL_AND_PROFESSIONAL"
    OFFICE_EXPENSE = "OFFICE_EXPENSE"
    SUPPLIES = "SUPPLIES"
    TAXES_AND_LICENSES = "TAXES_AND_LICENSES"
    TRAVEL = "TRAVEL"
    MEALS = "MEALS"
    COGS = "COGS"
    CONSULTING_INCOME = "CONSULTING_INCOME"
    SUBSCRIPTION_INCOME = "SUBSCRIPTION_INCOME"
    SALES_INCOME = "SALES_INCOME"
    REIMBURSABLE = "REIMBURSABLE"

    # ── Personal ──────────────────────────────────────────────────────────────
    CHARITABLE_CASH = "CHARITABLE_CASH"
    CHARITABLE_STOCK = "CHARITABLE_STOCK"
    MEDICAL = "MEDICAL"
    STATE_LOCAL_TAX = "STATE_LOCAL_TAX"
    MORTGAGE_INTEREST = "MORTGAGE_INTEREST"
    INVESTMENT_INCOME = "INVESTMENT_INCOME"
    PERSONAL_NON_DEDUCTIBLE = "PERSONAL_NON_DEDUCTIBLE"


class TaxSubcategory(enum.StrEnum):
    """Fine-grained subcategories used in split line items and vendor rules."""

    # Travel sub-items
    FLIGHTS = "flights"
    LODGING = "lodging"
    GROUND_TRANSPORT = "ground_transport"
    WIFI = "wifi"

    # Meals context
    MEALS_CLIENT = "meals_client"
    MEALS_SOLO = "meals_solo"

    # Income sub-items
    SUBSCRIPTION = "subscription"
    CONSULTING = "consulting"
    PRODUCT_SALES = "product_sales"

    # COGS
    INVENTORY = "inventory"
    SHIPPING_COGS = "shipping_cogs"

    # Office / Supplies
    SOFTWARE = "software"
    HARDWARE = "hardware"
    OFFICE_SUPPLIES = "office_supplies"

    # SaaS / cloud (more specific than SOFTWARE)
    AI_SERVICES = "ai_services"      # Anthropic, OpenRouter, RunPod, ElevenLabs
    SOFTWARE_TOOLS = "software_tools"  # Vercel, Render, Google Workspace, Lovable

    # BlackLine shipping / packaging
    PACKAGING = "packaging"
    SHIPPING = "shipping"
    RAW_MATERIALS = "raw_materials"

    # Charitable
    CASH_DONATION = "cash_donation"
    STOCK_DONATION = "stock_donation"

    # Investment
    CAPITAL_GAIN_SHORT = "capital_gain_short"
    CAPITAL_GAIN_LONG = "capital_gain_long"
    DIVIDEND = "dividend"

    # General
    OTHER = "other"


class TransactionStatus(enum.StrEnum):
    """Lifecycle status of a transaction record."""

    AUTO_CLASSIFIED = "auto_classified"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    SPLIT_PARENT = "split_parent"
    REJECTED = "rejected"


class Source(enum.StrEnum):
    """Originating data source / adapter."""

    GMAIL_N8N = "gmail_n8n"
    STRIPE = "stripe"
    SHOPIFY = "shopify"
    BROKERAGE_CSV = "brokerage_csv"
    BANK_CSV = "bank_csv"
    PHOTO_RECEIPT = "photo_receipt"
    DEDUCTION_EMAIL = "deduction_email"


class VendorRuleSource(enum.StrEnum):
    """How a vendor rule was created."""

    HUMAN = "human"
    LEARNED = "learned"


class IngestionStatus(enum.StrEnum):
    """Outcome of a single ingestion run."""

    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    FAILURE = "failure"


class FileStatus(enum.StrEnum):
    """Outcome of processing a source file."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ConfirmedBy(enum.StrEnum):
    """Who confirmed the transaction classification."""

    AUTO = "auto"
    HUMAN = "human"
