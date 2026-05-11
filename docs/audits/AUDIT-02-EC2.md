# AUDIT-02: EC2 Service Adapter

| Field | Value |
|-------|-------|
| **Adapter** | `services/adapters/ec2.py` (71 lines) |
| **Supporting Modules** | `services/advisor.py`, `services/ec2.py`, `services/_savings.py` |
| **Audit Date** | 2026-05-01 |
| **Auditor** | Automated audit agent |

---

## 1. Code Analysis

### 1.1 Cost Hub Recommendation Merging

**Verdict: ✅ PASS**

The adapter correctly retrieves Cost Hub recommendations from the pre-split cache:

```python
cost_hub_recs = ctx.cost_hub_splits.get("ec2", [])
```

The `ScanOrchestrator` (`core/scan_orchestrator.py:49-67`) pre-fetches all Cost Hub recommendations via `get_detailed_cost_hub_recommendations()` and splits them by `currentResourceType` using a `type_map` that maps `Ec2Instance` → `"ec2"`. This is architecturally sound — the split happens once before any adapter runs, avoiding N+1 API calls.

Savings aggregation uses `rec.get("estimatedMonthlySavings", 0)` which matches the Cost Optimization Hub API response shape (flat numeric field at top level).

### 1.2 Compute Optimizer Savings Extraction

**Verdict: ❌ FAIL — Silent $0 savings from Compute Optimizer**

The adapter reads Compute Optimizer savings as:

```python
co_total = sum(rec.get("estimatedMonthlySavings", 0) for rec in co_recs)
```

However, the AWS Compute Optimizer API (`get_ec2_instance_recommendations`) returns savings in a **nested** structure:

```
instanceRecommendationOptions[].savingsOpportunity.estimatedMonthlySavings.value
```

This is confirmed by `reporter_phase_b.py:336` which correctly accesses the nested path:

```python
savings = option.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0)
```

The adapter reads `estimatedMonthlySavings` from the **top-level** recommendation dict where it **does not exist** as a flat field. This means `co_total` will **always be 0.0** for real Compute Optimizer responses — the savings from Compute Optimizer are silently lost.

**Impact**: High. All Compute Optimizer EC2 rightsizing savings ($0 in the adapter) are dropped from the reported `total_monthly_savings`.

**Fix**: Extract savings from `rec["instanceRecommendationOptions"][0]["savingsOpportunity"]["estimatedMonthlySavings"]["value"]` or add a helper in `advisor.py` that flattens the CO response.

### 1.3 parse_dollar_savings() Usage

**Verdict: ⚠️ WARN — Returns 0.0 for text-based savings estimates**

`parse_dollar_savings()` (`services/_savings.py:8-16`) extracts dollar amounts from strings like `"$12.50/month"` using regex `r"\$(\d+\.?\d*)"`. This is used for `enhanced_recs` and `advanced_recs`.

**Problem**: Many `EstimatedSavings` strings in `services/ec2.py` do **not** contain a dollar amount:

| EstimatedSavings String | parse_dollar_savings() Result |
|---|---|
| `"Up to 100% if terminated, 50-70% if downsized"` | `0.0` |
| `"30-50% cost reduction with proper rightsizing"` | `0.0` |
| `"Potential cost reduction with better performance per dollar"` | `0.0` |
| `"Significant cost reduction if shared tenancy acceptable"` | `0.0` |
| `"EBS storage costs only (compute already saved)"` | `0.0` |
| `"CloudWatch detailed monitoring required for assessment"` | `0.0` |
| `"80-95% cost reduction vs always-on EC2"` | `0.0` |
| `"60-90% cost reduction with Spot instances in Batch"` | `0.0` |

Only one advanced check produces a parseable dollar amount:

```python
"EstimatedSavings": f"${(size_gb - 20) * 0.10:.2f}/month if reduced to 20GB + separate data volume"
```

**Impact**: Medium. The `enhanced_recs` and `advanced_recs` contributions to `total_monthly_savings` are almost entirely $0.00, making the savings total undercount for these two sources.

### 1.4 Graviton Filtering

**Verdict: ⚠️ WARN — Graviton filtered at HTML layer only, not adapter layer**

The adapter does **not** filter `MigrateToGraviton` recommendations. Graviton filtering happens exclusively in `html_report_generator.py:1635-1674` via `_filter_recommendations()`:

```python
filtered_recs = [
    rec for rec in original_recs
    if isinstance(rec, dict) and rec.get("actionType") != "MigrateToGraviton"
]
```

This means:
- **JSON output** includes Graviton recommendations and their savings in `total_monthly_savings`
- **HTML report** excludes Graviton recommendations and subtracts their savings

**Impact**: Low-Medium. JSON and HTML outputs report different total savings for EC2. The Graviton savings are included in JSON but excluded from HTML. This is inconsistent but documented as intentional (Graviton requires recompilation/re-architecture, not a simple config change).

**Recommendation**: Either (a) document this discrepancy clearly, or (b) add a `--include-graviton` flag so both outputs are consistent.

### 1.5 Double-Counting Prevention

**Verdict: ❌ FAIL — No deduplication between sources**

The adapter sums savings from four independent sources without any deduplication:

```python
savings += sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)  # Source 1
savings += co_total  # Source 2 (Compute Optimizer)
savings += parse_dollar_savings(rec.get("EstimatedSavings", "")) for enhanced  # Source 3
savings += parse_dollar_savings(rec.get("EstimatedSavings", "")) for advanced  # Source 4
```

A search for "dedup", "deduplic", "double.count", "overlap", "duplicate" across `services/` found **zero** deduplication logic in the adapter layer or the orchestrator.

**Overlap scenario**: Cost Optimization Hub and Compute Optimizer **frequently recommend the same instance for rightsizing**. Per AWS documentation:

> "By default, Compute Optimizer uses pricing discounts in savings estimates for management accounts and delegated administrators with Cost Optimization Hub enabled." ([source](https://docs.aws.amazon.com/compute-optimizer/latest/ug/savings-estimation-mode.html))

Both services analyze the same EC2 fleet. If instance `i-abc123` is flagged by Cost Hub as "Rightsizing" with $15/mo savings AND by Compute Optimizer as "Under-provisioned" with $12/mo savings, the adapter adds both ($27), **double-counting** the same instance.

Additionally, `enhanced_checks` (CloudWatch CPU-based idle/rightsizing) may flag the same instance that Compute Optimizer flags, triple-counting.

**Impact**: High. Total EC2 savings can be inflated by 1.5-3x for accounts with Cost Hub + Compute Optimizer enabled. The HTML report does not correct for this.

---

## 2. Pricing Validation

**Verdict: ✅ PASS**

AWS Pricing API query for `m5.large` On-Demand Linux in `eu-west-1`:

| Attribute | Value |
|---|---|
| **Instance Type** | m5.large |
| **Region** | EU (Ireland) |
| **On-Demand Price** | $0.107/hour |
| **Monthly (730h)** | $78.11 |
| **vCPU** | 2 |
| **Memory** | 8 GiB |
| **Processor** | Intel Xeon Platinum 8175 |
| **Storage** | EBS only |
| **Network** | Up to 10 Gigabit |
| **Effective Date** | 2026-04-01 |
| **Pricing Data Version** | 20260429185429 |

The `PricingEngine.get_ec2_hourly_price("m5.large")` method queries the same API. Test expects $0.096/hour (us-east-1 pricing). The eu-west-1 price of $0.107/hour is ~11.5% higher, consistent with EU regional pricing.

The adapter does **not** directly call `PricingEngine` — it relies on Cost Hub's `estimatedMonthlySavings` (which uses live pricing internally) and the percentage-based text estimates from enhanced/advanced checks. The `pricing_multiplier` from `ScanContext` is passed to `get_enhanced_ec2_checks()` and `get_advanced_ec2_checks()` but is **never used** inside those functions.

---

## 3. RI Savings

**Verdict: ✅ PASS (with caveat)**

The adapter passes Cost Hub's `estimatedMonthlySavings` through directly — no re-computation. Cost Optimization Hub internally considers existing RI/Savings Plans coverage when computing estimated savings (per AWS docs on savings estimation mode). The adapter does not modify these values.

**Caveat**: The Compute Optimizer savings are silently $0 (see §1.2), so any RI-based savings from that source are lost. For the Cost Hub path, RI savings pass through correctly.

---

## 4. Graviton Filtering

**Verdict: 🔵 INFO**

See §1.4. Graviton recommendations (`actionType == "MigrateToGraviton"`) from Cost Optimization Hub:

1. **Included** in the adapter's `total_monthly_savings` and `total_recommendations`
2. **Filtered out** in the HTML report layer (`html_report_generator.py:1635-1674`)
3. **Present** in JSON output
4. Graviton savings are **subtracted** from the HTML total via:
   ```python
   filtered_data["total_monthly_savings"] = max(
       0, filtered_data.get("total_monthly_savings", 0) - graviton_savings
   )
   ```

The EC2 adapter itself has no Graviton-specific logic. Other services (ElastiCache, OpenSearch, Lambda, Batch) have Graviton migration checks at the service module level.

---

## 5. Documentation

**Verdict: ✅ PASS**

- Adapter docstring (`ec2.py:26-38`) accurately describes the multi-source strategy
- `advisor.py` has clear module docstring explaining its consolidation role
- `services/adapters/CLAUDE.md` documents the EC2 adapter's pricing strategy
- `README.md` correctly lists EC2 optimizations: idle instances (CloudWatch), rightsizing, previous-gen, burstable credits, stopped instances
- IAM policy includes both `cost-optimization-hub:ListRecommendations` and `compute-optimizer:GetEC2InstanceRecommendations`

---

## 6. Pass Criteria Checklist

| # | Criterion | Verdict | Notes |
|---|-----------|---------|-------|
| 1 | Cost Hub recs correctly merged from pre-split cache | ✅ PASS | `cost_hub_splits.get("ec2", [])` |
| 2 | Compute Optimizer savings correctly extracted | ❌ FAIL | Nested path not traversed — always $0 |
| 3 | `parse_dollar_savings()` handles all EstimatedSavings formats | ⚠️ WARN | 90%+ of strings contain no `$` amount |
| 4 | Graviton filtering at adapter level | ⚠️ WARN | HTML-only, not adapter-level |
| 5 | Double-counting prevented between Cost Hub and Compute Optimizer | ❌ FAIL | No dedup logic exists |
| 6 | Double-counting prevented between Cost Hub and enhanced checks | ❌ FAIL | No dedup logic exists |
| 7 | RI savings passed through correctly (not re-computed) | ✅ PASS | Direct passthrough |
| 8 | Pricing API values match live AWS prices | ✅ PASS | m5.large $0.107/hr eu-west-1 verified |
| 9 | `pricing_multiplier` used for regional adjustment | ⚠️ WARN | Passed but unused in enhanced/advanced |
| 10 | Error handling graceful (no crashes on API failures) | ✅ PASS | All calls wrapped in try/except |
| 11 | Pagination handled for large fleets | ✅ PASS | `get_paginator("describe_instances")` used |
| 12 | Fast mode respected for CloudWatch queries | ✅ PASS | `fast_mode` param passed (but not checked in enhanced) |

---

## 7. Issues

### Critical

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| EC2-001 | Compute Optimizer savings silently $0 due to nested field mismatch | `ec2.py:50`, `advisor.py:66-93` | All CO rightsizing savings lost |
| EC2-002 | No dedup between Cost Hub, Compute Optimizer, and enhanced checks | `ec2.py:48-56` | Savings inflated 1.5-3x |

### Warning

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| EC2-003 | `parse_dollar_savings()` returns 0.0 for 90%+ of EstimatedSavings strings | `ec2.py:53-56`, `_savings.py:15` | Enhanced/advanced savings undercounted |
| EC2-004 | Graviton filtered in HTML only, not JSON | `html_report_generator.py:1635` | Inconsistent JSON vs HTML totals |
| EC2-005 | `pricing_multiplier` passed but unused in enhanced/advanced checks | `ec2.py:43-45` | Regional pricing not applied |
| EC2-006 | `fast_mode` param passed to `get_advanced_ec2_checks()` but not checked before CloudWatch calls | `ec2.py:45`, `services/ec2.py:556-560` | CloudWatch queries run even in fast mode |

### Info

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| EC2-007 | Compute Optimizer OptInRequiredException generates synthetic rec with text savings | `advisor.py:82-92` | `$0` contribution to total, informational only |
| EC2-008 | `get_compute_optimizer_recommendations()` in `ec2.py:394-404` is a dead-code re-export | `services/ec2.py:394-404` | No callers (adapter uses `advisor.py` directly) |

---

## 8. Verdict

| Category | Verdict |
|----------|---------|
| **Overall** | **❌ FAIL — 2 critical issues** |
| Cost Hub Integration | ✅ PASS |
| Compute Optimizer Integration | ❌ FAIL |
| Savings Accuracy | ❌ FAIL |
| Graviton Handling | ⚠️ WARN |
| Pricing Validation | ✅ PASS |
| Documentation | ✅ PASS |
| Error Handling | ✅ PASS |

### Summary

The EC2 adapter's architecture (multi-source with 4 independent check sources) is sound, but two critical bugs undermine its accuracy:

1. **EC2-001**: Compute Optimizer savings are structurally inaccessible due to a field path mismatch (`estimatedMonthlySavings` read from top level instead of nested `savingsOpportunity.estimatedMonthlySavings.value`). This means **all Compute Optimizer EC2 rightsizing savings are reported as $0**.

2. **EC2-002**: No deduplication exists between the four recommendation sources. The same instance can be counted 2-3 times across Cost Hub, Compute Optimizer, and enhanced checks, inflating reported savings by 1.5-3x.

The combined effect is paradoxical: actual Compute Optimizer savings are silently dropped (undercount), while overlap between Cost Hub and enhanced checks inflates the remaining total (overcount). The net error direction depends on the specific AWS account's recommendation mix.

### Recommended Priority

1. Fix EC2-001 (nested field access) — one-line fix, immediate impact
2. Implement instance-level dedup (EC2-002) — requires InstanceId-based set intersection
3. Add dollar amounts to EstimatedSavings strings (EC2-003) or switch to PricingEngine lookups
4. Unify Graviton filtering between JSON and HTML (EC2-004)
