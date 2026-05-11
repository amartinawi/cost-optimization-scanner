# AUDIT-08: ElastiCache Service Adapter

| Field | Value |
|-------|-------|
| **Adapter** | `services/adapters/elasticache.py` (75 lines) |
| **Legacy module** | `services/elasticache.py` (177 lines) |
| **Pricing method** | `PricingEngine.get_instance_monthly_price("AmazonElastiCache", node_type)` |
| **Region tested** | `eu-west-1` (EU Ireland) |
| **Date** | 2026-05-01 |
| **Auditor** | Automated audit agent |

**Verdict Codes:** ✅ PASS | ⚠️ WARN | ❌ FAIL | 🔵 INFO

---

## 1. Executive Summary

| Criterion | Verdict | Notes |
|-----------|---------|-------|
| Node Pricing Accuracy | ✅ PASS | Live AWS API pricing × 730 matches adapter computation |
| RI Discount Rates | ⚠️ WARN | 35% flat rate reasonable for 1yr (31–36%), underestimates 3yr (48–55%) |
| Graviton Migration Delta | ❌ FAIL | Adapter claims 25%; actual r5→r6g price-only delta is 5.0% |
| Valkey Migration Savings | ❌ FAIL | Adapter uses 15%; actual OD discount is 20% |
| Valkey Recommendation Text | ❌ FAIL | Legacy module says "Same pricing as Redis" — factually incorrect |
| Engine Filter in Pricing | ❌ FAIL | `_fetch_generic_instance_price` omits cacheEngine filter |
| Reserved Fallback Values | ❌ FAIL | $200/node exceeds typical monthly cost ($167 for r6g.large) |
| Underutilized Rate | ✅ PASS | 40% reasonable for rightsizing estimates |
| Code Quality | ✅ PASS | Clean ServiceModule implementation with fallback |
| Overall | ❌ FAIL | 5 FAIL-level issues require remediation |

---

## 2. Code Analysis

### 2.1 Architecture

```
elasticache.py (adapter, 75 lines)
  └─ scan() → calls services/elasticache.get_enhanced_elasticache_checks(ctx)
                  └─ returns {recommendations: [...], reserved_nodes: [...], ...}
  └─ keyword-rate loop over recommendations
       └─ matches EstimatedSavings text → ri_rate (0.15–0.40)
       └─ savings += monthly_price × num_nodes × ri_rate
```

The adapter delegates all AWS API calls to `services/elasticache.py`, which queries `DescribeCacheClusters` and `CloudWatch` metrics. The adapter itself only computes savings via keyword-based rate assignment.

### 2.2 Keyword-Rate Mapping (adapter lines 41–52)

| Keyword in `EstimatedSavings` | `ri_rate` | Implied savings % |
|-------------------------------|-----------|-------------------|
| `"Reserved"` | 0.35 | 35% |
| `"Graviton"` or `"20-40%"` | 0.25 | 25% |
| `"Valkey"` | 0.15 | 15% |
| `"Underutilized"` | 0.40 | 40% |
| (default/else) | 0.20 | 20% |

### 2.3 Recommendation Categories (from legacy module)

| Category | Trigger | EstimatedSavings text |
|----------|---------|----------------------|
| Reserved Nodes | `num_nodes >= 2` | "30-60% vs On-Demand" |
| Underutilized | `avg_cpu < 20%` (14-day CW) | "30-50%" |
| Graviton Migration | Non-Graviton family | "20-40% price-performance improvement" |
| Valkey Migration | `engine == "redis"` | "Same pricing as Redis..." |
| Old Engine Version | Redis major < 7 | (no savings text) |

### 2.4 Code Quality Verdict

| Check | Result |
|-------|--------|
| ServiceModule Protocol compliance | ✅ PASS — extends `BaseServiceModule` |
| `required_clients()` returns `("elasticache",)` | ✅ PASS |
| `scan()` returns `ServiceFindings` | ✅ PASS |
| Live pricing via `ctx.pricing_engine` | ✅ PASS — line 57–58 |
| Fallback when `pricing_engine is None` | ✅ PASS — lines 60–68 |
| `print()` used instead of `logging` | ⚠️ WARN — line 36 |
| No type annotation on `scan(self, ctx: Any)` | ⚠️ WARN — `Any` instead of `ScanContext` |

---

## 3. Pricing Validation

### 3.1 Live API Pricing (eu-west-1, 2026-04-01)

| Instance Type | Engine | On-Demand $/hr | × 730 = Monthly |
|---------------|--------|-----------------|-----------------|
| cache.r6g.large | Redis | $0.2290 | **$167.17** |
| cache.r6g.large | Memcached | $0.2290 | **$167.17** |
| cache.r6g.large | Valkey | $0.1832 | **$133.74** |
| cache.r5.large | Redis | $0.2410 | **$175.93** |
| cache.r5.large | Memcached | $0.2410 | **$175.93** |
| cache.r5.large | Valkey | $0.1928 | **$140.74** |

### 3.2 Pricing Engine Computation

The adapter calls:
```python
ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)
```

This invokes `_fetch_generic_instance_price()` (pricing_engine.py:414–422) which:
1. Calls `pricing:GetProducts` with `instanceType` + `location` filters only
2. Returns `price_hourly * 730`
3. **Does NOT filter by `cacheEngine`**

**Problem:** ElastiCache returns separate SKUs per engine (Redis, Memcached, Valkey). Without an engine filter, the API returns multiple products. The first match is used — which could be Valkey's lower price instead of Redis/Memcached. For Redis/Memcached this is benign (same OD price), but if a Valkey SKU is returned first for a Redis cluster, savings would be computed against a lower base.

### 3.3 Pricing Verdict

| Check | Result |
|-------|--------|
| Hourly rate × 730 = monthly | ✅ PASS — correct formula |
| Redis/Memcached price parity | ✅ PASS — both $0.229/hr |
| Engine filter omitted | ❌ FAIL — Valkey pricing could contaminate results |

---

## 4. RI Discount Rate Validation

### 4.1 Actual Reserved Node Pricing (cache.r6g.large, Redis, eu-west-1)

| Term | Payment Option | Effective $/hr | Effective $/mo | Savings vs OD |
|------|---------------|-----------------|-----------------|---------------|
| On-Demand | — | $0.2290 | $167.17 | 0% |
| 1yr | No Upfront | $0.1570 | $114.61 | **31.4%** |
| 1yr | Partial Upfront | $0.075 + $652 upfront | ~$101.78/mo* | **39.1%** |
| 1yr | All Upfront | $0.00 + $1,278 upfront | $106.50/mo | **36.3%** |
| 3yr | No Upfront | $0.1190 | $86.87 | **48.0%** |
| 3yr | Partial Upfront | $0.055 + $1,445 upfront | ~$79.09/mo* | **52.7%** |
| 3yr | All Upfront | $0.00 + $2,715 upfront | $75.42/mo | **54.9%** |

*Partial Upfront effective monthly = (hourly × 730 + upfront/term_months)

### 4.2 AWS Documentation Confirmation

Per AWS docs (aws.amazon.com/elasticache/reserved-cache-nodes/):
- Reserved nodes offer "significant discounts" with 1- or 3-year terms
- Three payment options: All Upfront (largest discount), No Upfront, Partial Upfront
- Size-flexible within instance family since Oct 2024

### 4.3 Adapter Rate vs Reality

| Adapter rate | Actual range | Assessment |
|-------------|-------------|------------|
| 35% (single rate) | 31.4%–54.9% | ⚠️ Conservative for 3yr, reasonable for 1yr No Upfront |

**Verdict: ⚠️ WARN** — The 35% rate is a reasonable mid-point for 1-year commitments (31–36%) but significantly underestimates 3-year savings (48–55%).

---

## 5. Graviton Migration Delta

### 5.1 Live Price Comparison (eu-west-1, Redis OD)

| Migration Path | From (monthly) | To (monthly) | Delta | Savings % |
|----------------|---------------|-------------|-------|-----------|
| r5.large → r6g.large (Redis) | $175.93 | $167.17 | −$8.76 | **5.0%** |
| r5.large → r6g.large (Memcached) | $175.93 | $167.17 | −$8.76 | **5.0%** |
| r5.large → r6g.large (Valkey) | $175.93 | $133.74 | −$42.19 | **24.0%** |

### 5.2 Assessment

**Adapter rate: 25%** — The actual Graviton price-only savings for r5→r6g is **5.0%**, not 25%.

The "20-40% price-performance improvement" text in the legacy module (elasticache.py:99) conflates **price** with **performance**. While Graviton processors offer better performance per dollar, the pure price delta between r5 and r6g is only 5%. The adapter's 25% rate overstates price savings by **5×**.

The rate happens to be approximately correct if the user also migrates from Redis to Valkey simultaneously (24%), but the recommendation text does not mention Valkey.

**Verdict: ❌ FAIL** — Graviton-only savings overestimated by 20 percentage points.

---

## 6. Valkey Migration Check

### 6.1 Live API Price Comparison (eu-west-1, OD)

| Instance Type | Redis $/hr | Valkey $/hr | Delta | Savings |
|---------------|-----------|-------------|-------|---------|
| cache.r6g.large | $0.2290 | $0.1832 | −$0.0458 | **20.0%** |
| cache.r5.large | $0.2410 | $0.1928 | −$0.0482 | **20.0%** |

AWS documentation confirms: Valkey is priced **20% lower** than Redis OSS at the OD tier.

### 6.2 Assessment

**Adapter rate: 15%** — Actual savings are **20%**. The adapter **underestimates** Valkey savings by 5 percentage points.

Additionally, the Valkey recommendation text in the legacy module says:

> "Same pricing as Redis for identical node types; consider Valkey for feature parity/security updates"

This is **factually incorrect** — Valkey has its own lower pricing tier (20% cheaper). The text should be corrected.

### 6.3 RI Savings for Valkey (bonus finding)

| Term | Redis All Upfront | Valkey All Upfront | Delta |
|------|-------------------|---------------------|-------|
| 1yr | $1,278 | $1,022.40 | **20.0% cheaper** |
| 3yr | $2,715 | $2,172.00 | **20.0% cheaper** |

Valkey maintains the 20% discount across all RI tiers.

**Verdict: ❌ FAIL** — Rate understates savings; recommendation text is factually wrong.

---

## 7. Underutilized Cluster Rate

**Adapter rate: 40%** — Triggered for clusters with <20% average CPU over 14 days.

For a single-step downsize within the same family (e.g., r5.xlarge → r5.large = 50% savings, or r5.large → r5.medium = 50% savings), 40% is a reasonable midpoint estimate.

**Verdict: ✅ PASS** — Conservative but reasonable for rightsizing heuristics.

---

## 8. Fallback Values

When `ctx.pricing_engine is None` (lines 60–68):

| Category | Flat rate | vs percentage-based (r6g.large) | Assessment |
|----------|-----------|----------------------------------|------------|
| Reserved | $200/node | 35% × $167.17 = **$58.51** | ❌ 3.4× overestimate |
| Graviton | $80/node | 25% × $167.17 = **$41.79** | ⚠️ 1.9× overestimate |
| Valkey | $50/node | 15% × $167.17 = **$25.08** | ⚠️ 2.0× overestimate |
| Underutilized | $100/node | 40% × $167.17 = **$66.87** | ⚠️ 1.5× overestimate |

The Reserved fallback of $200/node exceeds the monthly cost of the instance itself ($167.17), which would imply >100% savings — an impossibility.

**Verdict: ❌ FAIL** — Reserved fallback exceeds monthly instance cost.

---

## 9. Summary of Issues

| # | Severity | Category | Description | Location |
|---|----------|----------|-------------|----------|
| 1 | ❌ FAIL | Pricing | Engine filter missing — Valkey SKU may contaminate pricing | pricing_engine.py:414–422 |
| 2 | ❌ FAIL | Accuracy | Graviton rate 25% vs actual r5→r6g price delta of 5% | elasticache.py:45–46 |
| 3 | ❌ FAIL | Accuracy | Valkey rate 15% vs actual 20% OD discount | elasticache.py:47–48 |
| 4 | ❌ FAIL | Text | Valkey rec says "Same pricing as Redis" — factually wrong (20% cheaper) | services/elasticache.py:84–85 |
| 5 | ❌ FAIL | Fallback | Reserved flat rate $200 exceeds monthly cost of r6g.large ($167) | elasticache.py:61–62 |
| 6 | ⚠️ WARN | Rate | RI rate 35% underestimates 3-year savings by 13–20pp | elasticache.py:44 |
| 7 | ⚠️ WARN | Style | `print()` instead of `logging` | elasticache.py:36 |
| 8 | ⚠️ WARN | Types | `ctx: Any` instead of `ScanContext` | elasticache.py:23 |
| 9 | 🔵 INFO | Docs | README missing Valkey and underutilized from ElastiCache features | README.md |

---

## 10. Final Verdict

### Overall: ❌ FAIL

The adapter is structurally sound (correct ServiceModule implementation, live pricing integration, graceful fallback). However, it has **5 FAIL-level issues** affecting savings accuracy:

1. **Graviton rate (25%)** overstates price-only savings by **5×** (actual 5%)
2. **Valkey rate (15%)** understates actual savings (actual 20%)
3. **Valkey text** incorrectly claims "Same pricing as Redis"
4. **Engine filter** missing from pricing engine — non-deterministic pricing
5. **Reserved fallback** ($200) exceeds typical monthly instance cost

**Recommended priority fixes:**
1. Add `cacheEngine` filter to `_fetch_generic_instance_price()` or use dedicated `get_elasticache_node_monthly_price()`
2. Correct Graviton rate to ~5–10% for price-only, or clarify it represents price-performance
3. Correct Valkey rate to 20% and fix recommendation text
4. Reduce Reserved fallback from $200 to ~$60

---

## 11. Appendix: Raw Pricing Data

### cache.r6g.large (eu-west-1)
```json
{
  "OnDemand": {
    "Redis": "$0.229/hr → $167.17/mo",
    "Memcached": "$0.229/hr → $167.17/mo",
    "Valkey": "$0.1832/hr → $133.74/mo"
  },
  "Reserved": {
    "1yr No Upfront": "Redis $0.157/hr, Valkey $0.1256/hr",
    "1yr All Upfront": "Redis $1,278 upfront, Valkey $1,022.40 upfront",
    "3yr No Upfront": "Redis $0.119/hr, Valkey $0.0952/hr",
    "3yr All Upfront": "Redis $2,715 upfront, Valkey $2,172 upfront"
  }
}
```

### cache.r5.large (eu-west-1)
```json
{
  "OnDemand": {
    "Redis": "$0.241/hr → $175.93/mo",
    "Memcached": "$0.241/hr → $175.93/mo",
    "Valkey": "$0.1928/hr → $140.74/mo"
  },
  "Reserved": {
    "1yr No Upfront": "Redis $0.164/hr, Valkey $0.1312/hr",
    "3yr No Upfront": "Redis $0.125/hr, Valkey $0.100/hr",
    "3yr All Upfront": "Redis $2,853 upfront, Valkey $2,282.40 upfront"
  }
}
```

### Graviton Delta (r5.large → r6g.large)
```
Redis:     $0.241 → $0.229 = −5.0%
Memcached: $0.241 → $0.229 = −5.0%
Valkey:    $0.1928 → $0.1832 = −5.0%
```

### Valkey vs Redis Delta
```
r6g.large: $0.229 → $0.1832 = −20.0%
r5.large:  $0.241 → $0.1928 = −20.0%
```

---

*End of Audit Report*
