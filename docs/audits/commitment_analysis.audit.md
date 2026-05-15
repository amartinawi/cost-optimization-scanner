# Audit: commitment_analysis Adapter

**Adapter**: `services/adapters/commitment_analysis.py` (689 lines — self-contained, no shim)
**ALL_MODULES index**: 29
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| L2 Calculation | **WARN** | 3 (1 MEDIUM, 2 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** — strong AWS-API-derived savings, scope-rule compliant. Main issues: AccessDenied not classified + CE API cost is now ~$0.19/scan after term×payment fan-out + `AVG_SP_DISCOUNT_RATE=0.30` is conservative midpoint of 27-72% |

**Notable**: this is the **best-in-class** adapter for AWS-API-derived dollar values. Every `monthly_savings` figure comes from Cost Explorer's own `EstimatedMonthlySavings` field — no module-const multipliers, no invented constants. Recent fan-out across `(term, payment)` matrix gives operators full scenario comparison.

---

## Pre-flight facts

- **Required clients**: `("ce",)` ✓
- **Flags**: `requires_cloudwatch=False` ✓ (honest), `reads_fast_mode` default False (CE calls are not fast-mode-skippable currently)
- **Sources**: 7 distinct sources (`cost_optimization_hub`, `sp_utilization`, `sp_coverage_gaps`, `ri_utilization`, `ri_coverage_gaps`, `expiring_commitments`, `purchase_recommendations`)
- **Pricing**: 100% from AWS Cost Explorer API. Zero PricingEngine dependence
- **CE API cost**: ~$0.19/scan after matrix expansion (7 base calls + 12 fan-out term×payment calls)

---

## L1 — Technical findings

### L1-001 Eight `print()` statements in error paths instead of `ctx.warn` / `ctx.permission_issue`  [HIGH]
- **Evidence**: lines 84, 177, 208, 266, 322, 378, 433, 497, 593
- **Why it matters**: Cost Explorer requires explicit IAM permissions (`ce:GetSavingsPlansUtilization`, etc.). A denial fails silently → empty findings → user thinks they have no commitments to optimize. The HTML "Permission Issues" section stays blank
- **Recommended fix**: classify each `Exception` — `AccessDeniedException` → `ctx.permission_issue(action="ce:GetSavingsPlansUtilization")`; other errors → `ctx.warn`

### L1-002 No `reads_fast_mode` flag, but CE calls would be expensive to fast-skip  [MEDIUM]
- **Evidence**: adapter doesn't honor `ctx.fast_mode`; CE API charges $0.01/call. Fast scans should skip the 19 calls
- **Recommended fix**: set `reads_fast_mode = True`; gate all CE calls behind `if not ctx.fast_mode`; emit a single "fast-mode skip" rec when set

### L1-003 CE fan-out matrix can generate up to 19 paid API calls per scan  [MEDIUM]
- **Evidence**: 7 base calls + 6 SP-purchase combinations (1 type × 2 terms × 3 payments) + 6 RI-purchase combinations (1 service × 2 terms × 3 payments) = 19 × $0.01 = $0.19/scan. Across a 30-account org, $5.70/day if scanned daily
- **Recommended fix**: cache CE results per-account-per-day server-side; or expose a `--commitments-detail-level` flag (fast=summary-only, full=fan-out)

---

## L2 — Calculation findings

### Source-classification table

| Recommendation | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| SP utilization waste | `aws-api` (CE `AmortizedCommitment.TotalHourlyCommitment × (1 - util) × 730`) | line 189-190 | YES — direct from CE |
| SP coverage gap | `derived` (`OnDemandCost × (1-rate) × AVG_SP_DISCOUNT_RATE=0.30`) | line 244 | **WARN** — 30% midpoint of 27-72% AWS-published SP discounts; conservative |
| RI utilization waste | `aws-api` (`TotalAmortizedCost × (1 - rate)`) | line 299-301 | YES |
| RI coverage gap | `derived` (× `AVG_SP_DISCOUNT_RATE=0.30`) | line 356 | **WARN** — same 30% factor for RI; RI discounts also 27-72% |
| SP purchase | `aws-api` (CE `EstimatedMonthlySavings`) | line 502 | YES — direct |
| RI purchase | `aws-api` (sum `EstimatedMonthlySavings` across details) | line 601-603 | YES |
| Expiring commitments | flat 0.0 (informational only) | line 423 | YES — scope-rule clean |
| Cost Hub | `aws-api` (top-level `estimatedMonthlySavings`) | line 108 | YES |

### L2-001 `AVG_SP_DISCOUNT_RATE = 0.30` is conservative midpoint  [MEDIUM]
- **Evidence**: line 51 — `AVG_SP_DISCOUNT_RATE: float = 0.30`. AWS Compute SP discounts: 27% (1yr no-upfront) to 66% (3yr all-upfront). EC2-specific SP: up to 72%. RI discounts: 30-75%
- **Why it matters**: applied uniformly to coverage-gap savings for both SP and RI. Coverage gap is a "what could I save by buying more commitment?" estimate — using midpoint is reasonable, but a user buying 3yr all-upfront commitments would actually save ~2× the figure shown
- **Recommended fix**: split into `AVG_SP_DISCOUNT_NO_UPFRONT_1YR = 0.27`, `AVG_RI_DISCOUNT_ALL_UPFRONT_3YR = 0.66`, etc. Or document the 30% as "1yr no-upfront equivalent"

### L2-002 `EstimatedMonthlySavings` from CE is trusted directly  [LOW / POSITIVE]
- **Evidence**: lines 502, 535, 601-603 — adapter takes CE-returned savings as-is
- **Status**: PASS — CE handles pricing math (region-correct), so no multiplier needed per L2.3.1

### L2-003 Coverage gap savings may overlap with purchase recs  [LOW]
- **Evidence**: `sp_coverage_gaps` recs show "you'd save $X by covering this gap"; `purchase_recommendations` show "buy this SP for $X savings". Same dollars counted in two sources
- **Why it matters**: `total_monthly_savings` is the sum of both. User sees inflated total
- **Recommended fix**: dedupe — either omit `coverage_gaps` when `purchase_recommendations` covers the same service, or label coverage as "potential" and purchase as "concrete"

---

## L3 — Reporting findings

**None** — adapter declares `stat_cards` and `grouping` correctly, sources well-named, `_empty_findings` covers permission-denied case.

---

## Recommended next steps

1. **[HIGH] L1-001** — Classify all 8 `print` sites; route AccessDenied to `ctx.permission_issue`.
2. **[MEDIUM] L1-002 / L1-003** — Honor fast_mode; cache CE results per-account-per-day.
3. **[MEDIUM] L2-001** — Per-term/payment discount rates instead of flat 30%.
4. **[LOW] L2-003** — Dedupe coverage_gaps ∪ purchase_recommendations.
