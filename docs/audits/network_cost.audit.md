# Audit: network_cost Adapter

**Adapter**: `services/adapters/network_cost.py` (466 lines — self-contained)
**ALL_MODULES index**: 32
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **WARN** | 4 (1 HIGH, 2 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** — CE-derived spend used correctly; main issues are inconsistent multiplier application + invented savings factors (30/50/40%) |

---

## Pre-flight facts

- **Required clients**: `("ce", "ec2")` ✓
- **Flags**: `requires_cloudwatch=False` ✓, `reads_fast_mode=False` (CE is paid so should fast-skip)
- **Sources**: 4 (`cross_region_transfer`, `cross_az_transfer`, `internet_egress`, `tgw_vs_peering`)
- **Pricing**: 100% from CE; TGW costs hardcoded `$0.05/GB attachment + $0.02/GB processing` ✓ matches AWS list

---

## L1 — Technical findings

### L1-001 Four `print()` statements  [MEDIUM]
- Lines 98, 224, 352, 359
- **Recommended fix**: `ctx.warn` / `ctx.permission_issue`

### L1-002 No `fast_mode` honoring despite CE API charges $0.01/call  [MEDIUM]
- Even though only 1 CE call, fast-mode should still skip CE+EC2 calls or short-circuit
- **Recommended fix**: honor `ctx.fast_mode`

---

## L2 — Calculation findings

### Source-classification table

| Recommendation | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Cross-region | `derived` from CE × 0.30 | line 246 | **WARN** — 30% factor invented (no AWS doc); CE spend is real |
| Cross-AZ | `derived` from CE × 0.50 | line 283 | **WARN** — 50% via `CROSS_AZ_SAVINGS_FACTOR`; documented but not AWS-sourced |
| Internet egress | `derived` from CE × 0.40 × multiplier | line 318 | **WARN** — 40% factor + **inconsistent multiplier application** |
| TGW vs peering | flat 0.0 (informational only) or derived × 0.20 | lines 396, 412, 424 | OK — info-only, scope-rule compliant |

### L2-001 Internet-egress path applies `pricing_multiplier`; cross-region/cross-AZ do not  [HIGH]
- **Evidence**:
  - Line 246 — `potential_savings = monthly_est * 0.30` (no multiplier)
  - Line 283 — `potential_savings = monthly_est * CROSS_AZ_SAVINGS_FACTOR` (no multiplier)
  - Line 318 — `potential_savings = monthly_est * CLOUDFRONT_SAVINGS_FACTOR * multiplier` (multiplier applied)
- **Why it matters**: `monthly_est` comes from CE which is already real region-priced spend. Per L2.3.1, multiplier should NOT be applied to AWS-API-derived spend. Internet egress path is wrong; cross-region/cross-AZ paths are correct. Inconsistent
- **Recommended fix**: drop `* multiplier` from line 318. Consistency restored

### L2-002 `_analyze_cross_region` accepts `multiplier` but doesn't use it (dead param)  [MEDIUM]
- **Evidence**: line 228 — function takes `multiplier` param but never references it. Same for `_analyze_cross_az` (line 265). Documents intent vs implementation
- **Recommended fix**: drop unused param or apply consistently (per L2-001 the correct call is no multiplier)

### L2-003 30% cross-region / 50% cross-AZ / 40% CloudFront factors invented  [MEDIUM]
- **Evidence**: lines 246, 283, 318. Each factor is conservative but unsourced
- **Recommended fix**: document the factors against AWS-published references, or measure actual reduction via test deployments

### L2-004 `usage_type` keyword matching is fragile  [LOW]
- **Evidence**: lines 214-221 — uses string matching on `usage_type` to categorize. Real AWS USAGE_TYPE strings like `USE1-USW2-AWS-In-Bytes` (us-east-1 → us-west-2 inbound) require careful parsing
- **Recommended fix**: improve regex with documented USAGE_TYPE format

---

## L3 — Reporting findings

**None** — strong `stat_cards`, `grouping`, sources, descriptions.

---

## Recommended next steps

1. **[HIGH] L2-001** — Drop `* multiplier` from `_analyze_internet_egress` (line 318). CE-spend already region-correct.
2. **[MEDIUM] L1-001 / L1-002** — Classify error prints; honor fast_mode.
3. **[MEDIUM] L2-003** — Document the 30/50/40% factors.
4. **[MEDIUM] L2-002** — Drop unused `multiplier` params.
5. **[LOW] L2-004** — Improve usage_type pattern matching.
