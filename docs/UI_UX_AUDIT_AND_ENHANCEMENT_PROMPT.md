# UI/UX Audit & Enhancement Prompt — AWS Cost Optimization Report

> **Purpose**: Complete reference for current UI state, audit findings, fixes applied, and enhancement roadmap. Use as a prompt when asking an AI agent to continue UI/UX work on this report.

---

## Table of Contents

1. [Project Context](#1-project-context)
2. [Architecture Overview](#2-architecture-overview)
3. [Current UI State (Post-Fix)](#3-current-ui-state-post-fix)
4. [Audit Findings — Complete Catalog](#4-audit-findings--complete-catalog)
5. [Fixes Applied (19/19 High+Medium)](#5-fixes-applied-1919-highmedium)
6. [Remaining Enhancement Roadmap (26 Low)](#6-remaining-enhancement-roadmap-26-low)
7. [Modern Dashboard Best Practices Reference](#7-modern-dashboard-best-practices-reference)
8. [Implementation Guidelines](#8-implementation-guidelines)
9. [Verification Checklist](#9-verification-checklist)

---

## 1. Project Context

### What This Is

An AWS cost optimization scanner that generates **self-contained HTML reports** with:
- Executive dashboard with interactive Chart.js charts (pie + bar)
- Per-service tabs (37 AWS services, 260+ checks)
- Dark mode with `localStorage` persistence
- Responsive design (desktop + mobile)
- Zero external file dependencies (all CSS/JS/fonts inline except Chart.js CDN and Google Fonts)

### Key Files

| File | Lines | Role |
|------|-------|------|
| `html_report_generator.py` | ~2794 | Main generator — CSS (L531-1408), JS (L2466-2731), SVG sprite (L507-516), HTML structure |
| `reporter_phase_a.py` | ~454 | Descriptor-driven grouped rendering, `_priority_class()` helper |
| `reporter_phase_b.py` | ~1727 | Per-service handler rendering, `_priority_class()` helper |

### Constraints

- **Python 3.8+** — no walrus operator, no `match/case`, no `str.removeprefix`
- **Self-contained HTML** — all CSS/JS embedded in the file (only Chart.js CDN and Google Fonts are external)
- **No external build tools** — no webpack, no PostCSS, no npm. Pure Python string concatenation
- **Zero write operations** — scanner is read-only
- **Regression gate**: `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py` must pass (71 tests)

### Generated Report Example

- File: `<profile>_<region>.html` (e.g., 1255 lines, ~85KB)
- Run: `python3 cli.py <region> --profile <profile>`

---

## 2. Architecture Overview

### CSS Architecture

```
:root { /* 22 CSS custom properties — colors, shadows */ }
[data-theme="dark"] { /* 22 overrides */ }
[data-theme="dark"] .success { /* savings badge dark override */ }
[data-theme="dark"] .badge-* { /* 4 badge variant overrides */ }

body { /* base font, antialiasing */ }
.container { /* 1440px max-width */ }

Layout Components:
  .header (.header::before pseudo for gradient orb)
  .summary-grid → .summary-card (with hover lift + left-bar animation)
  .tabs → .tab-buttons (flex, overflow-x, scroll-snap) → .tab-button
  .tab-content (fadeIn animation)
  .service-header → .service-stats → .stat-card
  .recommendation-list → .rec-item (priority-colored left border)
  .badge (-warning, -info, -success, -danger)
  .recommendations-table / .top-buckets-table

Animations:
  @keyframes fadeIn { opacity + translateY }
  .summary-card:hover → translateY(-4px) + ::before scaleY(1)
  .rec-item:hover → translateX(4px)
  .tab-button::after → scaleX indicator

SVG Sprite Sheet (L507-516):
  8 symbols: #icon-chart, #icon-lightbulb, #icon-clipboard, #icon-dollar,
             #icon-trending, #icon-alert, #icon-moon, #icon-sun
  Referenced via <svg><use href="#icon-xxx"/></svg>
```

### JavaScript Architecture

```
State:
  currentFilter — active service filter
  pieChart, barChart — Chart.js instances
  chartData — [{service, service_key, savings, recommendations}]

Functions:
  toggleTheme() — flips data-theme, saves to localStorage, re-inits charts
  updateThemeToggle(theme) — swaps moon/sun SVG icon
  initializeTheme() — reads localStorage, applies on load
  showTab(tabId, evt) — tab switching, clears filter on exec summary
  filterByService(serviceKey) — cross-chart-to-tab drill-down
  updateFilterIndicators() — highlights active filter tab
  initializeCharts() — creates/destroys Chart.js pie + bar with dark mode colors

Chart Config:
  Colors: Dynamic HSL generation based on data length
          `hsl((i * 360 / max(chartData.length, 10)) % 360, 65%, 55%)`
  Dark mode: border/text/grid/tooltip colors via ternary on isDark
  Interactions: onClick → filterByService() for drill-down
```

### HTML Structure

```
<html data-theme="light">
  <head>
    <svg style="display:none"> <!-- sprite sheet -->
    <link Google Fonts (Roboto, Roboto Mono)>
    <script Chart.js CDN>
    <style> <!-- all CSS --> </style>
  </head>
  <body>
    <div.container>
      <div.header> Account, Region, Date, Scan Duration, Services </div>
      <div.summary-grid> 4 KPI cards (Services, Savings, Recs, Resources) </div>
      <div.tabs>
        <div.tab-buttons> Executive Summary | EC2 | S3 | RDS | ... </div>
        <div#executive-summary.tab-content> Pie Chart + Bar Chart + Top Buckets </div>
        <div#ec2.tab-content> Stats + Grouped Recommendations </div>
        ...
      </div>
    </div>
    <script> <!-- all JS --> </script>
  </body>
</html>
```

---

## 3. Current UI State (Post-Fix)

### Design Tokens (CSS Custom Properties)

```css
:root {
  --primary: #1976d2;      /* Material Blue 700 */
  --primary-dark: #0d47a1; /* Material Blue 900 */
  --primary-light: #42a5f5;/* Material Blue 400 */
  --secondary: #ff9800;    /* Material Orange */
  --success: #4caf50;      /* Material Green */
  --warning: #ff9800;      /* Material Orange (same as secondary) */
  --danger: #f44336;       /* Material Red */
  --info: #2196f3;         /* Material Blue */
  --surface: #ffffff;
  --background: #f5f5f5;
  --text-primary: #212121;
  --text-secondary: #757575;
  --divider: #e0e0e0;
  --shadow-1 through --shadow-5: Material elevation shadows
}

[data-theme="dark"] {
  --primary: #42a5f5;      /* Lighter blue for dark bg */
  --surface: #1e1e1e;      /* Material dark surface */
  --background: #121212;   /* Material dark bg */
  --text-primary: #ffffff;
  --text-secondary: #b0b0b0;
  /* All shadows intensified for dark mode */
}
```

### Typography

- **Primary**: `'Roboto', 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif`
- **Monospace**: `'Roboto Mono', 'Consolas', monospace` (code blocks)
- **Headings**: H1 (2.75rem/400), Service Title (2rem/400), H5 (1.125rem/500)
- **Body**: `line-height: 1.6`, `-webkit-font-smoothing: antialiased`

### Color Palette for Charts

Dynamic HSL generation — no fixed array. Colors spread evenly across the hue wheel:
```javascript
const awsColors = Array.from({length: Math.max(chartData.length, 10)}, (_, i) =>
    `hsl(${(i * 360 / Math.max(chartData.length, 10)) % 360}, 65%, 55%)`
);
```

### Component Inventory

| Component | CSS Class | Interactive? | Dark Mode? |
|-----------|-----------|-------------|------------|
| Header banner | `.header` | No | Via CSS vars |
| KPI cards | `.summary-card` | Hover lift + left-bar | Via CSS vars |
| Tab bar | `.tab-buttons` | Click, scroll-snap | Via CSS vars |
| Tab buttons | `.tab-button` | Click, active indicator | Via CSS vars |
| Pie chart | `#savingsPieChart` | Click-to-drill-down | JS ternary |
| Bar chart | `#savingsBarChart` | Click-to-drill-down | JS ternary |
| Service stats | `.stat-card` | Hover lift | Via CSS vars |
| Recommendations | `.rec-item` | Hover slide, priority border | Via CSS vars |
| Savings badge | `.savings` | No | Dark override |
| Status badges | `.badge-*` (4 variants) | No | Dark overrides |
| Tables | `.recommendations-table` | No | Via CSS vars |
| Theme toggle | `#theme-toggle-btn` | Click | Full support |

---

## 4. Audit Findings — Complete Catalog

### Severity Distribution
- **Critical**: 0
- **High**: 5 (all fixed)
- **Medium**: 14 (13 fixed, 1 correctly skipped)
- **Low**: 26 (not yet addressed)

### HIGH Severity (5 — All Fixed)

| ID | Finding | Fix Applied |
|----|---------|-------------|
| H1 | Emoji usage in KPI cards and headers (render as □ on some systems) | Replaced with inline SVG sprite sheet (8 `<symbol>` definitions + `<use href>` references) |
| H2 | Pie chart color palette fixed to 10 entries — breaks with >10 services | Dynamic HSL generation: `Array.from({length: Math.max(chartData.length, 10)}, (_, i) => hsl(...))` |
| H3 | Badge colors illegible in dark mode — hardcoded light-mode values | Added 4 `[data-theme="dark"] .badge-*` overrides with semi-transparent backgrounds |
| H4 | Mobile tab scroll has no visual affordance — users can't see more tabs exist | Added `scroll-snap-type: x proximity`, gradient fade overlay via `.tabs::after` |
| H5 | Priority CSS classes (`.high-priority`, `.medium-priority`, `.low-priority`) defined but never applied in HTML | Added `_priority_class()` helper to both reporters, wired at 25 recommendation rendering sites |

### MEDIUM Severity (14 — 13 Fixed, 1 Skipped)

| ID | Finding | Fix Applied |
|----|---------|-------------|
| M1 | `@media (max-width: 768px)` — hard breakpoint misses tablets | Changed to `(max-width: 1024px)` with graduated responsive rules |
| M2 | Summary cards lack `aria-label` or `role` | Added `role="status"` and `aria-label` to summary cards |
| M3 | Tab buttons not keyboard-navigable | Added `role="tablist"` / `role="tab"` + keyboard event handlers |
| M4 | Chart.js canvas has no `aria-label` | Added `aria-label` and `role="img"` to chart canvases |
| M5 | Print CSS missing — wastes ink on dark mode, tabs, charts | Added `@media print` block: force light theme, hide tabs/charts, expand all sections |
| M6 | `.summary-card:hover` duplicated (L697 + L715) | Removed duplicate declaration |
| M7 | No `prefers-reduced-motion` respect | Added `@media (prefers-reduced-motion: reduce)` to disable animations |
| M8 | Chart tooltip font doesn't match report typography | Set `tooltip.font.family` in Chart.js config |
| M9 | Mobile tab buttons too narrow (160px min-width on small screens) | Reduced to `min-width: 120px` below 768px, `min-width: 100px` below 480px |
| M10 | Rec-items have no spacing variation by priority | Priority border width varies (4px high, 3px medium, 2px low) |
| M11 | No focus-visible outlines on interactive elements | Added `:focus-visible` outlines for tab buttons, cards, theme toggle |
| M12 | Theme toggle button lacks `aria-pressed` | Added `aria-pressed` attribute and updates in JS |
| M13 | Header gradient orb uses fixed 500px size — doesn't scale | Changed to `clamp(300px, 40vw, 600px)` for responsive sizing |
| M14 | Chart tooltip colors use JS ternary instead of CSS vars | **SKIPPED** — Canvas-rendered tooltips cannot use CSS vars. Current approach is correct |

### LOW Severity (26 — Not Yet Addressed)

| ID | Finding | Category | Enhancement Suggestion |
|----|---------|----------|----------------------|
| L1 | No skeleton/loading states | Performance | Add CSS shimmer animations for initial load |
| L2 | Tab transitions lack smooth height animation | Animation | Use `max-height` transition or `<details>` pattern |
| L3 | Rec-items have no expand/collapse for long content | UX | Add progressive disclosure with "Show more" |
| L4 | No visual indicator for which chart segment was clicked | Feedback | Add `.active` ring/glow on clicked pie segment |
| L5 | Summary cards lack trend indicators (up/down arrows) | Data Viz | Add SVG trend arrows comparing to previous scan |
| L6 | No "Back to top" button on long reports | Navigation | Floating FAB with scroll-triggered visibility |
| L7 | Header doesn't show scan profile/account name prominently | Information | Add account alias from STS to header |
| L8 | Service stats grid doesn't use container queries | CSS | Replace `minmax(140px, 1fr)` with `@container` queries |
| L9 | Dark mode chart legend dot color doesn't update | Theme | Ensure legend `usePointStyle` picks up theme colors |
| L10 | No export/share button for report | Feature | Add "Export PDF" button (via print) or "Share Link" |
| L11 | Table rows lack hover highlight | UX | Add subtle `tr:hover` background |
| L12 | No visual grouping of related recommendations | Organization | Add category headers/banners between groups |
| L13 | Savings amounts lack currency formatting consistency | Formatting | Standardize to `$XX,XXX.XX` with `Intl.NumberFormat` |
| L14 | Service tab count badges missing | Navigation | Add rec count badge on each tab button |
| L15 | No breadcrumb or "You are here" indicator | Navigation | Show active service name above recommendations |
| L16 | Mobile: charts overflow on narrow viewports | Responsive | Add `overflow: hidden` + `aspect-ratio` on chart containers |
| L17 | No color-blind safe palette option | Accessibility | Add toggle for colorblind-safe chart palette |
| L18 | Stat cards have no comparison context (e.g., "vs last scan") | Data Viz | Add delta indicators when historical data available |
| L19 | Print layout doesn't paginate between services | Print | Add `page-break-before: always` on service sections |
| L20 | No skip-to-content link for keyboard users | Accessibility | Add hidden skip link at top of page |
| L21 | Font loading has no `font-display: swap` | Performance | Add `&display=swap` to Google Fonts URL |
| L22 | No error state if chart rendering fails | Reliability | Add try/catch in `initializeCharts()` with fallback message |
| L23 | Rec-item savings amount not visually prominent enough | Visual Hierarchy | Make savings larger font, add dollar sign icon |
| L24 | Tab content area has no min-height — layout shifts | Layout | Add `min-height: 400px` to `.tab-content` |
| L25 | No loading indicator for Chart.js CDN failure | Reliability | Add `<noscript>` + fallback text for chart containers |
| L26 | Header subtitle says "Cost Optimization Report" — could show date range | Information | Show scan date range if available in data |

---

## 5. Fixes Applied (19/19 High+Medium)

### Files Modified

| File | Backup Location | Changes |
|------|----------------|---------|
| `html_report_generator.py` | `backup_20260504_103334/html_report_generator.py` | SVG sprite, CSS additions (dark mode badges, print media, reduced motion, focus-visible, responsive breakpoints, priority classes, badge overrides), JS additions (keyboard nav, ARIA, chart font) |
| `reporter_phase_a.py` | `backup_20260504_103334/reporter_phase_a.py` | Added `_priority_class()` helper (L14-23), wired at 4 rec-item sites |
| `reporter_phase_b.py` | `backup_20260504_103334/reporter_phase_b.py` | Added `_priority_class()` helper (L12-21), wired at 21 rec-item sites |

### Verification

```
✅ All 3 files import cleanly (python3 -c "import ...")
✅ 71/71 regression + reporter snapshot tests pass
✅ 27 matches of `_priority_class` confirmed across reporters
✅ SVG sprite sheet renders correctly in generated HTML
✅ Dynamic chart colors generate for any service count
✅ Dark mode badge colors verified in [data-theme="dark"] block
```

---

## 6. Remaining Enhancement Roadmap (26 Low)

### Priority Groupings for Implementation

**Quick Wins** (single CSS/JS line changes — do first):
- L6: Back-to-top FAB button
- L10: "Export PDF" button (just calls `window.print()`)
- L11: Table row hover highlight
- L13: Currency formatting with `Intl.NumberFormat`
- L21: Add `&display=swap` to Google Fonts URL
- L22: try/catch in `initializeCharts()`
- L24: `min-height: 400px` on `.tab-content`
- L25: `<noscript>` fallback for chart containers

**Accessibility Enhancements**:
- L17: Color-blind safe chart palette toggle
- L20: Skip-to-content link
- L8: Container queries for service stats grid

**UX Polish**:
- L2: Smooth tab transition height animation
- L3: Expand/collapse for long recommendations
- L4: Visual indicator for clicked chart segment
- L12: Category header banners
- L14: Service tab count badges
- L15: Breadcrumb/active indicator
- L23: More prominent savings amounts

**Data Visualization Upgrades**:
- L5: Trend indicators (arrows) in summary cards
- L18: Comparison context in stat cards
- L9: Dark mode chart legend color fix

**Print & Performance**:
- L1: Skeleton/loading states
- L7: Account name in header
- L16: Mobile chart overflow fix
- L19: Print pagination
- L26: Date range in subtitle

---

## 7. Modern Dashboard Best Practices Reference

> Research from 2024-2025 dashboard design patterns, WCAG 2.2, and enterprise FinOps tools.

### KPI Cards (Summary Cards)

**Current State**: 4 cards in `summary-grid` with hover lift animation and left-bar reveal.

**Best Practices (2024-2025)**:
1. **Sparkline mini-charts** — small inline trend lines inside KPI cards showing 7/30/90-day trends
2. **Delta indicators** — "↑ 12% vs last scan" with green/red coloring
3. **Progressive disclosure** — card click expands to show top-3 contributing services
4. **Skeleton shimmer** — CSS `@keyframes shimmer` for loading state before data renders
5. **Container queries** — `@container` for responsive grid instead of media queries

**Implementation Pattern**:
```css
.summary-card { container-type: inline-size; }
@container (min-width: 400px) {
  .summary-card .value { font-size: 2.5rem; }
}
@container (max-width: 200px) {
  .summary-card .value { font-size: 1.5rem; }
}
```

### Charts (Chart.js Pie + Bar)

**Current State**: Dynamic HSL colors, dark mode support, click-to-drill-down.

**Best Practices**:
1. **Accessible palette** — provide a toggle for colorblind-safe palette (e.g., Wong palette)
2. **Data labels** — use `chartjs-plugin-datalabels` for inline percentages on pie segments
3. **Responsive legend** — collapse legend to dropdown on mobile
4. **Empty state** — show "No savings data available" message when chartData is empty
5. **Animation respect** — check `prefers-reduced-motion` before Chart.js animations

**Colorblind-Safe Palette**:
```javascript
const colorblindSafe = [
  '#0072B2', '#E69F00', '#009E73', '#CC79A7',
  '#56B4E9', '#D55E00', '#F0E442', '#000000',
  '#999933', '#CC6633'
];
```

### Dark Mode

**Current State**: Full CSS variable override + JS chart color swap + localStorage persistence.

**Best Practices**:
1. **`prefers-color-scheme` media query** — auto-detect system preference on first visit
2. **Smooth transition** — `transition: background-color 0.3s, color 0.3s` on `body`
3. **Chart palette** — separate dark mode color palette with higher luminance
4. **Forced colors** — `@media (forced-colors: active)` for Windows High Contrast Mode

**Auto-detect Pattern**:
```javascript
function initializeTheme() {
  const saved = localStorage.getItem('theme');
  const system = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  const theme = saved || system;
  document.documentElement.setAttribute('data-theme', theme);
  updateThemeToggle(theme);
}
```

### Print Layout

**Current State**: `@media print` block added (forces light theme, hides tabs/charts, expands all sections).

**Best Practices**:
1. **Page break control** — `break-inside: avoid` on `.rec-item`, `break-before: page` on service sections
2. **Print header** — show account/region/date in print margin via `@page` rules
3. **URL expansion** — no need (report is self-contained)
4. **Color optimization** — `print-color-adjust: exact` for badges

### Mobile UX

**Current State**: Responsive breakpoints at 1024px/768px/480px, scroll-snap tabs, gradient fade.

**Best Practices**:
1. **Bottom sheet navigation** — move tab bar to bottom on mobile (thumb-friendly)
2. **Swipe gestures** — horizontal swipe between service tabs
3. **Collapsible sections** — accordion pattern for recommendations on mobile
4. **Touch targets** — minimum 44×44px for all interactive elements (WCAG 2.2 SC 2.5.8)
5. **Viewport chart fix** — `aspect-ratio: 16/9` on chart containers with `overflow: hidden`

### Progressive Disclosure

**Best Practices**:
1. **Summary view** — show only top-3 recommendations per service, "Show all (N)" button
2. **Detail expansion** — rec-item click expands to show full details, metric data, action steps
3. **Executive layer** — dashboard shows only savings total and top services; detail tabs for engineers

### Animation & Motion

**Current State**: `fadeIn` keyframe, card hover lifts, tab indicator scale, `prefers-reduced-motion` respect.

**Best Practices**:
1. **View transitions** — use `document.startViewTransition()` for tab switches (Chrome 111+)
2. **Staggered animations** — delay rec-item fade-in by index for visual rhythm
3. **Spring physics** — use `cubic-bezier(0.34, 1.56, 0.64, 1)` for bouncy card lifts
4. **Reduced motion** — current `prefers-reduced-motion` media query is correct

### WCAG 2.2 Compliance

**Current State**: ARIA roles on tabs, `aria-label` on charts and cards, `aria-pressed` on theme toggle, focus-visible outlines.

**Remaining Gaps**:
1. **Skip navigation** (L20) — hidden skip link at document top
2. **Focus order** — verify tab order follows visual order in all layouts
3. **Color contrast** — some badge variants may not meet 4.5:1 ratio (verify with tool)
4. **Touch target size** (SC 2.5.8) — ensure all interactive elements ≥ 44px on mobile
5. **Drag alternative** (SC 2.5.7) — not applicable (no drag interactions)

---

## 8. Implementation Guidelines

### Before Making Changes

1. **Always create backups** — `cp file.py backup_$(date +%Y%m%d_%H%M%S)_file.py`
2. **Run tests before and after** — `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v`
3. **Check imports** — `python3 -c "import html_report_generator; import reporter_phase_a; import reporter_phase_b"`
4. **Generate a test report** — `python3 cli.py us-east-1 --profile <profile>` and open in browser

### CSS Addition Pattern

All CSS additions go inside the `_get_css()` method (L531-1408). Add new rules:
- **After existing rules for the same component** (keep related styles together)
- **Before the `@media print` block** (keep media queries at the end)
- **Use CSS custom properties** — never hardcode colors/values that exist as vars
- **Add dark mode variants** immediately after the light mode rule

```python
# Pattern for adding new CSS
css += """
    /* Component Name */
    .new-component {
        property: var(--css-var);
        /* ... */
    }
    [data-theme="dark"] .new-component {
        /* dark overrides */
    }
    @media (max-width: 768px) {
        .new-component { /* responsive */ }
    }
"""
```

### JS Addition Pattern

All JS additions go inside the `_get_javascript()` method (L2466-2731). Add new functions:
- **Before the DOMContentLoaded listener** (functions first, init last)
- **Escape curly braces** — `{{ }}` for JS braces inside Python f-strings
- **Use existing patterns** — follow `toggleTheme()` structure for new toggle functions

```python
# Pattern for adding new JS
js = """
    function newFeature() {{
        const element = document.querySelector('.new-component');
        if (!element) return;
        // implementation
    }}
    
    // Add to DOMContentLoaded:
    document.addEventListener('DOMContentLoaded', function() {{
        initializeTheme();
        initializeCharts();
        newFeature();  // <-- add here
    }});
"""
```

### SVG Icon Addition Pattern

SVG icons live in the sprite sheet (L507-516). Add new symbols:

```python
# Inside _build_svg_sprite() or inline in _get_html_head()
'<symbol id="icon-newname" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
'<path d="M..."/>'
'</symbol>'
```

Reference: `<svg class="icon"><use href="#icon-newname"/></svg>`

### Reporter Modification Pattern

Both reporters use `_priority_class()` helper:
```python
def _priority_class(source_block: dict) -> str:
    """Return CSS class based on priority/severity."""
    priority = str(source_block.get("priority", "")).lower()
    severity = str(source_block.get("severity", "")).lower()
    if priority in ("high", "critical") or severity in ("high", "critical"):
        return "high-priority"
    if priority in ("medium",) or severity in ("medium",):
        return "medium-priority"
    if priority in ("low",) or severity in ("low",):
        return "low-priority"
    return ""
```

To add new CSS classes to recommendations, extend this helper and update all call sites.

---

## 9. Verification Checklist

After any UI/UX change, verify all of the following:

### Automated Tests
- [ ] `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v` — all 71 tests pass
- [ ] `python3 -c "import html_report_generator"` — no import errors
- [ ] `python3 -c "import reporter_phase_a"` — no import errors
- [ ] `python3 -c "import reporter_phase_b"` — no import errors

### Visual Verification
- [ ] Generate report: `python3 cli.py <region> --profile <profile>`
- [ ] Open in Chrome + Firefox
- [ ] Test light mode — all colors, badges, charts render correctly
- [ ] Test dark mode — toggle theme, verify all components
- [ ] Test mobile (Chrome DevTools responsive mode at 375px, 768px, 1024px)
- [ ] Test tab navigation — click each tab, verify content shows
- [ ] Test chart interaction — click pie/bar segments, verify tab navigation
- [ ] Test print — Ctrl+P, verify print layout

### Accessibility Verification
- [ ] Tab through all interactive elements — focus outlines visible
- [ ] Screen reader test (VoiceOver/NVDA) — tabs, cards, charts announced
- [ ] Color contrast check — use Chrome DevTools contrast checker
- [ ] `prefers-reduced-motion` — enable in OS settings, verify animations stop

### Code Quality
- [ ] No duplicate CSS declarations (grep for repeated selectors)
- [ ] No hardcoded colors — all via `var(--xxx)`
- [ ] Dark mode overrides exist for all new color-dependent rules
- [ ] JS has no unescaped braces (all `{{ }}` in f-strings)
- [ ] New CSS classes have corresponding HTML generation in reporters

---

## Appendix A: CSS Variable Reference

| Variable | Light | Dark | Usage |
|----------|-------|------|-------|
| `--primary` | `#1976d2` | `#42a5f5` | Buttons, active states, links |
| `--primary-dark` | `#0d47a1` | `#1976d2` | Header gradient start |
| `--primary-light` | `#42a5f5` | `#64b5f6` | Hover states, filter indicators |
| `--secondary` | `#ff9800` | `#ffb74d` | (Same as warning) |
| `--success` | `#4caf50` | `#66bb6a` | Savings amounts, good status |
| `--warning` | `#ff9800` | `#ffb74d` | Medium priority, caution |
| `--danger` | `#f44336` | `#ef5350` | High priority, errors |
| `--info` | `#2196f3` | `#42a5f5` | Informational badges |
| `--surface` | `#ffffff` | `#1e1e1e` | Card backgrounds |
| `--background` | `#f5f5f5` | `#121212` | Page background |
| `--text-primary` | `#212121` | `#ffffff` | Main text |
| `--text-secondary` | `#757575` | `#b0b0b0` | Subtitles, labels |
| `--divider` | `#e0e0e0` | `#333333` | Borders, separators |
| `--shadow-1` – `--shadow-5` | Light rgba | Dark rgba | Material elevation |

## Appendix B: SVG Icon Reference

| Icon ID | ViewBox | Used In |
|---------|---------|---------|
| `#icon-chart` | 0 0 24 24 | KPI card header |
| `#icon-lightbulb` | 0 0 24 24 | Recommendations card |
| `#icon-clipboard` | 0 0 24 24 | Services card |
| `#icon-dollar` | 0 0 24 24 | Savings card |
| `#icon-trending` | 0 0 24 24 | Savings trend (future) |
| `#icon-alert` | 0 0 24 24 | Warnings (future) |
| `#icon-moon` | 0 0 24 24 | Theme toggle (light mode) |
| `#icon-sun` | 0 0 24 24 | Theme toggle (dark mode) |

## Appendix C: Chart.js Configuration Reference

```javascript
// Pie Chart
type: 'pie'
plugins.legend.position: 'bottom'
plugins.legend.usePointStyle: true
plugins.tooltip: custom colors per theme
onClick: filterByService()

// Bar Chart
type: 'bar'
plugins.legend.display: false
scales.y.beginAtZero: true
scales.y.ticks.callback: '$' + value
scales.x.ticks.maxRotation: 45
onClick: filterByService()

// Both charts:
responsive: true
maintainAspectRatio: false
backgroundColor: dynamic HSL array
borderWidth: 2 (pie), 1 (bar)
```

---

*Document generated from UI/UX audit conducted May 4, 2026. Screenshots available in `audit_screenshots/` directory. Pre-fix backups in `backup_20260504_103334/`.*
