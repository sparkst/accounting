# Proposal: Per-Product COGS Tracking for BlackLine MTB

**Author:** Quark (CFO/Accounting Systems)
**To:** Picard (CEO)
**Date:** 2026-03-26
**Status:** Draft — awaiting scoring

---

## Problem

BlackLine MTB currently tracks total COGS as a single line item on the P&L ($X in manufacturing + raw materials + shipping). As Travis launches products through Amazon FBA, we need margin-by-product to answer: "Which products are profitable and which are losing money?"

Today we know: total revenue ($43 from Shopify this year) and total COGS. We don't know: revenue per SKU, cost per SKU, or margin per SKU.

## Proposed Solution

Add a `Product` model and per-product cost allocation to the accounting system.

### Data Model

```
Product (NEW)
  id: UUID
  sku: string (unique)
  name: string
  entity: blackline
  unit_cost: Decimal (landed cost per unit)
  category: enum (apparel, parts, accessories, custom)
  active: boolean
  created_at, updated_at

ProductCostEntry (NEW)
  id: UUID
  product_id: FK → Product
  cost_type: enum (raw_material, manufacturing, shipping_inbound, packaging, fba_fee, platform_fee)
  amount: Decimal
  date: date
  notes: string
  source_transaction_id: FK → Transaction (optional, links to expense transaction)
```

### How It Works

1. **Product catalog** — CRUD for products with SKU, name, unit cost breakdown
2. **Cost allocation** — When a COGS transaction is confirmed, optionally allocate it to a product (or split across products)
3. **Revenue matching** — Shopify adapter already captures order line items. Map order items to products by SKU.
4. **Margin report** — New `/profitability` page (or section on Financials): Product | Units Sold | Revenue | COGS | Gross Margin | Margin %
5. **Amazon FBA integration** — When FBA settlement reports start flowing (via bank CSV), map FBA fees to products by ASIN→SKU lookup

### What Changes

| Area | Change | Effort |
|------|--------|--------|
| Backend: Models | New Product + ProductCostEntry models | S |
| Backend: API | CRUD endpoints for products + cost entries | M |
| Backend: Shopify adapter | Map order line items to products by SKU | M |
| Backend: Alembic | Migration for new tables | S |
| Dashboard: Product catalog page | New `/products` page with CRUD | M |
| Dashboard: Profitability report | Per-product margin table + visualization | M |
| Dashboard: Transaction allocation | "Allocate to product" on TransactionCard for COGS items | M |
| Dashboard: Nav | Add Products under Money group, Profitability under Money | S |

### What Doesn't Change

- Existing P&L, Tax, and export functionality — unaffected
- Total COGS line on Financials — still works (sum of all product costs)
- Existing Shopify adapter — enhanced, not replaced

## Scope Estimate

**8-10 tasks, 1 sprint (estimated 2-3 days with QRALPH)**

Sprint breakdown:
1. Product + ProductCostEntry models + Alembic migration (S)
2. Product CRUD API (M)
3. ProductCostEntry API (M)
4. Product catalog dashboard page (M)
5. Shopify adapter SKU matching (M)
6. Cost allocation on TransactionCard (M)
7. Per-product profitability report (M)
8. Nav updates + integration testing (S)

## Dependencies

- BlackLine product catalog (Travis needs to define SKUs and base costs)
- Shopify order data (already flowing via adapter)
- Amazon FBA settlement reports (future — bank CSV import when available)

## Risk

| Risk | Mitigation |
|------|------------|
| SKU mismatch between Shopify and product catalog | Fuzzy matching + manual override |
| FBA fee structure changes | Cost types are extensible (enum) |
| Retroactive allocation for existing transactions | Bulk allocation tool, not automated |

## Success Criteria

- Travis can see margin per product on the Profitability page
- When a new Shopify order comes in, revenue is auto-attributed to the correct product
- When a COGS expense is confirmed, it can be allocated to one or more products
- Monthly close includes a per-product margin check

## Recommendation

Start after Amazon FBA setup begins (when product data is available). The model and API can be built now; the reporting and allocation features are most valuable once real product data flows.

---

*Ready for scoring. — Quark*
