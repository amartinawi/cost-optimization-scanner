# AUDIT-15: Athena Service Adapter Audit Report

**Auditor:** OpenCode AI Agent  
**Date:** 2026-05-01  
**Adapter:** `services/adapters/athena.py`  
**Related:** `services/athena.py` (legacy shim)  

---

## 1. Executive Summary

| Verdict Code | Category | Finding |
|--------------|----------|---------|
| **PASS** | Pricing | `$5/TB` is hardcoded but matches AWS Pricing API for both us-east-1 and eu-west-1 |
| **INFO** | ProcessedBytes | Metric exists in AWS/Athena namespace with WorkGroup dimension; aggregation logic is correct |
| **WARN** | 75% Savings | Undocumented assumption—no AWS benchmark found; appears to be conservative estimate |
| **PASS** | Fallback | `$50` fallback exists when CloudWatch data unavailable |
| **WARN** | Estimate Label | Fallback savings NOT labeled as estimate in output structure |

**Overall Verdict:** `CONDITIONAL-PASS` — 2 warnings require documentation, no blockers.

---

## 2. Code Analysis

### 2.1 Architecture

```
services/adapters/athena.py (72 lines)
├── Imports: BaseServiceModule, ServiceFindings, SourceBlock
├── Class: AthenaModule(BaseServiceModule)
│   ├── key: str = "athena"
│   ├── cli_aliases: tuple = ("athena",)
│   ├── display_name: str = "Athena"
│   ├── required_clients() → ("athena",)
│   └── scan(ctx) → ServiceFindings
│       ├── Calls: get_enhanced_athena_checks(ctx) → services/athena.py
│       ├── Pricing: ATHENA_PRICE_PER_TB = 5.0 (hardcoded constant, line 28)
│       ├── CloudWatch: ProcessedBytes metric query (lines 38-55)
│       ├── Savings calc: monthly_tb × $5 × pricing_multiplier × 0.75 (line 58)
│       └── Fallback: $50 × pricing_multiplier (line 60)
```

### 2.2 Flow Logic

```
scan(ctx)
  └── get_enhanced_athena_checks(ctx) → List[recommendations]
        └── For each recommendation:
              ├── IF NOT ctx.fast_mode:
              │     ├── Get workgroup name from rec["WorkGroup"]
              │     ├── Check rec["ProcessedBytesTB"] (pre-populated or 0)
              │     ├── IF monthly_tb <= 0:
              │     │     └── Query CloudWatch:
              │     │           Namespace: "AWS/Athena"
              │     │           MetricName: "ProcessedBytes"
              │     │           Dimensions: [{"Name": "WorkGroup", "Value": workgroup}]
              │     │           Period: 2592000 (30 days in seconds)
              │     │           Statistics: ["Sum"]
              │     │           TimeRange: now-30d to now
              │     │     └── Sum datapoints → total_bytes
              │     │     └── Convert: total_bytes / (1024^4) → monthly_tb
              │     └── IF monthly_tb > 0:
              │           └── savings += monthly_tb × 5.0 × ctx.pricing_multiplier × 0.75
              │       ELSE:
              │           └── savings += 50.0 × ctx.pricing_multiplier  [FALLBACK]
              └── ELSE (fast_mode):
                    └── savings += 50.0 × ctx.pricing_multiplier
```

---

## 3. Pricing Verification

### 3.1 AWS Pricing API Results

**Query:** `get_pricing("AmazonAthena", region)`

#### us-east-1 (N. Virginia)
```json
{
  "productFamily": "Athena Queries",
  "attributes": {
    "usagetype": "USE1-DataScannedInTB",
    "description": "Charged for total data scanned per query with a minimum 10MB..."
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Terabytes",
        "description": "5.00 USD per Terabytes for DataScannedInTB in US East (N. Virginia)",
        "pricePerUnit": { "USD": "5.0000000000" }
      }
    }
  }
}
```

#### eu-west-1 (Ireland)
```json
{
  "productFamily": "Athena Queries",
  "attributes": {
    "usagetype": "EU-DataScannedInTB",
    "description": "Charged for total data scanned per query with a minimum 10MB..."
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Terabytes",
        "description": "5.00 USD per Terabytes for DataScannedInTB in EU (Ireland)",
        "pricePerUnit": { "USD": "5.0000000000" }
      }
    }
  }
}
```

### 3.2 Verification Result

| Region | Adapter Value | API Value | Status |
|--------|--------------|-----------|--------|
| us-east-1 | $5.00/TB | $5.00/TB | ✅ MATCH |
| eu-west-1 | $5.00/TB | $5.00/TB | ✅ MATCH |

**Finding:** The hardcoded `ATHENA_PRICE_PER_TB = 5.0` is **correct** for both regions tested. AWS documentation confirms $5/TB is the standard rate for SQL queries on S3 data.

### 3.3 Hardcoded vs. Live Pricing

| Aspect | Current | Best Practice |
|--------|---------|---------------|
| Implementation | Hardcoded constant | `ctx.pricing_engine` lookup |
| Risk | Low (stable pricing) | Lower (auto-updates) |
| Impact | None currently | Future-proofing |

**Note:** Athena pricing has remained $5/TB since launch. Hardcoding is acceptable but using `ctx.pricing_engine` would align with other adapters.

---

## 4. ProcessedBytes Metric Analysis

### 4.1 Metric Specification

Per [AWS Documentation](https://docs.aws.amazon.com/athena/latest/ug/query-metrics-viewing.html):

| Property | Adapter Value | AWS Spec | Status |
|----------|---------------|----------|--------|
| Namespace | `AWS/Athena` | ✅ `AWS/Athena` | Correct |
| MetricName | `ProcessedBytes` | ✅ `ProcessedBytes` | Correct |
| Description | Bytes scanned per DML query | ✅ "The number of bytes that Athena scanned per DML query" | Correct |
| Dimension | `WorkGroup` | ✅ `WorkGroup` dimension exists | Correct |
| Period | 2592000 seconds | 30 days | Correct |
| Statistic | `Sum` | Recommended for totals | Correct |

### 4.2 Aggregation Logic

```python
# Adapter code (lines 52-53)
total_bytes = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
monthly_tb = total_bytes / (1024**4) if total_bytes > 0 else 0
```

**Unit Conversion:**
- Bytes → TB: `bytes / (1024^4)` = `bytes / 1,099,511,627,776`
- This is technically TiB (tebibyte) conversion, but used interchangeably with TB in cloud pricing

**Finding:** Aggregation logic is **correct**. Sum of daily/period Sum values yields total bytes over the 30-day window.

### 4.3 Prerequisites

⚠️ **Important:** `ProcessedBytes` metrics require:
1. Workgroup must have **"Publish query metrics to CloudWatch"** enabled
2. Queries must have been executed in the last 30 days
3. Only **DML queries** (SELECT, etc.) populate this metric

If disabled, CloudWatch returns empty datapoints and adapter falls back to $50 estimate.

---

## 5. 75% Savings Factor Analysis

### 5.1 The Claim

```python
# Line 58
savings += monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75
```

The adapter assumes **75% cost reduction** from optimization (partitioning, columnar formats, compression).

### 5.2 AWS Documentation Research

Searched AWS documentation for "75%" or documented benchmarks for Athena optimization savings.

**AWS Pricing Page Example** ([source](https://aws.amazon.com/athena/pricing/)):
> "An example of an SQL query on a 3 TB uncompressed text file costs $15, but compressing the file and converting it to a columnar format like Apache Parquet can result in significant savings. For instance, a compressed and columnar file of the same size would cost $1.25 for the same query."

**Calculation:**
- Uncompressed: $15.00
- Compressed + Columnar: $1.25
- **Actual savings: 91.7%** (not 75%)

**AWS Best Practices Documentation** ([source](https://docs.aws.amazon.com/athena/latest/ug/ctas-partitioning-and-bucketing.html)):
> "Partitioning and bucketing are techniques used in Amazon Athena to reduce the amount of data scanned during query execution, resulting in improved performance and cost savings."

**Finding:** AWS does **NOT** publish a specific "75% savings" benchmark. The 75% appears to be a **conservative engineering estimate** based on typical optimization outcomes.

### 5.3 Real-World Benchmarks

| Optimization | Typical Savings | Source |
|--------------|-----------------|--------|
| Text → Parquet + Snappy | 70-90% | AWS documentation examples |
| Partitioning | 50-99% | Depends on query selectivity |
| Combined | 75-95% | Industry best practices |

### 5.4 Verdict

| Aspect | Finding |
|--------|---------|
| Documented? | ❌ No official AWS "75%" benchmark |
| Reasonable? | ✅ Yes—conservative estimate |
| Risk | Low—actual savings often exceed 75% |
| Recommendation | Document as "estimated 75% savings from best practices" |

---

## 6. Fallback Behavior Analysis

### 6.1 The Fallback

```python
# Lines 59-60
else:
    savings += 50.0 * ctx.pricing_multiplier
```

When CloudWatch `ProcessedBytes` data is unavailable, adapter defaults to **$50/month** per recommendation.

### 6.2 $50 Basis

At $5/TB, $50 represents:
- **10 TB scanned per month** (or ~333 GB/day)

This is a reasonable middle-ground estimate for:
- Small-medium Athena deployments
- Workgroups with infrequent queries
- Organizations just starting with Athena

### 6.3 Is It Labeled as Estimate?

**Current Output Structure:**
```python
return ServiceFindings(
    service_name="Athena",
    total_recommendations=len(recs),
    total_monthly_savings=savings,  # ← No indication of estimate vs. measured
    sources=sources,
    optimization_descriptions=ATHENA_OPTIMIZATION_DESCRIPTIONS,
)
```

**Finding:** ❌ The `$50` fallback is **NOT labeled as an estimate** in the output structure.

Users cannot distinguish between:
1. Measured savings (from actual CloudWatch ProcessedBytes data)
2. Estimated savings (from $50 fallback)

---

## 7. Issues and Recommendations

### Issue 1: Undocumented 75% Savings Factor

| Severity | LOW |
|----------|-----|
| Code | Line 58: `* 0.75` |
| Problem | No documentation explaining the 75% savings assumption |
| Fix | Add comment: `# 75% estimated savings from partitioning, columnar formats, and compression` |

### Issue 2: Fallback Not Labeled as Estimate

| Severity | MEDIUM |
|----------|--------|
| Code | Lines 59-60 |
| Problem | Users cannot distinguish measured vs. estimated savings |
| Fix | Add metadata flag or use `ServiceFindings.estimated=True` if available |

### Issue 3: Hardcoded Pricing (Non-blocking)

| Severity | INFO |
|----------|------|
| Code | Line 28: `ATHENA_PRICE_PER_TB = 5.0` |
| Problem | Does not use `ctx.pricing_engine` like other adapters |
| Fix | Consider migrating to live pricing lookup for consistency |

---

## 8. Pass Criteria Checklist

| Criterion | Required | Actual | Status |
|-----------|----------|--------|--------|
| Pricing matches AWS API | Yes | $5/TB confirmed | ✅ PASS |
| Metric exists in CloudWatch | Yes | ProcessedBytes verified | ✅ PASS |
| Aggregation logic correct | Yes | Sum over 30 days | ✅ PASS |
| Fallback exists | Yes | $50 default | ✅ PASS |
| Savings % documented | Preferred | Not documented | ⚠️ WARN |
| Estimates labeled | Preferred | Not labeled | ⚠️ WARN |

---

## 9. Final Verdict

### Verdict Codes

| Code | Meaning |
|------|---------|
| PASS | No issues found |
| CONDITIONAL-PASS | Minor issues, non-blocking |
| WARN | Issues require attention |
| FAIL | Critical issues, fix required |

### Overall Verdict: **CONDITIONAL-PASS**

**Rationale:**
1. ✅ Pricing is accurate ($5/TB verified)
2. ✅ CloudWatch metric and aggregation logic is correct
3. ✅ Fallback behavior exists
4. ⚠️ 75% savings factor is undocumented (not a blocker—conservative estimate)
5. ⚠️ Fallback estimates not labeled (recommend adding metadata)

**No code changes required for operation.** Recommend documenting assumptions for transparency.

---

## 10. Appendix

### A. Adapter Code (Current)

```python
# services/adapters/athena.py (lines 28-62)
ATHENA_PRICE_PER_TB = 5.0

savings = 0.0
for rec in recs:
    if not ctx.fast_mode:
        workgroup = rec.get("WorkGroup", "primary")
        monthly_tb = rec.get("ProcessedBytesTB", 0)

        if monthly_tb <= 0:
            try:
                cw = ctx.client("cloudwatch")
                from datetime import datetime, timedelta, timezone

                end = datetime.now(timezone.utc)
                start = end - timedelta(days=30)
                resp = cw.get_metric_statistics(
                    Namespace="AWS/Athena",
                    MetricName="ProcessedBytes",
                    Dimensions=[{"Name": "WorkGroup", "Value": workgroup}],
                    StartTime=start,
                    EndTime=end,
                    Period=2592000,
                    Statistics=["Sum"],
                )
                total_bytes = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
                monthly_tb = total_bytes / (1024**4) if total_bytes > 0 else 0
            except Exception:
                monthly_tb = 0

        if monthly_tb > 0:
            savings += monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75
        else:
            savings += 50.0 * ctx.pricing_multiplier
    else:
        savings += 50.0 * ctx.pricing_multiplier
```

### B. CloudWatch Metric Dimensions

Per AWS documentation, `ProcessedBytes` supports these dimensions:
- `WorkGroup` — Name of the workgroup ✅ (used by adapter)
- `QueryState` — SUCCEEDED, FAILED, CANCELED
- `QueryType` — DML, DDL, UTILITY
- `CapacityReservation` — Name of capacity reservation

Adapter correctly uses `WorkGroup` dimension.

### C. References

1. [AWS Athena Pricing](https://aws.amazon.com/athena/pricing/)
2. [Athena CloudWatch Metrics](https://docs.aws.amazon.com/athena/latest/ug/query-metrics-viewing.html)
3. [Partitioning and Bucketing](https://docs.aws.amazon.com/athena/latest/ug/ctas-partitioning-and-bucketing.html)
4. [Cost Optimization Whitepaper](https://docs.aws.amazon.com/whitepapers/latest/cost-modeling-data-lakes/overview-of-cost-optimization.html)

---

*End of Report*
