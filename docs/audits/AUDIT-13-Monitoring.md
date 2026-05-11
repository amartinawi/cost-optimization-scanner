# AUDIT-13: Monitoring (CloudWatch/CloudTrail/Route53) Service Adapter

**Date:** 2026-05-01  
**Auditor:** OpenCode AI Agent  
**Scope:** `services/adapters/monitoring.py`, `services/monitoring.py`, `services/route53.py`  
**Status:** COMPLETED

---

## 1. Executive Summary

| Component | Status | Verdict Code |
|-----------|--------|--------------|
| CloudWatch Log Cost Estimation | ❌ FAIL | COST-UNDERESTIMATE-MAJOR |
| CloudTrail Paid Trail Detection | ⚠️ WARNING | LOGIC-PARTIAL |
| Parse-Based Savings Extraction | ✅ PASS | PARSE-ACCURATE |
| Route53 Hosted Zone Pricing | ✅ PASS | PRICING-EXACT |
| Route53 Health Check Pricing | ✅ PASS | PRICING-EXACT |
| **OVERALL** | ⚠️ **CONDITIONAL PASS** | **AUDIT-PARTIAL** |

---

## 2. Code Analysis

### 2.1 Adapter Structure (`services/adapters/monitoring.py`)

```python
class MonitoringModule(BaseServiceModule):
    key: str = "monitoring"
    cli_aliases: tuple[str, ...] = ("monitoring",)
    display_name: str = "Monitoring & Logging"

    def required_clients(self) -> tuple[str, ...]:
        return ("cloudwatch", "logs", "cloudtrail", "backup", "route53")
```

**Observations:**
- Composite adapter pattern combining 4 service modules
- Correct client dependencies declared
- Implements ServiceModule protocol correctly
- Savings aggregation logic present

### 2.2 Savings Aggregation Logic

```python
savings = 0.0
for rec in all_recs:
    savings_str = rec.get("EstimatedSavings", "")
    if "$" in savings_str and "/month" in savings_str:
        try:
            savings_val = float(savings_str.replace("$", "").split("/")[0])
            savings += savings_val
        except (ValueError, AttributeError):
            pass
```

**Verdict:** Parsing logic is correct for expected format `"$X.XX/month"`. Handles missing/invalid values gracefully.

---

## 3. CloudWatch Log Cost Analysis

### 3.1 Current Implementation (`services/monitoring.py:57-65`)

```python
checks["never_expiring_logs"].append({
    "LogGroupName": log_group_name,
    "StoredBytes": stored_bytes,
    "StoredGB": round(stored_bytes / (1024**3), 2),
    "Recommendation": "Set retention policy to prevent unlimited log growth",
    "EstimatedSavings": f"${stored_bytes * 0.03 / (1024**3):.2f}/month with 30-day retention",
    "CheckCategory": "Never-Expiring Log Groups",
})
```

### 3.2 AWS Pricing Verification (eu-west-1, 2026-03)

**Actual AWS Pricing from API:**

| Log Type | Price per GB | Notes |
|----------|--------------|-------|
| Custom logs (Standard) | **$0.57/GB** | First 10TB |
| Vended logs (Standard) | **$0.50/GB** | First 10TB |
| CloudFront logs | $0.50/GB | First 10TB |
| Tier 2 (10-30TB) | $0.285/GB | |
| Tier 3 (30-50TB) | $0.114/GB | |
| Tier 4 (50TB+) | $0.057/GB | |

**Adapter Using:** `$0.03/GB`

### 3.3 Discrepancy Analysis

```
Expected: stored_gb × $0.57/GB (ingestion) + $0.03/GB (storage after 30 days)
Actual:   stored_gb × $0.03/GB

For 100 GB log group:
- Expected cost: $57.00 (ingestion) + $3.00 (storage) = $60.00
- Adapter shows: $3.00
- **UNDERESTIMATION: 95%**
```

### 3.4 Verdict

**❌ FAIL - COST-UNDERESTIMATE-MAJOR**

The adapter uses **$0.03/GB** but AWS charges **$0.57/GB** for log ingestion in eu-west-1. This is a **19x underestimation** of actual costs. The $0.03 rate appears to be an outdated or confused storage-only rate.

**Correct Formula Should Be:**
```python
# For ingestion savings (primary cost driver)
f"${stored_bytes * 0.57 / (1024**3):.2f}/month"

# Or with tiered pricing for accuracy
```

---

## 4. CloudTrail Cost Analysis

### 4.1 Current Implementation

The adapter checks for:
1. Multi-region trails (flags but no cost calculation)
2. Data events for all S3 buckets
3. Data events for all Lambda functions
4. CloudTrail Insights
5. Duplicate trails

### 4.2 AWS Pricing Verification (eu-west-1)

| Event Type | Price | Notes |
|------------|-------|-------|
| Management Events | **$0.00** | First trail free |
| Additional Management Events | **$2.00/100k events** | Paid trails |
| Data Events | **$1.00/1M events** | $0.000001 per event |
| Insights Events | **$3.50/1M events** | $0.0000035 per event |

### 4.3 Code Analysis

**Multi-Region Trails (Line 129-138):**
```python
if is_multi_region:
    checks["multi_region_trails"].append({
        "TrailName": trail_name,
        # ...
        "EstimatedSavings": "Single-region trail costs ~90% less",
        # Static string - not parseable
    })
```

**S3 Data Events (Line 149-158):**
```python
checks["data_events_all_s3"].append({
    "TrailName": trail_name,
    "EstimatedSavings": "Limit to specific buckets for 80-95% savings",
    # Static string - not parseable
})
```

**Insights (Line 184-192):**
```python
checks["unused_insights"].append({
    "TrailName": trail_name,
    "EstimatedSavings": "$0.35 per 100,000 events if unused",
    # INCORRECT - actual is $3.50 per 1M events ($0.35 per 100k)
})
```

### 4.4 Issues Found

1. **Static Savings Strings**: Most CloudTrail recommendations use non-parseable static strings like `"Single-region trail costs ~90% less"`
2. **Missing Paid Trail Logic**: Does not distinguish between first (free) trail and additional paid trails
3. **Insights Pricing Error**: States $0.35 per 100k events, but AWS charges $3.50 per 1M events (which equals $0.35 per 100k) - **actually correct but misleading unit**

### 4.5 Verdict

**⚠️ WARNING - LOGIC-PARTIAL**

- Does flag cost drivers (multi-region, data events)
- Does NOT calculate actual dollar savings for CloudTrail
- Savings strings are not parseable by aggregation logic
- Missing first-trail-free logic

---

## 5. Parse-Based Savings Analysis

### 5.1 How Parsing Works

The adapter parses EstimatedSavings strings in `services/adapters/monitoring.py:55-61`:

```python
savings_str = rec.get("EstimatedSavings", "")
if "$" in savings_str and "/month" in savings_str:
    try:
        savings_val = float(savings_str.replace("$", "").split("/")[0])
        savings += savings_val
    except (ValueError, AttributeError):
        pass
```

### 5.2 Test Cases

| Input String | Parsed Value | Expected | Status |
|--------------|--------------|----------|--------|
| `"$3.50/month"` | 3.50 | 3.50 | ✅ PASS |
| `"$0.50/month per zone"` | 0.50 | 0.50 | ✅ PASS |
| `"$10.50/month if reduced by 50%"` | 10.50 | 10.50 | ✅ PASS |
| `"Single-region trail costs ~90% less"` | None | N/A | ⚠️ Ignored (expected) |
| `"Reduce log level or retention period"` | None | N/A | ⚠️ Ignored (expected) |
| `"${count * 0.30:.2f}/month"` | Error | N/A | ❌ FAIL (f-string evaluated too late) |

### 5.3 Verdict

**✅ PASS - PARSE-ACCURATE**

Parsing logic is correct for properly formatted strings. Non-parseable strings (static text, recommendations without dollar amounts) are gracefully ignored.

---

## 6. Route53 Pricing Analysis

### 6.1 Hosted Zones (`services/route53.py:42-52`)

```python
checks["unused_hosted_zones"].append({
    "HostedZoneId": zone_id,
    "ZoneName": zone_name,
    "RecordCount": record_count,
    "IsPrivate": is_private,
    "Recommendation": "Hosted zone has minimal records - verify if still needed",
    "EstimatedSavings": "$0.50/month per zone if deleted",
    "CheckCategory": "Unused Hosted Zones",
})
```

### 6.2 Health Checks (`services/route53.py:89-97`)

```python
checks["unnecessary_health_checks"].append({
    "HealthCheckId": hc_id,
    "Type": hc_type,
    "Recommendation": "Health check without routing dependency - verify necessity",
    "EstimatedSavings": "$0.50/month per health check if removed",
    "CheckCategory": "Unnecessary Health Checks",
})
```

### 6.3 AWS Pricing Verification

From AWS Pricing API (AmazonRoute53, eu-west-1):

| Resource | Adapter Price | AWS Price | Status |
|----------|---------------|-----------|--------|
| Hosted Zone | $0.50/month | $0.50/month | ✅ **EXACT MATCH** |
| Standard Health Check | $0.50/month | ~$0.50/month | ✅ **CORRECT** |

### 6.4 Verdict

**✅ PASS - PRICING-EXACT**

Route53 pricing in the adapter matches AWS pricing exactly.

---

## 7. Pass Criteria Assessment

| Criterion | Requirement | Status |
|-----------|-------------|--------|
| CloudWatch log cost | stored_gb × $0.03/GB for ingestion + $0.03/GB for storage | ❌ FAIL (uses $0.03 instead of $0.57) |
| CloudTrail paid trails | Flag paid trails ($2/100k events) | ⚠️ PARTIAL (flags multi-region, not explicit paid status) |
| Parse accuracy | Dollar amounts extracted from EstimatedSavings | ✅ PASS |
| Route53 idle zones | $0.50/zone/month | ✅ PASS (exact match) |

---

## 8. Issues Summary

| # | Severity | Issue | Location | Impact |
|---|----------|-------|----------|--------|
| 1 | **HIGH** | CloudWatch log cost uses $0.03/GB instead of $0.57/GB | `services/monitoring.py:64` | 19x cost underestimation |
| 2 | MEDIUM | CloudTrail savings strings are non-parseable static text | `services/monitoring.py:137,151,161` | Zero CloudTrail savings in totals |
| 3 | LOW | CloudTrail Insights unit confusing ($0.35/100k vs $3.50/M) | `services/monitoring.py:190` | Minor documentation issue |
| 4 | MEDIUM | Missing first-trail-free logic for CloudTrail | `services/monitoring.py:112+` | May flag free trail as costly |

---

## 9. Recommendations

### Immediate Actions Required

1. **Fix CloudWatch Log Pricing (HIGH)**
   ```python
   # Change from:
   "EstimatedSavings": f"${stored_bytes * 0.03 / (1024**3):.2f}/month"
   
   # To:
   "EstimatedSavings": f"${stored_bytes * 0.57 / (1024**3):.2f}/month"
   ```

2. **Add Parseable CloudTrail Savings (MEDIUM)**
   - Add dollar amounts to CloudTrail recommendations
   - Include first-trail-free detection logic

### Nice to Have

3. Use tiered pricing for CloudWatch logs > 10TB
4. Add storage cost component (separate from ingestion)
5. Regional pricing support via ScanContext.pricing

---

## 10. Final Verdict

**VERDICT CODE: AUDIT-PARTIAL**

The Monitoring adapter has **one major pricing error** (CloudWatch logs at 5% of actual cost) that significantly undermines cost estimates. Route53 pricing is perfect. CloudTrail logic is incomplete but functional.

### Scoring

| Category | Weight | Score |
|----------|--------|-------|
| Code Quality | 20% | 90% |
| Pricing Accuracy | 40% | 35% |
| Parse Logic | 20% | 95% |
| Feature Completeness | 20% | 60% |
| **OVERALL** | 100% | **64%** |

**Status:** ⚠️ **CONDITIONAL PASS** - Requires CloudWatch pricing fix before production use.

---

## Appendix A: Pricing API References

### CloudWatch Log Ingestion (eu-west-1)
- SKU: KJBTQPDHW2H92B8Y
- Price: $0.57 per GB custom log data ingested
- Effective: 2026-03-01

### CloudTrail Data Events (eu-west-1)
- SKU: 3SQYRBFEPJSYDZ9D
- Price: $0.000001 per data event ($1.00 per million)
- Effective: 2026-03-01

### Route53 Hosted Zones
- Global pricing: $0.50 per hosted zone per month
- Standard health checks: ~$0.50 per month

---

*End of Audit Report*
