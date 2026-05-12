---
name: AWS Cost Optimization Scanner Report
description: Audit-grade visual system for the HTML cost-optimization report. The Audit Trail.
colors:
  primary: "#1976d2"
  primary-deep: "#0d47a1"
  primary-light: "#42a5f5"
  secondary: "#ff9800"
  secondary-deep: "#f57c00"
  success: "#4caf50"
  warning: "#ff9800"
  danger: "#f44336"
  info: "#2196f3"
  surface: "#ffffff"
  background: "#f5f5f5"
  text-primary: "#212121"
  text-secondary: "#757575"
  hairline: "#e0e0e0"
  primary-dark-mode: "#42a5f5"
  surface-dark-mode: "#1e1e1e"
  background-dark-mode: "#121212"
  text-primary-dark-mode: "#ffffff"
  text-secondary-dark-mode: "#b0b0b0"
  hairline-dark-mode: "#333333"
  badge-success-fg: "#1b5e20"
  badge-success-bg: "#e8f5e9"
  badge-warning-fg: "#e65100"
  badge-warning-bg: "#fff3e0"
  badge-danger-fg: "#b71c1c"
  badge-danger-bg: "#ffebee"
  badge-info-fg: "#01579b"
  badge-info-bg: "#e1f5fe"
typography:
  display:
    fontFamily: "Roboto, 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif"
    fontSize: "clamp(1.75rem, 4vw, 2.75rem)"
    fontWeight: 400
    lineHeight: 1.1
    letterSpacing: "-0.5px"
  headline:
    fontFamily: "Roboto, sans-serif"
    fontSize: "2rem"
    fontWeight: 400
    lineHeight: 1.2
    letterSpacing: "-0.5px"
  metric:
    fontFamily: "Roboto, sans-serif"
    fontSize: "2.5rem"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Roboto, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 500
    lineHeight: 1.4
  body:
    fontFamily: "Roboto, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Roboto, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.5px"
  micro-label:
    fontFamily: "Roboto, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.5px"
  mono:
    fontFamily: "'Roboto Mono', 'SF Mono', Consolas, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  sm: "4px"
  md: "8px"
  lg: "16px"
  pill: "50px"
  full: "50%"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  2xl: "48px"
components:
  card-summary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "24px"
  card-summary-hover:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "24px"
  card-rec:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "24px"
  card-rec-high:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "24px"
  card-stat:
    backgroundColor: "{colors.background}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "16px"
  button-tab:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    rounded: "0"
    padding: "16px 24px"
  button-tab-active:
    backgroundColor: "transparent"
    textColor: "{colors.primary}"
    rounded: "0"
    padding: "16px 24px"
  button-pill:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.pill}"
    padding: "8px 16px"
  chip-badge-success:
    backgroundColor: "{colors.badge-success-bg}"
    textColor: "{colors.badge-success-fg}"
    rounded: "{rounded.lg}"
    padding: "4px 12px"
  chip-badge-warning:
    backgroundColor: "{colors.badge-warning-bg}"
    textColor: "{colors.badge-warning-fg}"
    rounded: "{rounded.lg}"
    padding: "4px 12px"
  chip-badge-danger:
    backgroundColor: "{colors.badge-danger-bg}"
    textColor: "{colors.badge-danger-fg}"
    rounded: "{rounded.lg}"
    padding: "4px 12px"
  chip-badge-info:
    backgroundColor: "{colors.badge-info-bg}"
    textColor: "{colors.badge-info-fg}"
    rounded: "{rounded.lg}"
    padding: "4px 12px"
  table-rec:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "16px"
---

# Design System: AWS Cost Optimization Scanner Report

## 1. Overview

**Creative North Star: "The Audit Trail"**

The report is an evidence chain in document form. Every number on the page should be traceable back to a CloudWatch metric, a Pricing API call, or a Cost Hub recommendation; every visual element on the page should support that chain rather than ornament it. The reader is a senior practitioner — a FinOps engineer, a cloud architect defending a proposal, an SRE auditing a recommendation before acting — who needs to verify the report's claims as quickly as they accept them. The system serves that act of verification.

This DESIGN.md documents the live visual baseline in `html_report_generator.py` as it ships today: a Material Design v1 vocabulary (Roboto, blue/orange/red semantics, five-step elevation shadow scale, gradient hero header, left-stripe priority cards). That baseline predates `PRODUCT.md` and is being navigated away from. The Named Rules and Do's and Don'ts below carry the strategic direction forward — restrained palette, hairline-led depth, color used only as a status channel — so any future variant generated against this spec drifts toward the audit-grade target rather than reinforcing the SaaS-dashboard baseline.

The system explicitly rejects three lanes: cluttered analytics dashboards (Datadog at its worst), reflexive AI-startup styling (purple gradients, glassmorphism, gradient text), and enterprise PowerPoint heaviness. It also rejects its own current Material-cliché surface where that surface contradicts the audit voice.

**Key Characteristics:**

- One document, three reading depths (executive scan, FinOps audit, DevOps drill-down) — hierarchy, not separate templates.
- Numbers are typographically first-class; everything else supports them.
- Status is encoded in label + position + color, never color alone. Print and colorblind reads are equal to color-screen reads.
- Flat by default. Depth comes from hairlines and tonal background steps; shadows are reserved for genuinely floating chrome.
- The Material baseline is documented honestly here; the rules below pull it toward the target.

## 2. Colors

A functional palette: every color encodes status or hierarchy. There is no decorative color. The current implementation uses Material 700-family hues; the audit-grade direction asks future variants to reduce saturation, tint neutrals toward the primary hue, and confine accents to the status channels.

### Primary

- **Ledger Blue** (`#1976d2` / Material Blue 700): the system's one accent. Used for active tab underline, table-header focus state, recommendation-card stripe at default priority, code spans inside tables, and the executive-summary card-hover stripe. Never used as a section background or large flat fill except in the legacy gradient header. Dark-mode variant is `#42a5f5` (Material Blue 400) for adequate contrast on `#121212`.
- **Ledger Blue Deep** (`#0d47a1`): the heavy end of the gradient hero header and the hover target for `.show-more-link`. The Don'ts below prohibit using it for new gradients.
- **Ledger Blue Light** (`#42a5f5`): hover and focus echoes only.

### Secondary

- **Audit Amber** (`#ff9800` / Material Orange 500): doubles as warning. Used for medium-priority recommendation stripe, summary banners on partial-confidence findings, and the `rec-summary` callout. Used sparingly; not a decorative accent.

### Status

Functional, not decorative. These tokens must always be paired with a label or icon — never communicate status by color alone.

- **Evidence Green** (`#4caf50` / Material Green 500): savings figures, low-priority recommendation stripe, success callouts. Always paired with a `$` figure or an explicit `Savings` label.
- **Breach Red** (`#f44336` / Material Red 500): high-priority recommendation stripe, danger badge foreground accents. Always paired with a `HIGH` label or explicit severity text.
- **Signal Blue** (`#2196f3` / Material Blue 500): informational callouts, pricing notes, neutral guidance. Distinct from primary Ledger Blue by saturation; never used in the same element.

### Neutral

- **Paper White** (`#ffffff`): card surfaces, table backgrounds, footer.
- **Vellum** (`#f5f5f5`): page background, stat-card resting fill, code-span and table-header fill. Subtly warmer than `#fff`, gives the page a stationery feel rather than a screen-glow feel.
- **Ink** (`#212121`): body and headline text. Not pure black — pure black would feel screen-harsh on Vellum.
- **Ink Muted** (`#757575`): secondary labels, table-header text, footnotes.
- **Hairline** (`#e0e0e0`): all 1px dividers, card borders, table row-separators. The system's primary depth tool, alongside background tonality.

### Dark Mode

Inverse pair, not a mood. Same semantics, recalibrated for `#121212` page background and `#1e1e1e` surfaces. Primary shifts up the Material ramp (`#42a5f5`) for AA contrast. Status colors shift one shade lighter (`#66bb6a`, `#ef5350`, `#ffb74d`) to maintain readable contrast on dark surfaces. Badges keep colored type on tinted-transparent backgrounds (e.g. `rgba(76,175,80,0.2)`).

### Badge Tonal Pairs

Soft chips for inline labels, not the heavy semantic colors. Each is a foreground/background pair derived from Material's 50/900 tonal steps:

- Success: `#1b5e20` on `#e8f5e9`
- Warning: `#e65100` on `#fff3e0`
- Danger: `#b71c1c` on `#ffebee`
- Info: `#01579b` on `#e1f5fe`

### Named Rules

**The Status-Channel Rule.** Color in this system is reserved for status (severity, savings, anomaly, info). Color is forbidden as section dressing, decorative accent, or category-coding. If a colored element does not encode status, recolor it Ink or remove the color.

**The Three-Channel Rule.** Every status signal must be expressed through at least three of: typographic weight, an explicit label word (`HIGH`, `Savings`, `Recommended`), a position cue (left-stripe, badge corner, priority sort order), and color. Color alone is forbidden. This survives colorblind reads and black-and-white prints.

**The No-Decorative-Gradient Rule.** Gradients are forbidden everywhere except the legacy hero header, which is on the redesign list. No gradient text, no gradient buttons, no gradient backgrounds on cards.

## 3. Typography

**Display & Body:** Roboto (300 / 400 / 500 / 700), with fallbacks to Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif. Loaded via Google Fonts CDN.
**Mono:** Roboto Mono (400 / 500), with fallbacks to SF Mono, Consolas, monospace. Used for AWS resource identifiers, CloudWatch metric names, and code spans inside tables.

**Character:** Roboto is the most-saturated UI typeface there is. It is recognizably Material and recognizably 2014. Future variants may swap it (Inter, IBM Plex Sans, or a neutral grotesque) without changing the hierarchy below. The hierarchy is the doctrine; the family is replaceable.

### Hierarchy

- **Display** (400, `clamp(1.75rem, 4vw, 2.75rem)`, line-height 1.1, letter-spacing -0.5px): hero report title at the top of the document. Only one Display element per page.
- **Headline** (400, 2rem, line-height 1.2, letter-spacing -0.5px): service-section titles ("S3 Cost Optimization", "EC2 Cost Optimization"). One per service tab.
- **Metric** (400, 2.5rem, line-height 1.0, letter-spacing -0.01em): the executive-summary numerics — total savings, recommendation count, services scanned. Weight 400, not 700: the number sells itself; bolder weight would feel SaaS-marketing.
- **Stat-Card Metric** (400, 1.75rem, line-height 1.2): per-service rolled-up figures.
- **Title** (500, 1.125–1.25rem, line-height 1.4): section titles inside tabs, recommendation card titles (`<h5>` inside `.rec-item`).
- **Body** (400, 1rem, line-height 1.6): recommendation paragraphs, page prose. Max line length 65–75ch where the parent layout doesn't already cap it.
- **Label** (500, 0.875rem, letter-spacing 0.5px, uppercase): card subtitles ("TOTAL RECOMMENDATIONS"), tab-button text, table column headers. Uppercase + tracked, never sentence case.
- **Micro Label** (600, 0.75rem, letter-spacing 0.5px, uppercase): badge text, tab-count chips.
- **Mono** (400–500, 0.875rem, line-height 1.5): resource IDs, ARNs, metric names, `<code>` inside table cells.

### Named Rules

**The Numbers-First Rule.** Numeric figures use weight 400, never 700. Bold numerics in a financial document read as advertising; weight 400 at the assigned size reads as a stated fact. The exception is `.rec-item .savings` (700), which is a callout-within-prose, not a headline number.

**The Tabular Figures Rule.** Any element rendering aligned numerics (tables, decimal columns, the executive summary grid) must specify `font-variant-numeric: tabular-nums` so digits don't shift width between rows. Roboto supports it; replacements must too.

**The Uppercase-Means-Label Rule.** Uppercase text is reserved for labels, badges, and tab buttons. Body prose and headlines stay in their natural case. Uppercase headings are forbidden.

## 4. Elevation

**Flat by default, hairline-led depth.** The system communicates hierarchy through 1px Hairline borders, tonal steps between Paper White (surfaces) and Vellum (page background), and typographic scale — not through stacked shadows. Shadows are reserved for genuinely floating chrome that overlaps page content (theme-toggle pill, export-btn pill, back-to-top FAB).

The current implementation ships a Material 5-step elevation scale (`--shadow-1` through `--shadow-5`) and applies it broadly: every summary card, tab container, recommendation card, table, info-box, and footer carries a shadow at rest. The Named Rules below pull the system toward the flat baseline; new components should opt in to shadows only with explicit reason.

### Shadow Vocabulary (legacy, in transition)

- **Shadow-1** (`0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24)`): currently on `.rec-item`, `.recommendations-table`, `.opportunity:hover`. Target state: removed from `.rec-item` and `.recommendations-table`, replaced by a 1px Hairline border.
- **Shadow-2** (`0 3px 6px rgba(0,0,0,0.16), 0 3px 6px rgba(0,0,0,0.23)`): currently on `.summary-card`, `.tabs`, `.top-buckets-table`, floating pill buttons. Target: kept on floating pill buttons only; flat for in-flow cards.
- **Shadow-3** through **Shadow-5**: hover-elevation theatre. Target state: hover does not change elevation; hover changes border or background tone instead.

### Named Rules

**The Flat-By-Default Rule.** Surfaces are flat at rest unless they actually float over scrolling content. A card sitting in document flow is not floating and does not earn a shadow. Hairline + tonal step is the depth tool.

**The No-Hover-Elevation Rule.** Hover is not a moment to inflate elevation. The current `transform: translateY(-4px)` + `box-shadow: var(--shadow-4)` hover on summary cards is the SaaS-dashboard tic the redesign moves away from. New components: hover changes border color, background tonality, or text weight only.

**The Floating-Chrome Exception.** Shadows are permitted on elements that genuinely overlap content during scroll: the theme-toggle pill, the export pill, the back-to-top FAB, and modal scrims. Maximum Shadow-3.

## 5. Components

### Header

- **Layout:** full-width banner across the top of the report, 1440px container width.
- **Treatment (current):** linear gradient from `Ledger Blue Deep` (`#0d47a1`) to `Ledger Blue` (`#1976d2`) at 135°, with a radial-gradient white halo top-right. White text, info chips with `backdrop-filter: blur(10px)` over a 15% white tint.
- **Treatment (target):** flat surface, restrained type, no gradient, no halo, no glass chips. The gradient header is the most visible Material reflex on the page and the first thing a redesign should remove.
- **Type:** Display title, Label info-chip headers, body text in 0.875rem.

### Summary Cards (Executive Metrics)

- **Layout:** grid, `repeat(auto-fit, minmax(280px, 1fr))`, gap 16px.
- **Resting state (target):** Paper White surface, 1px Hairline border, 24px padding, no shadow. `border-radius: 8px`. Metric uses the Metric type role (400, 2.5rem); subtitle uses Label.
- **Resting state (current):** same but with Shadow-2 and an animated left-stripe accent on hover.
- **Hover:** Hairline shifts to `Ledger Blue` and the metric label gains weight to 500. No translateY, no shadow change.

### Stat Cards (Per-Service Rollups)

- **Layout:** grid inside the service header, `repeat(auto-fit, minmax(140px, 1fr))`.
- **Treatment:** Vellum background, Hairline border, 16px padding, 8px radius. Smaller and quieter than the Summary card — it's a rollup, not a headline.
- **Savings stat variant:** `rgba(76,175,80,0.1)` background with `rgba(76,175,80,0.3)` border; the metric uses Evidence Green. Earns its color because it explicitly encodes a savings status.

### Tabs

- **Layout:** horizontal scroll, sticky-feel fade on right edge.
- **Treatment:** Paper White surface, 1px Hairline bottom border, 8px outer radius. Tab buttons are Label-cased, 16px × 24px padding, 160px minimum width.
- **States:** rest in Ink Muted; hover gets a `rgba(128,128,128,0.08)` fill; active gets Ledger Blue text + a 2px Ledger Blue underline that scales in from 0 (the one piece of motion theatre worth keeping — it's diegetic, not decorative).
- **Tab count chip:** Micro Label, 18px pill, Vellum fill in light mode and `rgba(255,255,255,0.12)` in dark.

### Recommendation Cards (`.rec-item`)

- **Layout:** stacked list, 16px gap between cards. Full-width within the tab content.
- **Treatment (current — banned pattern):** Paper White surface, 24px padding, 8px radius, Shadow-1 at rest, **4px colored left-stripe** keyed by priority: Breach Red (high), Audit Amber (medium), Evidence Green (low). Hover: Shadow-3 + `translateX(4px)`.
- **Treatment (target):** Paper White surface, 1px Hairline border (full, not stripe), 24px padding, 8px radius, no shadow. Priority encoded by a leading badge (`HIGH PRIORITY`, `MEDIUM PRIORITY`, `LOW PRIORITY`) in the card's top-left and by sort order — not by a colored side-stripe. Hover: border color shifts to the priority hue.
- **Body:** Title (500, 1.125rem) for the recommendation heading, Body prose, an inline `.savings` callout (700, 1.125rem, Evidence Green on `rgba(76,175,80,0.1)`).

### Badges / Chips

- **Style:** pill (16px / `rounded.lg`), 4px × 12px padding, Micro Label uppercase. Inline-flex, tabular figures inside.
- **Variants:** success, warning, danger, info — each with its foreground/background tonal pair. Dark-mode variants drop the solid background tints in favor of `rgba(color, 0.2)` fills, keeping the foreground color recognizable.

### Tables (`.recommendations-table`, `.rec-table`)

- **Style:** Paper White surface, 8px outer radius, 1px Hairline row-separators. Header row uses Vellum fill, Ink Muted Label text, uppercase + tracked.
- **Cell padding:** 16px on `.recommendations-table`, 8–10px on the denser `.rec-table` used inside recommendation cards.
- **Hover:** row gets a `rgba(0,0,0,0.04)` tint in light mode, `rgba(255,255,255,0.06)` in dark. No shadow change, no transform.
- **Code spans:** Vellum fill, Ledger Blue text, Mono font, 0.875rem, 4px radius.

### Info / Warning / Success Boxes

- **Current treatment (banned pattern):** semantic-tinted background (`rgba(76,175,80,0.1)` for success, etc.) with a **4px colored left-stripe**.
- **Target treatment:** same tinted background, **1px full Hairline border in the semantic color**. No stripe. Leading icon (info / alert / check) in the semantic color carries the status signal; the box itself stays neutral in shape.
- **Padding:** 16px. Radius 8px.

### Floating Chrome

- **Theme toggle pill** (top-right): Paper White surface, 1px Hairline border, 50px radius, Shadow-2. 8px × 16px padding.
- **Export pill** (top-right, sibling of theme toggle): same treatment, smaller icon.
- **Back-to-top FAB** (bottom-right): 44px × 44px circle, Ledger Blue fill, white icon, Shadow-3. Appears at scroll > 300px, fades on hover to Shadow-4.

### Print Output

A `@media print` stylesheet ships in `html_report_generator.py`:

- Body font swaps to Georgia / Times serif. This is intentional — print reads more like a formal disclosure document than a screen UI.
- Background goes white; theme-toggle, export-btn, back-to-top, tab-buttons, and header decorative halo are hidden.
- All tab content reveals (`display: block !important`), with `page-break-inside: avoid` on `.rec-item`, `.recommendations-table`, and `canvas`.
- `prefers-reduced-motion: reduce` cancels all transitions and animations.

This is the only place in the system where the Roboto-to-Georgia swap is permitted. Do not introduce a serif anywhere else.

## 6. Do's and Don'ts

### Do:

- **Do** treat color as a status channel. If a colored element does not encode status (severity, savings, anomaly, info), recolor it Ink or remove it. The Status-Channel Rule.
- **Do** express every status signal through at least three of: typographic weight, an explicit label word, a position cue, and color. Never color alone. The Three-Channel Rule.
- **Do** keep surfaces flat at rest. Use 1px Hairline and tonal background steps for depth. The Flat-By-Default Rule.
- **Do** show provenance on every recommendation: CloudWatch metric reference, Pricing API source, Cost Hub trace. The audit chain is the product.
- **Do** apply `font-variant-numeric: tabular-nums` on every element rendering aligned figures (tables, the executive summary, decimal columns).
- **Do** test every status color in deuteranopia and protanopia simulation before introducing it. The Three-Channel Rule must survive both.
- **Do** verify print output and screen output are equivalent in information density before shipping a new section. The print stylesheet is a first-class output, not a fallback.
- **Do** keep one Display element per page and one Headline per service tab. Hierarchy collapses when these multiply.

### Don't:

- **Don't** use `border-left` (or `border-right`) greater than 1px as a colored stripe on cards, recommendation items, info boxes, or callouts. This is the absolute-banned pattern. The current `.rec-item.high-priority { border-left-color: var(--danger) }` treatment is on the redesign list — do not propagate it to new components. Use full Hairline borders, leading badges, or sort order instead.
- **Don't** introduce gradient text (`background-clip: text`). The current report has no gradient text; keep it that way. The No-Decorative-Gradient Rule.
- **Don't** add new gradient backgrounds. The legacy hero header is grandfathered and on the redesign list. New gradients are forbidden everywhere — cards, buttons, sections, charts, badges.
- **Don't** add glassmorphism (`backdrop-filter: blur(...)`) to new components. The legacy `header-info-item` chips use it; do not extend the pattern.
- **Don't** use bold weight (700) on metric figures. Numbers in this report use weight 400. Bold numerics read as advertising. The Numbers-First Rule.
- **Don't** rely on color alone to communicate severity, savings, success, or anomaly. The colorblind read and the black-and-white print must be equivalent to the color screen read.
- **Don't** look like a cluttered analytics dashboard. Every badge needs a reason. Every status color needs a label. The stated primary anti-reference.
- **Don't** look like a generic Material Design SaaS dashboard. The current report has a Material baseline that is being navigated away from — gradient header, equal-weight summary card grid, left-stripe priority cards, Material 5-step elevation scale. The redesign target is the Audit Trail, not Material 2014.
- **Don't** look like AI-tool slop: purple/pink gradients, glassmorphism cards, neon-on-dark, hero-metric templates with gradient text. Reflexive 2025 AI-startup styling is explicitly out of scope.
- **Don't** look like corporate enterprise PowerPoint: stock photos, navy + light-blue + orange palette, "Executive Dashboard 2024" template energy.
- **Don't** inflate hover states with elevation theatre (translateY + shadow upgrade). Hover changes border or background tone only. The No-Hover-Elevation Rule.
- **Don't** introduce a serif anywhere except the existing print stylesheet. The serif swap is a deliberate, scoped move; replicating it on screen breaks the system.
- **Don't** use uppercase headlines or section titles. Uppercase is reserved for labels, badges, and tab buttons. The Uppercase-Means-Label Rule.
- **Don't** add a card grid of identical-looking cards (icon + heading + paragraph repeated four times). The executive summary is the system's only repeating card grid and it earns it through differentiated metrics.
- **Don't** use the hero-metric template (big gradient number + small label + supporting stats + accent). The executive summary already implements the restrained version; do not push it toward marketing-page energy.
