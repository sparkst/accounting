"""Pre-seed the VendorRule table with known vendors.

Call :func:`seed_vendor_rules` once during initial setup (or whenever the
``vendor_rules`` table is empty) to populate it with real data from Travis's
business operations. The function is idempotent — it checks for existing
patterns before inserting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.models.enums import Direction, Entity, TaxCategory, TaxSubcategory, VendorRuleSource
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RuleDef:
    """Compact definition of a seed vendor rule."""

    vendor_pattern: str
    entity: Entity
    tax_category: TaxCategory
    direction: Direction
    tax_subcategory: TaxSubcategory | None = None
    deductible_pct: float = 1.0
    confidence: float = 0.95
    examples: int = 5
    # REQ-FIX-ING-005: rules.py now matches vendor_pattern as a literal
    # substring unless is_regex=True. Most seed patterns are plain vendor
    # words (identical behavior either way), but a handful deliberately use
    # regex syntax (alternation, wildcards, \b/\s) — those are explicitly
    # flagged True below so their matching semantics are unchanged.
    is_regex: bool = False


# ---------------------------------------------------------------------------
# Known vendor rules
# ---------------------------------------------------------------------------

_SEED_RULES: list[_RuleDef] = [
    # ── Sparkry LLC ──────────────────────────────────────────────────────

    # AI API providers
    _RuleDef(
        vendor_pattern=r"anthropic",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.AI_SERVICES,
        confidence=0.97,
        examples=12,
    ),
    _RuleDef(
        vendor_pattern=r"openrouter",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.AI_SERVICES,
        confidence=0.97,
        examples=3,
    ),
    _RuleDef(
        vendor_pattern=r"runpod",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.AI_SERVICES,
        confidence=0.97,
        examples=4,
    ),
    _RuleDef(
        vendor_pattern=r"eleven.*labs|elevenlabs",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.AI_SERVICES,
        confidence=0.97,
        examples=2,
    ),
    _RuleDef(
        vendor_pattern=r"lovable",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.AI_SERVICES,
        confidence=0.97,
        examples=5,
    ),

    # SaaS / dev tools
    _RuleDef(
        vendor_pattern=r"amazon.*aws|aws\.amazon",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.97,
        examples=24,
    ),
    _RuleDef(
        vendor_pattern=r"\brender\b",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.97,
        examples=6,
    ),
    _RuleDef(
        vendor_pattern=r"\bvercel\b",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.97,
        examples=3,
    ),
    _RuleDef(
        vendor_pattern=r"google.*workspace|google.*payments",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.95,
        examples=10,
    ),
    _RuleDef(
        vendor_pattern=r"elementor",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.97,
        examples=2,
    ),
    _RuleDef(
        vendor_pattern=r"\bspoton\b",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.95,
        examples=2,
    ),

    # Insurance
    _RuleDef(
        vendor_pattern=r"hiscox",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.INSURANCE,
        direction=Direction.EXPENSE,
        confidence=0.97,
        examples=4,
    ),

    # Contract labor
    _RuleDef(
        vendor_pattern=r"fiverr",
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.CONTRACT_LABOR,
        direction=Direction.EXPENSE,
        confidence=0.95,
        examples=8,
    ),

    # Infrastructure / hosting
    _RuleDef(
        vendor_pattern=r"\bcloudflare\b",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.95,
        examples=2,
    ),
    _RuleDef(
        vendor_pattern=r"\bdreamhost\b",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.95,
        examples=2,
    ),
    _RuleDef(
        vendor_pattern=r"\bgodaddy\b",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOFTWARE_TOOLS,
        confidence=0.95,
        examples=2,
    ),

    # Travel
    _RuleDef(
        vendor_pattern=r"wi-?fi.*onboard|wifionboard",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.TRAVEL,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.WIFI,
        confidence=0.97,
        examples=8,
    ),

    # Advertising
    _RuleDef(
        vendor_pattern=r"pinterest",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.ADVERTISING,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SOCIAL_ADS,
        confidence=0.97,
        examples=5,
    ),

    # Income
    _RuleDef(
        vendor_pattern=r"stripe.*substack|substack.*stripe",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.SUBSCRIPTION_INCOME,
        direction=Direction.INCOME,
        tax_subcategory=TaxSubcategory.SUBSCRIPTION,
        confidence=0.97,
        examples=18,
    ),
    _RuleDef(
        vendor_pattern=r"cardinal.*health|fascinate.*os",
        is_regex=True,
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.CONSULTING_INCOME,
        direction=Direction.INCOME,
        tax_subcategory=TaxSubcategory.CONSULTING,
        confidence=0.97,
        examples=10,
    ),

    # Personal
    _RuleDef(
        vendor_pattern=r"apple.*receipt|apple\.com|\bapple\b",
        is_regex=True,
        entity=Entity.PERSONAL,
        tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
        direction=Direction.EXPENSE,
        confidence=0.90,
        examples=3,
    ),

    # ── BlackLine MTB LLC ───────────────────────────────────────────────────

    _RuleDef(
        vendor_pattern=r"northwest.*registered|northwest\s+registered\s+agent",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.LEGAL_AND_PROFESSIONAL,
        direction=Direction.EXPENSE,
        confidence=0.97,
        examples=3,
    ),
    _RuleDef(
        vendor_pattern=r"\bshopify\b",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SALES_INCOME,
        direction=Direction.INCOME,
        tax_subcategory=TaxSubcategory.PRODUCT_SALES,
        confidence=0.95,
        examples=40,
    ),
    # Shopify monthly PLATFORM/HOSTING fee — "SHOPIFY* <digits>" on the card —
    # is a website-hosting EXPENSE, not sales income. The asterisk distinguishes
    # it from the payout ("SHOPIFYPMT") and order ("Shopify Order #") income,
    # which lack one. examples=41 so it outranks the generic \bshopify\b income
    # rule (examples=40) when both match the "SHOPIFY*" charge string.
    _RuleDef(
        vendor_pattern=r"shopify\s*\*",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.ECOMMERCE_PLATFORM,
        confidence=0.97,
        examples=41,
    ),
    # Ecommerce platforms
    _RuleDef(
        vendor_pattern=r"woocommerce",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.ECOMMERCE_PLATFORM,
        confidence=0.95,
        examples=4,
    ),

    # Print & packaging
    _RuleDef(
        vendor_pattern=r"minuteman.*press",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.PRINT_MARKETING,
        confidence=0.95,
        examples=3,
    ),

    # Manufacturing & sourcing
    _RuleDef(
        vendor_pattern=r"brist.*mfg",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.COGS,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.MANUFACTURING,
        confidence=0.97,
        examples=4,
    ),
    _RuleDef(
        vendor_pattern=r"leeline",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.COGS,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.RAW_MATERIALS,
        confidence=0.97,
        examples=3,
    ),
    _RuleDef(
        vendor_pattern=r"boelter",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.COGS,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.INVENTORY,
        confidence=0.95,
        examples=2,
    ),

    # Shipping & logistics
    _RuleDef(
        vendor_pattern=r"\bdhl\b",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SHIPPING_INBOUND,
        confidence=0.95,
        examples=4,
    ),
    _RuleDef(
        vendor_pattern=r"\bfedex\b",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SHIPPING,
        confidence=0.93,
        examples=15,
    ),
    _RuleDef(
        vendor_pattern=r"\busps\b|stamps\.com",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.SHIPPING,
        confidence=0.93,
        examples=5,
    ),

    # Packaging & labels
    _RuleDef(
        vendor_pattern=r"ecoenclose",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.PACKAGING,
        confidence=0.95,
        examples=6,
    ),
    _RuleDef(
        vendor_pattern=r"stickermule|sticker\s+mule",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.PACKAGING,
        confidence=0.95,
        examples=4,
    ),

    # Events & races
    _RuleDef(
        vendor_pattern=r"tune.?up.*event|nwtuneup",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.MEALS,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.MEALS_EVENT,
        deductible_pct=0.5,
        confidence=0.90,
        examples=2,
    ),
    _RuleDef(
        vendor_pattern=r"usa\s*cycling|usac",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.ADVERTISING,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.EVENT_FEES,
        confidence=0.95,
        examples=2,
    ),
    _RuleDef(
        vendor_pattern=r"bikereg|bike\s*reg",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.ADVERTISING,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.EVENT_FEES,
        confidence=0.95,
        examples=3,
    ),

    # Photography
    _RuleDef(
        vendor_pattern=r"gaby.*photo",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.CONTRACT_LABOR,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.PHOTOGRAPHY,
        confidence=0.95,
        examples=2,
    ),

    # Stripe fees (BlackLine payment processing)
    _RuleDef(
        vendor_pattern=r"stripe.*fee|stripe.*inc",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.PAYMENT_PROCESSING,
        confidence=0.90,
        examples=10,
    ),

    # BlackLine income
    _RuleDef(
        vendor_pattern=r"black.*line.*mtb|blacklinemtb",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.SALES_INCOME,
        direction=Direction.INCOME,
        tax_subcategory=TaxSubcategory.PRODUCT_SALES,
        confidence=0.95,
        examples=20,
    ),

    # Fiverr for BlackLine (design work)
    _RuleDef(
        vendor_pattern=r"fiverr",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.CONTRACT_LABOR,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.BRAND_DESIGN,
        confidence=0.85,
        examples=3,
    ),

    # Hartford insurance (BlackLine)
    _RuleDef(
        vendor_pattern=r"hartford",
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.INSURANCE,
        direction=Direction.EXPENSE,
        confidence=0.95,
        examples=2,
    ),

    # Alaska Airlines (travel for races/events)
    _RuleDef(
        vendor_pattern=r"alaska.*air",
        is_regex=True,
        entity=Entity.BLACKLINE,
        tax_category=TaxCategory.TRAVEL,
        direction=Direction.EXPENSE,
        tax_subcategory=TaxSubcategory.FLIGHTS,
        confidence=0.85,
        examples=2,
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_vendor_rules(session: Session, *, force: bool = False) -> int:
    """Insert seed VendorRule rows into the database.

    Existing rules with the same ``vendor_pattern`` and ``entity`` combination
    are skipped unless *force* is ``True``.

    Args:
        session: Open SQLAlchemy session. The function commits after inserting.
        force: When ``True``, skip the existence check and always insert.

    Returns:
        Number of rows inserted.
    """
    inserted = 0
    for defn in _SEED_RULES:
        if not force:
            existing = (
                session.query(VendorRule)
                .filter(
                    VendorRule.vendor_pattern == defn.vendor_pattern,
                    VendorRule.entity == defn.entity.value,
                )
                .first()
            )
            if existing:
                logger.debug(
                    "Skipping existing rule: pattern=%r entity=%s",
                    defn.vendor_pattern,
                    defn.entity,
                )
                continue

        rule = VendorRule(
            vendor_pattern=defn.vendor_pattern,
            is_regex=defn.is_regex,
            entity=defn.entity.value,
            tax_category=defn.tax_category.value,
            tax_subcategory=defn.tax_subcategory.value if defn.tax_subcategory else None,
            direction=defn.direction.value,
            deductible_pct=defn.deductible_pct,
            confidence=defn.confidence,
            source=VendorRuleSource.HUMAN.value,
            examples=defn.examples,
        )
        session.add(rule)
        inserted += 1
        logger.info(
            "Seeded vendor rule: pattern=%r entity=%s category=%s",
            defn.vendor_pattern,
            defn.entity,
            defn.tax_category,
        )

    if inserted:
        session.commit()

    return inserted
