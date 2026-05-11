# AUDIT-04: S3 Service Adapter

**Adapter:** `services/adapters/s3.py` (69 lines)
**Legacy logic:** `services/s3.py` (931 lines)
**Pricing engine:** `core/pricing_engine.py` — `get_s3_monthly_price_per_gb()`
**Date:** 2026-05-01
**Auditor:** Automated (opencode)

---

## 1. Code Analysis

### Architecture

The adapter (`services/adapters/s3.py`) is a thin shell that delegates to two legacy functions in `services/s3.py`:

| Function | Lines | Purpose |
|----------|-------|---------|
| `get_s3_bucket_analysis()` | 562–761 | Per-bucket analysis: size, cost, lifecycle, Intelligent-Tiering status |
| `get_enhanced_s3_checks()` | 764–931 | Cross-cutting checks: multipart uploads, versioning, replication, logging, empty buckets |

### EstimatedMonthlyCost Population

**Two code paths populate `EstimatedMonthlyCost`:**

1. **Bucket analysis (`s3.py:639-684`):**
   - Fast mode: `_calculate_s3_storage_cost(SizeGB, "STANDARD", region, ctx=ctx)` — flat Standard pricing on sample
   - Full mode: Same function on total CloudWatch-derived size — **always uses `"STANDARD"` regardless of actual storage class mix**

2. **Enhanced checks (`s3.py:840-847`):**
   - `lifecycle_missing` entries set `EstimatedMonthlyCost: 0` — **no dollar savings calculated**
   - Other check categories (multipart, versioning, etc.) have **no `EstimatedMonthlyCost` field at all**

3. **Savings aggregation in adapter (`s3.py:42`):**
   ```python
   savings = sum(rec.get("EstimatedMonthlyCost", 0) for rec in opt_opps)
   ```
   This sums `EstimatedMonthlyCost` from bucket analysis only — the `bucket_info["EstimatedMonthlyCost"]` is the **current monthly storage cost**, NOT the projected savings.

**Verdict: ⚠️ WARN** — `total_monthly_savings` in `ServiceFindings` contains the **current monthly cost** of all analyzed buckets, not the delta savings from optimization.

---

## 2. Pricing Validation (eu-west-1)

### AWS Pricing API Results (live, 2026-04-27 publication)

| Storage Class | Adapter Constant | API Price (first 50TB) | Delta |
|---------------|-----------------|----------------------|-------|
| General Purpose (STANDARD) | $0.0230 | $0.0230 | ✅ Exact match |
| Infrequent Access (STANDARD_IA) | $0.0125 | $0.0125 | ✅ Exact match |
| One Zone IA (ONEZONE_IA) | $0.0100 | $0.0100 | ✅ Exact match |
| Intelligent-Tiering FA | $0.0230 | $0.0230 | ✅ Exact match |
| Deep Archive | $0.00099 | $0.00099 | ✅ Exact match |

### Intelligent-Tiering Tier Breakdown (from live API)

| Tier | API $/GB/month | In `S3_STORAGE_COSTS`? |
|------|---------------|-------------|
| Frequent Access | $0.0230 | ✅ `INTELLIGENT_TIERING: 0.023` |
| Infrequent Access | $0.0125 | ❌ Not in dict |
| Archive Instant Access | $0.0040 | ❌ Not in dict |
| Archive Access | $0.0036 | ❌ Not in dict |
| Deep Archive Access | $0.00099 | ❌ Not in dict |

### Pricing Engine S3 Fetch (`pricing_engine.py:397-412`)

The `_fetch_s3_price()` method maps storage class keys to AWS API `storageClass` filter values:

```python
storage_class_map = {
    "STANDARD": "General Purpose",
    "STANDARD_IA": "Infrequent Access",
    "ONEZONE_IA": "One Zone - Infrequent Access",
    "GLACIER_IR": "Amazon Glacier Instant Retrieval",
    "GLACIER": "Amazon Glacier Flexible Retrieval",
    "DEEP_ARCHIVE": "Amazon Glacier Deep Archive",
    "INTELLIGENT_TIERING": "Intelligent-Tiering",
}
```

API filter `"Intelligent-Tiering"` returns the Frequent Access tier ($0.023). The engine picks the first price dimension from the first result (0–50TB tier).

**S3 tiered pricing (50TB, 500TB+ breakpoints):** The pricing engine returns only the first price dimension (0–50TB). For buckets storing >50TB, costs are underestimated by ~4-9%.

**Verdict: ✅ PASS** — Base pricing constants match live API within first 50TB tier.

---

## 3. Intelligent-Tiering Monitoring Charge

### Constant Definition

```python
# services/s3.py:25
S3_INTELLIGENT_TIERING_MONITORING_FEE: float = 0.0025  # $0.0025 per 1,000 objects/month
```

### Where Applied

In `_estimate_s3_bucket_cost()` (`s3.py:530-533`):

```python
if cost_key == "INTELLIGENT_TIERING":
    estimated_objects = class_size_gb * 1000
    monitoring_fee = (estimated_objects / 1000) * S3_INTELLIGENT_TIERING_MONITORING_FEE
    storage_cost += monitoring_fee
```

### Issues Found

| Issue | Severity | Detail |
|-------|----------|--------|
| Object count estimation | ⚠️ WARN | Uses `class_size_gb * 1000` — assumes average object size of 1MB. Real-world average varies wildly (KB to GB). Should use S3 Inventory or `list_objects_v2` for actual counts. |
| Only in secondary path | ⚠️ WARN | Monitoring fee is NOT applied in `_calculate_s3_storage_cost()` (`s3.py:443`), which is the primary function called from bucket analysis. When `get_s3_bucket_analysis()` estimates costs, monitoring fees are **not included**. |
| Not in enhanced checks | 🔵 INFO | Enhanced checks recommending Intelligent-Tiering do not factor monitoring fees into cost-benefit analysis. |

**Verdict: ⚠️ WARN** — Monitoring fee constant ($0.0025/1000 objects) correctly matches AWS pricing, but **not applied** in the primary cost estimation path used by bucket analysis.

---

## 4. Lifecycle Savings Calculation

### How Savings Are Computed

The adapter does **not** compute lifecycle transition savings. Instead:

1. `get_s3_bucket_analysis()` calculates `EstimatedMonthlyCost` as the **current total monthly storage cost** (all classes at Standard pricing)
2. `buckets_without_lifecycle` and `buckets_without_intelligent_tiering` are identified as flag lists
3. No target-class price is calculated — no `current - target` delta is computed

### What's Missing

| Component | Status | Impact |
|-----------|--------|--------|
| Current class breakdown cost | ✅ Computed | `_estimate_s3_bucket_cost()` uses CloudWatch metrics per storage class |
| Target class recommendation | ❌ Missing | No recommendation of which cheaper class to transition to |
| Per-GB savings delta | ❌ Missing | `current_class_price - target_class_price` per GB never calculated |
| Transition timing estimate | ❌ Missing | No "transition after N days" recommendation |
| Minimum storage duration penalty | ❌ Missing | Standard-IA has 30-day minimum; Glacier has 90-day — not modeled |

### Legacy `_SC_MAP` Key Bridge

```python
# services/s3.py:440
_SC_MAP = {"GLACIER_FLEXIBLE_RETRIEVAL": "GLACIER", "GLACIER_INSTANT_RETRIEVAL": "GLACIER_IR"}
```

Correctly bridges `S3_STORAGE_COSTS` keys (e.g., `GLACIER_FLEXIBLE_RETRIEVAL`) to pricing engine keys (e.g., `GLACIER`) when calling `ctx.pricing_engine.get_s3_monthly_price_per_gb()`.

**Verdict: ❌ FAIL** — No lifecycle transition savings calculated. `EstimatedMonthlyCost` contains current cost, not projected savings. Buckets are flagged but dollar benefit is not quantified.

---

## 5. Versioning — Non-Current Version Storage

### What the Adapter Does

In `get_enhanced_s3_checks()` (`s3.py:849-861`):

```python
versioning_response = bucket_s3_client.get_bucket_versioning(Bucket=bucket_name)
if versioning_response.get("Status") == "Enabled":
    checks["versioning_growth"].append({
        "BucketName": bucket_name,
        "VersioningStatus": "Enabled",
        "Recommendation": "Monitor versioning growth and configure lifecycle for old versions",
        "CheckCategory": "Versioning Optimization",
    })
```

### Issues Found

| Issue | Severity | Detail |
|-------|----------|--------|
| Non-current versions not measured | ❌ FAIL | Does NOT query `list_object_versions()` or CloudWatch `NonCurrentVersionStorage` metric |
| No cost attribution | ❌ FAIL | No `EstimatedMonthlyCost` — $0 contribution to total savings |
| Generic recommendation | ⚠️ WARN | No specific `NoncurrentVersionTransition` lifecycle rule generated |
| Version count/size unknown | ⚠️ WARN | Cannot assess severity without non-current version data |

### What Should Happen

Per AWS docs, versioned buckets should:
1. Query `BucketSizeBytes` with `StorageType=NonCurrentVersionStorage` for non-current GB
2. Calculate cost of non-current versions at Standard rate
3. Recommend `NoncurrentVersionTransition` lifecycle rule (e.g., IA after 30 days, Glacier after 90)
4. Estimate savings: `non_current_gb × (STANDARD_price - target_price)`

**Verdict: ❌ FAIL** — Non-current version storage not measured or costed. Versioning recommendations carry $0 savings.

---

## 6. Documentation Cross-Reference

### AWS S3 Cost Optimization Best Practices (from AWS docs search)

| Best Practice | Adapter Coverage |
|---------------|-----------------|
| Enable S3 Intelligent-Tiering for unknown/changing access | ✅ Detected and recommended |
| Configure S3 Lifecycle policies | ✅ Detected if missing |
| Use S3 Storage Class Analysis | ❌ Not integrated |
| Monitor and clean incomplete multipart uploads | ✅ Detected |
| Transition to Standard-IA after 30 days | ❌ No transition timing recommended |
| Transition to Glacier after 90 days | ❌ No transition timing recommended |
| Configure non-current version lifecycle | ⚠️ Generic recommendation only |
| Use S3 Batch Operations for bulk transitions | ❌ Not mentioned |
| Monitor with S3 Storage Lens | 🔵 Mentioned in descriptions only |
| Consider S3 Object Lock costs | ❌ Not checked |

### S3 Express One Zone

`S3_STORAGE_COSTS` includes `EXPRESS_ONE_ZONE: 0.11` but the CloudWatch metric loop (`s3.py:480-487`) does not include `ExpressOneZoneStorage`. Express One Zone uses a different metrics namespace — these buckets are skipped.

**Verdict: ⚠️ WARN** — Core recommendations present but missing quantitative lifecycle transition advice and Storage Class Analysis integration.

---

## 7. Pass Criteria Assessment

| # | Criterion | Verdict | Notes |
|---|-----------|---------|-------|
| 1 | `EstimatedMonthlyCost` correctly populated | ⚠️ WARN | Populated but contains current cost, not savings delta |
| 2 | Pricing constants match live AWS API | ✅ PASS | All 5 checked classes match exactly (eu-west-1, first 50TB tier) |
| 3 | Intelligent-Tiering monitoring fee accounted | ⚠️ WARN | Constant correct, but only applied in secondary cost path |
| 4 | Lifecycle savings = `current - target` per GB | ❌ FAIL | No target class pricing or per-GB delta calculated |
| 5 | Non-current version storage included in savings | ❌ FAIL | Not measured, not costed |
| 6 | `total_monthly_savings` reflects projected savings | ❌ FAIL | Contains current storage cost, not optimization savings |
| 7 | Tiered pricing (>50TB, >500TB) modeled | ⚠️ WARN | Not modeled; flat first-tier pricing only |
| 8 | Pricing engine fallback graceful | ✅ PASS | Falls back to hardcoded constants with regional multiplier |
| 9 | `_SC_MAP` key alignment | ✅ PASS | `GLACIER_FLEXIBLE_RETRIEVAL → GLACIER` mapping present |
| 10 | S3 Express One Zone coverage | ⚠️ WARN | In constants but not in CloudWatch metric queries |

---

## 8. Issues Summary

### Critical (❌ FAIL) — 3 issues

| ID | Location | Description |
|----|----------|-------------|
| S3-F001 | `services/adapters/s3.py:42` | `total_monthly_savings` sums `EstimatedMonthlyCost` which contains **current monthly cost** of all analyzed buckets, not optimization savings. Any bucket with cost > $0 inflates reported savings. |
| S3-F002 | `services/s3.py:562-761` | No lifecycle transition savings calculation. Buckets flagged as needing lifecycle policies but no `$/GB/month` savings from Standard→IA→Glacier transitions quantified. |
| S3-F003 | `services/s3.py:849-861` | Non-current version storage not measured (no CloudWatch `NonCurrentVersionStorage` query). Carries $0 savings estimate. |

### Warnings (⚠️ WARN) — 5 issues

| ID | Location | Description |
|----|----------|-------------|
| S3-W001 | `services/s3.py:531` | Object count estimation `class_size_gb * 1000` (assumes 1MB avg). Should use S3 Inventory or `list_objects` for actual count. |
| S3-W002 | `services/s3.py:443-458` | Intelligent-Tiering monitoring fee not applied in `_calculate_s3_storage_cost()` — the primary cost function called from bucket analysis. |
| S3-W003 | `core/pricing_engine.py:397-412` | Tiered pricing (>50TB, >500TB breakpoints) not modeled. Flat first-tier price returned. Large buckets underestimated by ~4-9%. |
| S3-W004 | `services/s3.py:480-487` | S3 Express One Zone not included in CloudWatch `storage_classes` list — Express buckets skipped in cost analysis. |
| S3-W005 | `services/s3.py:840-847` | Enhanced check `lifecycle_missing` entries set `EstimatedMonthlyCost: 0` — no savings quantification for missing lifecycle policies. |

### Informational (🔵 INFO) — 2 items

| ID | Location | Description |
|----|----------|-------------|
| S3-I001 | `services/s3.py:14-23` | `S3_STORAGE_COSTS` dict uses `GLACIER_FLEXIBLE_RETRIEVAL` / `GLACIER_INSTANT_RETRIEVAL` keys; CloudWatch uses `GlacierStorage`. The `_SC_MAP` bridge handles this correctly. |
| S3-I002 | `services/s3.py:625-638` | Fast mode samples only first 100 objects via `MaxKeys=100` — size estimate can be significantly understated for large buckets. Warning printed but no flag in output. |

---

## 9. Verdict

| Category | Result |
|----------|--------|
| **Overall** | ⚠️ **CONDITIONAL PASS** |
| Pricing accuracy | ✅ PASS |
| Savings calculation | ❌ FAIL |
| Intelligent-Tiering | ⚠️ WARN |
| Versioning | ❌ FAIL |
| Documentation alignment | ⚠️ WARN |

### Summary

The S3 adapter correctly identifies optimization opportunities (missing lifecycle policies, missing Intelligent-Tiering, incomplete multipart uploads, versioned buckets) and its pricing constants match live AWS API data exactly. However, **the `total_monthly_savings` field does not contain projected savings** — it sums the current monthly storage cost of all analyzed buckets. No `current_class_price - target_class_price` delta is calculated for lifecycle or Intelligent-Tiering transitions. Non-current version storage is detected but not measured or costed.

### Recommendations for Fix

1. **Fix `total_monthly_savings`** to sum `(current_cost - projected_cost_with_optimization)` per bucket
2. **Add lifecycle transition savings**: `size_gb × (STANDARD_price - target_class_price)` for buckets without lifecycle
3. **Query `NonCurrentVersionStorage`** CloudWatch metric for versioned buckets
4. **Apply Intelligent-Tiering monitoring fee** in `_calculate_s3_storage_cost()`, not just `_estimate_s3_bucket_cost()`

---

## Appendix: Pricing Reference (eu-west-1, live API 2026-04-27)

| Storage Class | Price/GB/Month |
|---------------|---------------|
| Standard (General Purpose) | $0.023 |
| Standard (50-500TB) | $0.022 |
| Standard (500TB+) | $0.021 |
| Standard-IA | $0.0125 |
| One Zone-IA | $0.010 |
| Intelligent-Tiering (Frequent) | $0.023 |
| Intelligent-Tiering (Infrequent) | $0.0125 |
| Intelligent-Tiering (Archive Instant) | $0.004 |
| Intelligent-Tiering (Archive) | $0.0036 |
| Intelligent-Tiering (Deep Archive) | $0.00099 |
| **IT Monitoring Fee** | **$0.0025/1,000 objects/month** |

---

*Report generated by opencode agent for AWS Cost Optimization Scanner*
