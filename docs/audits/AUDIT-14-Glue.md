# AUDIT-14: Glue Adapter Audit

**Date:** May 1, 2026  
**Auditor:** AI Agent  
**Scope:** `services/adapters/glue.py`, `services/glue.py`  
**AWS Pricing API Region:** eu-west-1 (EU Ireland)  

---

## Code Analysis

### Adapter Structure (`services/adapters/glue.py`)

The Glue adapter implements the `ServiceModule` Protocol with a DPU-based pricing strategy:

| Component | Details |
|-----------|---------|
| **Key** | `glue` |
| **CLI Aliases** | `glue` |
| **Required Clients** | `glue` |
| **Pricing Model** | Hardcoded constant `$0.44/DPU-hour` with 160 hours/month assumption |

**Key Functions:**

1. **`scan(ctx)`** (lines 23–60): Main entry point that:
   - Calls `get_enhanced_glue_checks()` for analysis
   - Iterates through recommendations to calculate savings
   - Applies formula: `monthly_cost = $0.44 × dpu_count × 160 × pricing_multiplier`
   - Assumes 30% savings from rightsizing

### Underlying Service (`services/glue.py`)

The service module performs three check categories:

| Check Category | Logic | Trigger |
|----------------|-------|---------|
| **job_rightsizing** | Flags jobs with `MaxCapacity > 10` or `NumberOfWorkers > 10` | Over-provisioned DPU allocation |
| **dev_endpoints** | Flags endpoints with `Status == "READY"` | Running but potentially idle dev endpoints |
| **crawler_optimization** | Flags crawlers with `cron` schedules | Potentially over-frequent crawlers |

**Dev Endpoint Cost Warning:**
- Recommendation: "Dev endpoints cost $0.44/hour - delete when not in use"
- Estimated Savings: "$316/month per endpoint"

---

## Pricing Validation

### Live AWS API Pricing (eu-west-1)

| Usage Type | SKU | Rate | Code Matches |
|------------|-----|------|--------------|
| ETL DPU-Hour | NB9R6DZZ9HCFD3YB | **$0.44** | ✅ Yes |
| ETL Memory-Optimized DPU-Hour | 3UYF6ZHB53FN3ANM | **$0.52** | ❌ Not used |
| Crawler DPU-Hour | RMAVV78MM2ZC7YJ4 | **$0.44** | ✅ Yes |
| Dev Endpoint DPU-Hour | ZWNGC5RT5GH88CSZ | **$0.44** | ✅ Yes |
| Interactive Sessions DPU-Hour | Y4YPXJZYGWU7MRW5 | **$0.44** | ✅ Yes |
| ETL Flex DPU-Hour | YEZ556JWKJ8XF8W4 | **$0.29** | ❌ Not used |
| DataBrew Node-Hour | XKNAJHYXEZH3PJBN | **$0.48** | ❌ Not used |

### Pricing Constants in Code

```python
# services/adapters/glue.py:40
GLUE_DPU_HOURLY = 0.44  # ✅ MATCHES AWS API for standard ETL
```

**Verdict on DPU Rate:** The `$0.44/DPU-hour` constant is **accurate** for standard ETL jobs, crawlers, and dev endpoints in eu-west-1. However, the adapter does not account for:
- Memory-optimized jobs ($0.52/DPU-hour)
- Flex jobs ($0.29/DPU-hour — 34% cheaper)
- DataBrew ($0.48/node-hour)

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | DPU-hour rate accurate ($0.44) | ✅ PASS | Matches AWS Pricing API for standard ETL, crawlers, and dev endpoints |
| 2 | 160 hours/month assumption documented | ❌ FAIL | No documentation found. Assumed 8 hrs/day × 20 workdays = 160 hrs |
| 3 | Dev endpoint savings calculation verified | ⚠️ WARN | $316/month per endpoint — calculation unclear (see Issue GLUE-003) |
| 4 | Uses PricingEngine for live pricing | ❌ FAIL | Uses hardcoded constant instead of `ctx.pricing_engine` |
| 5 | Considers Flex pricing ($0.29/DPU-hour) | ❌ FAIL | Only uses standard $0.44 rate, misses 34% cheaper Flex option |
| 6 | Flags idle dev endpoints | ✅ PASS | Flags READY endpoints with recommendation to delete |
| 7 | Regional pricing applied via multiplier | ✅ PASS | Uses `ctx.pricing_multiplier` for regional adjustments |

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| GLUE-001 | ⚠️ Medium | **160 hours/month assumption is arbitrary and undocumented** | The 160-hour assumption (line 48) appears to be `8 hrs/day × 20 workdays` but this is not documented. Actual Glue job runtime varies significantly — batch jobs may run only minutes, while streaming jobs run 24/7. This leads to inaccurate savings estimates. |
| GLUE-002 | 🔵 Low | **Adapter uses hardcoded pricing instead of PricingEngine** | Unlike 19 other adapters that use `ctx.pricing_engine`, Glue uses a hardcoded `$0.44` constant. This prevents automatic updates if AWS changes pricing and doesn't account for job-type variations (Flex vs Standard). |
| GLUE-003 | ⚠️ Medium | **Dev endpoint $316/month estimate calculation unclear** | Line 63 claims "$316/month per endpoint" at $0.44/hour. For a 2-DPU endpoint running full month (720 hrs): `$0.44 × 2 × 720 = $633.60`. The $316 figure suggests either: (a) 1-DPU endpoint, (b) 50% utilization, or (c) outdated pricing. No calculation provided. |
| GLUE-004 | 🔵 Low | **Does not distinguish between job types** | Different Glue job types have different pricing: Standard ($0.44), Memory-Optimized ($0.52), Flex ($0.29). Adapter treats all jobs as Standard, potentially over/under-estimating costs. |
| GLUE-005 | 🔵 Low | **No consideration for auto-scaling savings** | The adapter assumes 30% savings from rightsizing (line 49) but doesn't verify if auto-scaling is already enabled or calculate actual savings based on historical DPU utilization. |
| GLUE-006 | 🔵 Low | **Dev endpoint check doesn't verify actual usage** | The adapter flags all READY endpoints without checking CloudWatch metrics for actual activity. An endpoint may be READY but idle vs actively used. |

---

## Detailed Analysis

### 160 Hours/Month Assumption

**Location:** `services/adapters/glue.py:48`

```python
monthly_cost = GLUE_DPU_HOURLY * dpu_count * 160 * ctx.pricing_multiplier
```

**Analysis:**
- 160 hours ≈ 8 hours/day × 20 workdays (standard work month)
- Glue jobs often don't follow work schedules:
  - Batch ETL: May run only minutes per day
  - Streaming: Runs 24/7 (730 hrs/month)
  - Scheduled: Highly variable

**Recommendation:** Document the assumption and consider making it configurable or deriving from job schedule metadata.

### Dev Endpoint Savings Estimate

**Location:** `services/glue.py:63`

**Claimed:** $316/month per endpoint at $0.44/hour

**Calculation Verification:**
```
Monthly hours = 730 (continuous)
DPU per endpoint = Unknown (default is 2)
Cost at 2 DPU = $0.44 × 2 × 730 = $643.84
Cost at 1 DPU = $0.44 × 1 × 730 = $321.92 ≈ $316?
```

The $316 figure appears to assume a **single-DPU endpoint** or **~98% utilization of 1 DPU**. This should be documented or calculated dynamically based on actual endpoint configuration.

### Comparison with Other Adapters

| Adapter | Pricing Source | Live API? |
|---------|---------------|-----------|
| EC2 | `pricing_engine.get_ec2_instance_monthly_price()` | ✅ Yes |
| RDS | `pricing_engine.get_rds_instance_monthly_price()` | ✅ Yes |
| EBS | `pricing_engine.get_ebs_monthly_price_per_gb()` | ✅ Yes |
| Lambda | Cost Hub + Compute Optimizer | ✅ Yes |
| **Glue** | Hardcoded `$0.44` | ❌ **No** |
| Lightsail | `pricing_engine.get_instance_monthly_price()` | ✅ Yes |

---

## Recommendations

### Immediate (Pre-Release)

1. **Document the 160-hour assumption** — Add a comment explaining it's based on 8 hrs/day × 20 days
2. **Fix or document the $316/month dev endpoint calculation** — Clarify DPU assumptions

### Short-Term (Next Sprint)

3. **Migrate to PricingEngine** — Add `get_glue_dpu_hourly_price(job_type=None)` method to `PricingEngine`
4. **Support job-type-aware pricing** — Distinguish between Standard, Flex, and Memory-Optimized jobs
5. **Calculate dev endpoint savings dynamically** — Use actual endpoint DPU configuration

### Long-Term (Future Enhancement)

6. **CloudWatch integration for dev endpoints** — Check actual endpoint activity before flagging
7. **Derive hours from job schedules** — Use cron expressions or job run history for accurate hour estimates
8. **Consider Flex migration recommendations** — Flag jobs that could use Flex pricing for 34% savings

---

## Verdict

**Status:** ⚠️ **WARN** — **72/100**

### Scoring Breakdown

| Category | Score | Max | Notes |
|----------|-------|-----|-------|
| Pricing Accuracy | 20/20 | 20 | $0.44 rate is correct for standard ETL |
| Documentation | 5/15 | 15 | 160-hour assumption and $316 estimate undocumented |
| Live Pricing Integration | 5/20 | 20 | Hardcoded, no PricingEngine usage |
| Cost Optimization Logic | 20/25 | 25 | Flags over-provisioned resources but uses arbitrary assumptions |
| Completeness | 12/20 | 20 | Missing Flex pricing, job-type distinctions |
| Dev Endpoint Handling | 10/10 | 10 | Correctly flags READY endpoints |

**Total: 72/100**

### Summary

The Glue adapter correctly identifies cost optimization opportunities (over-provisioned jobs, idle dev endpoints, over-frequent crawlers) and uses an accurate DPU-hour rate. However, it relies on **undocumented assumptions** (160 hours/month, $316/endpoint) and **does not use the PricingEngine** like other modern adapters. The savings calculations are **directionally correct but potentially inaccurate**.

**Recommendation:** Address GLUE-001 and GLUE-003 (documentation issues) before release. Consider GLUE-002 (PricingEngine migration) for the next sprint to align with other adapters.

---

## Appendix: AWS Pricing API Raw Data

### Verified Rates (eu-west-1, 2026-01-08)

| Service Feature | Unit | USD Price | SKU |
|-----------------|------|-----------|-----|
| AWS Glue ETL Job | DPU-Hour | $0.44 | NB9R6DZZ9HCFD3YB |
| AWS Glue ETL Memory-Optimized | M-DPU-Hour | $0.52 | 3UYF6ZHB53FN3ANM |
| AWS Glue ETL Flex | DPU-Hour | $0.29 | YEZ556JWKJ8XF8W4 |
| AWS Glue Crawler | DPU-Hour | $0.44 | RMAVV78MM2ZC7YJ4 |
| AWS Glue Dev Endpoint | DPU-Hour | $0.44 | ZWNGC5RT5GH88CSZ |
| AWS Glue Interactive Sessions | DPU-Hour | $0.44 | Y4YPXJZYGWU7MRW5 |
| AWS Glue DataBrew Jobs | Node-Hour | $0.48 | XKNAJHYXEZH3PJBN |
| AWS Glue DataBrew Sessions | Session | $1.00 | 4JDKM5C7QACSK4W4 |
| AWS Glue Data Catalog Requests | Per 1M | $1.00 | GQRMB5HN28MMNCFH |
| AWS Glue Optimization (Iceberg) | DPU-Hour | $0.44 | AQ5MR4GM9392FP77 |
| AWS Glue Statistics | DPU-Hour | $0.44 | AMUHT9MMMY689WS6 |
| AWS Glue Materialized View Refresh | DPU-Hour | $0.44 | ZJ76TDKU8G5D5QV5 |

### Data Sources

1. AWS Pricing API via `aws-pricing-mcp-server` — Retrieved 2026-05-01
2. AWS Glue Pricing Documentation — https://aws.amazon.com/glue/pricing/
3. AWS Documentation Search — "AWS Glue pricing DPU per hour development endpoint crawler"

---

*End of Audit Report*
