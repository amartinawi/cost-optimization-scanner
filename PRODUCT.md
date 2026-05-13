# Product

## Register

product

## Users

The HTML cost-optimization report serves a three-deep funnel of cloud practitioners working a single AWS account or organization:

- **Cloud architect / engineering leader** opens the report first, usually in a cost-review or platform-review meeting. They skim the executive summary, screenshot specific cards for slides, and need the document to feel credible enough to defend in front of finance, security, and engineering peers in the same room.
- **FinOps engineer** owns the report week to week. They read top-to-bottom, prioritize the largest defensible savings, cross-check the CloudWatch and pricing evidence, and turn recommendations into a remediation queue. They are the most adversarial reader of the document.
- **DevOps / SRE engineer** receives the prioritized list and works per-service tabs. They want dense technical detail (resource IDs, metric windows, pricing source links), copy resource ARNs, and act in console or Terraform.

The same artifact must work at all three depths in a single read. There is no separate "executive dashboard" and "engineering view" — only one report, opened with different intentions.

## Product Purpose

The scanner inspects 37 AWS service categories with 260+ checks, backs every recommendation with CloudWatch metrics and live AWS Pricing API data, and writes a single HTML report plus a JSON snapshot. The HTML report is the canonical user-facing surface of the entire project; CLI flags and JSON output exist to feed it.

The report succeeds when, in one read, a practitioner can:

1. Answer the executive question — "how much can this account save, where, and is the number trustworthy?" — within the first 30 seconds of opening it.
2. Drill into any single service and audit a recommendation to the level of evidence required to defend it (CloudWatch metric reference, pricing source, resource scope, assumptions).
3. Export or screenshot specific sections into review meetings without losing legibility, color meaning, or the chain of evidence behind each number.

The report fails when it looks decorative, when status depends on color alone, when the headline number is unsupported by drill-down evidence, or when a printed or screen-shared copy degrades into unreadable noise.

## Brand Personality

Audit-grade, instrumented, restrained. The report should read like a financial document a senior practitioner would put their name on — closer in spirit to a Stripe Atlas summary, a Bloomberg Terminal panel, or an IRS-1040 line-item disclosure than to a SaaS marketing dashboard.

Three-word voice: **precise, defensible, evidence-first**.

Numbers carry the weight of the page. Color is functional only, used to encode status (savings, severity, anomaly) rather than to decorate sections. Typography hierarchy and whitespace, not gradients or elevation, communicate importance. The reader should feel they are looking at a working instrument that has been calibrated for them, not a template that was filled in for them.

## Anti-references

The report must NOT look like any of the following. Each becomes a forceful Don't in DESIGN.md:

- **Cluttered analytics dashboards.** Datadog and New Relic at their worst: every widget styled differently, badges and chips everywhere, no hierarchy, status colors competing with brand colors. Stated directly by the user as the primary anti-reference.
- **Generic Material Design SaaS dashboard.** Gradient hero header, equal-weight summary card grids, colored left-stripe borders as priority signals, Material 5-step elevation shadow scale, Roboto-on-light-grey. Was the inherited baseline; retired across commits 50c6b2f → 65feb65. New variants must not reintroduce any of those elements.
- **AI-tool slop aesthetic.** Purple/pink gradients, glassmorphism on cards, neon-on-dark, gradient text, hero-metric templates. Reflexive 2025-era AI-startup styling.
- **Corporate enterprise PowerPoint.** Navy + light-blue + orange, stock photography, "Executive Dashboard" template energy, IBM-watson heaviness.

The live risk is now drift: every future component must be checked against this list before it ships, because the report has already escaped these lanes once and the next round of changes is what carries the risk of regression.

## Design Principles

1. **Defensible over decorative.** Every visual element should support evidence (a number, a metric, a source) rather than ornament a section. If removing it does not weaken the argument the report is making, remove it.
2. **One artifact, three depths.** Executive scan, FinOps audit, DevOps drill-down must all work in the same document. Hierarchy is the answer, not separate templates — top-of-page is for triage, mid-page is for prioritization, deep-page is for evidence.
3. **Status is a position, not a color.** Severity (high / medium / low priority, savings vs. spend, anomaly vs. normal) must be readable from typographic weight, label position, and shape — color is the third channel, not the only one. Colorblind readers and printed pages must read the same as a color screen.
4. **Numbers are first-class.** Tabular figures, consistent decimal alignment, explicit units ($, GB, hours), and visible source references (CloudWatch metric, Pricing API, Cost Hub) on every recommendation. The report is a number-bearing document first and a styled page second.
5. **Print is not a fallback.** The PDF export and the screen view are the same document. No hover-only information, no JS-dependent reveals on critical data, no gradient backgrounds that wash out in black-and-white. If a section does not survive a 200 DPI black-and-white print, it is not finished.

## Accessibility & Inclusion

**Target standard: WCAG 2.1 AA, print-ready, colorblind-safe.**

- Body text contrast ratio at minimum 4.5:1; headlines and large numerics 3:1.
- Status (success / warning / danger, priority high / medium / low, savings vs. spend) communicated by **shape or label + position + color** — never color alone. Deuteranopia and protanopia simulations of the palette must remain readable.
- A print stylesheet ships with the report. Dark-mode toggle does not affect the printed output.
- Keyboard navigation across tabs, focus rings on every interactive element, `prefers-reduced-motion` respected for the existing hover translations and the tab fade-in.
- No information conveyed exclusively in animation, hover state, or chart color.
