# Accounting Dashboard Design System

## Brand Identity
- **Product:** Cash-basis accounting for multi-entity entrepreneur
- **Aesthetic:** High-trust financial, Apple-inspired minimalism, enterprise data density
- **Typography:** Inter for UI, JetBrains Mono / SF Mono for figures
- **Mood:** Calm authority, clean precision, professional confidence

## Color Tokens

### Entity Colors (ALWAYS used for entity identification)
| Entity | Color Name | Hex | Usage |
|--------|-----------|-----|-------|
| Sparkry AI LLC | Blue | `#1e40af` (bg: `#eff6ff`) | Badges, borders, row highlights |
| BlackLine MTB LLC | Purple | `#7c3aed` (bg: `#f5f3ff`) | Badges, borders, row highlights |
| Personal | Slate | `#475569` (bg: `#f8fafc`) | Badges, borders, row highlights |

### Semantic Colors (WCAG AA compliant on white)
| Role | Light Mode | Dark Mode | Ratio |
|------|-----------|-----------|-------|
| Income/Positive | `#15803d` (green-700) | `#4ade80` | 4.8:1 |
| Expense/Negative | `#b91c1c` (red-700) | `#fca5a5` | 5.7:1 |
| Warning | `#b45309` (amber-700) | `#fbbf24` | 4.6:1 |
| Info | `#1d4ed8` (blue-700) | `#93c5fd` | 5.1:1 |
| Muted text | `#4b5563` (gray-600) | `#9ca3af` | 5.9:1 |

### Surface Colors
| Role | Light | Dark |
|------|-------|------|
| Page background | `#f8fafc` | `#0f172a` |
| Card surface | `#ffffff` | `#1e293b` |
| Card border | `#e2e8f0` | `#334155` |
| Table header bg | `#f1f5f9` | `#1e293b` |
| Table row hover | `#f8fafc` | `#334155` |
| Table row selected | `#eff6ff` | `#1e3a5f` |

## Typography Scale

| Element | Font | Size | Weight | Features |
|---------|------|------|--------|----------|
| Page title | Inter | 24px / 1.5rem | 600 | — |
| Section heading | Inter | 18px / 1.125rem | 600 | letter-spacing: -0.01em |
| Body text | Inter | 14px / 0.875rem | 400 | — |
| Table header | Inter | 11px / 0.6875rem | 600 | text-transform: uppercase; letter-spacing: 0.05em |
| Amount (table) | JetBrains Mono | 13px / 0.8125rem | 500 | font-variant-numeric: tabular-nums |
| Amount (card hero) | JetBrains Mono | 28px / 1.75rem | 600 | font-variant-numeric: tabular-nums |
| Badge/pill | Inter | 11px / 0.6875rem | 500 | — |
| Kbd shortcut | JetBrains Mono | 11px | 400 | border: 1px solid; border-radius: 3px; padding: 1px 4px |

## Amount Formatting Rules
1. **Positive amounts:** `$1,234.56` in green-700 (`#15803d`)
2. **Negative amounts:** `($1,234.56)` in red-700 (`#b91c1c`) — parenthetical notation, NO minus sign
3. **Zero/neutral:** `$0.00` in gray-600 (`#4b5563`)
4. **All amounts:** right-aligned, `font-variant-numeric: tabular-nums`, monospace font
5. **Thousands separators:** always present

## Spacing System
- Card padding: 20px
- Card gap (between cards): 16px
- Section gap (between sections): 32px
- Table cell padding: 10px 12px
- Nav height: 52px
- Page max-width: 1280px
- Page horizontal padding: 24px

## Component Patterns

### Status Pills
| Status | Background | Text | Icon |
|--------|-----------|------|------|
| confirmed | `#f0fdf4` | `#15803d` | checkmark |
| auto_classified | `#f1f5f9` | `#475569` | sparkles |
| needs_review | `#fef3c7` | `#b45309` | clock |
| rejected | `#fef2f2` | `#b91c1c` | x-circle |
| sent | `#eff6ff` | `#1d4ed8` | arrow-up-right |
| paid | `#f0fdf4` | `#15803d` | check-circle |

### Entity Badges
Rounded pill, entity color border + light bg:
- Sparkry: `border: 1px solid #93c5fd; background: #eff6ff; color: #1e40af`
- BlackLine: `border: 1px solid #c4b5fd; background: #f5f3ff; color: #7c3aed`
- Personal: `border: 1px solid #cbd5e1; background: #f8fafc; color: #475569`

### Cards
```css
.card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 20px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
```

### Tables
- Sticky header: `position: sticky; top: 52px; z-index: 10`
- Header row: uppercase, 11px, gray-500, bottom border
- Zebra striping: none (hover highlight instead)
- Row hover: `background: var(--color-row-hover)`
- Numeric columns: right-aligned, monospace

### Keyboard Shortcut Badges
Inline `<kbd>` elements on action buttons:
```css
kbd {
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 1px 5px;
  border: 1px solid var(--color-border);
  border-radius: 3px;
  background: var(--color-bg);
  color: var(--color-muted);
  margin-left: 6px;
}
```

## Navigation
- Horizontal top bar, 52px height, sticky
- Clean white background with subtle bottom border (no glassmorphism)
- 8 items with badge counts for actionable pages
- Active page: filled pill background
- Entity color bar at very top (2px) showing active entity context
