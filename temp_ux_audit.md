# UX Audit: Accounting Dashboard

## Product Context
Cash-basis accounting system for solo entrepreneur Travis Sparks managing 3 entities:
- **Sparkry AI LLC** (single-member, Schedule C, monthly B&O)
- **BlackLine MTB LLC** (partnership, Form 1065 + K-1, quarterly B&O)
- **Personal** (1040 Schedule A/D)

**Target User:** Tax-savvy power user, keyboard-first workflow, daily transaction review.
**Tech Stack:** SvelteKit 5 (runes mode) + FastAPI + SQLite. No component library — hand-rolled CSS.

---

## Critical Findings

### P0 — WCAG Failures
1. **Green income color (#16a34a) on white FAILS WCAG AA** — contrast ratio 3.30:1, needs 4.5:1. Every income amount, positive balance, and "confirmed" status pill is inaccessible. **Fix: Use #15803d (green-700) or darker.**
2. **Double-dash negative amounts (--$695.87)** — semantically broken. The `--` prefix is a CSS custom property convention, not a financial one. Users see two dashes instead of a minus sign. **Fix: Use parenthetical notation ($695.87) or single minus with color.**

### P0 — Information Architecture
3. **Tax deadlines duplicated on 3 pages** (Dashboard, Health, Accounts) with inconsistent formatting. Creates confusion about which is authoritative. **Fix: Single canonical location (Dashboard), link from other pages.**
4. **Source health duplicated** on Dashboard and Health page. **Fix: Summary on Dashboard, detail on Health — no duplication.**

### P1 — Typography & Financial Credibility
5. **No tabular numerals (tnum)** — financial figures use proportional sans-serif. Columns of numbers don't align vertically. This is the #1 credibility killer for financial software. **Fix: Use `font-variant-numeric: tabular-nums` on all amount cells, or switch to a monospace font (SF Mono, JetBrains Mono) for figures.**
6. **Amount columns not consistently right-aligned** — some amounts float left within their cells. **Fix: Strict `text-align: right` on all numeric columns.**
7. **No thousands separators on large amounts** in some views.

### P1 — Visual Hierarchy & Density
8. **Status pills lack differentiation** — "sent" (amber) and "paid" (green) are visually similar at a glance. **Fix: Add icons or distinct shapes, not just color.**
9. **Entity labels have no visual coding** — Sparkry, BlackLine, Personal are plain gray text badges. **Fix: Assign each entity a distinct accent color and use consistently across all pages.**
10. **Cards lack consistent internal spacing** — some cards have 16px padding, others 24px. Vertical rhythm is broken. **Fix: Standardize on 20px/24px card padding with 12px internal gaps.**

### P1 — Interaction Design
11. **Keyboard shortcuts not discoverable** — extensive shortcuts exist (j/k/y/e/s/r/1/2/3) but only shown on Review page via a modal. No tooltip hints on buttons. **Fix: Show shortcut badges on action buttons (e.g., "Confirm [Y]"), add global `?` help overlay.**
12. **Register table lacks sticky headers** — scrolling 207 rows loses column context. **Fix: `position: sticky; top: 0` on thead.**
13. **No loading skeletons** — pages show "Loading..." text instead of shimmer placeholders. Feels broken. **Fix: Implement skeleton screens matching content layout.**
14. **Empty states are bare** — Review "All caught up" has a gray checkmark and a button. No context about what to do next. **Fix: Rich empty states with guidance, related actions, and positive reinforcement.**

### P2 — Navigation & Structure
15. **8 nav items** is at the upper limit of Miller's 7±2 cognitive load. **Fix: Group related items — e.g., "Money" (Register, Review, Reconciliation) and "Compliance" (Tax, Health, Accounts). Or use a secondary nav tier.**
16. **No breadcrumbs** — deep pages (invoice detail, invoice edit) lose context.
17. **No page-level status indicators** — the nav doesn't show which pages have pending actions (review count badge exists but is inconsistent).

### P2 — Data Visualization
18. **Stacked bar chart (Health page)** — relies solely on color for status differentiation. Colorblind users cannot distinguish segments. **Fix: Add patterns or direct labels.**
19. **B&O monthly revenue table** — 12 rows of mostly empty dashes ("—"). Wastes vertical space. **Fix: Only show months with data, or use a sparkline.**
20. **Dashboard "Recent Activity"** — plain text list with no visual scanning aids. All rows look identical. **Fix: Add category icons, amount formatting hierarchy, entity color coding.**

### P2 — Polish & Consistency
21. **No dark mode** — power users working late benefit from reduced eye strain.
22. **Glassmorphism nav** doesn't match the flat card aesthetic elsewhere. Feels disconnected.
23. **"File B&O" button on Dashboard** uses dark filled style while peers are outlined — inconsistent emphasis hierarchy.

---

## Contrast Audit Results

| Pair | Ratio | WCAG AA | Verdict |
|------|-------|---------|---------|
| Red (#dc2626) on white | 4.83:1 | 4.5:1 | PASS |
| Green (#16a34a) on white | 3.30:1 | 4.5:1 | **FAIL** |
| Amber (#d97706) on white | ~3.2:1 | 4.5:1 | **LIKELY FAIL** |
| Gray text (#6b7280) on white | ~4.6:1 | 4.5:1 | Marginal |

---

## Data Visualization Issues
- Stacked bar: color-only encoding, no patterns
- No alt text on chart
- No data table alternative for screen readers
- Direct labels would help (segment percentages are shown in separate legend, not on bars)

---

## Design Directives for Redesign

1. **Tabular numerals everywhere** — `font-variant-numeric: tabular-nums` or monospace font for all financial figures
2. **Right-align all numeric columns** without exception
3. **Fix green to #15803d or darker** for WCAG AA compliance
4. **Fix amber to #b45309 or darker** for WCAG AA
5. **Use parenthetical notation** for negative amounts: ($695.87) in red, not --$695.87
6. **Entity color coding** — assign Sparkry=blue, BlackLine=purple, Personal=slate and use everywhere
7. **Sticky table headers** on all data tables
8. **Loading skeletons** instead of "Loading..." text
9. **Keyboard shortcut badges** on action buttons
10. **Consolidate tax deadlines** to one canonical location
11. **Professional aesthetic** — high-trust, high-density, clean lines, consistent spacing
12. **Dark mode ready** — use CSS custom properties for all colors
