# Audit: cloudfront Adapter

**Adapter**: `services/adapters/cloudfront.py` (83 lines)
**Legacy shim**: `services/cloudfront.py`
**ALL_MODULES index**: 13
**Renderer path**: **Phase A** (`cloudfront` is in `_PHASE_A_SERVICES`)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 |
| L2 Calculation | **FAIL** | 5 (1 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 2 |
| **Overall**    | **FAIL** | invented 0.5 KB/request + flat $0.10/GB + $10 fallback for un-quantified recs |

---

## Pre-flight facts

- **Required clients**: `("cloudfront",)`
- **Flags**: `requires_cloudwatch=False`, `reads_fast_mode=False`
- **Sources**: `{"enhanced_checks"}`
- **Pricing methods**: **none** from PricingEngine — `price_per_gb=0.10` hardcoded local

---

## L1 — Technical findings

### L1-001 Two `print()` calls (banner + warning)  [MEDIUM]
- Lines 35, 39
- **Recommended fix**: `logger.debug` / `ctx.warn`

### L1-002 Adapter wraps shim call in try/except but uses `print` not `ctx.warn`  [MEDIUM]
- Line 38-40
- **Recommended fix**: `ctx.warn(f"cloudfront enhanced checks failed: {e}", service="cloudfront")`

### L1-003 `WeeklyRequests` field read as string then float-parsed  [LOW]
- Line 46-50 — shim emits numeric; adapter accepts string with `float()` fallback. Defensive but suggests shim's emission shape isn't enforced
- **Recommended fix**: shim should emit `WeeklyRequests` as `int` / `float`; adapter trusts the type

---

## L2 — Calculation findings

### L2-001 `0.5 KB per request` average response size invented  [CRITICAL]
- **Evidence**: line 53 — `est_gb = weekly_requests * 0.0005 / 4.33` — `0.0005` = 0.5 KB per request × `1 / 4.33` weeks/month
- **Why it matters**: average response size varies wildly:
  - REST API JSON: ~1 KB
  - Static HTML page: ~50-200 KB
  - Image-heavy page: 1-5 MB
  - Video segment: 5-50 MB

  At 0.5 KB/request assumed, a video distribution with 1M weekly requests is reported as `1M × 0.5 KB / 4.33 = 0.115 GB/month` of transfer → **$0.012/month savings**. Reality: same distribution likely transfers 10TB+ at ~$850/month — **70,000× under-stated**
- **Recommended fix**: shim should query CloudWatch `BytesDownloaded` metric directly (already a CloudFront standard metric). Compute `actual_GB = BytesDownloaded / (1024^3)`. Stop guessing per-request size

### L2-002 Flat $0.10/GB ignores AWS tiered + regional pricing  [HIGH]
- **Evidence**: line 44 — `price_per_gb = 0.10`. AWS CloudFront data transfer out:
  - US/EU first 10 TB: $0.085/GB
  - US/EU next 40 TB: $0.080/GB
  - US/EU over 5 PB: $0.020/GB
  - Asia Pacific first 10 TB: $0.120/GB
  - South America first 10 TB: $0.110/GB
- **Why it matters**: $0.10 is midway between regions and tiers — wrong for any specific edge. Combined with L2-001's invented size, errors compound non-linearly
- **Recommended fix**: add `PricingEngine.get_cloudfront_data_transfer_per_gb(price_class)`; consume per-distribution `PriceClass` attribute

### L2-003 `$10` fallback for non-quantified recs  [HIGH]
- **Evidence**: line 58 — `savings += 10.0` when `weekly_requests == 0` AND not `"CloudFront Unused Distribution"` (which is correctly $0). Hides every other rec type behind a flat $10
- **Recommended fix**: emit 0 + `pricing_warning` when traffic data unavailable

### L2-004 `pricing_multiplier` applied at end (correct for module-const path)  [LOW]
- **Evidence**: line 60-61
- **Status**: PASS per L2.3.2

### L2-005 No CloudFront-specific category breakdown  [MEDIUM]
- **Evidence**: shim emits at least 4 categories (price-class optimization, unused distribution, origin shield, HTTPS-only) — all funnel into single `enhanced_checks` source

---

## L3 — Reporting findings

### L3-001 Single `enhanced_checks` source — multiple categories collapsed  [MEDIUM]
- **Recommended fix**: split sources by category

### L3-002 No `priority` field  [LOW]

---

## External validation

| Finding | Tool | Result |
|---|---|---|
| L2-002 | `mcp__aws-pricing-mcp-server__get_pricing` `AmazonCloudFront, transferType="CloudFront Outbound"` | tiered $0.085-$0.120/GB by region/tier; flat $0.10 isn't a real list price |
| L2-001 | AWS CloudFront docs | `BytesDownloaded` metric available per distribution — shim should use it |

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Replace per-request size guess with CloudWatch `BytesDownloaded` metric. Requires adding `cloudwatch` to `required_clients()`.
2. **[HIGH] L2-002** — Add tiered/regional CloudFront pricing via PricingEngine.
3. **[HIGH] L2-003** — Drop $10 fallback; emit 0 + warning.
4. **[MEDIUM] L1-001 / L1-002** — Route prints to logger / ctx.warn.
5. **[MEDIUM] L2-005 / L3-001** — Split sources by check category.
6. **[LOW] L3-002** — Set priority.
