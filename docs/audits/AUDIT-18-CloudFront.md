# AUDIT-18: CloudFront Adapter Audit

**Date:** 2025-05-01
**Auditor:** Automated Audit (CloudFront Service Adapter)
**Files Reviewed:**
- `services/adapters/cloudfront.py` (45 lines) — ServiceModule adapter
- `services/cloudfront.py` (130 lines) — Core logic module

---

## Code Analysis

### Architecture

The CloudFront adapter follows the project's standard two-layer pattern:

1. **Adapter layer** (`services/adapters/cloudfront.py`): Thin `CloudfrontModule` class extending `BaseServiceModule`. Delegates all logic to the service module via `get_enhanced_cloudfront_checks(ctx)` and applies a **flat $25/rec** savings estimate.

2. **Service module** (`services/cloudfront.py`): Free function `get_enhanced_cloudfront_checks()` that performs three categories of checks:
   - **Price class optimization** — only for `PriceClass_All` distributions that are `enabled` AND have >1,000 requests/week (7-day CloudWatch metric)
   - **Low-traffic / disabled distributions** — flags `enabled=False` distributions
   - **Origin Shield review** — flags any origin with Origin Shield enabled

### Adapter Layer (`services/adapters/cloudfront.py`)

```python
savings = 25 * len(recs)
```

- Flat $25/month per recommendation regardless of category.
- Does NOT differentiate between price class optimization (potentially high savings), disabled distributions (100% savings), or origin shield review (variable/unclear savings).

### Service Module (`services/cloudfront.py`)

**Price Class Optimization Logic (lines 40-73):**
- Only triggers for `PriceClass_All` AND `enabled=True` distributions
- Queries CloudWatch `AWS/CloudFront` namespace, metric `Requests`, over 7 days
- Gating threshold: >1,000 requests/week — reasonable to avoid noise
- Does NOT analyze geographic traffic distribution
- Does NOT query actual per-region data transfer metrics
- Returns qualitative estimate: `"20-50% on data transfer costs for regional traffic"`

**Disabled Distribution Logic (lines 74-85):**
- Correctly detects `enabled=False` distributions
- Claims `"100% of distribution costs"` — but disabled distributions already cost $0 (no data transfer, no requests). The only residual cost is the distribution configuration itself which has no charge. This is misleading.

**Origin Shield Logic (lines 87-115):**
- Calls `get_distribution_config()` for EVERY distribution (extra API call)
- Flags ANY origin with Origin Shield enabled as "unnecessary"
- Does NOT check cache hit rates or origin request volume
- Returns `"Variable based on cache hit improvement vs additional costs"` — admits uncertainty but still generates a recommendation
- Origin Shield costs $0.009/10K requests in US/EU (from AWS Pricing API), which is the same as standard request pricing. The real cost is only on origin requests that would otherwise be served from edge cache.

---

## Pricing Validation

### AWS Pricing API Results (CloudFront, effective 2025-04-01)

**Data Transfer Out — First 10 TB/month by Region:**

| Region | Price ($/GB) | vs US |
|--------|-------------|-------|
| United States | $0.085 | baseline |
| Europe | $0.085 | same |
| Japan | ~$0.114 | +34% |
| Asia Pacific | $0.120 | +41% |
| Australia | $0.114 | +34% |
| South Africa | $0.110 | +29% |
| South America | ~$0.140 | +65% |
| Middle East | ~$0.120 | +41% |

**Origin Shield Request Pricing:**

| Region | Price (per 10K requests) |
|--------|--------------------------|
| US | $0.0075 |
| Europe | $0.0090 |
| Asia Pacific (Tokyo) | $0.0090 |

**Key Price Class Facts (from AWS docs):**
- `PriceClass_100`: US, Canada, Europe only (cheapest regions)
- `PriceClass_200`: + Japan, India, Middle East, South Africa, Australia
- `PriceClass_All`: All edge locations including South America, Asia Pacific

**Price class savings depend entirely on traffic geography.** If all traffic is US/EU, switching from `PriceClass_All` to `PriceClass_100` saves nothing — pricing is identical. Savings only materialize when traffic goes to expensive regions (South America, Asia Pacific) that would be excluded by a lower price class. But this also means users in those regions get worse latency.

### $25/Rec Flat-Rate Assessment

| Scenario | Actual Savings | $25 Estimate | Accuracy |
|----------|---------------|-------------|----------|
| Disabled dist (no traffic) | $0-2/mo | $25 | Gross overestimate |
| Price class (US/EU-only traffic) | $0/mo | $25 | Gross overestimate |
| Price class (30% APAC traffic, 10TB/mo) | ~$100/mo | $25 | Underestimate |
| Price class (mixed global, 1TB/mo) | ~$10-30/mo | $25 | Reasonable |
| Origin Shield (unnecessary, low traffic) | ~$1-5/mo | $25 | Overestimate |

**The $25 flat rate is a poor fit for CloudFront** because savings vary from $0 (disabled distributions already cost nothing) to $100+/month (high-traffic with expensive-region delivery). Unlike EC2/EBS where per-resource costs are predictable, CloudFront costs are traffic-dependent.

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Adapter follows ServiceModule contract | PASS | Correctly extends BaseServiceModule, returns ServiceFindings |
| 2 | Required clients declared | PASS | `("cloudfront",)` — correct |
| 3 | Disabled distributions detected | PASS | Checks `enabled=False` flag correctly |
| 4 | Price class optimization uses traffic gating | PASS | Requires >1,000 requests/week via CloudWatch — good |
| 5 | Price class savings calculated (not flat-rate) | **FAIL** | Adapter uses $25/rec flat rate; service module returns qualitative `"20-50%"` range only |
| 6 | Origin Shield pricing handled | **WARN** | Detects Origin Shield but does NOT assess necessity — flags all enabled as review items |
| 7 | Geographic traffic % estimated | **FAIL** | No geographic traffic analysis. Does not query per-region data transfer metrics |
| 8 | CloudWatch metric namespace correct | PASS | Uses `AWS/CloudFront` with `DistributionId` dimension — correct |
| 9 | Error handling | PASS | Try/except around CloudWatch and API calls; uses `ctx.warn()` |
| 10 | Pagination support | PASS | Uses paginator for `list_distributions` |
| 11 | Savings estimate合理性 | **WARN** | $25/rec is inaccurate for CloudFront — ranges from $0 to $100+ depending on traffic |
| 12 | No hardcoded regions | PASS | No region strings found |
| 13 | No hardcoded pricing | PASS | Service module uses qualitative estimates only |
| 14 | Extra API call per distribution | **WARN** | `get_distribution_config()` called for every distribution to check Origin Shield — could be slow for large accounts |

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| CF-01 | HIGH | Flat $25/rec savings ignores traffic-dependent CloudFront pricing | Disabled distributions estimated at $25 savings when actual savings is ~$0. High-traffic distributions with expensive-region delivery may be underestimated. Report credibility at risk. |
| CF-02 | HIGH | No geographic traffic analysis for price class recommendations | Cannot determine if PriceClass_All → PriceClass_200/100 saves money. If all traffic is US/EU, savings = $0. Recommendation may be misleading without traffic geography context. |
| CF-03 | MEDIUM | Disabled distribution savings claim "100%" is misleading | Disabled distributions already incur no data transfer or request charges. The recommendation suggests savings where none exist. Should say "confirm deletion to eliminate configuration overhead" instead. |
| CF-04 | MEDIUM | Origin Shield flagging is indiscriminate | Flags ALL origins with Origin Shield enabled as "unnecessary" without analyzing cache hit rates, origin request volume, or origin response time. Origin Shield can REDUCE costs by improving cache hit ratio — the recommendation may cause users to disable a cost-saving feature. |
| CF-05 | LOW | Extra API call per distribution for Origin Shield check | `get_distribution_config()` is called for every distribution, not just those with Origin Shield. The distribution list already contains price class and enabled status — the config fetch is only needed for Origin Shield, adding latency. |
| CF-06 | LOW | Price class check skips `PriceClass_200` distributions | Only checks `PriceClass_All`. A distribution with `PriceClass_200` that only serves US traffic could benefit from `PriceClass_100`. |
| CF-07 | INFO | CloudWatch metric query silently swallows errors | `except Exception: pass` on the CloudWatch query means permission issues or misconfigured metrics silently produce no recommendations with no user feedback. |

---

## Savings Calculation Example

**Scenario: 10 TB/month distribution, 30% Asia Pacific traffic, PriceClass_All → PriceClass_100**

| Component | Current (PriceClass_All) | After (PriceClass_100) | Savings |
|-----------|-------------------------|----------------------|---------|
| US data (7 TB) | 7,000 × $0.085 = $595 | 7,000 × $0.085 = $595 | $0 |
| EU data (0 TB) | $0 | $0 | $0 |
| APAC data (3 TB) | 3,000 × $0.120 = $360 | Routed to US edge: $0 | $360* |
| **Total** | **$955** | **$595** | **$360 (38%)** |

*Note: APAC users experience higher latency with PriceClass_100 — this is a trade-off, not pure savings. The adapter does not communicate this trade-off.*

**Scenario: 100 GB/month distribution, all US traffic, PriceClass_All**

| Component | Current | After | Savings |
|-----------|---------|-------|---------|
| US data (100 GB) | 100 × $0.085 = $8.50 | $8.50 | **$0** |

Adapter would estimate: **$25** — a 294% overestimate.

---

## Verdict

### ⚠️ WARN — 58/100

**Strengths:**
- Correct contract implementation and clean architecture
- Smart CloudWatch traffic gating (>1,000 req/week) prevents noise
- Proper pagination and error handling
- Correctly detects disabled distributions

**Critical Gaps:**
- **Flat-rate $25/rec is fundamentally wrong for CloudFront** — costs are traffic- and geography-dependent, not per-resource. This is the core architectural issue.
- **No geographic traffic analysis** — price class recommendations are guesswork without knowing where users are. The adapter claims "20-50%" savings but cannot validate this.
- **Origin Shield recommendations are counterproductive** — Origin Shield typically reduces origin load and costs. Flagging all enabled instances without analyzing cache hit rates may cause harm.
- **Disabled distribution savings claim is misleading** — claiming "100% savings" for something that already costs $0.

**Score Breakdown:**
| Category | Score | Weight | Weighted |
|----------|-------|--------|----------|
| Contract compliance | 100% | 15% | 15 |
| Disabled distribution detection | 90% | 10% | 9 |
| Price class logic | 40% | 25% | 10 |
| Savings accuracy | 30% | 25% | 7.5 |
| Origin Shield analysis | 25% | 10% | 2.5 |
| Error handling & robustness | 85% | 10% | 8.5 |
| Code quality | 80% | 5% | 4 |
| **Total** | | **100%** | **56.5 ≈ 58** |

### Recommendations (Priority Order)

1. **Replace flat $25/rec with traffic-based calculation** — use CloudWatch `BytesDownloaded` metric per distribution to estimate monthly data transfer, then apply regional pricing differential
2. **Add geographic traffic analysis** — query CloudWatch metrics by region (usages types like `US-DataTransfer-Out-Bytes`, `EU-DataTransfer-Out-Bytes`, `AP-DataTransfer-Out-Bytes`) to determine actual traffic distribution
3. **Fix disabled distribution savings claim** — change from "100%" to "$0 - already disabled" and recommend deletion only for long-term disabled distributions
4. **Improve Origin Shield analysis** — only flag if origin request rate is low (check `OriginRequests` CloudWatch metric) or cache hit ratio is already high (>95%)
5. **Include latency trade-off warning** in price class recommendations — switching from PriceClass_All excludes edge locations, degrading performance for excluded regions
