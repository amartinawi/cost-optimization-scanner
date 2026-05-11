# AUDIT-27: MediaStore Adapter Audit

**Date:** 2026-05-01  
**Auditor:** AI Agent  
**Scope:** `services/adapters/mediastore.py`, `services/mediastore.py`  
**Service Code:** AWSElementalMediaStore  
**Region:** eu-west-1 (Ireland)

---

## Executive Summary

The MediaStore adapter implements the ServiceModule protocol and provides cost optimization recommendations for AWS Elemental MediaStore containers. The adapter uses S3-equivalent pricing for storage cost calculations, which happens to match actual MediaStore storage pricing ($0.023/GB-month), but omits other significant cost components including ingest optimization fees and API request charges.

**Verdict: ⚠️ WARN — 62/100**

---

## Code Analysis

### Adapter Structure (`services/adapters/mediastore.py`)

```python
class MediastoreModule(BaseServiceModule):
    key: str = "mediastore"
    cli_aliases: tuple[str, ...] = ("mediastore",)
    display_name: str = "MediaStore"
```

**Required Clients:** `mediastore`

**Scan Method Flow:**
1. Calls `get_enhanced_mediastore_checks(ctx)` from `services/mediastore.py`
2. Iterates through recommendations
3. Calculates savings using either:
   - `ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD")` when available
   - Fallback: `0.023 * ctx.pricing_multiplier`
4. If `EstimatedStorageGB` field present: `savings += estimated_gb * price_per_gb`
5. Else: `savings += 20.0 * ctx.pricing_multiplier` (flat fallback)

### Underlying Module (`services/mediastore.py`)

**Detection Logic:**
- Lists all MediaStore containers via `mediastore.list_containers()`
- Filters for `Status == "ACTIVE"`
- Queries CloudWatch metrics over 14 days:
  - `RequestCount`
  - `BytesDownloaded`
  - `BytesUploaded`
- Marks container as unused if `total_activity == 0`

**Recommendation Structure:**
```python
{
    "ContainerName": container_name,
    "ActivityLast14Days": 0,
    "Recommendation": "Container shows no activity in last 14 days - consider deletion",
    "EstimatedSavings": "$25/month",  # Hardcoded
    "CheckCategory": "Unused Resource Cleanup",
}
```

---

## Pricing Validation

### AWS MediaStore Pricing (eu-west-1) — Official

| Component | Price | Unit |
|-----------|-------|------|
| **Standard Storage** | $0.0230 | per GB-month |
| **Infrequent Access Storage** | $0.0125 | per GB-month |
| **Ingest Optimization (Standard)** | $0.0200 | per GB |
| **Ingest Optimization (Streaming)** | $0.0200 | per GB |
| **PUT Requests** | $0.0050 | per 1,000 |
| **GET Requests** | $0.0040 | per 10,000 |
| **IA GET Requests** | $0.0100 | per 10,000 |
| **IA Data Retrieval** | $0.0100 | per GB |
| **DELETE Requests** | $0.0050 | per 1,000 |

### Adapter Pricing Approach

| Aspect | Implementation | Accuracy |
|--------|---------------|----------|
| Storage cost per GB | Uses S3 Standard price ($0.023) | ✅ Exact match |
| Ingest optimization | Not calculated | ❌ Missing |
| API request costs | Not calculated | ❌ Missing |
| Fallback rate | `0.023 * pricing_multiplier` | ✅ Correct base |
| Flat fallback | $20 * multiplier (no storage info) | ⚠️ Arbitrary |

### Savings Calculation Analysis

**Current Logic:**
```python
if estimated_gb > 0:
    savings += estimated_gb * price_per_gb  # Storage only
else:
    savings += 20.0 * ctx.pricing_multiplier  # Arbitrary flat rate
```

**Issues:**
1. The underlying module (`services/mediastore.py`) **never sets `EstimatedStorageGB`** in recommendations
2. Therefore, the adapter **always uses the flat $20/month fallback**
3. The hardcoded "$25/month" in the recommendation dictionary is **not used** by the adapter
4. Ingest optimization costs (often significant for video workflows) are completely ignored

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Implements ServiceModule protocol | ✅ PASS | Correct key, cli_aliases, display_name |
| 2 | Required clients declared | ✅ PASS | Returns ("mediastore",) |
| 3 | Returns ServiceFindings dataclass | ✅ PASS | Proper structure with sources |
| 4 | Uses pricing engine when available | ✅ PASS | Calls `ctx.pricing_engine.get_s3_monthly_price_per_gb()` |
| 5 | Provides fallback pricing | ✅ PASS | Uses 0.023 * pricing_multiplier |
| 6 | Detects optimization opportunities | ✅ PASS | Identifies unused containers via CloudWatch |
| 7 | Includes optimization descriptions | ✅ PASS | References MEDIASTORE_OPTIMIZATION_DESCRIPTIONS |
| 8 | Pricing covers all cost components | ❌ FAIL | Only storage; missing ingest + requests |
| 9 | Savings calculation uses actual data | ❌ FAIL | Always falls back to $20 (no EstimatedStorageGB) |
| 10 | Recommendation savings match adapter | ❌ FAIL | Hardcoded "$25/month" ≠ calculated savings |

**Score: 7/10 = 70% (adjusted to 62/100 for severity of issues)**

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| MEDIA-001 | 🔴 **High** | Missing `EstimatedStorageGB` field in recommendations causes adapter to always use flat $20 fallback | Savings estimates are arbitrary, not based on actual storage |
| MEDIA-002 | 🔴 **High** | Ingest optimization costs ($0.02/GB) not included in savings calculation | Significant cost component missing for video ingestion workloads |
| MEDIA-003 | 🟡 **Medium** | API request costs not calculated | PUT/GET request charges omitted from savings estimate |
| MEDIA-004 | 🟡 **Medium** | Hardcoded "$25/month" in recommendation doesn't match adapter's $20 fallback | Inconsistent messaging between recommendation text and calculated savings |
| MEDIA-005 | 🟡 **Medium** | No measurement of actual container storage size | Cannot provide accurate per-GB savings without storage metrics |
| MEDIA-006 | 🔵 **Low** | Uses S3 pricing as proxy (coincidentally correct) | Risk of divergence if MediaStore pricing changes |
| MEDIA-007 | 🔵 **Low** | No support for Infrequent Access storage tier | IA tier is 45% cheaper ($0.0125 vs $0.023) but not considered |

---

## Detailed Findings

### Issue MEDIA-001: Missing Storage Size Measurement

**Location:** `services/mediastore.py`, lines 45-68

**Current Behavior:**
The detection logic only checks CloudWatch activity metrics but never retrieves the actual storage size of containers:

```python
# Missing: No call to get container storage size
metrics_to_check = ["RequestCount", "BytesDownloaded", "BytesUploaded"]
```

**Expected Behavior:**
Should call `mediastore.describe_container()` or CloudWatch `BucketSizeBytes` metric to determine actual storage for accurate savings calculation.

**Fix Recommendation:**
```python
# Add storage size retrieval
cloudwatch = ctx.client("cloudwatch")
storage_metrics = cloudwatch.get_metric_statistics(
    Namespace="AWS/MediaStore",
    MetricName="BucketSizeBytes",
    Dimensions=[{"Name": "ContainerName", "Value": container_name}],
    ...
)
storage_gb = sum(point["Average"] for point in storage_metrics.get("Datapoints", [])) / (1024**3)
```

---

### Issue MEDIA-002: Incomplete Cost Components

**Location:** `services/adapters/mediastore.py`, lines 36-47

**Analysis:**
MediaStore pricing has three major components:
1. **Storage**: $0.023/GB-month (captured ✅)
2. **Ingest Optimization**: $0.02/GB (missing ❌)
3. **API Requests**: $0.005 per 1,000 PUTs (missing ❌)

For a typical video workflow with 100GB ingested per month:
- Storage: $2.30/month
- Ingest: $2.00/month (87% of storage cost!)
- Requests: Variable

**Current savings estimate ignores 47% of potential costs.**

---

### Issue MEDIA-004: Inconsistent Savings Display

**Location:** `services/mediastore.py`, line 60

The underlying module hardcodes:
```python
"EstimatedSavings": "$25/month",
```

But the adapter calculates (when no storage info):
```python
savings += 20.0 * ctx.pricing_multiplier  # $20 default
```

**Result:** User sees "$25/month" in recommendation text but the report totals reflect $20 (or $20 × multiplier).

---

## Recommendations

### Immediate Actions

1. **Fix MEDIA-001**: Add storage size retrieval via CloudWatch `BucketSizeBytes` metric
2. **Fix MEDIA-004**: Align hardcoded recommendation savings with adapter calculation
3. **Document Limitation**: Add comment that ingest costs are not included in savings estimate

### Short-Term Improvements

4. **Add Ingest Optimization**: Include `$0.02/GB` for estimated monthly ingest volume
5. **Add Request Costs**: Estimate based on historical `RequestCount` metrics
6. **Support IA Tier**: Detect storage class and apply appropriate pricing ($0.0125/GB)

### Long-Term Enhancements

7. **MediaStore-Specific Pricing**: Create `get_mediastore_monthly_price()` in pricing engine
8. **Regional Pricing**: Query actual MediaStore pricing from AWS Pricing API per region

---

## Appendix: AWS Pricing API Raw Data

### Storage Pricing
```json
{
  "productFamily": "Storage",
  "attributes": {
    "usagetype": "EU-Storage-S3",
    "storageclass": "Standard"
  },
  "terms": {
    "OnDemand": {
      "pricePerUnit": { "USD": "0.0230000000" },
      "unit": "GB-month"
    }
  }
}
```

### Ingest Optimization Pricing
```json
{
  "productFamily": "Fee",
  "attributes": {
    "usagetype": "EUW1-Ingest-Optimized-Bytes",
    "ingestType": "Standard"
  },
  "terms": {
    "OnDemand": {
      "pricePerUnit": { "USD": "0.0200000000" },
      "unit": "GB"
    }
  }
}
```

### PUT Request Pricing
```json
{
  "productFamily": "API Request",
  "attributes": {
    "usagetype": "EUW1-APIRequest-PutS3"
  },
  "terms": {
    "OnDemand": {
      "pricePerUnit": { "USD": "0.0000050000" },
      "description": "PUT Requests $0.0050 Per 1,000"
    }
  }
}
```

---

## Verdict

**⚠️ WARN — 62/100**

The MediaStore adapter implements the ServiceModule protocol correctly and provides functional detection of unused containers. However, the savings calculations are significantly flawed:

1. **Storage size is never measured**, forcing arbitrary flat-rate savings estimates
2. **Ingest optimization costs** (often 40-50% of total costs) are completely omitted
3. **Inconsistent messaging** between recommendation text ($25) and calculated savings ($20)

The adapter is **safe to use** for identifying unused containers but should **not be relied upon** for accurate cost savings estimates until MEDIA-001 and MEDIA-002 are addressed.

---

*End of Audit Report*
