# AUDIT-19: API Gateway Service Adapter

**Adapter:** `services/adapters/api_gateway.py` (46 lines)
**Legacy logic:** `services/api_gateway.py` (107 lines)
**Pricing engine:** Not used — keyword-based flat rates
**Date:** 2026-05-01
**Auditor:** Automated (opencode)

---

## 1. Code Analysis

### 1.1 REST → HTTP API Migration Savings

**Location:** `services/adapters/api_gateway.py` lines 41-43

**How it works:**
- The adapter calculates total savings as a flat rate: `savings = 15 * len(recs)`
- Each recommendation contributes exactly $15 to `total_monthly_savings`
- This is NOT calculated from actual CloudWatch `Count` metric data
- The underlying service logic (`services/api_gateway.py`) only identifies REST APIs with ≤10 resources as migration candidates

**AWS Pricing Reality (eu-west-1):**
| API Type | Price per Million Requests |
|----------|---------------------------|
| REST API | $3.50 (first 333M), $3.19 (next 667M), $2.71 (next 19B), $1.72 (over 20B) |
| HTTP API | $1.11 (first 300M), $1.00 (over 300M) |
| **Savings** | **~68-71% for typical usage** |

**Code evidence:**
```python
# services/adapters/api_gateway.py
result = get_enhanced_api_gateway_checks(ctx)
recs = result.get("recommendations", [])
savings = 15 * len(recs)  # ← Flat rate, NOT from CloudWatch metrics
```

**Verdict:** ❌ FAIL — REST→HTTP savings use flat $15/rec estimate, NOT computed from CloudWatch Count metrics

---

### 1.2 Caching Savings Calculation

**Location:** `services/api_gateway.py` lines 82-93

**How it works:**
- The adapter identifies stages without `cacheClusterEnabled`
- Recommendations include generic text: `"EstimatedSavings": "Reduced backend costs"`
- No dollar amount is calculated
- No CloudWatch metrics are queried to determine:
  - Current request volume to backend
  - Cache hit rate potential
  - Backend cost per request

**Cache Pricing (eu-west-1):**
| Cache Size | Price/Hour | Price/Month |
|------------|------------|-------------|
| 0.5 GB | $0.02 | ~$14.60 |
| 1.55 GB | $0.038 | ~$27.74 |
| 6.05 GB | $0.20 | ~$146.00 |
| 13.5 GB | $0.25 | ~$182.50 |
| 28.4 GB | $0.50 | ~$365.00 |
| 58.2 GB | $1.00 | ~$730.00 |
| 118 GB | $1.90 | ~$1,387.00 |
| 237 GB | $3.80 | ~$2,774.00 |

**Code evidence:**
```python
checks["caching_opportunities"].append({
    "ApiId": api_id,
    "ApiName": api_name,
    "StageName": stage_name,
    "Recommendation": "Enable caching to reduce backend calls",
    "EstimatedSavings": "Reduced backend costs",  # ← Generic text, no dollar value
    "CheckCategory": "API Gateway Caching",
})
```

**Verdict:** ❌ FAIL — Caching savings modeled as generic text, NOT from actual request volume

---

### 1.3 Flat Rate Labeling

**Location:** `services/adapters/api_gateway.py` line 42

**How it works:**
- Savings calculated as: `$15 × number of recommendations`
- The value is assigned to `total_monthly_savings` field
- NO indication that this is an estimate
- NO "estimated" or "approximate" labeling in the output

**Code evidence:**
```python
return ServiceFindings(
    service_name="API Gateway",
    total_recommendations=len(recs),
    total_monthly_savings=savings,  # ← $15 × len(recs), not labeled as estimate
    sources={...},
    optimization_descriptions=API_GATEWAY_OPTIMIZATION_DESCRIPTIONS,
)
```

**Comparison with other adapters:**
- Some adapters (e.g., AMI) also use flat rates ($5 per AMI)
- The field name `total_monthly_savings` implies calculated savings, not estimated

**Verdict:** ⚠️ WARN — $15 flat rate per recommendation is NOT labeled as an estimate

---

## 2. Pricing Validation

### 2.1 eu-west-1 Pricing Verification

| Metric | Hardcoded Value | AWS API Value | Status |
|--------|-----------------|---------------|--------|
| REST API (first 333M) | N/A (not used) | $3.50/million | N/A |
| REST API (next 667M) | N/A (not used) | $3.19/million | N/A |
| REST API (next 19B) | N/A (not used) | $2.71/million | N/A |
| REST API (over 20B) | N/A (not used) | $1.72/million | N/A |
| HTTP API (first 300M) | N/A (not used) | $1.11/million | N/A |
| HTTP API (over 300M) | N/A (not used) | $1.00/million | N/A |
| Cache 0.5 GB | N/A (not used) | $0.02/hour | N/A |
| Cache 6.05 GB | N/A (not used) | $0.20/hour | N/A |
| Cache 58.2 GB | N/A (not used) | $1.00/hour | N/A |

**Analysis:**
- The adapter does NOT query AWS Pricing API for any API Gateway pricing
- All savings are keyword-based flat rates
- No integration with `core/pricing_engine.py`

**Verdict:** 🔵 INFO — Adapter uses flat rates, no live pricing validation possible

---

## 3. CloudWatch Metrics Analysis

### 3.1 Metrics NOT Queried

The adapter does NOT query any CloudWatch metrics:

| Metric | Purpose | Queried? |
|--------|---------|----------|
| `Count` | API request count for REST→HTTP savings calc | ❌ NO |
| `4xxError` | Error rate analysis | ❌ NO |
| `5xxError` | Error rate analysis | ❌ NO |
| `Latency` | Performance analysis | ❌ NO |
| `CacheHitCount` | Caching effectiveness | ❌ NO |
| `CacheMissCount` | Caching opportunity sizing | ❌ NO |

**Code evidence:**
- `services/api_gateway.py` only uses `apigateway` client
- No `cloudwatch` client usage
- No `get_metric_statistics` or `get_metric_data` calls

**Verdict:** ❌ FAIL — No CloudWatch metrics queried for actual usage-based calculations

---

## 4. Adapter Architecture Review

### 4.1 ServiceModule Protocol Compliance

| Protocol Field | Implementation | Status |
|----------------|----------------|--------|
| `key` | `"api_gateway"` | ✅ PASS |
| `cli_aliases` | `("api_gateway",)` | ✅ PASS |
| `display_name` | `"API Gateway"` | ✅ PASS |
| `required_clients()` | `("apigateway",)` | ✅ PASS |
| `scan(ctx)` | Returns `ServiceFindings` | ✅ PASS |

**Verdict:** ✅ PASS — Protocol compliant

### 4.2 Savings Calculation Method

| Check Type | Calculation Method | CloudWatch-Based? |
|------------|-------------------|-------------------|
| REST→HTTP migration | $15 flat per API | ❌ NO |
| Caching opportunities | Generic text only | ❌ NO |
| Unused stages | No savings calculated | N/A |
| Throttling optimization | No savings calculated | N/A |
| Request validation | No savings calculated | N/A |

**Verdict:** ❌ FAIL — All savings are flat-rate estimates, none from actual metrics

---

## 5. Documentation Cross-Reference

### 5.1 AWS Documentation Findings

**From AWS docs (API Gateway Pricing):**
- REST API: $3.50/million (first 333M) — verified in pricing API ✅
- HTTP API: $1.00/million (over 300M) — verified in pricing API ✅
- Caching: $0.02-$3.80/hour depending on size — verified in pricing API ✅
- WebSocket: $1.14/million messages — NOT checked by adapter

**README.md Claims:**
- "API Gateway: REST→HTTP migration, caching opportunities"
- Does NOT mention savings are estimates

**Verdict:** ⚠️ WARN — Documentation implies real analysis, but adapter uses flat rates

---

## 6. Pass Criteria Assessment

| Criteria | Status | Notes |
|----------|--------|-------|
| Pricing accuracy | 🔵 INFO | No live pricing used — flat rates only |
| REST→HTTP from CloudWatch Count | ❌ FAIL | Uses $15 flat rate, not metrics |
| Caching from request volume | ❌ FAIL | Generic text only, no dollar amount |
| Flat rate labeled as estimate | ⚠️ WARN | $15/rec not labeled |
| Protocol compliance | ✅ PASS | ServiceModule correctly implemented |

---

## 7. Issues Summary

### Critical Issues (❌ FAIL)

1. **REST→HTTP Savings Not Calculated from Metrics**
   - Flat $15 per recommendation used
   - No CloudWatch `Count` metric queried
   - No actual request volume considered
   - Real savings would be: `(REST_price - HTTP_price) × request_count`

2. **Caching Savings Not Calculated from Volume**
   - Generic "Reduced backend costs" text only
   - No dollar amount provided
   - No calculation of backend cost reduction from cache hits

3. **No CloudWatch Metrics Queried**
   - Adapter operates entirely without usage data
   - Cannot provide accurate savings estimates
   - False positives possible (recommends caching for low-traffic APIs)

### Warning Issues (⚠️ WARN)

4. **Flat Rate Not Labeled as Estimate**
   - `$15 × len(recs)` presented as `total_monthly_savings`
   - No indication this is an approximation
   - Users may believe this is a precise calculation

### Info Items (🔵 INFO)

5. **Pricing Engine Not Integrated**
   - No usage of `core/pricing_engine.py`
   - Keyword-based approach documented in AGENTS.md
   - Consistent with other simple adapters

6. **Limited Check Coverage**
   - Only 2 of 5 check categories populate recommendations
   - Unused stages, throttling, validation have no savings logic

---

## 8. Final Verdict

### Overall: ❌ FAIL

The API Gateway adapter implements the ServiceModule Protocol correctly but **fails to calculate actual savings** from CloudWatch metrics. All savings figures are flat-rate estimates ($15 per recommendation) that are not labeled as estimates. The adapter cannot provide meaningful dollar savings for REST→HTTP migration or caching opportunities because it does not query usage metrics.

### Recommendations for Fix

1. **Query CloudWatch Count metric:** For each REST API, get `Count` metric over 14 days, calculate monthly request volume, then compute: `(REST_price - HTTP_price) × monthly_requests`

2. **Calculate caching savings:** Query `CacheMissCount` and backend latency metrics to estimate backend cost reduction from enabling cache

3. **Label estimates:** Add `"is_estimate": true` to `extras` field when using flat-rate calculations

4. **Add CloudWatch client:** Update `required_clients()` to include `"cloudwatch"` and query actual usage metrics

---

## Appendix: Pricing Reference (eu-west-1)

### API Gateway Request Pricing

| Tier | REST API | HTTP API |
|------|----------|----------|
| First 333M/300M | $3.50/million | $1.11/million |
| Next 667M | $3.19/million | $1.11/million |
| Next 19B | $2.71/million | $1.00/million |
| Over 20B | $1.72/million | $1.00/million |

### API Gateway Cache Pricing

| Cache Size | Price/Hour | Price/Month (approx) |
|------------|------------|---------------------|
| 0.5 GB | $0.02 | $14.60 |
| 1.55 GB | $0.038 | $27.74 |
| 6.05 GB | $0.20 | $146.00 |
| 13.5 GB | $0.25 | $182.50 |
| 28.4 GB | $0.50 | $365.00 |
| 58.2 GB | $1.00 | $730.00 |
| 118 GB | $1.90 | $1,387.00 |
| 237 GB | $3.80 | $2,774.00 |

---

*Report generated by OpenCode Agent for AWS Cost Optimization Scanner*
