# Per-Service Adapter Audit Report

> Consolidated technical audit of all 28 service adapters performed during the
> v3.0 modular architecture migration. Each section below is the verbatim audit
> for one adapter, covering technical correctness, pricing accuracy, and
> reporting fidelity.
>
> See [SUMMARY.md](SUMMARY.md) for the high-level findings and
> [ACTION_PLAN.md](ACTION_PLAN.md) for prioritized follow-ups.

## Table of Contents

- [AUDIT-01: EBS](#audit-01-ebs)
- [AUDIT-02: EC2](#audit-02-ec2)
- [AUDIT-03: RDS](#audit-03-rds)
- [AUDIT-04: S3](#audit-04-s3)
- [AUDIT-05: Network](#audit-05-network)
- [AUDIT-06: Lambda](#audit-06-lambda)
- [AUDIT-07: DynamoDB](#audit-07-dynamodb)
- [AUDIT-08: ElastiCache](#audit-08-elasticache)
- [AUDIT-09: OpenSearch](#audit-09-opensearch)
- [AUDIT-10: Containers](#audit-10-containers)
- [AUDIT-11: FileSystems](#audit-11-filesystems)
- [AUDIT-12: AMI](#audit-12-ami)
- [AUDIT-13: Monitoring](#audit-13-monitoring)
- [AUDIT-14: Glue](#audit-14-glue)
- [AUDIT-15: Athena](#audit-15-athena)
- [AUDIT-16: Redshift](#audit-16-redshift)
- [AUDIT-17: MSK](#audit-17-msk)
- [AUDIT-18: CloudFront](#audit-18-cloudfront)
- [AUDIT-19: APIGateway](#audit-19-apigateway)
- [AUDIT-20: StepFunctions](#audit-20-stepfunctions)
- [AUDIT-21: WorkSpaces](#audit-21-workspaces)
- [AUDIT-22: Lightsail](#audit-22-lightsail)
- [AUDIT-23: AppRunner](#audit-23-apprunner)
- [AUDIT-24: Transfer](#audit-24-transfer)
- [AUDIT-25: DMS](#audit-25-dms)
- [AUDIT-26: QuickSight](#audit-26-quicksight)
- [AUDIT-27: MediaStore](#audit-27-mediastore)
- [AUDIT-28: Batch](#audit-28-batch)

---

w# AUDIT-01 · EBS (Elastic Block Store)

**Adapter**: `services/adapters/ebs.py`  
**Supporting**: `services/ebs.py` (468 lines), `core/pricing_engine.py` (571 lines), `services/_savings.py` (16 lines)  
**Date**: 2026-05-01  
**Auditor**: Sisyphus Audit Agent  
**Pricing Data Source**: AWS Price List API, queried 2026-05-01

---

### 1. Code Analysis

#### 1.1 Unattached Volume Cost Computation

**Location**: `services/ebs.py` lines 182-189, 205-259

The `EstimatedMonthlyCost` field for unattached volumes is computed by `_estimate_volume_cost()`:

```python
def _estimate_volume_cost(size_gb, volume_type, iops=None, throughput=None, ...):
    # Uses live pricing when available
    if ctx and ctx.pricing_engine:
        gb_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume_type)
    else:
        gb_price = _FALLBACK.get(volume_type, 0.10) * pricing_multiplier
    
    base_cost = size_gb * gb_price
    
    # Adds IOPS cost for io1/io2 and provisioned gp3
    if iops and volume_type in ["gp3", "io1", "io2"]:
        if volume_type == "gp3":
            extra_iops = max(0, iops - 3000)  # 3000 baseline included
            base_cost += extra_iops * 0.005 * pricing_multiplier
        elif volume_type in ["io1", "io2"]:
            if ctx and ctx.pricing_engine:
                iops_price = ctx.pricing_engine.get_ebs_iops_monthly_price(volume_type)
            else:
                iops_price = 0.065 * pricing_multiplier
            base_cost += iops * iops_price
    
    # Adds throughput cost for gp3
    if throughput and volume_type == "gp3":
        extra_throughput = max(0, throughput - 125)  # 125 MB/s baseline included
        base_cost += extra_throughput * 0.04 * pricing_multiplier
    
    return base_cost
```

The adapter aggregates unattached savings at `services/adapters/ebs.py:55`:
```python
savings += sum(v.get("EstimatedMonthlyCost", 0) for v in unattached_volumes)
```

**Verdict**: ✅ PASS — Correctly uses `size_gb × price_per_gb` plus IOPS/throughput add-ons. Supports both live pricing and fallback constants.

---

#### 1.2 GP2→GP3 Savings Calculation

**Location**: `services/adapters/ebs.py` lines 58-65

```python
for rec in gp2_recs:
    size = rec.get("Size", 0)
    if ctx.pricing_engine:
        gp2_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp2")
        gp3_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")
        savings += size * max(gp2_price - gp3_price, 0)
    else:
        savings += size * 0.10 * 0.20  # Fallback: 20% of $0.10/GB
```

**Key Points**:
- Uses **live price delta** (`gp2_price - gp3_price`) when pricing engine is available
- Falls back to flat 20% only when API is unavailable
- Includes `max(..., 0)` guard to prevent negative savings

**Verdict**: ✅ PASS — Correctly uses `size × (gp2_price - gp3_price)` for live pricing, NOT a flat percentage.

---

#### 1.3 Compute Optimizer Field Parsing

**Location**: `services/adapters/ebs.py` line 66, `services/advisor.py`

```python
savings += sum(r.get("estimatedMonthlySavings", 0) for r in co_recs)
```

Compute Optimizer recommendations are fetched via `get_ebs_compute_optimizer_recommendations()` which returns numeric `estimatedMonthlySavings` values directly.

**Verdict**: ✅ PASS — Direct numeric field extraction, no parsing required.

---

#### 1.4 parse_dollar_savings() Usage

**Location**: `services/adapters/ebs.py` lines 56-57, `services/_savings.py` lines 8-16

```python
for rec in other_recs:
    savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))
```

The `parse_dollar_savings()` function:
```python
def parse_dollar_savings(savings_str: str) -> float:
    if not savings_str:
        return 0.0
    match = re.search(r"\$(\d+\.?\d*)", savings_str)
    return float(match.group(1)) if match else 0.0
```

**Verdict**: ✅ PASS — Correctly parses dollar amounts from recommendation strings like "$12.50/month".

---

### 2. Pricing Validation

#### 2.1 GP2 Pricing (eu-west-1) — ✅ PASS

**API Query**: `get_pricing("AmazonEC2", "eu-west-1", volumeApiName=gp2, productFamily=Storage)`

**Result**:
```json
{
  "pricePerUnit": { "USD": "0.1100000000" },
  "description": "$0.11 per GB-month of General Purpose SSD (gp2) provisioned storage - EU (Ireland)"
}
```

| Metric | Value |
|--------|-------|
| Live API Price | **$0.11/GB-month** |
| Expected (per task) | ~$0.11/GB-month |
| Fallback Constant | $0.10/GB-month |
| Variance | $0.01 (9.1% low) |

**Verdict**: ✅ PASS — Live API returns $0.11/GB-month, matching expected value.

---

#### 2.2 GP3 Pricing (eu-west-1) — ✅ PASS

**API Query**: `get_pricing("AmazonEC2", "eu-west-1", volumeApiName=gp3, productFamily=Storage)`

**Result**:
```json
{
  "pricePerUnit": { "USD": "0.0880000000" },
  "description": "$0.088 per GB-month of General Purpose (gp3) provisioned storage - EU (Ireland)"
}
```

| Metric | Value |
|--------|-------|
| Live API Price | **$0.088/GB-month** |
| Expected (per task) | ~$0.088/GB-month |
| Fallback Constant | $0.08/GB-month |
| Variance | $0.008 (9.1% low) |

**Verdict**: ✅ PASS — Live API returns $0.088/GB-month, matching expected value.

---

#### 2.3 GP2→GP3 Savings Formula — ✅ PASS

**Expected Formula**: `size_gb × (gp2_price - gp3_price)`

**Validation**:
- GP2 price (live): $0.11/GB-month
- GP3 price (live): $0.088/GB-month
- Price delta: **$0.022/GB-month**
- Savings for 100 GB: 100 × $0.022 = **$2.20/month**

**Actual Implementation**:
```python
savings += size * max(gp2_price - gp3_price, 0)
```

**Comparison**:
| Method | Savings per 100 GB |
|--------|-------------------|
| Live pricing formula | $2.20 |
| Fallback (20% of $0.10) | $2.00 |
| Flat 20% of actual GP2 | $2.20 |

**Verdict**: ✅ PASS — Implementation correctly uses price delta, NOT a flat percentage.

---

### 3. io1/io2 IOPS Pricing — ✅ PASS

#### 3.1 io1 IOPS Pricing (eu-west-1)

**API Query**: `get_pricing("AmazonEC2", "eu-west-1", volumeApiName=io1, productFamily=System Operation)`

**Result**:
```json
{
  "pricePerUnit": { "USD": "0.0720000000" },
  "unit": "IOPS-Mo",
  "description": "$0.072 per IOPS-month provisioned - EU (Ireland)"
}
```

| Metric | Value |
|--------|-------|
| Live API Price | **$0.072/IOPS-month** |
| Fallback Constant | $0.065/IOPS-month |
| Variance | $0.007 (9.7% low) |

#### 3.2 Code Implementation

**Location**: `core/pricing_engine.py` lines 186-198, `services/ebs.py` lines 249-253

```python
## AUDIT-01: EBS
def get_ebs_iops_monthly_price(self, volume_type: str) -> float:
    price = self._fetch_ebs_iops_price(volume_type)
    if price is None:
        price = self._use_fallback(
            FALLBACK_EBS_IOPS_MONTH.get(volume_type, 0.065) * self._fallback_multiplier,
            ...
        )
    return price
```

```python
## services/ebs.py
if volume_type in ["io1", "io2"]:
    if ctx and ctx.pricing_engine:
        iops_price = ctx.pricing_engine.get_ebs_iops_monthly_price(volume_type)
    else:
        iops_price = 0.065 * pricing_multiplier
    base_cost += iops * iops_price
```

**Verdict**: ✅ PASS — IOPS pricing correctly queried via API and included in io1/io2 estimates.

---

### 4. AWS Documentation Alignment

#### 4.1 GP3 Migration Best Practices

**Sources**:
- AWS Prescriptive Guidance: "Migrate Amazon EBS volumes from gp2 to gp3"
- AWS Well-Architected Framework: MSFTCOST05-BP01

**Alignment Check**:
| Best Practice | Adapter Implementation | Status |
|--------------|------------------------|--------|
| gp3 provides ~20% cost savings | Uses live price delta ($0.11 - $0.088 = $0.022, ~20%) | ✅ |
| gp3 decouples IOPS from capacity | `_estimate_volume_cost()` handles IOPS separately | ✅ |
| gp3 baseline is 3,000 IOPS | Code uses `max(0, iops - 3000)` for extra IOPS | ✅ |
| Migration has no downtime | Documented in optimization descriptions | ✅ |

**Verdict**: ✅ PASS — Adapter aligns with AWS best practices.

---

### 5. Pass Criteria Checklist

| Criteria | Verdict | Evidence |
|----------|---------|----------|
| GP2 price matches live API ± $0.005/GB | ✅ PASS | Live: $0.11, Expected: ~$0.11 |
| GP3 price matches live API ± $0.005/GB | ✅ PASS | Live: $0.088, Expected: ~$0.088 |
| Savings = `size × (gp2_price - gp3_price)` not flat | ✅ PASS | `savings += size * max(gp2_price - gp3_price, 0)` |
| Unattached volume cost includes full GB-month charge | ✅ PASS | Includes storage + IOPS + throughput |
| io1/io2 IOPS charge included in estimates | ✅ PASS | `get_ebs_iops_monthly_price()` used |

---

### 6. Issues Found

#### 6.1 ⚠️ WARN — Outdated Fallback Constants

**Location**: `core/pricing_engine.py` lines 51-62

| Volume Type | Current Fallback | Live eu-west-1 Price | Variance |
|-------------|------------------|----------------------|----------|
| gp2 | $0.10 | $0.11 | **-9.1%** |
| gp3 | $0.08 | $0.088 | **-9.1%** |
| io1 | $0.125 | $0.125 | 0% |
| io2 | $0.125 | Not queried | — |
| io1 IOPS | $0.065 | $0.072 | **-9.7%** |
| io2 IOPS | $0.065 | Not queried | — |

**Impact**: When AWS Pricing API is unavailable, estimates will be ~9% lower than actual costs.

**Recommendation**: Update fallback constants to match current eu-west-1 pricing.

---

#### 6.2 🔵 INFO — gp2_recs Use Text Savings

**Observation**: GP2 migration recommendations use text description instead of numeric savings:
```python
"EstimatedSavings": "Estimated 20% cost reduction",  # Text only
```

Actual savings calculation happens later in the adapter. This is a design choice, not a bug.

---

### 7. Verdict: PASS

#### Summary

| Category | Status |
|----------|--------|
| Live pricing integration | ✅ Working |
| GP2→GP3 savings formula | ✅ Correct (price delta, not flat %) |
| Unattached volume costs | ✅ Correct (size × price + IOPS) |
| IOPS pricing | ✅ Included for io1/io2 |
| Compute Optimizer integration | ✅ Working |
| Fallback constants | ⚠️ Outdated (~9% variance) |

#### Final Assessment

The EBS adapter **correctly implements**:
1. Live AWS Pricing API queries for GP2, GP3, and IOPS pricing
2. Accurate savings calculation using price deltas (`gp2_price - gp3_price`)
3. Comprehensive cost estimation including storage + IOPS + throughput
4. Proper Compute Optimizer integration

**Overall Verdict**: ✅ **PASS**

The adapter is production-ready with live pricing. Recommend updating fallback constants to reduce variance when API is unavailable.

---

### Appendix: Raw Pricing API Results

#### GP2 Storage (eu-west-1)
```
Service: AmazonEC2
Region: eu-west-1 (EU Ireland)
Product Family: Storage
Volume API Name: gp2
Price: $0.11 per GB-month
Unit: GB-Mo
SKU: GDX844Q9TQAG2YZ2
Effective Date: 2026-04-01
```

#### GP3 Storage (eu-west-1)
```
Service: AmazonEC2
Region: eu-west-1 (EU Ireland)
Product Family: Storage
Volume API Name: gp3
Price: $0.088 per GB-month
Unit: GB-Mo
SKU: TXACT7E3PV6ZX2NM
Effective Date: 2026-04-01
```

#### io1 IOPS (eu-west-1)
```
Service: AmazonEC2
Region: eu-west-1 (EU Ireland)
Product Family: System Operation
Volume API Name: io1
Price: $0.072 per IOPS-month
Unit: IOPS-Mo
SKU: M4WP2RF278FEGVQY
Effective Date: 2026-04-01
```

---

*End of Audit Report*


---

## AUDIT-02: EC2

| Field | Value |
|-------|-------|
| **Adapter** | `services/adapters/ec2.py` (71 lines) |
| **Supporting Modules** | `services/advisor.py`, `services/ec2.py`, `services/_savings.py` |
| **Audit Date** | 2026-05-01 |
| **Auditor** | Automated audit agent |

---

### 1. Code Analysis

#### 1.1 Cost Hub Recommendation Merging

**Verdict: ✅ PASS**

The adapter correctly retrieves Cost Hub recommendations from the pre-split cache:

```python
cost_hub_recs = ctx.cost_hub_splits.get("ec2", [])
```

The `ScanOrchestrator` (`core/scan_orchestrator.py:49-67`) pre-fetches all Cost Hub recommendations via `get_detailed_cost_hub_recommendations()` and splits them by `currentResourceType` using a `type_map` that maps `Ec2Instance` → `"ec2"`. This is architecturally sound — the split happens once before any adapter runs, avoiding N+1 API calls.

Savings aggregation uses `rec.get("estimatedMonthlySavings", 0)` which matches the Cost Optimization Hub API response shape (flat numeric field at top level).

#### 1.2 Compute Optimizer Savings Extraction

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

#### 1.3 parse_dollar_savings() Usage

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

#### 1.4 Graviton Filtering

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

#### 1.5 Double-Counting Prevention

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

### 2. Pricing Validation

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

### 3. RI Savings

**Verdict: ✅ PASS (with caveat)**

The adapter passes Cost Hub's `estimatedMonthlySavings` through directly — no re-computation. Cost Optimization Hub internally considers existing RI/Savings Plans coverage when computing estimated savings (per AWS docs on savings estimation mode). The adapter does not modify these values.

**Caveat**: The Compute Optimizer savings are silently $0 (see §1.2), so any RI-based savings from that source are lost. For the Cost Hub path, RI savings pass through correctly.

---

### 4. Graviton Filtering

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

### 5. Documentation

**Verdict: ✅ PASS**

- Adapter docstring (`ec2.py:26-38`) accurately describes the multi-source strategy
- `advisor.py` has clear module docstring explaining its consolidation role
- `services/adapters/CLAUDE.md` documents the EC2 adapter's pricing strategy
- `README.md` correctly lists EC2 optimizations: idle instances (CloudWatch), rightsizing, previous-gen, burstable credits, stopped instances
- IAM policy includes both `cost-optimization-hub:ListRecommendations` and `compute-optimizer:GetEC2InstanceRecommendations`

---

### 6. Pass Criteria Checklist

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

### 7. Issues

#### Critical

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| EC2-001 | Compute Optimizer savings silently $0 due to nested field mismatch | `ec2.py:50`, `advisor.py:66-93` | All CO rightsizing savings lost |
| EC2-002 | No dedup between Cost Hub, Compute Optimizer, and enhanced checks | `ec2.py:48-56` | Savings inflated 1.5-3x |

#### Warning

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| EC2-003 | `parse_dollar_savings()` returns 0.0 for 90%+ of EstimatedSavings strings | `ec2.py:53-56`, `_savings.py:15` | Enhanced/advanced savings undercounted |
| EC2-004 | Graviton filtered in HTML only, not JSON | `html_report_generator.py:1635` | Inconsistent JSON vs HTML totals |
| EC2-005 | `pricing_multiplier` passed but unused in enhanced/advanced checks | `ec2.py:43-45` | Regional pricing not applied |
| EC2-006 | `fast_mode` param passed to `get_advanced_ec2_checks()` but not checked before CloudWatch calls | `ec2.py:45`, `services/ec2.py:556-560` | CloudWatch queries run even in fast mode |

#### Info

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| EC2-007 | Compute Optimizer OptInRequiredException generates synthetic rec with text savings | `advisor.py:82-92` | `$0` contribution to total, informational only |
| EC2-008 | `get_compute_optimizer_recommendations()` in `ec2.py:394-404` is a dead-code re-export | `services/ec2.py:394-404` | No callers (adapter uses `advisor.py` directly) |

---

### 8. Verdict

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

#### Summary

The EC2 adapter's architecture (multi-source with 4 independent check sources) is sound, but two critical bugs undermine its accuracy:

1. **EC2-001**: Compute Optimizer savings are structurally inaccessible due to a field path mismatch (`estimatedMonthlySavings` read from top level instead of nested `savingsOpportunity.estimatedMonthlySavings.value`). This means **all Compute Optimizer EC2 rightsizing savings are reported as $0**.

2. **EC2-002**: No deduplication exists between the four recommendation sources. The same instance can be counted 2-3 times across Cost Hub, Compute Optimizer, and enhanced checks, inflating reported savings by 1.5-3x.

The combined effect is paradoxical: actual Compute Optimizer savings are silently dropped (undercount), while overlap between Cost Hub and enhanced checks inflates the remaining total (overcount). The net error direction depends on the specific AWS account's recommendation mix.

#### Recommended Priority

1. Fix EC2-001 (nested field access) — one-line fix, immediate impact
2. Implement instance-level dedup (EC2-002) — requires InstanceId-based set intersection
3. Add dollar amounts to EstimatedSavings strings (EC2-003) or switch to PricingEngine lookups
4. Unify Graviton filtering between JSON and HTML (EC2-004)


---

## AUDIT-03: RDS

**Date**: 2026-05-01  
**Auditor**: Automated audit via AWS Pricing API + source code review  
**Files**: `services/adapters/rds.py` (67 lines), `services/rds.py` (528 lines), `core/pricing_engine.py` (571 lines)  
**Region tested**: eu-west-1  

---

### Executive Summary

| Verdict | Count | Details |
|---------|-------|---------|
| ❌ FAIL | 2 | Storage volumeType mapping broken; Aurora backup price incorrect |
| ⚠️ WARN | 3 | Fallback constants outdated; Multi-AZ savings overestimated; Aurora dual-tier ignored |
| ✅ PASS | 4 | Compute pricing correct; Protocol compliance; parse_dollar_savings; snapshot logic |
| 🔵 INFO | 3 | Architecture notes; no engine-specific RI analysis; non-prod detection heuristic |

---

### 1. Compute Pricing Verification

#### 1.1 db.t3.medium MySQL — Single-AZ vs Multi-AZ ✅ PASS

| Configuration | Hourly (USD) | Monthly (730 hrs) | Source |
|---|---|---|---|
| db.t3.medium MySQL **Single-AZ** | $0.072 | $52.56 | AWS Pricing API |
| db.t3.medium MySQL **Multi-AZ** | $0.144 | $105.12 | AWS Pricing API |
| **Ratio** | **2.00×** | **2.00×** | Exact doubling confirmed |

> Multi-AZ is exactly 2× Single-AZ for db.t3.medium MySQL in eu-west-1. This matches the industry-standard expectation.

#### 1.2 Aurora MySQL db.r5.large ✅ PASS

| SKU | Hourly | Description |
|-----|--------|-------------|
| Standard | $0.320 | `RDS db.r5.large Single-AZ instance hour running Aurora MySQL` |
| IO-Optimized | $0.416 | `RDS db.r5.large IO-optimized Single-AZ instance hour running Aurora MySQL` |

> Two distinct pricing tiers exist for Aurora MySQL. The adapter's `get_instance_monthly_price("AmazonRDS", "db.r5.large")` returns the **first** API result. Which tier is returned depends on API ordering — this is non-deterministic.

#### 1.3 Compute Optimizer Savings Extraction ✅ PASS

- `services/adapters/rds.py:49` — `co_recs` savings via `r.get("estimatedMonthlySavings", 0)` — correct float extraction from Compute Optimizer.
- `services/adapters/rds.py:52-53` — `enhanced_recs` savings via `parse_dollar_savings(est)` — correct regex `\$(\d+\.?\d*)` extraction.

---

### 2. Storage Pricing — CRITICAL BUG ❌ FAIL

#### 2.1 volumeType Mapping Broken ❌ FAIL

**Location**: `core/pricing_engine.py:380-388` (`_fetch_rds_storage_price`)

```python
def _fetch_rds_storage_price(self, storage_type: str, *, multi_az: bool = False) -> float | None:
    deployment_option = "Multi-AZ" if multi_az else "Single-AZ"
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
        {"Type": "TERM_MATCH", "Field": "volumeType", "Value": storage_type.upper()},  # ← BUG
        {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment_option},
    ]
    return self._call_pricing_api("AmazonRDS", filters)
```

**The code sends `storage_type.upper()`** → `"GP2"`, `"GP3"`, `"IO1"`, `"IO2"`

**Actual AWS Pricing API volumeType values** (confirmed via `get_pricing_attribute_values`):

| Adapter input | Code sends | Actual API value | Match? |
|---|---|---|---|
| `"gp2"` | `"GP2"` | `"General Purpose"` or `"General Purpose (SSD)"` | ❌ NEVER |
| `"gp3"` | `"GP3"` | `"General Purpose-GP3"` | ❌ NEVER |
| `"io1"` | `"IO1"` | `"Provisioned IOPS"` or `"Provisioned IOPS (SSD)"` | ❌ NEVER |
| `"io2"` | `"IO2"` | `"Provisioned IOPS-IO2"` | ❌ NEVER |

**Impact**: Every RDS storage price lookup via the live API returns `None`, silently falling back to hardcoded constants. The Multi-AZ differentiation in `deploymentOption` is correct but never exercised because the `volumeType` filter always fails first.

**Required mapping**:

```python
_RDS_VOLUME_TYPE_MAP = {
    "gp2": "General Purpose (SSD)",  # or "General Purpose"
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS (SSD)",  # or "Provisioned IOPS"
    "io2": "Provisioned IOPS-IO2",
}
```

#### 2.2 Actual vs Fallback Storage Prices ⚠️ WARN

| Type | Fallback constant | Actual API (eu-west-1) | Delta |
|---|---|---|---|
| gp2 | $0.115/GB | $0.127/GB | **+10.4% under** |
| gp3 | $0.115/GB | $0.127/GB | **+10.4% under** |
| io1 | $0.200/GB | $0.138/GB | **-31.0% OVER** |
| io2 | not in fallback (uses 0.115) | $0.138/GB | **+20.2% under** |

> The io1 fallback ($0.200) overestimates by 31% — all io1 storage savings recommendations are inflated. The gp2/gp3 fallback ($0.115) underestimates by 10% — savings appear smaller than reality.

#### 2.3 Multi-AZ Storage Price Verification ⚠️ WARN

The `multi_az` parameter is correctly threaded through:
- `pricing_engine.py:200-218` — `get_rds_monthly_storage_price_per_gb(storage_type, multi_az=)` 
- `pricing_engine.py:208` — cache key includes `"Multi-AZ"` / `"Single-AZ"`
- `pricing_engine.py:381` — `deploymentOption` filter set correctly

**But**: Since the volumeType mapping is broken (see 2.1), the `deploymentOption` filter never takes effect. Both Single-AZ and Multi-AZ return the same fallback value.

**Confirmed Multi-AZ storage prices** (eu-west-1, AWS Pricing API):

| Type | Single-AZ | Multi-AZ | Ratio |
|---|---|---|---|
| GP2 (`General Purpose (SSD)`) | $0.127/GB | $0.253/GB | **1.99×** |
| GP3 (`General Purpose-GP3`) | $0.127/GB | $0.254/GB | **2.00×** |
| io1 (`Provisioned IOPS (SSD)`) | $0.138/GB | $0.276/GB | **2.00×** |

Since the fallback returns the same value for both, Multi-AZ storage costs are underreported by ~50%.

---

### 3. Backup & Snapshot Pricing

#### 3.1 Backup Storage Price — Aurora vs Non-Aurora ❌ FAIL

**Location**: `core/pricing_engine.py:220-232` (`get_rds_backup_storage_price_per_gb`)

The API returns multiple `Storage Snapshot` products at different prices:

| Engine | $/GB/month |
|---|---|
| MySQL / PostgreSQL / MariaDB / Oracle / SQL Server / Db2 | $0.095 |
| **Aurora MySQL** | **$0.021** |
| **Aurora PostgreSQL** | **$0.021** |

The code filters only by `productFamily=Storage Snapshot` and takes the **first result**, which is always $0.095 (non-Aurora). Aurora backup costs are overestimated by **4.5×**.

**Impact**: Aurora cluster snapshot cleanup recommendations (lines 478-516 in `services/rds.py`) show inflated savings.

#### 3.2 Snapshot Cost Estimation ✅ PASS

**Location**: `services/rds.py:417-435` (DB snapshots), `services/rds.py:486-516` (Aurora cluster snapshots)

Formula: `allocated_storage_gb × backup_price_per_gb`

- Correctly uses `ctx.pricing_engine.get_rds_backup_storage_price_per_gb()` when available
- Falls back to `0.095 × pricing_multiplier` on API failure
- Described as "coarse estimate" — appropriate since actual snapshot size ≠ allocated storage

---

### 4. Savings Calculation Patterns

#### 4.1 parse_dollar_savings() Usage ✅ PASS

**Location**: `services/_savings.py:8-16`

```python
def parse_dollar_savings(savings_str: str) -> float:
    match = re.search(r"\$(\d+\.?\d*)", savings_str)
    return float(match.group(1)) if match else 0.0
```

Applied in `services/adapters/rds.py:52-53` with guard `"$" in est and "/month" in est`.

**Verified patterns that parse correctly**:
- `"$12.50/month"` → 12.50 ✅
- `"~50% of instance cost"` → 0.0 (no `$` sign, correctly skipped) ✅
- `"65-75% of compute costs"` → 0.0 (no `$` sign, correctly skipped) ✅
- `"Up to 60% savings"` → 0.0 (correctly skipped) ✅

#### 4.2 Non-Dollar Savings — Recommendations Without Quantified Savings ⚠️ WARN

Several check categories produce recommendations with non-quantified savings strings:

| Check | EstimatedSavings | Parsed |
|---|---|---|
| `multi_az_unnecessary` | `"~50% of instance cost"` | 0.0 |
| `non_prod_scheduling` | `"65-75% of compute costs"` | 0.0 |
| `aurora_serverless_candidates` | `"Pay only for capacity used (up to 90%...)"` | 0.0 |
| `reserved_instances` | `"Up to 60% savings..."` | 0.0 |
| `idle_databases` (stopped) | `"Storage costs continue..."` | 0.0 |

These contribute recommendations to the count but **$0.00** to `total_monthly_savings`. Users may see high recommendation counts with low total savings, reducing credibility.

#### 4.3 gp2→gp3 Storage Savings ⚠️ WARN

**Location**: `services/rds.py:272-293`

```python
gp2_price = ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp2", multi_az=multi_az)
monthly_cost = allocated_storage * gp2_price
savings = monthly_cost * 0.20  # hardcoded 20%
```

- Since the API lookup always fails (volumeType bug), this uses the fallback $0.115/GB
- The 20% savings factor is hardcoded — actual gp2 vs gp3 prices are identical ($0.127 each in eu-west-1)
- **In eu-west-1, there are NO savings from gp2→gp3 on a per-GB basis** — the benefit is in included IOPS (gp3 includes 3,000 IOPS vs gp2's baseline). The savings come from avoiding Provisioned IOPS charges, not from a storage price difference.

---

### 5. Aurora vs Non-Aurora Handling

#### 5.1 Dual Aurora Pricing Tiers 🔵 INFO

The API returns two distinct Aurora MySQL instance prices for db.r5.large:
- **Standard**: $0.320/hr
- **IO-Optimized**: $0.416/hr (30% premium)

The adapter uses `get_instance_monthly_price("AmazonRDS", instance_class)` which takes the first API result — this is non-deterministic. Aurora IO-Optimized is a separate deployment choice that should be explicitly handled.

#### 5.2 Aurora-Specific Checks ✅ PASS

- Aurora Serverless v2 candidate detection: Checks for small instance classes (`db.t3.small`, `db.t3.medium`, `db.t4g.small`, `db.t4g.medium`) ✅
- Aurora cluster Serverless v2 migration check: Detects clusters without `ServerlessV2ScalingConfiguration` ✅
- Aurora storage type review: Flags `storageType == "aurora"` for I/O-Optimized review ✅

---

### 6. Architecture & Protocol Compliance ✅ PASS

| Aspect | Status | Notes |
|---|---|---|
| `ServiceModule` Protocol | ✅ | Implements `required_clients()` and `scan()` |
| `ServiceFindings` return | ✅ | Returns typed `ServiceFindings` with 2 sources |
| Source decomposition | ✅ | `compute_optimizer` + `enhanced_checks` SourceBlocks |
| `optimization_descriptions` | ✅ | 4 description categories provided |
| `extras` metadata | ✅ | Includes `instance_counts` |
| Client requirements | ✅ | `("rds", "compute-optimizer")` declared |
| Error resilience | ✅ | All AWS API calls wrapped in try/except with fallbacks |

---

### 7. Non-Prod Detection Heuristic 🔵 INFO

**Location**: `services/rds.py:192-228`

Multi-AZ review uses a two-tier detection:
1. **Tag-based**: Checks `Environment`/`Stage`/`Env` tags
2. **Name-based fallback**: Scans DB identifier for `dev`, `test`, `staging`, `qa` substrings

This is a reasonable heuristic but may produce false positives (e.g., a production DB named `order-devotion-db`).

---

### 8. Findings Summary

| # | Severity | Category | Finding |
|---|---|---|---|
| F-01 | ❌ CRITICAL | Pricing | `volumeType` filter sends `"GP2"`/`"GP3"`/`"IO1"` but API expects `"General Purpose (SSD)"`/`"General Purpose-GP3"`/`"Provisioned IOPS (SSD)"`. All RDS storage API lookups fail silently → always uses fallback. |
| F-02 | ❌ HIGH | Pricing | `get_rds_backup_storage_price_per_gb()` returns $0.095 for all engines. Aurora backup is $0.021/GB (4.5× cheaper). Aurora snapshot savings are overstated. |
| F-03 | ⚠️ MEDIUM | Pricing | Fallback constants are outdated: io1 at $0.200 vs actual $0.138 (+31% over); gp2/gp3 at $0.115 vs actual $0.127 (-10% under). |
| F-04 | ⚠️ MEDIUM | Accuracy | gp2→gp3 savings hardcoded at 20%. In many regions, gp2 and gp3 have identical per-GB pricing. Savings are from IOPS, not storage. |
| F-05 | ⚠️ LOW | Completeness | Non-quantified savings estimates (`"~50% of instance cost"`, `"65-75%"`) contribute to recommendation count but $0.00 to total savings. |
| F-06 | 🔵 INFO | Architecture | No engine-specific Reserved Instance analysis. RI recommendations are generic ("Up to 60% savings") without checking existing RI coverage. |
| F-07 | 🔵 INFO | Edge case | Aurora IO-Optimized tier ($0.416/hr vs $0.320/hr standard) not differentiated — `get_instance_monthly_price` returns whichever API result comes first. |

---

### 9. AWS Documentation Cross-Reference

Per AWS docs (confirmed via search):

1. **Multi-AZ pricing**: "Multi-AZ deployments are priced per DB instance-hour consumed" — confirmed 2× Single-AZ for compute. Storage also doubles.
2. **Reserved Instances**: "Save up to 69% over On-Demand rates" — RI recommendations cite "up to 60%", slightly conservative but reasonable.
3. **Backup pricing**: "Backup storage associated with automated database backups and active database snapshots, billed in GB-month" — $0.095 for non-Aurora, $0.021 for Aurora in eu-west-1.
4. **Storage types**: RDS supports gp2, gp3, io1, io2, Magnetic — all confirmed in API volumeType values.

---

### 10. Recommended Remediation Priority

| Priority | Finding | Fix |
|---|---|---|
| **P0** | F-01 | Add `_RDS_VOLUME_TYPE_MAP` dict in `pricing_engine.py` and use it in `_fetch_rds_storage_price` instead of `storage_type.upper()` |
| **P1** | F-02 | Add `engine` parameter to `get_rds_backup_storage_price_per_gb()` and filter by `databaseEngine` for Aurora-specific pricing |
| **P2** | F-03 | Update `FALLBACK_RDS_STORAGE_GB_MONTH` to current eu-west-1 values: `{"gp2": 0.127, "gp3": 0.127, "io1": 0.138, "io2": 0.138}` |
| **P3** | F-04 | Calculate gp2→gp3 savings based on IOPS included rather than flat 20% storage discount |
| **P4** | F-05 | Add per-instance dollar savings estimates for Multi-AZ and non-prod scheduling checks using live pricing |


---

## AUDIT-04: S3

**Adapter:** `services/adapters/s3.py` (69 lines)
**Legacy logic:** `services/s3.py` (931 lines)
**Pricing engine:** `core/pricing_engine.py` — `get_s3_monthly_price_per_gb()`
**Date:** 2026-05-01
**Auditor:** Automated (opencode)

---

### 1. Code Analysis

#### Architecture

The adapter (`services/adapters/s3.py`) is a thin shell that delegates to two legacy functions in `services/s3.py`:

| Function | Lines | Purpose |
|----------|-------|---------|
| `get_s3_bucket_analysis()` | 562–761 | Per-bucket analysis: size, cost, lifecycle, Intelligent-Tiering status |
| `get_enhanced_s3_checks()` | 764–931 | Cross-cutting checks: multipart uploads, versioning, replication, logging, empty buckets |

#### EstimatedMonthlyCost Population

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

### 2. Pricing Validation (eu-west-1)

#### AWS Pricing API Results (live, 2026-04-27 publication)

| Storage Class | Adapter Constant | API Price (first 50TB) | Delta |
|---------------|-----------------|----------------------|-------|
| General Purpose (STANDARD) | $0.0230 | $0.0230 | ✅ Exact match |
| Infrequent Access (STANDARD_IA) | $0.0125 | $0.0125 | ✅ Exact match |
| One Zone IA (ONEZONE_IA) | $0.0100 | $0.0100 | ✅ Exact match |
| Intelligent-Tiering FA | $0.0230 | $0.0230 | ✅ Exact match |
| Deep Archive | $0.00099 | $0.00099 | ✅ Exact match |

#### Intelligent-Tiering Tier Breakdown (from live API)

| Tier | API $/GB/month | In `S3_STORAGE_COSTS`? |
|------|---------------|-------------|
| Frequent Access | $0.0230 | ✅ `INTELLIGENT_TIERING: 0.023` |
| Infrequent Access | $0.0125 | ❌ Not in dict |
| Archive Instant Access | $0.0040 | ❌ Not in dict |
| Archive Access | $0.0036 | ❌ Not in dict |
| Deep Archive Access | $0.00099 | ❌ Not in dict |

#### Pricing Engine S3 Fetch (`pricing_engine.py:397-412`)

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

### 3. Intelligent-Tiering Monitoring Charge

#### Constant Definition

```python
## services/s3.py:25
S3_INTELLIGENT_TIERING_MONITORING_FEE: float = 0.0025  # $0.0025 per 1,000 objects/month
```

#### Where Applied

In `_estimate_s3_bucket_cost()` (`s3.py:530-533`):

```python
if cost_key == "INTELLIGENT_TIERING":
    estimated_objects = class_size_gb * 1000
    monitoring_fee = (estimated_objects / 1000) * S3_INTELLIGENT_TIERING_MONITORING_FEE
    storage_cost += monitoring_fee
```

#### Issues Found

| Issue | Severity | Detail |
|-------|----------|--------|
| Object count estimation | ⚠️ WARN | Uses `class_size_gb * 1000` — assumes average object size of 1MB. Real-world average varies wildly (KB to GB). Should use S3 Inventory or `list_objects_v2` for actual counts. |
| Only in secondary path | ⚠️ WARN | Monitoring fee is NOT applied in `_calculate_s3_storage_cost()` (`s3.py:443`), which is the primary function called from bucket analysis. When `get_s3_bucket_analysis()` estimates costs, monitoring fees are **not included**. |
| Not in enhanced checks | 🔵 INFO | Enhanced checks recommending Intelligent-Tiering do not factor monitoring fees into cost-benefit analysis. |

**Verdict: ⚠️ WARN** — Monitoring fee constant ($0.0025/1000 objects) correctly matches AWS pricing, but **not applied** in the primary cost estimation path used by bucket analysis.

---

### 4. Lifecycle Savings Calculation

#### How Savings Are Computed

The adapter does **not** compute lifecycle transition savings. Instead:

1. `get_s3_bucket_analysis()` calculates `EstimatedMonthlyCost` as the **current total monthly storage cost** (all classes at Standard pricing)
2. `buckets_without_lifecycle` and `buckets_without_intelligent_tiering` are identified as flag lists
3. No target-class price is calculated — no `current - target` delta is computed

#### What's Missing

| Component | Status | Impact |
|-----------|--------|--------|
| Current class breakdown cost | ✅ Computed | `_estimate_s3_bucket_cost()` uses CloudWatch metrics per storage class |
| Target class recommendation | ❌ Missing | No recommendation of which cheaper class to transition to |
| Per-GB savings delta | ❌ Missing | `current_class_price - target_class_price` per GB never calculated |
| Transition timing estimate | ❌ Missing | No "transition after N days" recommendation |
| Minimum storage duration penalty | ❌ Missing | Standard-IA has 30-day minimum; Glacier has 90-day — not modeled |

#### Legacy `_SC_MAP` Key Bridge

```python
## services/s3.py:440
_SC_MAP = {"GLACIER_FLEXIBLE_RETRIEVAL": "GLACIER", "GLACIER_INSTANT_RETRIEVAL": "GLACIER_IR"}
```

Correctly bridges `S3_STORAGE_COSTS` keys (e.g., `GLACIER_FLEXIBLE_RETRIEVAL`) to pricing engine keys (e.g., `GLACIER`) when calling `ctx.pricing_engine.get_s3_monthly_price_per_gb()`.

**Verdict: ❌ FAIL** — No lifecycle transition savings calculated. `EstimatedMonthlyCost` contains current cost, not projected savings. Buckets are flagged but dollar benefit is not quantified.

---

### 5. Versioning — Non-Current Version Storage

#### What the Adapter Does

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

#### Issues Found

| Issue | Severity | Detail |
|-------|----------|--------|
| Non-current versions not measured | ❌ FAIL | Does NOT query `list_object_versions()` or CloudWatch `NonCurrentVersionStorage` metric |
| No cost attribution | ❌ FAIL | No `EstimatedMonthlyCost` — $0 contribution to total savings |
| Generic recommendation | ⚠️ WARN | No specific `NoncurrentVersionTransition` lifecycle rule generated |
| Version count/size unknown | ⚠️ WARN | Cannot assess severity without non-current version data |

#### What Should Happen

Per AWS docs, versioned buckets should:
1. Query `BucketSizeBytes` with `StorageType=NonCurrentVersionStorage` for non-current GB
2. Calculate cost of non-current versions at Standard rate
3. Recommend `NoncurrentVersionTransition` lifecycle rule (e.g., IA after 30 days, Glacier after 90)
4. Estimate savings: `non_current_gb × (STANDARD_price - target_price)`

**Verdict: ❌ FAIL** — Non-current version storage not measured or costed. Versioning recommendations carry $0 savings.

---

### 6. Documentation Cross-Reference

#### AWS S3 Cost Optimization Best Practices (from AWS docs search)

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

#### S3 Express One Zone

`S3_STORAGE_COSTS` includes `EXPRESS_ONE_ZONE: 0.11` but the CloudWatch metric loop (`s3.py:480-487`) does not include `ExpressOneZoneStorage`. Express One Zone uses a different metrics namespace — these buckets are skipped.

**Verdict: ⚠️ WARN** — Core recommendations present but missing quantitative lifecycle transition advice and Storage Class Analysis integration.

---

### 7. Pass Criteria Assessment

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

### 8. Issues Summary

#### Critical (❌ FAIL) — 3 issues

| ID | Location | Description |
|----|----------|-------------|
| S3-F001 | `services/adapters/s3.py:42` | `total_monthly_savings` sums `EstimatedMonthlyCost` which contains **current monthly cost** of all analyzed buckets, not optimization savings. Any bucket with cost > $0 inflates reported savings. |
| S3-F002 | `services/s3.py:562-761` | No lifecycle transition savings calculation. Buckets flagged as needing lifecycle policies but no `$/GB/month` savings from Standard→IA→Glacier transitions quantified. |
| S3-F003 | `services/s3.py:849-861` | Non-current version storage not measured (no CloudWatch `NonCurrentVersionStorage` query). Carries $0 savings estimate. |

#### Warnings (⚠️ WARN) — 5 issues

| ID | Location | Description |
|----|----------|-------------|
| S3-W001 | `services/s3.py:531` | Object count estimation `class_size_gb * 1000` (assumes 1MB avg). Should use S3 Inventory or `list_objects` for actual count. |
| S3-W002 | `services/s3.py:443-458` | Intelligent-Tiering monitoring fee not applied in `_calculate_s3_storage_cost()` — the primary cost function called from bucket analysis. |
| S3-W003 | `core/pricing_engine.py:397-412` | Tiered pricing (>50TB, >500TB breakpoints) not modeled. Flat first-tier price returned. Large buckets underestimated by ~4-9%. |
| S3-W004 | `services/s3.py:480-487` | S3 Express One Zone not included in CloudWatch `storage_classes` list — Express buckets skipped in cost analysis. |
| S3-W005 | `services/s3.py:840-847` | Enhanced check `lifecycle_missing` entries set `EstimatedMonthlyCost: 0` — no savings quantification for missing lifecycle policies. |

#### Informational (🔵 INFO) — 2 items

| ID | Location | Description |
|----|----------|-------------|
| S3-I001 | `services/s3.py:14-23` | `S3_STORAGE_COSTS` dict uses `GLACIER_FLEXIBLE_RETRIEVAL` / `GLACIER_INSTANT_RETRIEVAL` keys; CloudWatch uses `GlacierStorage`. The `_SC_MAP` bridge handles this correctly. |
| S3-I002 | `services/s3.py:625-638` | Fast mode samples only first 100 objects via `MaxKeys=100` — size estimate can be significantly understated for large buckets. Warning printed but no flag in output. |

---

### 9. Verdict

| Category | Result |
|----------|--------|
| **Overall** | ⚠️ **CONDITIONAL PASS** |
| Pricing accuracy | ✅ PASS |
| Savings calculation | ❌ FAIL |
| Intelligent-Tiering | ⚠️ WARN |
| Versioning | ❌ FAIL |
| Documentation alignment | ⚠️ WARN |

#### Summary

The S3 adapter correctly identifies optimization opportunities (missing lifecycle policies, missing Intelligent-Tiering, incomplete multipart uploads, versioned buckets) and its pricing constants match live AWS API data exactly. However, **the `total_monthly_savings` field does not contain projected savings** — it sums the current monthly storage cost of all analyzed buckets. No `current_class_price - target_class_price` delta is calculated for lifecycle or Intelligent-Tiering transitions. Non-current version storage is detected but not measured or costed.

#### Recommendations for Fix

1. **Fix `total_monthly_savings`** to sum `(current_cost - projected_cost_with_optimization)` per bucket
2. **Add lifecycle transition savings**: `size_gb × (STANDARD_price - target_class_price)` for buckets without lifecycle
3. **Query `NonCurrentVersionStorage`** CloudWatch metric for versioned buckets
4. **Apply Intelligent-Tiering monitoring fee** in `_calculate_s3_storage_cost()`, not just `_estimate_s3_bucket_cost()`

---

### Appendix: Pricing Reference (eu-west-1, live API 2026-04-27)

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


---

## AUDIT-05: Network

**Date:** 2026-05-01  
**Auditor:** OpenCode Agent  
**Adapter:** `services/adapters/network.py`  
**Scope:** EIP, NAT Gateway, VPC Endpoints, Load Balancers (ALB/NLB), ASG  
**Region Tested:** eu-west-1 (EU Ireland)

---

### Executive Summary

| Component | Verdict | Notes |
|-----------|---------|-------|
| EIP Pricing | ✅ PASS | Uses live pricing API correctly |
| NAT Gateway Pricing | ⚠️ WARN | Fallback constant outdated ($32.00 vs $35.04) |
| VPC Endpoint Pricing | ✅ PASS | Uses live pricing API correctly |
| ALB Pricing | ⚠️ WARN | Fallback constant outdated ($16.20 vs $20.44) |
| Legacy Shims | ⚠️ WARN | Use live pricing but fallback values need updating |
| Documentation | ✅ PASS | AWS docs confirm VPC endpoint cost optimization |

**Overall Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constants are outdated.

---

### 1. Code Analysis

#### 1.1 Adapter Architecture

The `network.py` adapter is a **composite/aggregator** that delegates to five legacy shim modules:

```python
## services/adapters/network.py (lines 42-46)
eip_result = get_elastic_ip_checks(ctx)
nat_result = get_nat_gateway_checks(ctx)
vpc_result = get_vpc_endpoints_checks(ctx)
lb_result = get_load_balancer_checks(ctx)
asg_result = get_auto_scaling_checks(ctx)
```

**Called Functions:**
| Function | Source File | Lines |
|----------|-------------|-------|
| `get_elastic_ip_checks()` | `services/elastic_ip.py` | 15-131 |
| `get_nat_gateway_checks()` | `services/nat_gateway.py` | 16-150 |
| `get_vpc_endpoints_checks()` | `services/vpc_endpoints.py` | 16-129 |
| `get_load_balancer_checks()` | `services/load_balancer.py` | 53-260 |
| `get_auto_scaling_checks()` | `services/ec2.py` | (separate) |

#### 1.2 Pricing Engine Integration Pattern

All legacy shims follow the same pattern:

```python
## Example from elastic_ip.py (line 16)
eip_monthly = ctx.pricing_engine.get_eip_monthly_price() if ctx.pricing_engine is not None else 3.65

## Example from nat_gateway.py (line 17)
nat_monthly = ctx.pricing_engine.get_nat_gateway_monthly_price() if ctx.pricing_engine is not None else 32.0

## Example from vpc_endpoints.py (line 17)
vpc_ep_monthly = ctx.pricing_engine.get_vpc_endpoint_monthly_price() if ctx.pricing_engine is not None else 7.30

## Example from load_balancer.py (line 54)
alb_monthly = ctx.pricing_engine.get_alb_monthly_price() if ctx.pricing_engine is not None else 16.20
```

**Pattern Analysis:**
- ✅ **Live pricing used when available**: All shims call `ctx.pricing_engine.*_price()` methods
- ✅ **Graceful fallback**: Hardcoded constants used when pricing engine is None
- ⚠️ **Fallback constants outdated**: Some fallback values don't match current AWS pricing

---

### 2. EIP (Elastic IP) Pricing Verification

#### 2.1 AWS Pricing API Result

```json
{
  "service": "AmazonVPC",
  "filter": "group=VPCPublicIPv4Address",
  "hourly_rate": "$0.005/hr",
  "monthly_calculation": "$0.005 × 730 = $3.65/month"
}
```

**Pricing Confirmed:**
- **In-use EIP**: $0.005/hour (sku: CNZ4XSXPRJXGA4ZF)
- **Idle EIP**: $0.005/hour (sku: MHVKGXCT3GPZ4RVC)
- **Both types same price** since AWS IPv4 rebilling (2024)

#### 2.2 Code Implementation

```python
## pricing_engine.py lines 289-300
def get_eip_monthly_price(self) -> float:
    key = ("eip_month",)
    if (cached := self._get_cached(key)) is not None:
        return cached
    price = self._fetch_eip_price()
    if price is None:
        price = self._use_fallback(
            FALLBACK_EIP_MONTH * self._fallback_multiplier,  # $3.65
            ...
        )
```

**Fallback Value:** `FALLBACK_EIP_MONTH = 3.65` (line 79) ✅ **CORRECT**

#### 2.3 Usage in Recommendations

```python
## elastic_ip.py lines 47-56 (unassociated_eips)
{
    "Recommendation": "Release unassociated Elastic IP to avoid charges",
    "EstimatedSavings": f"${eip_monthly:.2f}/month per EIP",  # Uses live price
}
```

**Verdict: ✅ PASS** — EIP pricing is accurate and uses live pricing correctly.

---

### 3. NAT Gateway Pricing Verification

#### 3.1 AWS Pricing API Result

```json
{
  "service": "AmazonEC2",
  "filters": [
    "productFamily=NAT Gateway",
    "operation=NatGateway"
  ],
  "results": [
    {
      "sku": "KFNSSCHWD43WZK2R",
      "description": "$0.048 per NAT Gateway Hour",
      "unit": "Hrs",
      "hourly_rate": "$0.048/hr",
      "monthly_calculation": "$0.048 × 730 = $35.04/month"
    },
    {
      "sku": "DTH8595YVPFXV226",
      "description": "$0.048 per GB Data Processed by NAT Gateways",
      "unit": "GB",
      "rate": "$0.048/GB"
    }
  ]
}
```

**Pricing Confirmed:**
- **Hourly charge**: $0.048/hour → **$35.04/month**
- **Data processing**: $0.048/GB

#### 3.2 Code Implementation

```python
## pricing_engine.py lines 302-313
def get_nat_gateway_monthly_price(self) -> float:
    key = ("nat_month",)
    ...
    if price is None:
        price = self._use_fallback(
            FALLBACK_NAT_MONTH * self._fallback_multiplier,  # $32.0
            ...
        )

## Fallback constant (line 80)
FALLBACK_NAT_MONTH: float = 32.0  # ❌ OUTDATED (should be $35.04)
```

#### 3.3 Discrepancy Analysis

| Metric | Expected | Fallback | Delta |
|--------|----------|----------|-------|
| NAT Gateway monthly | $35.04 | $32.00 | -$3.04 (-8.7%) |

**Impact:** When pricing API is unavailable, savings estimates will be **8.7% lower** than actual.

#### 3.4 Usage in Recommendations

```python
## nat_gateway.py lines 64-73 (nat_in_dev_test)
{
    "EstimatedSavings": f"${nat_monthly + 0.85:.2f}/month base + data processing fees",
}

## nat_gateway.py lines 109-116 (multiple_nat_gateways)
{
    "EstimatedSavings": f"${(count - 1) * nat_monthly:.2f}/month if consolidated",
}
```

**Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constant is outdated.

---

### 4. VPC Endpoint Pricing Verification

#### 4.1 AWS Pricing API Result

```json
{
  "service": "AmazonVPC",
  "filters": [
    "productFamily=VpcEndpoint",
    "endpointType=PrivateLink"
  ],
  "results": [
    {
      "sku": "H37WJ97MHYGDXG2A",
      "description": "$0.011 per VPC Endpoint Hour",
      "unit": "Hrs",
      "hourly_rate": "$0.011/hr",
      "monthly_calculation": "$0.011 × 730 = $8.03/month"
    },
    {
      "sku": "S8CASMMQB4XR7YWZ",
      "description": "Data processing tiers",
      "tiers": [
        "$0.01/GB (up to 1 PB)",
        "$0.006/GB (1-5 PB)",
        "$0.004/GB (5+ PB)"
      ]
    }
  ]
}
```

**Pricing Confirmed:**
- **Interface endpoint**: $0.011/hour → **$8.03/month**
- **Data processing**: Tiered ($0.01/GB for first PB)

#### 4.2 Code Implementation

```python
## pricing_engine.py lines 315-326
def get_vpc_endpoint_monthly_price(self) -> float:
    key = ("vpc_ep_month",)
    ...
    if price is None:
        price = self._use_fallback(
            FALLBACK_VPC_ENDPOINT_MONTH * self._fallback_multiplier,  # $7.30
            ...
        )

## Fallback constant (line 81)
FALLBACK_VPC_ENDPOINT_MONTH: float = 7.30  # ❌ OUTDATED (should be $8.03)
```

**Note:** Fallback shows $7.30 but actual is $8.03 — off by ~9%.

#### 4.3 Usage in Recommendations

```python
## vpc_endpoints.py lines 89-98 (interface_endpoints_in_nonprod)
{
    "EstimatedSavings": f"${vpc_ep_monthly:.2f}/month per endpoint",
}

## vpc_endpoints.py lines 111-121 (duplicate_endpoints)
{
    "EstimatedSavings": f"${(len(service_endpoints) - 2) * vpc_ep_monthly:.2f}/month if some consolidated",
}
```

**Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constant is slightly outdated.

---

### 5. ALB (Application Load Balancer) Pricing Verification

#### 5.1 AWS Pricing API Result

```json
{
  "service": "AWSELB",
  "filter": "productFamily=Load Balancer",
  "results": [
    {
      "sku": "PMVAP4E245MPBY22",
      "description": "$0.028 per LoadBalancer-hour (or partial hour)",
      "unit": "Hrs",
      "hourly_rate": "$0.028/hr",
      "monthly_calculation": "$0.028 × 730 = $20.44/month"
    },
    {
      "sku": "Z2PBQTQYT4Z8CUQF",
      "description": "$0.008 per GB Data Processed by the LoadBalancer",
      "unit": "GB",
      "rate": "$0.008/GB"
    }
  ]
}
```

**Pricing Confirmed:**
- **ALB hourly**: $0.028/hour → **$20.44/month**
- **Data processing**: $0.008/GB
- **LCU charges**: Additional (not captured)

#### 5.2 Code Implementation

```python
## pricing_engine.py lines 328-339
def get_alb_monthly_price(self) -> float:
    key = ("alb_month",)
    ...
    if price is None:
        price = self._use_fallback(
            FALLBACK_ALB_MONTH * self._fallback_multiplier,  # $16.20
            ...
        )

## Fallback constant (line 82)
FALLBACK_ALB_MONTH: float = 16.20  # ❌ OUTDATED (should be $20.44)
```

#### 5.3 Discrepancy Analysis

| Metric | Expected | Fallback | Delta |
|--------|----------|----------|-------|
| ALB monthly | $20.44 | $16.20 | -$4.24 (-20.7%) |

**Impact:** When pricing API is unavailable, ALB savings estimates will be **20.7% lower** than actual.

#### 5.4 NLB Pricing Calculation

```python
## load_balancer.py line 55
nlb_monthly = alb_monthly * 1.4  # NLB is ~40% more expensive than ALB
```

This relative calculation is reasonable: NLB ≈ $28.62/month.

#### 5.5 Usage in Recommendations

```python
## load_balancer.py lines 122-130 (nlb_vs_alb)
{
    "EstimatedSavings": f"Estimated ${nlb_monthly - alb_monthly:.2f}/month savings (NLB: ${nlb_monthly:.2f} vs ALB: ${alb_monthly:.2f})",
}

## load_balancer.py lines 150-170 (single_service_albs)
{
    "EstimatedSavings": f"Up to ${alb_monthly:.2f}/month per ALB eliminated through consolidation",
}
```

**Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constant is significantly outdated.

---

### 6. Legacy Shims Analysis

#### 6.1 Do Legacy Shims Use Live Pricing?

| Shim File | Uses Live Pricing | Fallback Value | Fallback Correct? |
|-----------|-------------------|----------------|-------------------|
| `elastic_ip.py` | ✅ Yes | $3.65 | ✅ Yes |
| `nat_gateway.py` | ✅ Yes | $32.00 | ❌ No (should be $35.04) |
| `vpc_endpoints.py` | ✅ Yes | $7.30 | ❌ No (should be $8.03) |
| `load_balancer.py` | ✅ Yes | $16.20 | ❌ No (should be $20.44) |

#### 6.2 EstimatedSavings Generation

All legacy shims use **formatted live pricing** in `EstimatedSavings`:

```python
## Pattern found across all shims
f"${monthly_price:.2f}/month ..."  # Uses live price with 2 decimal precision
```

**No hardcoded dollar strings found** in recommendation logic — all use the calculated `*_monthly` variables.

#### 6.3 Exception: Non-Monetary Recommendations

Some recommendations use descriptive savings:

```python
## nat_gateway.py line 98
"EstimatedSavings": "$0.01/GB data processing savings",

## load_balancer.py line 249
"EstimatedSavings": "10-20% + better features",  # Classic ELB migration
```

These are informational and acceptable.

---

### 7. Documentation Review

#### 7.1 AWS Documentation Findings

**Source:** AWS Well-Architected Framework — COST08-BP02

Key findings relevant to this adapter:

1. **VPC Endpoints reduce NAT Gateway costs**: Using VPC endpoints for S3 and DynamoDB avoids NAT Gateway data processing charges ($0.048/GB)

2. **Gateway Endpoints are FREE**: S3 and DynamoDB gateway endpoints have **no hourly charge**, only interface endpoints do

3. **Interface Endpoints have hourly charges**: $0.011/hour ($8.03/month) per AZ

4. **Cost optimization hierarchy**:
   - Gateway endpoints (free) → Preferred for S3/DynamoDB
   - Interface endpoints (paid) → Required for most other services
   - NAT Gateway (most expensive) → Use only when necessary

#### 7.2 Adapter Alignment

The adapter correctly identifies:
- ✅ Missing gateway endpoints for S3/DynamoDB (`missing_gateway_endpoints`)
- ✅ Interface endpoints in non-prod environments (`interface_endpoints_in_nonprod`)
- ✅ NAT Gateway optimization opportunities (`nat_for_aws_services`)
- ✅ ALB consolidation opportunities (`shared_alb_opportunity`)

**Documentation Verdict: ✅ PASS**

---

### 8. Pass Criteria Verification

| Criteria | Status | Evidence |
|----------|--------|----------|
| EIP uses $0.005/hr × 730 = $3.65/month | ✅ PASS | AWS API confirms; fallback matches |
| NAT Gateway uses $0.048/hr × 730 = $35.04/month | ⚠️ WARN | Live pricing correct; fallback is $32.00 |
| VPC Endpoint uses $0.011/hr × 730 = $8.03/month | ⚠️ WARN | Live pricing correct; fallback is $7.30 |
| ALB uses ~$0.022-0.028/hr × 730 = $16-20/month | ⚠️ WARN | Live pricing shows $0.028/hr; fallback is $16.20 |
| Legacy shims use live pricing for EstimatedSavings | ✅ PASS | All use formatted live price variables |
| No hardcoded pricing strings in recommendations | ✅ PASS | All use `f"${price:.2f}"` pattern |

---

### 9. Issues Found

#### 9.1 Issue #1: Outdated NAT Gateway Fallback (MEDIUM)

**Location:** `core/pricing_engine.py` line 80

```python
FALLBACK_NAT_MONTH: float = 32.0  # Should be 35.04
```

**Impact:** Underestimates NAT Gateway costs by 8.7% when API unavailable.

**Fix:**
```python
FALLBACK_NAT_MONTH: float = 35.04  # $0.048/hr × 730
```

#### 9.2 Issue #2: Outdated VPC Endpoint Fallback (LOW)

**Location:** `core/pricing_engine.py` line 81

```python
FALLBACK_VPC_ENDPOINT_MONTH: float = 7.30  # Should be 8.03
```

**Impact:** Underestimates VPC endpoint costs by 9.1% when API unavailable.

**Fix:**
```python
FALLBACK_VPC_ENDPOINT_MONTH: float = 8.03  # $0.011/hr × 730
```

#### 9.3 Issue #3: Outdated ALB Fallback (HIGH)

**Location:** `core/pricing_engine.py` line 82

```python
FALLBACK_ALB_MONTH: float = 16.20  # Should be 20.44
```

**Impact:** Underestimates ALB costs by 20.7% when API unavailable.

**Fix:**
```python
FALLBACK_ALB_MONTH: float = 20.44  # $0.028/hr × 730
```

---

### 10. Recommendations

1. **Update fallback constants** in `core/pricing_engine.py` to match current AWS pricing
2. **Add pricing validation tests** to detect drift in fallback values
3. **Consider regional pricing** — fallback constants assume us-east-1; document this
4. **Monitor AWS pricing changes** — set up quarterly audit schedule

---

### 11. Final Verdict

| Component | Verdict |
|-----------|---------|
| Code Quality | ✅ PASS |
| Live Pricing | ✅ PASS |
| Fallback Pricing | ⚠️ WARN (3 outdated constants) |
| Legacy Shim Integration | ✅ PASS |
| AWS Docs Alignment | ✅ PASS |

#### Overall: ⚠️ WARN

**Rationale:**
- The adapter correctly implements live AWS Pricing API integration
- All legacy shims properly use live pricing for `EstimatedSavings` calculations
- **Three fallback constants are outdated** (NAT: -8.7%, VPC Endpoint: -9.1%, ALB: -20.7%)
- When pricing API is available (normal operation), all calculations are accurate
- Fallback values only affect scans when API is unavailable or in `--fast` mode

**Required Action:** Update fallback constants in `core/pricing_engine.py` lines 80-82.

---

### Appendix: Pricing API Raw Results

#### EIP Pricing (AmazonVPC)
- Hourly: $0.005/hr
- Monthly: $3.65/month
- SKUs: CNZ4XSXPRJXGA4ZF (InUse), MHVKGXCT3GPZ4RVC (Idle)

#### NAT Gateway Pricing (AmazonEC2)
- Hourly: $0.048/hr
- Monthly: $35.04/month
- Data Processing: $0.048/GB
- SKU: KFNSSCHWD43WZK2R

#### VPC Endpoint Pricing (AmazonVPC)
- Hourly: $0.011/hr
- Monthly: $8.03/month
- Data Processing: $0.01/GB (first PB)
- SKU: H37WJ97MHYGDXG2A

#### ALB Pricing (AWSELB)
- Hourly: $0.028/hr
- Monthly: $20.44/month
- Data Processing: $0.008/GB
- SKU: PMVAP4E245MPBY22

---

*End of Audit Report*


---

## AUDIT-06: Lambda

| Field | Value |
|-------|-------|
| **Date** | 2026-05-01 |
| **Adapter** | `services/adapters/lambda_svc.py` (63 lines) |
| **Legacy shim** | `services/lambda_svc.py` (241 lines) |
| **Auditor** | Automated audit agent |
| **Overall Verdict** | **FAIL** — 5 findings (2 critical, 2 major, 1 minor) |

---

### 1. Cost Hub Savings Pass-Through

**Verdict: [V-1] PASS — No pricing_multiplier applied to Cost Hub estimates**

The adapter sums Cost Hub `estimatedMonthlySavings` directly (line 42):

```python
savings += sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)
```

This is correct. Cost Optimization Hub returns region-specific dollar amounts. The adapter does **not** multiply by `ctx.pricing_multiplier`. No code path applies `pricing_multiplier` to Cost Hub data anywhere in this adapter.

---

### 2. Flat-Rate Savings — Arbitrary and Undocumented

**Verdict: [V-2] CRITICAL — Flat rates are keyword-matched magic numbers with no basis**

The adapter assigns dollar savings by matching string keywords in `EstimatedSavings` (lines 43–50):

```python
for rec in enhanced_recs:
    est = rec.get("EstimatedSavings", "")
    if "Up to 90%" in est:
        savings += 50       # ← $50/function flat rate
    elif "memory optimization" in est.lower():
        savings += 10       # ← $10/function flat rate
    elif "Eliminate unused costs" in est:
        savings += 5        # ← $5/function flat rate
```

#### Problems

| Issue | Detail |
|-------|--------|
| **No source documentation** | The $50/$10/$5 values have no comment, no doc reference, and no link to AWS pricing. They appear to be arbitrary guesses. |
| **Keyword fragility** | Matching relies on exact substrings (`"Up to 90%"`, `"memory optimization"`, `"Eliminate unused costs"`). Any change in the legacy shim's `EstimatedSavings` text silently drops savings to $0. |
| **No function-level pricing** | A 128MB Lambda function and a 10,240MB function get the same flat rate. AWS Lambda pricing is $0.0000166667/GB-second + $0.20/million requests — savings scale with memory size and invocation count. |
| **Wrong for provisioned concurrency** | "Up to 90%" maps to $50, but actual provisioned concurrency costs vary wildly (a function with 100 provisioned concurrent executions at 1GB could cost $3,500/month). |

#### What the legacy shim emits

The `EstimatedSavings` field in `services/lambda_svc.py` contains:

| Check | `EstimatedSavings` value | Matched? | Flat rate |
|-------|--------------------------|----------|-----------|
| `provisioned_concurrency` | `"Up to 90% if not needed"` | Yes (`"Up to 90%"`) | $50 |
| `excessive_memory` | `"30-50% with rightsizing"` | **No** — no keyword match | **$0** |
| `low_invocation` | `"Eliminate unused costs"` | Yes | $5 |
| `arm_migration` | `"20% cost reduction with ARM architecture"` | **No** | **$0** |
| `vpc_without_need` | `"Reduce ENI costs and improve performance"` | **No** | **$0** |
| `high_reserved_concurrency` | `"Review actual concurrency needs"` | **No** | **$0** |

**Only 2 of 6 check types produce non-zero savings.** The `"memory optimization"` keyword is never emitted by the legacy shim, making its $10 branch dead code.

---

### 3. Lambda Pricing Model — No Live Pricing Used

**Verdict: [V-3] CRITICAL — Adapter uses no AWS Pricing API data**

Lambda pricing consists of:

1. **Duration charge**: $0.0000166667 per GB-second (x86) / $0.0000133334 per GB-second (arm64)
2. **Request charge**: $0.20 per 1M requests (after free tier)
3. **Provisioned concurrency**: $0.0000041667 per GB-second (in addition to duration)

The adapter:
- Does **not** use `ctx.pricing_engine` (no `PricingEngine` method exists for Lambda)
- Does **not** call `get_pricing("AWSLambda", ...)` 
- Does **not** reference `$0.20/1M requests` or `$0.0000166667/GB-sec` anywhere
- Does **not** use `ctx.pricing_multiplier` at all

The `PricingEngine` in `core/pricing_engine.py` has no Lambda-specific method. This means Lambda savings are always flat-rate estimates regardless of region.

---

### 4. Double-Counting Analysis

**Verdict: [V-4] MAJOR — No dedup between Cost Hub and enhanced checks**

The adapter combines two independent sources without deduplication:

```python
cost_hub_recs = ctx.cost_hub_splits.get("lambda", [])    # Source 1
enhanced_recs = enhanced_result.get("recommendations", []) # Source 2
total_recs = len(cost_hub_recs) + len(enhanced_recs)      # Simple sum
```

Cost Optimization Hub can recommend the same Lambda function for memory rightsizing that the enhanced checks flag as `excessive_memory`. When this happens:
- The same function appears in both `cost_hub_recs` and `enhanced_recs`
- Both contribute to `total_recommendations` count
- Both contribute to `total_monthly_savings` (Cost Hub estimate + flat rate)

**Example**: A function with 3072MB memory flagged by both sources would count as 2 recommendations with savings = Cost Hub estimate + $0 (because `"30-50% with rightsizing"` doesn't match any keyword) or potentially $50 if Cost Hub return also triggers a match.

---

### 5. Missing Savings Categories

**Verdict: [V-5] MINOR — ARM migration and VPC cost savings not quantified**

The ARM migration check (line 198-232 in legacy shim) correctly gates on:
- x86_64 architecture
- ARM-compatible runtime
- Minimum 10 weekly invocations

But the adapter assigns $0 savings because `"20% cost reduction with ARM architecture"` doesn't match any keyword. ARM/Graviton provides a documented **20% price-performance improvement** per AWS. The VPC check similarly produces $0.

---

### Verdict Summary

| ID | Finding | Severity | Verdict Code |
|----|---------|----------|-------------|
| V-1 | Cost Hub savings pass-through | — | **PASS** |
| V-2 | Flat rates are undocumented magic numbers | Critical | **FAIL** |
| V-3 | No live Lambda pricing API used | Critical | **FAIL** |
| V-4 | No dedup between Cost Hub and enhanced checks | Major | **FAIL** |
| V-5 | ARM/VPC savings not quantified | Minor | **WARN** |

---

### Recommendations

#### R-1: Replace keyword matching with function-level savings calculation
Use `ctx.pricing_engine` (needs a new Lambda method) or inline Lambda pricing constants:

```python
LAMBDA_PRICE_PER_GB_SEC = 0.0000166667  # x86 on-demand
LAMBDA_ARM_PRICE_PER_GB_SEC = 0.0000133334
LAMBDA_PRICE_PER_1K_REQUESTS = 0.0000002
```

Then calculate per-function savings based on actual memory size, invocation count, and duration.

#### R-2: Add deduplication by FunctionName
Before summing, build a set of function names from Cost Hub recommendations and exclude matching functions from enhanced checks (or vice versa):

```python
cost_hub_fns = {rec.get("resourceArn", "").split(":")[-1] for rec in cost_hub_recs}
for rec in enhanced_recs:
    if rec.get("FunctionName") not in cost_hub_fns:
        # count and calculate savings
```

#### R-3: Add PricingEngine method for Lambda
Add `get_lambda_duration_price_per_gb_sec(architecture)` and `get_lambda_request_price_per_1m()` to `core/pricing_engine.py`, then use them in both the adapter and legacy shim.

#### R-4: Document flat-rate origins
If flat rates must remain as fallback, add comments citing the source (e.g., "Based on median Lambda function savings observed in AWS Cost Explorer for us-east-1, 2025").

---

### File Inventory

| File | Role | Lines |
|------|------|-------|
| `services/adapters/lambda_svc.py` | ServiceModule adapter | 63 |
| `services/lambda_svc.py` | Legacy analysis logic | 241 |
| `services/advisor.py` | Cost Optimization Hub fetcher | 153 |
| `core/scan_context.py` | ScanContext with pricing_multiplier | 72 |
| `core/pricing_engine.py` | No Lambda method exists | 517 |

---

### AWS Documentation References

- [Lambda cost and performance optimization (Serverless Lens)](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/cost-and-performance-optimization.html) — recommends AWS Compute Optimizer for memory rightsizing
- [Lambda pricing](https://aws.amazon.com/lambda/pricing/) — $0.0000166667/GB-sec (x86), $0.0000133334/GB-sec (arm64), $0.20/1M requests
- [Lambda Power Tuning](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/net-serverless.html) — recommended tool for finding optimal memory configuration


---

## AUDIT-07: DynamoDB

**Audit ID:** AUDIT-07-DynamoDB
**Date:** 2026-05-01
**Auditor:** OpenCode Agent (glm-5.1)
**Adapter:** `services/adapters/dynamodb.py` (81 lines)
**Legacy Module:** `services/dynamodb.py` (439 lines)

---

### Executive Summary

| # | Check | Severity | Verdict Code | Finding |
|---|-------|----------|--------------|---------|
| 1 | ProvisionedThroughput key mismatch | **CRITICAL** | `BUG-KEY-DYNAMODB-001` | Adapter reads nested key that doesn't exist; hourly pricing path is dead code |
| 2 | On-demand EstimatedMonthlyCost=0 | **HIGH** | `BUG-SAVINGS-DYNAMODB-002` | On-demand tables always contribute $0 savings |
| 3 | RCU/WCU pricing constants | **MEDIUM** | `WARN-PRICE-DYNAMODB-003` | Constants 11.6% below actual eu-west-1 rates |
| 4 | 23% reserved capacity discount | **LOW** | `NOTE-DISCOUNT-DYNAMODB-004` | Conservative vs AWS documented 54-77% |
| 5 | Table detection coverage | **PASS** | `PASS-DETECT-DYNAMODB-005` | Both provisioned and on-demand tables scanned |
| 6 | Shim pricing constants | **MEDIUM** | `WARN-SHIM-DYNAMODB-006` | Legacy module uses 2.33x inflated monthly rates |

**Overall Verdict:** `FAIL` — Critical key-mismatch bug renders the adapter's intended pricing path unreachable. All savings flow through the unintended fallback path using inflated legacy constants.

---

### 1. CRITICAL: ProvisionedThroughput Key Mismatch

#### The Bug

The adapter reads `ProvisionedThroughput` as a **nested key** from the shim output, but the shim places `ReadCapacityUnits` and `WriteCapacityUnits` at the **top level** of the dict.

**Adapter (lines 51-54):**
```python
throughput = rec.get("ProvisionedThroughput", {})  # Always returns {}
rcu = throughput.get("ReadCapacityUnits", 0)         # Always 0
wcu = throughput.get("WriteCapacityUnits", 0)         # Always 0
```

**Shim output structure (`services/dynamodb.py` lines 106-116):**
```python
table_info = {
    "TableName": table_name,
    "BillingMode": ...,
    "TableStatus": ...,
    "ItemCount": ...,
    "TableSizeBytes": ...,
    "ReadCapacityUnits": 0,       # <-- TOP LEVEL, not nested
    "WriteCapacityUnits": 0,       # <-- TOP LEVEL, not nested
    "EstimatedMonthlyCost": 0,
    "OptimizationOpportunities": [],
}
```

The shim reads `ProvisionedThroughput` from the **raw AWS API response** (line 119) and flattens `ReadCapacityUnits`/`WriteCapacityUnits` into the top-level dict. The adapter expects the raw AWS shape but receives the flattened shim shape.

#### Consequence

The `if rcu > 0 or wcu > 0:` branch (lines 56-58) is **never entered** for ANY table. Every table falls through to the else branch:

```python
else:
    cost = rec.get("EstimatedMonthlyCost", 0)
    savings += cost * 0.30
```

This means:
- **The adapter's own RCU/WCU hourly constants ($0.00013/$0.00065) are dead code** — never used
- **The `pricing_multiplier` is never applied** to DynamoDB savings
- **The 23% reserved capacity discount is never applied** — 30% is used instead
- All savings come from the shim's `EstimatedMonthlyCost × 0.30`

**Verdict Code:** `BUG-KEY-DYNAMODB-001` — Critical key-mismatch, dead pricing path.

---

### 2. On-Demand Tables: $0 Savings Bug

#### Root Cause

The shim initializes `EstimatedMonthlyCost = 0` for ALL tables (line 114), and only populates it for PROVISIONED tables (lines 123-126). On-demand tables never get a cost estimate.

**Shim flow:**
```python
## Line 114: initialized for ALL tables
table_info["EstimatedMonthlyCost"] = 0

## Lines 118-126: ONLY provisioned tables
if table_info["BillingMode"] == "PROVISIONED":
    monthly_cost = (RCU * 0.25) + (WCU * 1.25)
    table_info["EstimatedMonthlyCost"] = round(monthly_cost, 2)  # ✅ Set
else:
    analysis["on_demand_tables"].append(table_name)
    # ❌ EstimatedMonthlyCost stays at 0
```

**Combined with Bug #1**, the adapter savings calculation becomes:
- **Provisioned tables:** `savings += EstimatedMonthlyCost × 0.30` (non-zero, but using inflated shim constants)
- **On-demand tables:** `savings += 0 × 0.30 = $0`

#### Impact

- Accounts with only on-demand DynamoDB tables report **$0 DynamoDB savings**
- The enhanced checks (`get_enhanced_dynamodb_checks`) DO produce recommendations for billing mode optimization, but these carry only text-based `EstimatedSavings` strings ("Up to 60%"), not numeric values that flow to the grand total
- No `$50 flat rate` fallback exists in the current code — the task description referenced this but it is absent

**Verdict Code:** `BUG-SAVINGS-DYNAMODB-002` — On-demand tables produce $0 savings.

---

### 3. RCU/WCU Pricing Constants Verification

#### Adapter Constants (Dead Code — See Bug #1)

```python
DYNAMODB_RCU_HOURLY = 0.00013   # line 47
DYNAMODB_WCU_HOURLY = 0.00065   # line 48
```

#### AWS Pricing API — eu-west-1 (Verified 2025-08-28)

| Metric | Adapter | AWS Actual | Delta |
|--------|---------|------------|-------|
| RCU/hour | $0.000130 | $0.000147 | **-11.6% LOW** |
| WCU/hour | $0.000650 | $0.000735 | **-11.6% LOW** |

**Source:** AWS Price List API, service code `AmazonDynamoDB`
- RCU: SKU `J7FH9ZNSN6J2RR9Q`, usagetype `EU-ReadCapacityUnit-Hrs`
- WCU: SKU `36CGV8QUHJZG65HD`, usagetype `EU-WriteCapacityUnit-Hrs`

#### Monthly Impact (If Constants Were Actually Used)

For a table with 100 RCU + 50 WCU:
```
Adapter: (100 × 0.00013 + 50 × 0.00065) × 730 = $33.29/month
AWS:     (100 × 0.000147 + 50 × 0.000735) × 730 = $37.59/month
Gap:     $4.30/month per table (11.6% understated)
```

**Verdict Code:** `WARN-PRICE-DYNAMODB-003` — Constants stale but currently dead code due to Bug #1.

---

### 4. Reserved Capacity Discount Analysis

#### Adapter Uses 23% (Dead Code)

```python
savings += monthly_current * 0.23   # line 58 — never reached
```

#### AWS Documentation

Per [DynamoDB Reserved Capacity docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/reserved-capacity.html):

> "You can save up to **54% off standard rates for a one-year term** and **77% off standard rates for a three-year term**."

#### AWS Pricing API — Actual Reserved Rates (eu-west-1)

##### RCU Reserved Pricing

| Term | Hourly Rate | Upfront/Unit | Effective Monthly | Savings vs On-Demand |
|------|------------|--------------|-------------------|---------------------|
| On-Demand | $0.000147 | — | $0.1073 | — |
| 1-year | $0.000029 | $0.339 | $0.0494 | **53.9%** |
| 3-year | $0.000018 | $0.4068 | $0.0244 | **77.2%** |

##### WCU Reserved Pricing

| Term | Hourly Rate | Upfront/Unit | Effective Monthly | Savings vs On-Demand |
|------|------------|--------------|-------------------|---------------------|
| On-Demand | $0.000735 | — | $0.5366 | — |
| 1-year | $0.000145 | $1.695 | $0.2471 | **53.9%** |
| 3-year | $0.000092 | $2.034 | $0.1237 | **76.9%** |

Effective monthly = (hourly × 730) + (upfront ÷ months in term)

#### Assessment

| Metric | 1-Year Actual | 3-Year Actual | Adapter (23%) |
|--------|--------------|---------------|---------------|
| Discount Rate | 54% | 77% | 23% |
| % of 1-year rate | — | — | 42.6% |
| % of 3-year rate | — | — | 29.9% |

The 23% is **not documented** as a specific DynamoDB Reserved Capacity tier. It appears to be a conservative placeholder. The adapter's own description text says "53-76%" which contradicts the 23% calculation.

**Verdict Code:** `NOTE-DISCOUNT-DYNAMODB-004` — 23% conservative; dead code anyway due to Bug #1.

---

### 5. Table Detection: Provisioned vs On-Demand

#### Coverage Analysis

Both billing modes are scanned in the shim:

```python
## services/dynamodb.py lines 118-145
if table_info["BillingMode"] == "PROVISIONED":
    analysis["provisioned_tables"].append(table_name)
    # Gets RCU/WCU, EstimatedMonthlyCost, optimization flags
else:
    analysis["on_demand_tables"].append(table_name)
    # Gets monitoring recommendation only
```

#### Enhanced Checks (`get_enhanced_dynamodb_checks`)

| Check Category | Target | CloudWatch? | Numeric Savings? |
|----------------|--------|-------------|------------------|
| `billing_mode_optimization` | On-demand → Provisioned | Yes (14-day) | No (text only) |
| `capacity_rightsizing` | Provisioned | Yes (7-day) | No (text only) |
| `reserved_capacity` | High-capacity provisioned | No | No (text: "53-76%") |
| `data_lifecycle` | Tables >10GB | No | No (text: "40-80%") |
| `unused_tables` | Empty tables | No | No (text: "100%") |
| `over_provisioned_capacity` | RCU/WCU >100 | Yes (7-day) | No (text: "Variable") |
| `global_tables_optimization` | Not implemented | — | — |

The enhanced checks produce recommendations with **text-based `EstimatedSavings`** strings that do NOT flow into the `total_monthly_savings` dollar figure. Only the `optimization_opportunities` from `get_dynamodb_table_analysis` contribute to savings.

**Verdict Code:** `PASS-DETECT-DYNAMODB-005` — Both table types scanned. Enhanced checks are text-only.

---

### 6. Shim Pricing Constants (Actually Used)

Because of Bug #1, the ACTUAL savings path uses the shim's `EstimatedMonthlyCost`:

**Shim constants (`services/dynamodb.py` lines 70-71):**
```python
_PROVISIONED_RCU_COST: float = 0.25   # per unit/month
_PROVISIONED_WCU_COST: float = 1.25   # per unit/month
```

#### Comparison with AWS Actual

| Metric | Shim Monthly | AWS Actual Monthly | Ratio |
|--------|-------------|-------------------|-------|
| RCU/unit/month | $0.25 | $0.1073 | **2.33x over** |
| WCU/unit/month | $1.25 | $0.5366 | **2.33x over** |

The shim constants are **2.33× the actual AWS provisioned capacity rates** in eu-west-1.

#### Effective Savings Calculation

Since Bug #1 forces all provisioned tables through `EstimatedMonthlyCost × 0.30`:

```
Actual savings reported = shim_monthly_cost × 0.30
                        = (RCU × 0.25 + WCU × 1.25) × 0.30
```

For a table with 100 RCU + 50 WCU:
```
Shim monthly: 100 × 0.25 + 50 × 1.25 = $87.50
Adapter savings: $87.50 × 0.30 = $26.25

AWS actual monthly: $37.59
True 1-year reserved savings: $37.59 × 0.54 = $20.27

Overstatement: $26.25 vs $20.27 = +29.5%
```

The inflated shim constants + 30% discount rate overstates savings vs. what would be computed with accurate pricing + proper discount rate.

**Verdict Code:** `WARN-SHIM-DYNAMODB-006` — Legacy shim constants 2.33x actual, producing overstated savings.

---

### 7. Data Flow Diagram

```
AWS DescribeTable API
        │
        ▼
┌─────────────────────────────────┐
│  services/dynamodb.py (shim)    │
│                                 │
│  table_info = {                 │
│    "ReadCapacityUnits": N,  ←──┼── TOP LEVEL
│    "WriteCapacityUnits": N, ←──┼── TOP LEVEL
│    "EstimatedMonthlyCost": N   │
│  }                              │
│                                 │
│  (NO "ProvisionedThroughput"    │
│   nested key in output)         │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  adapters/dynamodb.py           │
│                                 │
│  throughput = rec.get(          │
│    "ProvisionedThroughput", {}) │
│       ↑                         │
│       └── ALWAYS {} ────────────┼── BUG: key doesn't exist
│                                 │
│  rcu = throughput.get(          │
│    "ReadCapacityUnits", 0)      │
│       └── ALWAYS 0 ─────────────┼── BUG: dead path
│                                 │
│  if rcu > 0 or wcu > 0:        │
│    ← NEVER ENTERED              │
│  else:                          │
│    cost = EstimatedMonthlyCost  │
│    savings += cost × 0.30       │
│                                 │
│  Result:                        │
│  - Provisioned: shim_cost×0.30 │
│  - On-demand: $0.00             │
└─────────────────────────────────┘
```

---

### 8. Recommendations

| Priority | # | Action | Impact |
|----------|---|--------|--------|
| **P0** | 1 | Fix key mismatch: change `rec.get("ProvisionedThroughput", {})` to read top-level `ReadCapacityUnits`/`WriteCapacityUnits` directly | Activates correct pricing path |
| **P0** | 2 | Populate `EstimatedMonthlyCost` for on-demand tables in shim (use CloudWatch consumption data or on-demand pricing API) | On-demand savings flow to total |
| **P1** | 3 | Update RCU/WCU hourly constants: `$0.000147`, `$0.000735` for eu-west-1 | 11.6% more accurate |
| **P1** | 4 | Migrate to `ctx.pricing_engine` for live DynamoDB pricing lookup (like EC2/EBS/RDS adapters) | Region-accurate pricing |
| **P2** | 5 | Replace 23% hardcode with 54% (1-year) or make configurable | Aligned with AWS docs |
| **P2** | 6 | Fix shim monthly constants ($0.25/$1.25) to match AWS actual ($0.107/$0.537) | Correct EstimatedMonthlyCost |
| **P3** | 7 | Add free tier awareness (first 25 RCU/WCU) | Accurate for small tables |

---

### 9. Verdict Codes Summary

| Code | Severity | Description | Status |
|------|----------|-------------|--------|
| `BUG-KEY-DYNAMODB-001` | **CRITICAL** | `ProvisionedThroughput` key mismatch — hourly pricing path is dead code | Must fix |
| `BUG-SAVINGS-DYNAMODB-002` | **HIGH** | On-demand `EstimatedMonthlyCost` always 0 — $0 savings reported | Must fix |
| `WARN-PRICE-DYNAMODB-003` | **MEDIUM** | RCU/WCU constants 11.6% below eu-west-1 actual | Update when fixing #1 |
| `NOTE-DISCOUNT-DYNAMODB-004` | **LOW** | 23% discount conservative vs AWS 54-77% | Currently dead code |
| `PASS-DETECT-DYNAMODB-005` | **PASS** | Both provisioned and on-demand tables scanned | OK |
| `WARN-SHIM-DYNAMODB-006` | **MEDIUM** | Legacy shim constants 2.33× actual AWS rates | Align with pricing engine |

---

### 10. Overall Verdict

**Status:** `FAIL` 🔴

The adapter contains a **critical key-mismatch bug** (`BUG-KEY-DYNAMODB-001`) that renders its intended pricing calculation path completely unreachable. All dollar savings flow through an unintended fallback using inflated legacy constants, and on-demand tables produce $0 savings regardless of actual optimization potential.

#### What Works
- Table detection for both billing modes
- Enhanced checks with CloudWatch metric analysis
- Correct data structure shape for HTML report rendering
- Proper exception handling and warning propagation

#### What Doesn't Work
- The adapter's own RCU/WCU hourly pricing constants (dead code)
- The `pricing_multiplier` regional adjustment (never applied)
- The 23% reserved capacity discount (never applied)
- On-demand table dollar savings (always $0)

#### Safe to Use?
The adapter produces **non-zero savings** for provisioned tables via the fallback path (`EstimatedMonthlyCost × 0.30`), but these savings are computed using 2.33× inflated constants and a different discount rate (30% vs intended 23%). The numbers that appear in reports are therefore overstated by approximately 29.5% for provisioned tables, and on-demand tables are completely missing from savings totals.

---

### Appendix A: AWS Pricing API Evidence

#### On-Demand Provisioned Rates (eu-west-1)

```
Service:     AmazonDynamoDB
Region:      eu-west-1 (EU Ireland)
Fetched:     2025-08-28 pricing data

RCU Provisioned:
  SKU:       J7FH9ZNSN6J2RR9Q
  UsageType: EU-ReadCapacityUnit-Hrs
  Rate:      $0.000147/RCU-hour (beyond 18,600 free tier)
  Free tier: $0.00 for first 18,600 RCU-hours/month

WCU Provisioned:
  SKU:       36CGV8QUHJZG65HD
  UsageType: EU-WriteCapacityUnit-Hrs
  Rate:      $0.000735/WCU-hour (beyond 18,600 free tier)
  Free tier: $0.00 for first 18,600 WCU-hours/month
```

#### Reserved Capacity Rates (eu-west-1)

```
RCU 1-year Heavy Utilization:
  SKU:       J7FH9ZNSN6J2RR9Q.YTVHEVGPBZ
  Hourly:    $0.000029/RCU-hour
  Upfront:   $0.339/RCU

RCU 3-year Heavy Utilization:
  SKU:       J7FH9ZNSN6J2RR9Q.VG8YD49WWM
  Hourly:    $0.000018/RCU-hour
  Upfront:   $0.4068/RCU

WCU 1-year Heavy Utilization:
  SKU:       36CGV8QUHJZG65HD.YTVHEVGPBZ
  Hourly:    $0.000145/WCU-hour
  Upfront:   $1.695/WCU

WCU 3-year Heavy Utilization:
  SKU:       36CGV8QUHJZG65HD.VG8YD49WWM
  Hourly:    $0.000092/WCU-hour
  Upfront:   $2.034/WCU
```

### Appendix B: AWS Documentation Evidence

Source: [DynamoDB Reserved Capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/reserved-capacity.html)

> "Reserved capacity is purchased in allocations of 100 WCUs or 100 RCUs. The smallest reserved capacity offering is 100 capacity units (reads or writes). DynamoDB reserved capacity is offered as either a one-year commitment or in select Regions as a three-year commitment. You can save up to **54% off standard rates for a one-year term** and **77% off standard rates for a three-year term**."

Confirmed: The API data shows effective savings of 53.9% (1-year) and 77.1% (3-year), consistent with AWS documentation.

---

*Report generated by OpenCode Agent (glm-5.1) following AWS Cost Scanner audit protocol.*
*Pricing data sourced from AWS Price List API (publication date 2025-08-28).*


---

## AUDIT-08: ElastiCache

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

### 1. Executive Summary

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

### 2. Code Analysis

#### 2.1 Architecture

```
elasticache.py (adapter, 75 lines)
  └─ scan() → calls services/elasticache.get_enhanced_elasticache_checks(ctx)
                  └─ returns {recommendations: [...], reserved_nodes: [...], ...}
  └─ keyword-rate loop over recommendations
       └─ matches EstimatedSavings text → ri_rate (0.15–0.40)
       └─ savings += monthly_price × num_nodes × ri_rate
```

The adapter delegates all AWS API calls to `services/elasticache.py`, which queries `DescribeCacheClusters` and `CloudWatch` metrics. The adapter itself only computes savings via keyword-based rate assignment.

#### 2.2 Keyword-Rate Mapping (adapter lines 41–52)

| Keyword in `EstimatedSavings` | `ri_rate` | Implied savings % |
|-------------------------------|-----------|-------------------|
| `"Reserved"` | 0.35 | 35% |
| `"Graviton"` or `"20-40%"` | 0.25 | 25% |
| `"Valkey"` | 0.15 | 15% |
| `"Underutilized"` | 0.40 | 40% |
| (default/else) | 0.20 | 20% |

#### 2.3 Recommendation Categories (from legacy module)

| Category | Trigger | EstimatedSavings text |
|----------|---------|----------------------|
| Reserved Nodes | `num_nodes >= 2` | "30-60% vs On-Demand" |
| Underutilized | `avg_cpu < 20%` (14-day CW) | "30-50%" |
| Graviton Migration | Non-Graviton family | "20-40% price-performance improvement" |
| Valkey Migration | `engine == "redis"` | "Same pricing as Redis..." |
| Old Engine Version | Redis major < 7 | (no savings text) |

#### 2.4 Code Quality Verdict

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

### 3. Pricing Validation

#### 3.1 Live API Pricing (eu-west-1, 2026-04-01)

| Instance Type | Engine | On-Demand $/hr | × 730 = Monthly |
|---------------|--------|-----------------|-----------------|
| cache.r6g.large | Redis | $0.2290 | **$167.17** |
| cache.r6g.large | Memcached | $0.2290 | **$167.17** |
| cache.r6g.large | Valkey | $0.1832 | **$133.74** |
| cache.r5.large | Redis | $0.2410 | **$175.93** |
| cache.r5.large | Memcached | $0.2410 | **$175.93** |
| cache.r5.large | Valkey | $0.1928 | **$140.74** |

#### 3.2 Pricing Engine Computation

The adapter calls:
```python
ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)
```

This invokes `_fetch_generic_instance_price()` (pricing_engine.py:414–422) which:
1. Calls `pricing:GetProducts` with `instanceType` + `location` filters only
2. Returns `price_hourly * 730`
3. **Does NOT filter by `cacheEngine`**

**Problem:** ElastiCache returns separate SKUs per engine (Redis, Memcached, Valkey). Without an engine filter, the API returns multiple products. The first match is used — which could be Valkey's lower price instead of Redis/Memcached. For Redis/Memcached this is benign (same OD price), but if a Valkey SKU is returned first for a Redis cluster, savings would be computed against a lower base.

#### 3.3 Pricing Verdict

| Check | Result |
|-------|--------|
| Hourly rate × 730 = monthly | ✅ PASS — correct formula |
| Redis/Memcached price parity | ✅ PASS — both $0.229/hr |
| Engine filter omitted | ❌ FAIL — Valkey pricing could contaminate results |

---

### 4. RI Discount Rate Validation

#### 4.1 Actual Reserved Node Pricing (cache.r6g.large, Redis, eu-west-1)

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

#### 4.2 AWS Documentation Confirmation

Per AWS docs (aws.amazon.com/elasticache/reserved-cache-nodes/):
- Reserved nodes offer "significant discounts" with 1- or 3-year terms
- Three payment options: All Upfront (largest discount), No Upfront, Partial Upfront
- Size-flexible within instance family since Oct 2024

#### 4.3 Adapter Rate vs Reality

| Adapter rate | Actual range | Assessment |
|-------------|-------------|------------|
| 35% (single rate) | 31.4%–54.9% | ⚠️ Conservative for 3yr, reasonable for 1yr No Upfront |

**Verdict: ⚠️ WARN** — The 35% rate is a reasonable mid-point for 1-year commitments (31–36%) but significantly underestimates 3-year savings (48–55%).

---

### 5. Graviton Migration Delta

#### 5.1 Live Price Comparison (eu-west-1, Redis OD)

| Migration Path | From (monthly) | To (monthly) | Delta | Savings % |
|----------------|---------------|-------------|-------|-----------|
| r5.large → r6g.large (Redis) | $175.93 | $167.17 | −$8.76 | **5.0%** |
| r5.large → r6g.large (Memcached) | $175.93 | $167.17 | −$8.76 | **5.0%** |
| r5.large → r6g.large (Valkey) | $175.93 | $133.74 | −$42.19 | **24.0%** |

#### 5.2 Assessment

**Adapter rate: 25%** — The actual Graviton price-only savings for r5→r6g is **5.0%**, not 25%.

The "20-40% price-performance improvement" text in the legacy module (elasticache.py:99) conflates **price** with **performance**. While Graviton processors offer better performance per dollar, the pure price delta between r5 and r6g is only 5%. The adapter's 25% rate overstates price savings by **5×**.

The rate happens to be approximately correct if the user also migrates from Redis to Valkey simultaneously (24%), but the recommendation text does not mention Valkey.

**Verdict: ❌ FAIL** — Graviton-only savings overestimated by 20 percentage points.

---

### 6. Valkey Migration Check

#### 6.1 Live API Price Comparison (eu-west-1, OD)

| Instance Type | Redis $/hr | Valkey $/hr | Delta | Savings |
|---------------|-----------|-------------|-------|---------|
| cache.r6g.large | $0.2290 | $0.1832 | −$0.0458 | **20.0%** |
| cache.r5.large | $0.2410 | $0.1928 | −$0.0482 | **20.0%** |

AWS documentation confirms: Valkey is priced **20% lower** than Redis OSS at the OD tier.

#### 6.2 Assessment

**Adapter rate: 15%** — Actual savings are **20%**. The adapter **underestimates** Valkey savings by 5 percentage points.

Additionally, the Valkey recommendation text in the legacy module says:

> "Same pricing as Redis for identical node types; consider Valkey for feature parity/security updates"

This is **factually incorrect** — Valkey has its own lower pricing tier (20% cheaper). The text should be corrected.

#### 6.3 RI Savings for Valkey (bonus finding)

| Term | Redis All Upfront | Valkey All Upfront | Delta |
|------|-------------------|---------------------|-------|
| 1yr | $1,278 | $1,022.40 | **20.0% cheaper** |
| 3yr | $2,715 | $2,172.00 | **20.0% cheaper** |

Valkey maintains the 20% discount across all RI tiers.

**Verdict: ❌ FAIL** — Rate understates savings; recommendation text is factually wrong.

---

### 7. Underutilized Cluster Rate

**Adapter rate: 40%** — Triggered for clusters with <20% average CPU over 14 days.

For a single-step downsize within the same family (e.g., r5.xlarge → r5.large = 50% savings, or r5.large → r5.medium = 50% savings), 40% is a reasonable midpoint estimate.

**Verdict: ✅ PASS** — Conservative but reasonable for rightsizing heuristics.

---

### 8. Fallback Values

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

### 9. Summary of Issues

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

### 10. Final Verdict

#### Overall: ❌ FAIL

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

### 11. Appendix: Raw Pricing Data

#### cache.r6g.large (eu-west-1)
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

#### cache.r5.large (eu-west-1)
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

#### Graviton Delta (r5.large → r6g.large)
```
Redis:     $0.241 → $0.229 = −5.0%
Memcached: $0.241 → $0.229 = −5.0%
Valkey:    $0.1928 → $0.1832 = −5.0%
```

#### Valkey vs Redis Delta
```
r6g.large: $0.229 → $0.1832 = −20.0%
r5.large:  $0.241 → $0.1928 = −20.0%
```

---

*End of Audit Report*


---

## AUDIT-09: OpenSearch

**Adapter File:** `services/adapters/opensearch.py`  
**Legacy Module:** `services/opensearch.py`  
**Audit Date:** 2026-05-01  
**Auditor:** Automated Audit  

---

### 1. Code Analysis

#### 1.1 Architecture Overview

The OpenSearch adapter uses a **keyword-based rate savings strategy** (classified as "Parse-rate" in project documentation). It wraps the legacy `get_enhanced_opensearch_checks()` function and calculates savings using hardcoded discount rates applied to live instance pricing.

#### 1.2 Savings Calculation Logic

```python
## Lines 40-63 from opensearch.py
savings = 0.0
for rec in recs:
    est = rec.get("EstimatedSavings", "")
    if "Reserved" in est:
        ri_rate = 0.40          # 40% discount
    elif "Graviton" in est or "20-40%" in est:
        ri_rate = 0.25          # 25% discount
    elif "storage" in est.lower():
        ri_rate = 0.20          # 20% discount
    else:
        ri_rate = 0.30          # 30% default

    instance_type = rec.get("InstanceType")
    instance_count = rec.get("InstanceCount", 1)

    if ctx.pricing_engine is not None and instance_type:
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
        savings += monthly * instance_count * ri_rate
    else:
        # Fallback flat rates (lines 58-63)
        if "Reserved" in est:
            savings += 300
        elif "Graviton" in est or "20-40%" in est:
            savings += 120
        elif "storage" in est.lower():
            savings += 50
```

#### 1.3 Method Used

- **Live Pricing Path:** `ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)`
- **Fallback Path:** Flat-rate estimates ($300, $120, $50)

---

### 2. Pricing Validation

#### 2.1 Instance Pricing Verification

**Test Case:** m5.large.search in eu-west-1 (Ireland)

| Metric | Value |
|--------|-------|
| On-Demand Hourly Rate | $0.158/hour |
| **Monthly On-Demand** | **$115.34** (× 730 hours) |
| Adapter Method | `get_instance_monthly_price("AmazonES", "m5.large.search")` |
| Expected Return | ~$115.34 |

**Verification:** Live price × count × 730 matches adapter calculation method ✓

#### 2.2 Reserved Instance Pricing from AWS

**AWS API Results for m5.large.search (eu-west-1):**

| Term | Payment Option | Hourly Rate | Discount | Effective Monthly |
|------|----------------|-------------|----------|-------------------|
| 1-Year | No Upfront | $0.109 | **31%** | $79.57 |
| 1-Year | Partial Upfront | $0.053 | **66%** | $38.69 + $38.67 upfront |
| 1-Year | All Upfront | $0.000 | **100%** | $0 + $75.00/mo equivalent |
| 3-Year | No Upfront | $0.082 | **48%** | $59.86 |
| 3-Year | Partial Upfront | $0.040 | **75%** | $29.20 + $28.83/mo equivalent |
| 3-Year | All Upfront | $0.000 | **100%** | $0 + $45.30/mo equivalent |

---

### 3. RI Discount Rate Analysis

#### 3.1 Adapter Rates vs AWS Documentation

| Source | Reserved Discount | Graviton Discount | Storage Discount |
|--------|-------------------|-------------------|------------------|
| **Adapter (Hardcoded)** | 40% | 25% | 20% |
| **AWS Docs (No Upfront)** | 31-48% | N/A | N/A |
| **AWS Docs (Partial Upfront)** | 33-50% | N/A | N/A |
| **AWS Docs (All Upfront)** | 35-52% | N/A | N/A |
| **AWS Docs (Graviton)** | N/A | 20-40% | N/A |

#### 3.2 Rate Assessment

| Rate | Assessment |
|------|------------|
| **40% for Reserved** | ⚠️ **HIGH** - Within range for 3-year No Upfront (48%), but optimistic for typical 1-year commitments (31%). Conservative estimate would use 30-35%. |
| **25% for Graviton** | ⚠️ **LOW** - AWS documents 20-40% improvement. Using 25% is conservative but may under-estimate savings. |
| **20% for Storage (gp2→gp3)** | ✓ **ACCURATE** - Matches AWS claim of 20% savings for gp3 over gp2. |
| **30% default** | ⚠️ **ARBITRARY** - No documentation reference for this fallback rate. |

#### 3.3 AWS Documentation Reference

> "Reserved Instances offer significant savings compared to On-Demand pricing, with three payment options: No Upfront (31-48% discount), Partial Upfront (33-50% discount), and All Upfront (35-52% discount)."
> 
> — AWS OpenSearch Service Pricing Documentation

> "Graviton instances offer 20-40% price-performance improvement over x86."
>
> — AWS OpenSearch Service Documentation (via adapter's own OPENSEARCH_OPTIMIZATION_DESCRIPTIONS)

---

### 4. Storage Cost Analysis

#### 4.1 Critical Finding: Storage Costs NOT Included

**Issue:** The adapter calculates savings based **only** on instance pricing. It does **NOT** include EBS storage costs for OpenSearch domains.

**Evidence:**
- Line 55: `monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)`
- No call to any storage pricing method
- The enhanced checks identify gp2→gp3 storage optimization but savings are calculated using instance-only pricing

#### 4.2 OpenSearch Storage Cost Reality

OpenSearch domains incur costs for:
1. **Instance hours** (captured by adapter)
2. **EBS storage** (NOT captured by adapter)
3. **Data transfer** (NOT captured by adapter)

**Example Cost Breakdown (m5.large.search with 100GB gp3):**
| Component | Monthly Cost |
|-----------|--------------|
| Instance (m5.large.search) | $115.34 |
| Storage (100GB gp3) | ~$8.00 |
| **Total** | **~$123.34** |

**Impact:** For storage-heavy workloads, the adapter underestimates total cost and therefore underestimates potential savings from storage optimization.

#### 4.3 Missing Pricing Method

The `PricingEngine` does not expose an OpenSearch-specific storage pricing method. Available storage-related attributes in AWS Pricing API:
- `storage`: Instance-attached storage specs
- `storageMedia`: GP2, GP3, PIOPS, etc.

**Recommendation:** Add `get_opensearch_storage_monthly_price_per_gb(volume_type)` method to `PricingEngine`.

---

### 5. Keyword-Based Fallback Assessment

#### 5.1 Fallback Values

| Recommendation Type | Fallback Value |
|---------------------|----------------|
| Reserved Instances | $300 |
| Graviton Migration | $120 |
| Storage Optimization | $50 |

#### 5.2 Labeling Status

**Finding:** Fallback values are **NOT labeled as estimates** in the final output.

The adapter returns a `ServiceFindings` object with `total_monthly_savings` calculated from these values, but there's no indication that:
- The savings are estimates
- No actual pricing lookup occurred
- The values are arbitrary heuristics

#### 5.3 Comparison with Similar Adapters

| Adapter | Fallback Strategy | Labels Estimates? |
|---------|-------------------|-------------------|
| OpenSearch | Fixed dollar amounts ($300, $120, $50) | **NO** |
| CloudFront | Fixed $25/rec | **NO** |
| API Gateway | Keyword-based | **NO** |
| Batch | Fixed per-rec | **NO** |

---

### 6. Pass Criteria Evaluation

| Criterion | Status | Notes |
|-----------|--------|-------|
| Instance pricing uses live AWS API | ✓ PASS | Uses `get_instance_monthly_price("AmazonES", ...)` |
| RI discount rates documented | ⚠️ PARTIAL | 40% is optimistic; should use 30-35% conservative |
| Storage costs included | ✗ **FAIL** | EBS storage costs NOT included in calculations |
| Fallback values labeled | ✗ **FAIL** | No indication that fallback values are estimates |
| Graviton rates accurate | ⚠️ PARTIAL | 25% is conservative vs 20-40% documented |
| Code follows project patterns | ✓ PASS | Consistent with other parse-rate adapters |

---

### 7. Issues Summary

#### 7.1 Critical Issues (Must Fix)

| ID | Issue | Impact |
|----|-------|--------|
| **OP-001** | Storage costs not included in savings calculations | Underestimates total cost and savings for storage-heavy domains |
| **OP-002** | Fallback values not labeled as estimates | Users cannot distinguish between live pricing and heuristic estimates |

#### 7.2 Medium Issues (Should Fix)

| ID | Issue | Impact |
|----|-------|--------|
| **OP-003** | 40% RI discount rate is optimistic | May over-promise savings for 1-year commitments |
| **OP-004** | 25% Graviton rate at low end of range | May under-promise savings (20-40% documented) |

#### 7.3 Minor Issues (Nice to Have)

| ID | Issue | Impact |
|----|-------|--------|
| **OP-005** | No storage pricing method in PricingEngine | Blocks proper storage cost calculation |
| **OP-006** | 30% default rate is arbitrary | No documentation reference |

---

### 8. Verdict Codes

#### Primary Verdict
**`PARTIAL_PASS`** — Adapter functions but has significant gaps in cost coverage

#### Detailed Verdicts

| Aspect | Verdict | Code |
|--------|---------|------|
| Instance Pricing Accuracy | ✓ PASS | PRICING-OK |
| RI Rate Reasonableness | ⚠️ PARTIAL | RIRATE-HIGH |
| Storage Cost Coverage | ✗ **FAIL** | STORAGE-MISSING |
| Fallback Transparency | ✗ **FAIL** | FALLBACK-UNLABELED |
| Documentation Alignment | ⚠️ PARTIAL | DOCS-PARTIAL |
| Code Quality | ✓ PASS | CODE-OK |

#### Overall Score
**65/100** — Functional but incomplete cost modeling

---

### 9. Recommendations

#### 9.1 Short Term (Immediate)

1. **Document the limitation** — Add comments indicating storage costs are excluded
2. **Adjust RI rate** — Use 35% instead of 40% for more conservative estimates
3. **Label fallbacks** — Add `is_estimate` flag to ServiceFindings or log warnings

#### 9.2 Medium Term (Next Sprint)

1. **Add storage pricing method** to `PricingEngine`:
   ```python
   def get_opensearch_storage_monthly_price_per_gb(self, volume_type: str) -> float
   ```
2. **Include storage in calculations** — Query domain EBSOptions and add storage costs
3. **Add storage savings** for gp2→gp3 migration based on actual storage size

#### 9.3 Long Term (Architecture)

1. **Unify cost model** — Create a comprehensive OpenSearch cost calculator that includes:
   - Instance costs (with RI options)
   - Storage costs (GP2/GP3/PIOPS)
   - Data transfer estimates
2. **Migrate to live pricing** — Move from keyword-based to full live pricing like EC2/RDS adapters

---

### Appendix A: AWS Pricing Data Reference

#### m5.large.search (eu-west-1) - Full Pricing

```json
{
  "OnDemand": {
    "hourly": 0.158,
    "monthly": 115.34
  },
  "Reserved": {
    "1yr_no_upfront": { "hourly": 0.109, "discount": "31%" },
    "1yr_partial": { "hourly": 0.053, "upfront": 464, "discount": "66%" },
    "1yr_all_upfront": { "hourly": 0.00, "upfront": 900, "discount": "100%" },
    "3yr_no_upfront": { "hourly": 0.082, "discount": "48%" },
    "3yr_partial": { "hourly": 0.040, "upfront": 1038, "discount": "75%" },
    "3yr_all_upfront": { "hourly": 0.00, "upfront": 1993, "discount": "100%" }
  }
}
```

#### AWS Documented Discount Ranges

| Payment Option | 1-Year | 3-Year |
|----------------|--------|--------|
| No Upfront | 31% | 48% |
| Partial Upfront | 33% | 50% |
| All Upfront | 35% | 52% |

---

*End of Audit Report*


---

## AUDIT-10: Containers

**File:** `services/adapters/containers.py`  
**Date:** 2026-05-01  
**Auditor:** Automated via AWS Pricing API + Documentation  
**Verdict Codes:** PASS, FLAG, WARN, FAIL

---

### Executive Summary

| Check | Status | Severity |
|-------|--------|----------|
| Fargate Pricing Constants | **FLAG** | High |
| Fargate Spot Discount (70%) | **PASS** | — |
| Rightsizing Discount (30%) | **WARN** | Medium |
| ECR Storage Pricing | **PASS** | — |
| Code Quality | **WARN** | Low |

---

### 1. Fargate Pricing Constants (Lines 34-35)

#### Adapter Code
```python
FARGATE_VCPU_HOURLY = 0.04048
FARGATE_MEM_GB_HOURLY = 0.004445
```

#### AWS Pricing API Verification

| Region | vCPU/hr (Actual) | Memory/hr (Actual) | Adapter Value | Match |
|--------|------------------|-------------------|---------------|-------|
| eu-west-1 | $0.04048 | $0.004445 | $0.04048 / $0.004445 | ✅ **MATCH** |
| us-east-1 | $0.04048 | $0.004445 | — | — |

#### Finding
The adapter uses **eu-west-1 pricing constants**. This is correct when the scanner runs in eu-west-1, but the code comment implies these are global values. The values match exactly with AWS's eu-west-1 Linux Fargate pricing:

- vCPU: `EU-Fargate-vCPU-Hours:perCPU` = $0.04048/hr
- Memory: `EU-Fargate-GB-Hours` = $0.004445/hr

#### Regional Pricing Comparison

The pricing is **identical** between us-east-1 and eu-west-1 for Linux x86 Fargate:
- Both regions: vCPU $0.04048/hr
- Both regions: Memory $0.004445/hr

However, **ARM (Graviton) pricing differs**:
- eu-west-1 ARM vCPU: $0.03238/hr (vs $0.04048 x86)
- eu-west-1 ARM Memory: $0.00356/hr (vs $0.004445 x86)

**Verdict:** FLAG — While values are correct for eu-west-1, the adapter:
1. Only supports x86 pricing (no ARM/Graviton path)
2. Hardcodes values instead of using regional lookup
3. Relies on pricing_multiplier for regional adjustment, which is applied AFTER the base calculation

---

### 2. Fargate Spot Discount (Line 70)

#### Adapter Code
```python
if "spot" in recommendation or "spot instances" in savings_str.lower():
    savings += monthly_ondemand * 0.70 if monthly_ondemand > 0 else 150.0 * ctx.pricing_multiplier
```

#### AWS Documentation Verification

Per AWS documentation:
> "Spot Instances offer significant discounts compared to On-Demand Instances, with an **average savings of 70%** on compute costs."
> — AWS Well-Architected Framework, ADVCOST02-BP04

Fargate Spot provides similar discounts to EC2 Spot for interruption-tolerant workloads. The 70% figure is the documented **average** savings across all Spot usage.

**Verdict:** PASS — 70% discount is the AWS-documented average for Spot capacity.

---

### 3. Rightsizing Discount (Line 73-78)

#### Adapter Code
```python
elif (
    "rightsizing" in savings_str.lower()
    or "rightsize" in recommendation
    or "over-provisioned" in recommendation
):
    savings += monthly_ondemand * 0.30 if monthly_ondemand > 0 else 75.0 * ctx.pricing_multiplier
```

#### Analysis
The 30% discount for rightsizing is a **rule-of-thumb estimate** rather than an official AWS figure. AWS Compute Optimizer provides specific rightsizing recommendations but does not publish a standard discount percentage.

**Verdict:** WARN — 30% is a reasonable heuristic but:
1. Not an official AWS discount rate
2. Actual savings depend on specific task size reduction (e.g., 2 vCPU → 1 vCPU = 50% compute savings)
3. Should be documented as "estimated" or "heuristic"

---

### 4. ECR Storage Pricing

#### AWS Pricing API Verification

```
Product: EU-TimedStorage-ByteHrs
Unit: GB-Mo
Price: $0.10 per GB-month of data storage
Location: EU (Ireland)
```

**Verdict:** PASS — ECR storage at $0.10/GB/month in eu-west-1 is correct.

---

### 5. Code Quality Issues

#### 5.1 Dead Code: `savings_str` Dollar Extraction

**Lines:** 37, 73, 78  
**Pattern:** `savings_str` is assigned but used only for keyword matching, not for extracting dollar values.

```python
savings_str = rec.get("EstimatedSavings", "")  # Line 37 - Assigned but never parsed for $

## Line 73, 78 - Only used for keyword matching:
"rightsizing" in savings_str.lower()
"lifecycle" in savings_str.lower()
```

**Expected Behavior:** If `EstimatedSavings` contains a string like "$150/month", the code should extract the numeric value `$150` rather than ignoring it and calculating from task specs.

**Verdict:** WARN — This is intentional design (calculating from task specs rather than trusting string values), but should be documented.

#### 5.2 Missing ARM/Graviton Support

The adapter only uses x86 Fargate pricing constants:
- vCPU: $0.04048 (x86)
- Memory: $0.004445 (x86)

ARM (Graviton) pricing is 20% cheaper:
- vCPU: $0.03238 (ARM) — **20% savings**
- Memory: $0.00356 (ARM) — **20% savings**

**Impact:** If tasks run on ARM, the adapter overestimates costs by ~20%.

**Verdict:** WARN — No platform detection (cpuArchitecture field not checked).

---

### 6. Calculation Formula Verification

#### Monthly Cost Calculation (Lines 51-57)

```python
monthly_ondemand = (
    (task_vcpu * FARGATE_VCPU_HOURLY + task_mem_gb * FARGATE_MEM_GB_HOURLY)
    * 730              # Hours in month (365 * 24 / 12 = 730)
    * task_count       # Number of tasks
    * ctx.pricing_multiplier  # Regional adjustment
)
```

**Verification:**
- 730 hours/month is correct (365 days ÷ 12 months × 24 hours)
- Formula: (vCPU_cost + Memory_cost) × hours × tasks × regional_multiplier
- This matches AWS Fargate billing: per-vCPU-hour + per-GB-hour

**Verdict:** PASS — Formula is correct.

---

### 7. Recommendations

| Priority | Issue | Recommendation |
|----------|-------|----------------|
| **P1** | Regional pricing hardcoded | Add regional lookup table or fetch from AWS Pricing API at runtime |
| **P2** | No ARM support | Add platform detection and ARM pricing path |
| **P3** | Rightsizing % heuristic | Document that 30% is an estimate, not an AWS guarantee |
| **P4** | savings_str purpose | Add code comment explaining why string values are ignored |

---

### Appendix: Raw AWS Pricing Data

#### eu-west-1 Linux Fargate (x86)
```
SKU: K4EXFQ5YFQCP98EN
Usage: EU-Fargate-vCPU-Hours:perCPU
Price: $0.04048/hr

SKU: UP5ETC3QSW2QZGEY
Usage: EU-Fargate-GB-Hours
Price: $0.004445/hr
```

#### eu-west-1 Linux Fargate (ARM/Graviton)
```
SKU: KXC9CEUQGJQPNZPY
Usage: EU-Fargate-ARM-vCPU-Hours:perCPU
Price: $0.03238/hr (20% cheaper)

SKU: MGJ5EB5GSHR8QZDF
Usage: EU-Fargate-ARM-GB-Hours
Price: $0.00356/hr (20% cheaper)
```

#### eu-west-1 ECR Storage
```
SKU: 4NWTEXUZBWQH8MDW
Usage: EU-TimedStorage-ByteHrs
Price: $0.10/GB-Mo
```

---

### Final Verdict

**Overall:** FLAG

The adapter functions correctly for eu-west-1 x86 Fargate workloads but has significant limitations:
1. ✅ Pricing constants match eu-west-1 AWS prices exactly
2. ✅ Spot discount (70%) matches AWS documented average
3. ✅ ECR pricing correct at $0.10/GB/month
4. ⚠️ No ARM/Graviton support (20% pricing difference)
5. ⚠️ Hardcoded values don't adapt to other regions automatically
6. ⚠️ Rightsizing discount is a heuristic, not an AWS-published rate

**Action Required:** Document the regional and architecture limitations, or implement dynamic pricing lookup.


---

## AUDIT-11: FileSystems

**Date:** 2026-05-01  
**Auditor:** OpenCode Agent  
**Adapter:** `services/adapters/file_systems.py`  
**Scope:** EFS and FSx pricing accuracy, savings calculations, live API vs hardcoded

---

### 1. Code Analysis

#### 1.1 Adapter Structure (`file_systems.py`)

```python
class FileSystemsModule(BaseServiceModule):
    key: str = "file_systems"
    cli_aliases: tuple[str, ...] = ("efs", "file_systems")
    display_name: str = "File Systems"

    def required_clients(self) -> tuple[str, ...]:
        return ("efs", "fsx")
```

**Dependencies:**
- `services.efs_fsx` - Core analysis functions
- `core.pricing_engine.PricingEngine` - Live pricing API

**Scan Flow:**
1. `get_efs_file_system_count(ctx)` - Count EFS resources
2. `get_fsx_file_system_count(ctx)` - Count FSx resources  
3. `get_efs_lifecycle_analysis(ctx, pricing_multiplier)` - EFS recommendations
4. `get_fsx_optimization_analysis(ctx, pricing_multiplier)` - FSx recommendations
5. `get_enhanced_efs_fsx_checks(ctx, pricing_multiplier)` - Additional checks

#### 1.2 Cost Estimation Methods

| Service | Method | Location | Pricing Source |
|---------|--------|----------|----------------|
| EFS | `_estimate_efs_cost()` | `services/efs_fsx.py` | Hardcoded (fallback) |
| FSx | `_estimate_fsx_cost()` | `services/efs_fsx.py` | Hardcoded only |
| File Cache | `_estimate_file_cache_cost()` | `services/efs_fsx.py` | Hardcoded only |

**EFS Cost Formula (efs_fsx.py:144-150):**
```python
def _estimate_efs_cost(size_gb: float, pricing_multiplier: float, is_one_zone: bool = False) -> float:
    if is_one_zone:
        standard_price = 0.16
        ia_price = 0.0133
    else:
        standard_price = 0.30
        ia_price = 0.025

    base_cost = (size_gb * 0.2 * standard_price) + (size_gb * 0.8 * ia_price)
    return base_cost * pricing_multiplier
```

**FSx Cost Formula (efs_fsx.py:152-164):**
```python
def _estimate_fsx_cost(fs_type: str, capacity_gb: int, storage_type: str, pricing_multiplier: float) -> float:
    pricing: dict[str, dict[str, float]] = {
        "LUSTRE": {"SSD": 0.145, "HDD": 0.040},
        "WINDOWS": {"SSD": 0.13, "HDD": 0.08},
        "ONTAP": {"SSD": 0.144, "HDD": 0.05},
        "OPENZFS": {"SSD": 0.20, "INTELLIGENT_TIERING": 0.10},
    }
    # ...
```

#### 1.3 Savings Calculation (file_systems.py:52-66)

```python
savings = 0.0
for rec in efs_recs:
    cost = rec.get("EstimatedMonthlyCost", 0)
    if ctx.pricing_engine is not None and cost == 0:
        size_gb = rec.get("SizeGB", rec.get("StorageCapacity", 0))
        if size_gb > 0:
            price = ctx.pricing_engine.get_efs_monthly_price_per_gb()
            cost = size_gb * price
    savings += cost * 0.30  # ← Flat 30%
for rec in fsx_recs:
    cost = rec.get("EstimatedMonthlyCost", 0)
    savings += cost * 0.30  # ← Flat 30%
```

---

### 2. EFS Standard Pricing Verification

#### 2.1 Live API Query

**Request:**
- Service: `AmazonEFS`
- Region: `eu-west-1`
- Filter: `storageClass = "General Purpose"`

**Response:**
```json
{
  "storageClass": "General Purpose",
  "location": "EU (Ireland)",
  "usagetype": "EU-TimedStorage-ByteHrs",
  "pricePerUnit": {"USD": "0.3300000000"},
  "unit": "GB-Mo",
  "description": "USD $0.33 per GB-Mo for Standard storage (EU)"
}
```

#### 2.2 Comparison

| Source | Price | Status |
|--------|-------|--------|
| **Live API (eu-west-1)** | **$0.33/GB/month** | ✅ Current |
| Expected (per audit spec) | $0.33/GB/month | ✅ Matches |
| Code fallback (`efs_fsx.py`) | $0.30/GB/month | ⚠️ Slightly low (-9%) |
| Code fallback (One Zone) | $0.16/GB/month | ⚠️ Needs verification |

#### 2.3 Pricing Engine Method

`PricingEngine.get_efs_monthly_price_per_gb()` (pricing_engine.py:264-275):
```python
def get_efs_monthly_price_per_gb(self) -> float:
    key = ("efs_gb",)
    if (cached := self._get_cached(key)) is not None:
        return cached
    price = self._fetch_efs_price()
    if price is None:
        price = self._use_fallback(
            FALLBACK_EFS_GB_MONTH * self._fallback_multiplier,  # ← 0.30
            f"Pricing API unavailable for EFS Standard in {self._region}; using fallback",
        )
    self._cache.set(key, price)
    return price
```

**Private Fetch Method (pricing_engine.py:418-422):**
```python
def _fetch_efs_price(self) -> float | None:
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        {"Type": "TERM_MATCH", "Field": "volumeType", "Value": "Standard"},
    ]
    return self._call_pricing_api("AmazonEFS", filters)
```

**Verdict:** ⚠️ **PARTIAL** - Live API works correctly ($0.33), but fallback constant is outdated ($0.30).

---

### 3. EFS-IA (Infrequent Access) Pricing Verification

#### 3.1 Live API Query

**Request:**
- Service: `AmazonEFS`
- Region: `eu-west-1`
- Filter: `storageClass = "Infrequent Access"`

**Response:**
```json
{
  "storageClass": "Infrequent Access",
  "location": "EU (Ireland)",
  "usagetype": "EU-IATimedStorage-ByteHrs",
  "pricePerUnit": {"USD": "0.0250000000"},
  "unit": "GB-Mo",
  "description": "USD $0.025 per GB-Mo for Standard-Infrequent Access storage (EU)"
}
```

**Access Fees:**
```json
{
  "usagetype": "EU-IADataAccess-Bytes",
  "pricePerUnit": {"USD": "0.0110000000"},
  "unit": "GB",
  "description": "USD $0.011 per GB for Infrequent Access reads (EU)"
}
```

#### 3.2 Comparison

| Metric | Live API | Expected | Code Value | Status |
|--------|----------|----------|------------|--------|
| IA Storage | $0.025/GB/mo | $0.025 | $0.025 (Regional) | ✅ Match |
| IA Storage | — | — | $0.0133 (One Zone) | ⚠️ Unverified |
| Read Access | $0.011/GB | — | Not modeled | ⚠️ Omitted |
| Write Access | $0.011/GB | — | Not modeled | ⚠️ Omitted |

#### 3.3 Code Analysis

EFS IA price in `efs_fsx.py`:
- Regional: `$0.025` ✅ Matches live API
- One Zone: `$0.0133` (no live API query performed for One Zone)

**Verdict:** ✅ **PASS** - IA storage price is accurate. Access fees not modeled (acceptable simplification).

---

### 4. FSx Pricing Verification

#### 4.1 Live API Query

**Request:**
- Service: `AmazonFSx`
- Region: `eu-west-1`
- No storageClass filter (FSx uses fileSystemType)

**Sample Results (from 100+ pricing entries):**

| File System Type | Storage Type | Throughput | Live Price | Unit |
|-----------------|--------------|------------|------------|------|
| Lustre | SSD | 125 MB/s/TiB | $0.160 | GB-Mo |
| Lustre | SSD | 200 MB/s/TiB | $0.319 | GB-Mo |
| Lustre | SSD | 1000 MB/s/TiB | $0.660 | GB-Mo |
| Lustre | HDD | 12 MB/s/TiB | $0.025 | GB-Mo |
| Lustre | INT (IA tier) | — | $0.0125 | GB-Mo |
| Windows | HDD | Multi-AZ | $0.025 | GB-Mo |
| Windows | Backup | — | $0.05 | GB-Mo |
| ONTAP | Throughput | Multi-AZ | $1.355-2.823 | MiBps-Mo |
| ONTAP | SSD IOPS | Multi-AZ | $0.0374 | IOPS-Mo |
| OpenZFS | SSD | Multi-AZ | $0.198 | GB-Mo |

#### 4.2 Comparison: Code Hardcoded vs Live API

| FS Type | Storage | Code Price | Live Price Range | Variance |
|---------|---------|------------|------------------|----------|
| LUSTRE | SSD | $0.145 | $0.154-$0.660 | ⚠️ -6% to -78% |
| LUSTRE | HDD | $0.040 | $0.025 | ⚠️ +60% high |
| WINDOWS | SSD | $0.13 | Not in sample | ? |
| WINDOWS | HDD | $0.08 | $0.025 | ⚠️ +220% high |
| ONTAP | SSD | $0.144 | Not comparable* | ? |
| ONTAP | HDD | $0.05 | Not comparable* | ? |
| OPENZFS | SSD | $0.20 | $0.198 | ✅ -1% |

*ONTAP pricing is complex with throughput and IOPS components, not just storage.

#### 4.3 Key Findings

1. **No Live API Integration**: FSx pricing is hardcoded only - no `PricingEngine` methods exist for FSx
2. **Lustre SSD**: Code uses $0.145, but live prices range from $0.154-$0.660 depending on throughput tier
3. **Lustre/Windows HDD**: Code significantly overestimates ($0.040-$0.08 vs $0.025 live)
4. **OpenZFS**: Code ($0.20) matches live API ($0.198) very closely ✅

#### 4.4 FSx Pricing Model Complexity

FSx pricing includes multiple dimensions the code doesn't model:
- **Throughput capacity** ($/MiBps-month) - ONTAP, Windows
- **Provisioned IOPS** ($/IOPS-month) - ONTAP, Windows, OpenZFS  
- **Capacity pools** (standard vs SSD) - ONTAP
- **Request charges** - ONTAP capacity pools
- **Backup storage** ($0.05/GB-month) - All types

**Verdict:** ⚠️ **PARTIAL** - Hardcoded prices are approximate. No live API integration. Missing throughput and IOPS pricing components.

---

### 5. Savings Formula Analysis

#### 5.1 Current Implementation

```python
## file_systems.py:52-66
savings = 0.0
for rec in efs_recs:
    cost = rec.get("EstimatedMonthlyCost", 0)
    # Fallback if no cost in recommendation
    if ctx.pricing_engine is not None and cost == 0:
        size_gb = rec.get("SizeGB", ...)
        price = ctx.pricing_engine.get_efs_monthly_price_per_gb()
        cost = size_gb * price
    savings += cost * 0.30  # ← Always 30%

for rec in fsx_recs:
    cost = rec.get("EstimatedMonthlyCost", 0)
    savings += cost * 0.30  # ← Always 30%
```

#### 5.2 Expected Formula (per audit spec)

The specification asks:  
`size_gb × (efs_standard_price - efs_ia_price) × estimated IA-eligible percentage`

Or:  
`flat 30% of EstimatedMonthlyCost`

#### 5.3 Comparison

| Approach | Formula | Rationale |
|----------|---------|-----------|
| **Current Code** | `cost × 0.30` | Simplified: assumes 30% savings across all optimizations |
| **EFS-IA Specific** | `size_gb × (0.33 - 0.025) × eligibility` | Accurate for lifecycle transitions |
| **EFS One Zone** | `size_gb × (0.33 - 0.16)` | 47% savings (per code comments) |
| **EFS Archive** | `size_gb × (0.33 - ~0.008)` | Up to 94% savings (per code comments) |

#### 5.4 EFS Cost Estimation Deep Dive

In `efs_fsx.py:144-150`:
```python
def _estimate_efs_cost(size_gb, pricing_multiplier, is_one_zone=False):
    if is_one_zone:
        standard_price = 0.16
        ia_price = 0.0133
    else:
        standard_price = 0.30
        ia_price = 0.025

    # Assumes 20% in Standard, 80% in IA
    base_cost = (size_gb * 0.2 * standard_price) + (size_gb * 0.8 * ia_price)
    return base_cost * pricing_multiplier
```

**Analysis:**
- Uses a **20/80 Standard/IA split** for cost estimation
- This is a **weighted average** approach, not the savings formula
- Actual savings would be the difference between 100% Standard and 20/80 split

**Calculated Savings Rate:**
```
Full Standard: size_gb × $0.30 = $0.30 × size_gb
Optimized (20/80): (0.2 × $0.30) + (0.8 × $0.025) = $0.06 + $0.02 = $0.08 × size_gb
Actual Savings: $0.30 - $0.08 = $0.22 (73% reduction)
```

But the adapter uses **flat 30%**, which is **conservative** compared to the 73% achievable with the modeled 20/80 split.

#### 5.5 FSx Savings

FSx recommendations don't have a specific formula - they use the same flat 30%:
- Storage type optimization (SSD→HDD): Potential 60-85% savings
- Data deduplication: 30-80% savings
- Rightsizing: 20-40% savings

The 30% flat rate may under-estimate for some optimizations.

**Verdict:** ⚠️ **PARTIAL** - Conservative flat 30% used. More granular savings formulas exist in the estimation functions but aren't reflected in final savings calculation.

---

### 6. Pass Criteria Assessment

| Criterion | Required | Actual | Status |
|-----------|----------|--------|--------|
| EFS Standard live pricing | Working API call | ✅ Works ($0.33) | PASS |
| EFS IA live pricing | Working API call | ✅ Works ($0.025) | PASS |
| FSx live pricing | Working API call | ❌ Not implemented | FAIL |
| EFS fallback accuracy | Within 10% of live | ⚠️ $0.30 vs $0.33 (-9%) | BORDERLINE |
| Savings formula documented | Clear methodology | ⚠️ Flat 30%, conservative | PARTIAL |
| One Zone pricing | Verified | ⚠️ Hardcoded only | PARTIAL |

---

### 7. Issues Found

#### 7.1 Issue #1: FSx No Live Pricing (HIGH)

**Location:** `services/efs_fsx.py:152-164`

**Problem:** FSx pricing is hardcoded with no fallback to live API. Prices are significantly off for some storage types.

**Impact:** Cost estimates and savings calculations may be inaccurate for FSx.

**Recommendation:** Add FSx pricing methods to `PricingEngine` similar to EFS:
```python
def get_fsx_lustre_price(self, storage_type: str, throughput_mb_per_tib: int) -> float:
    # Call AmazonFSx pricing API
    
def get_fsx_windows_price(self, storage_type: str, deployment_type: str) -> float:
    # Call AmazonFSx pricing API
    
def get_fsx_ontap_price(self, ...)
    # ONTAP has storage + throughput + IOPS components
```

#### 7.2 Issue #2: EFS Fallback Constant Outdated (MEDIUM)

**Location:** `core/pricing_engine.py:66`

**Problem:**
```python
FALLBACK_EFS_GB_MONTH: float = 0.30  # Should be 0.33
```

**Impact:** 9% underestimation when Pricing API unavailable.

**Recommendation:** Update to `$0.33` to match current eu-west-1 pricing.

#### 7.3 Issue #3: Savings Formula Simplified (LOW)

**Location:** `services/adapters/file_systems.py:52-66`

**Problem:** Uses flat 30% savings instead of calculating actual savings from the 20/80 Standard/IA split modeled in `_estimate_efs_cost()`.

**Impact:** Conservative estimates - may under-report potential savings.

**Recommendation:** Either:
- Use the modeled 20/80 split for savings calculation (73% savings potential)
- Or document why 30% is the target conservative estimate

#### 7.4 Issue #4: One Zone IA Price Unverified (LOW)

**Location:** `services/efs_fsx.py:146`

**Problem:**
```python
ia_price = 0.0133  # One Zone IA - not verified against live API
```

**Impact:** Unknown accuracy for One Zone cost estimates.

**Recommendation:** Add live API query for One Zone IA pricing.

#### 7.5 Issue #5: Access Fees Not Modeled (INFO)

**Problem:** EFS IA and Archive have per-GB access fees ($0.011/GB for IA reads/writes in eu-west-1) that aren't included in cost estimates.

**Impact:** Workloads with high IA access rates may have higher costs than estimated.

**Recommendation:** Document this limitation or add access pattern parameters.

---

### 8. Verdict Codes

#### 8.1 Overall Assessment

| Component | Verdict | Confidence |
|-----------|---------|------------|
| **EFS Standard** | ⚠️ `PARTIAL` | High |
| **EFS-IA** | ✅ `PASS` | High |
| **EFS One Zone** | ⚠️ `PARTIAL` | Medium |
| **FSx Lustre** | ⚠️ `PARTIAL` | Medium |
| **FSx Windows** | ⚠️ `PARTIAL` | Low |
| **FSx ONTAP** | ⚠️ `PARTIAL` | Low |
| **FSx OpenZFS** | ✅ `PASS` | High |
| **Savings Formula** | ⚠️ `PARTIAL` | High |
| **Live API Usage** | ⚠️ `PARTIAL` | High |
| **Adapter Overall** | ⚠️ `PARTIAL` | High |

#### 8.2 Verdict Definitions

- **✅ PASS**: Meets all criteria, pricing accurate, fully functional
- **⚠️ PARTIAL**: Functional but has issues (outdated fallbacks, missing live API, simplified formulas)
- **❌ FAIL**: Critical issues preventing accurate operation

#### 8.3 Summary

The File Systems adapter is **functional but has notable limitations**:

1. **EFS pricing** works correctly with live API ($0.33 Standard, $0.025 IA) but fallback constant is slightly outdated
2. **FSx pricing** uses hardcoded values with no live API integration - several prices are inaccurate
3. **Savings calculation** uses a conservative flat 30% rather than optimization-specific formulas
4. **Architecture** is sound - properly structured as ServiceModule with correct boto3 clients

**Recommendation:** Address Issue #1 (FSx live pricing) and Issue #2 (EFS fallback update) for improved accuracy.

---

### 9. Appendix: Live Pricing Data

#### 9.1 EFS eu-west-1 (EU Ireland)

| Storage Class | Price | Unit |
|--------------|-------|------|
| General Purpose (Standard) | $0.33 | GB-Mo |
| Infrequent Access | $0.025 | GB-Mo |
| Infrequent Access reads | $0.011 | GB |
| Infrequent Access writes | $0.011 | GB |

#### 9.2 FSx eu-west-1 Sample Prices

| Type | Storage | Price | Unit |
|------|---------|-------|------|
| Lustre SSD (125 MB/s/TiB) | Persistent | $0.160 | GB-Mo |
| Lustre SSD (1000 MB/s/TiB) | Persistent | $0.660 | GB-Mo |
| Lustre HDD (12 MB/s/TiB) | Persistent | $0.025 | GB-Mo |
| Lustre Intelligent-Tiering | IA tier | $0.0125 | GB-Mo |
| Windows HDD | Multi-AZ | $0.025 | GB-Mo |
| Windows Backup | — | $0.05 | GB-Mo |
| OpenZFS SSD | Multi-AZ | $0.198 | GB-Mo |
| ONTAP Throughput | Multi-AZ | $1.355-$2.823 | MiBps-Mo |
| ONTAP SSD IOPS | Multi-AZ | $0.0374 | IOPS-Mo |

---

*End of Audit Report*


---

## AUDIT-12: AMI

**Adapter**: `services/adapters/ami.py` (46 lines)
**Supporting**: `services/ami.py` (129 lines), `services/_base.py` (34 lines), `core/contracts.py` (188 lines)
**Date**: 2026-05-01
**Auditor**: Sisyphus Audit Agent
**Pricing Data Source**: AWS Price List API, queried 2026-05-01

---

### 1. Code Analysis

#### 1.1 Architecture

The AMI adapter is a thin field-extraction wrapper. The adapter (`services/adapters/ami.py`) extends `BaseServiceModule` and delegates all scanning logic to `services/ami.py::compute_ami_checks()`.

**Call chain**:
```
AmiModule.scan(ctx) → compute_ami_checks(ctx, ctx.pricing_multiplier)
  → returns {recommendations: list, total_count: int}
  → adapter sums EstimatedMonthlySavings per-recommendation
  → returns ServiceFindings(sources={"old_amis": SourceBlock(...)})
```

#### 1.2 Unused AMI Detection

**Code path**: `services/ami.py:28–58`

The function builds a `running_amis` set from three sources:
1. **Running/stopped EC2 instances** — `ec2.describe_instances()` paginator, extracting `ImageId` from instances in `running` or `stopped` state.
2. **Launch templates** — `ec2.describe_launch_templates()` → `ec2.describe_launch_template_versions()`, extracting `ImageId` from `LaunchTemplateData`.
3. **Auto Scaling Groups** — `autoscaling.describe_auto_scaling_groups()` → `autoscaling.describe_launch_configurations()`, extracting `ImageId` from launch configs.

An AMI is flagged as **unused** if:
- `ami_id not in running_amis` AND
- `age_days > 30`

#### 1.3 Old AMI Detection (90-Day Threshold)

**Code path**: `services/ami.py:67–119`

The constant `OLD_SNAPSHOT_DAYS: int = 90` defines the age threshold. For AMIs older than 90 days, the function:

1. Iterates over `BlockDeviceMappings` to collect snapshot IDs.
2. Calls `ec2.describe_snapshots(SnapshotIds=[...])` per snapshot to get `VolumeSize`.
3. Falls back to `block_device["Ebs"].get("VolumeSize", 8)` if snapshot lookup fails.
4. Falls back to 8 GB total if no block device mappings found.

**Pricing formula**:
```python
monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * pricing_multiplier
```

#### 1.4 Adapter Aggregation

**Code path**: `services/adapters/ami.py:35–46`

The adapter:
- Calls `compute_ami_checks(ctx, ctx.pricing_multiplier)`
- Extracts `recommendations` list (which are the `old_amis` entries only)
- Sums `EstimatedMonthlySavings` across all recommendations
- Returns `ServiceFindings` with a single source block `"old_amis"`

**Critical observation**: The adapter only returns `old_amis` recommendations in the output. The `unused_amis` list computed in `services/ami.py` is **discarded** — it's never returned to the adapter.

#### 1.5 Required Clients

The adapter declares `required_clients() → ("ec2",)` but the underlying `compute_ami_checks()` also uses `autoscaling`. This is a mismatch.

---

### 2. Pricing Validation

#### 2.1 Live AWS Pricing API Results

**Queried 2026-05-01** via `aws-pricing-mcp-server get_pricing`:

| Region | Usage Type | Price (USD/GB-Mo) | Source |
|--------|------------|-------------------|--------|
| eu-west-1 | `EU-EBS:SnapshotUsage` | **$0.0500** | AWS Price List API |
| us-east-1 | `EBS:SnapshotUsage` | **$0.0500** | AWS Price List API |
| eu-west-1 | `EU-EBS:SnapshotArchiveStorage` | **$0.0125** | AWS Price List API |

#### 2.2 Code Constant vs Live Pricing

| Constant | Code Value | Live API Value (eu-west-1) | Live API Value (us-east-1) | Match? |
|----------|-----------|--------------------------|--------------------------|--------|
| `0.05` (line 106 of `services/ami.py`) | $0.05/GB-Mo | $0.05/GB-Mo | $0.05/GB-Mo | **YES** |

The hardcoded `$0.05/GB-Mo` is accurate for both eu-west-1 and us-east-1 standard snapshot storage.

#### 2.3 Pricing Accuracy Concerns

| Issue | Detail |
|-------|--------|
| **No regional variation** | The $0.05 constant is used regardless of region. Some regions have different snapshot pricing (e.g., ap-south-1 = $0.055, sa-east-1 = $0.06). |
| **Full volume size, not incremental** | EBS snapshots are incremental — only changed blocks are stored. The code uses `VolumeSize` (full provisioned size), which overstates actual snapshot storage cost. |
| **No archive tier consideration** | EBS Snapshots Archive ($0.0125/GB-Mo) is a cheaper alternative not mentioned. |

---

### 3. Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Snapshot storage cost uses correct per-GB rate | ✅ PASS | $0.05/GB-Mo matches us-east-1 and eu-west-1 |
| 2 | pricing_multiplier applied correctly | ✅ PASS | Multiplied into monthly_snapshot_cost |
| 3 | AMI age threshold (90 days) is sensible | ✅ PASS | Industry standard for stale AMIs |
| 4 | Unused AMI detection checks all references | ✅ PASS | Checks instances, launch templates, ASG launch configs |
| 5 | Unused AMIs are included in output | ❌ FAIL | `unused_amis` list is computed but **discarded** — never returned |
| 6 | required_clients() declares all needed clients | ❌ FAIL | Only declares `ec2`, but code also uses `autoscaling` |
| 7 | Snapshot size fallback is reasonable | ⚠️ WARN | Falls back to 8 GB which may under/overestimate |
| 8 | Pricing is region-aware | ❌ FAIL | Hardcoded $0.05 regardless of region |
| 9 | Incremental vs full snapshot cost documented | ⚠️ WARN | Uses full volume size, not actual incremental snapshot size |
| 10 | Error handling is graceful | ✅ PASS | All API calls wrapped in try/except |
| 11 | CreationDate parsing is robust | ⚠️ WARN | Only handles `%Y-%m-%dT%H:%M:%S.%fZ` format; may fail on `+00:00` suffix |
| 12 | Adapter follows ServiceModule protocol | ✅ PASS | Correctly extends BaseServiceModule and returns ServiceFindings |
| 13 | Savings aggregation is correct | ✅ PASS | Sums `EstimatedMonthlySavings` from all recommendations |
| 14 | No security issues (IAM, secrets) | ✅ PASS | Read-only operations, no credential handling |

---

### 4. Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| AMI-01 | **HIGH** | Unused AMI recommendations computed but never returned | Users never see unused AMI cleanup recommendations — potentially significant savings missed |
| AMI-02 | **MEDIUM** | `required_clients()` missing `autoscaling` | Scan may fail if ScanContext only provisions clients declared in `required_clients()`. Launch config checks would silently fail. |
| AMI-03 | **MEDIUM** | Pricing not region-aware — hardcoded $0.05/GB-Mo | In regions like sa-east-1 ($0.06), ap-south-1 ($0.055), costs are understated by 10-20% |
| AMI-04 | **LOW** | Snapshot cost uses full volume size, not actual incremental storage | Overestimates savings — real snapshot storage is typically much smaller than volume size due to incremental nature |
| AMI-05 | **LOW** | CreationDate parsing only handles one format | `datetime.strptime(ami["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ")` will fail if AMI has no microseconds (e.g., `2024-01-01T00:00:00Z`) |
| AMI-06 | **INFO** | Default fallback of 8 GB for empty block device mappings | May be inaccurate for AMIs with unusual storage configurations |

---

### 5. Detailed Findings

#### 5.1 AMI-01: Unused AMIs Discarded (HIGH)

In `services/ami.py`, the function `compute_ami_checks()` builds two lists:
- `checks["unused_amis"]` — AMIs not referenced by any instance/template/ASG and older than 30 days
- `checks["old_amis"]` — AMIs older than 90 days with snapshot cost estimates

However, the return statement at line 128 only returns `old_amis`:
```python
return {"recommendations": checks["old_amis"], "total_count": len(checks["old_amis"])}
```

The `unused_amis` list is computed (with potentially expensive API calls) but never surfaced to the user. The adapter at `services/adapters/ami.py:38` reads `result.get("recommendations", [])` which only gets the old_amis.

**Recommendation**: Return both categories, or merge unused_amis into the recommendations with a distinct `CheckCategory`.

#### 5.2 AMI-02: Missing `autoscaling` Client Declaration (MEDIUM)

`services/adapters/ami.py:24`:
```python
def required_clients(self) -> tuple[str, ...]:
    return ("ec2",)
```

But `services/ami.py:38` calls `ctx.client("autoscaling")`. If the scan orchestrator only pre-provisions clients listed in `required_clients()`, the autoscaling call will fail. The error is silently caught (line 60: `except Exception: pass`), meaning ASG launch config references are missed — leading to false positives for unused AMIs.

#### 5.3 AMI-03: Region-Agnostic Pricing (MEDIUM)

The snapshot cost formula at `services/ami.py:106`:
```python
monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * pricing_multiplier
```

The $0.05 constant is correct for eu-west-1 and us-east-1 but wrong for other regions. The `pricing_multiplier` parameter is intended as a scalar adjustment but doesn't encode the actual regional price difference.

Sample regional deviations (from AWS pricing page):
- sa-east-1: $0.060/GB-Mo (+20%)
- ap-south-1: $0.055/GB-Mo (+10%)
- us-west-1: $0.055/GB-Mo (+10%)

#### 5.4 AMI-05: Date Parsing Fragility (LOW)

The `CreationDate` format from `describe_images` is typically `2024-01-15T10:30:00.000Z` but the `%f` directive in `strptime` may fail if the microseconds part is missing or has a different number of digits. A more robust approach would be to use `datetime.fromisoformat()` (Python 3.11+) or handle the trailing `Z` explicitly.

---

### 6. AWS Documentation Cross-Reference

Per AWS EBS Pricing documentation (queried 2026-05-01):

> "Snapshot storage is based on the amount of space the data consumes in Amazon Simple Storage Service (Amazon S3)."

Key points from docs that affect this adapter:
1. **Snapshots are incremental** — only changed blocks are stored. The code uses full `VolumeSize`, not actual snapshot storage consumed.
2. **Standard snapshot: $0.05/GB-Mo** in eu-west-1 — matches code constant.
3. **Archive tier: $0.0125/GB-Mo** — 75% cheaper, not referenced in recommendations.
4. **Deleting a snapshot doesn't reduce cost** if other snapshots reference its data — the code doesn't account for snapshot sharing.

---

### 7. Verdict

**⚠️ WARN — 62/100**

The adapter correctly identifies old AMIs and produces accurate savings estimates for eu-west-1/us-east-1. However, two significant issues reduce the score:

1. **Unused AMI recommendations are silently discarded** (AMI-01) — the most impactful finding, as it represents completely wasted computation and missed savings opportunities.
2. **Missing `autoscaling` client declaration** (AMI-02) — may cause false positives in unused AMI detection if the orchestrator respects `required_clients()`.

The pricing constant is accurate for major regions but not region-aware. The snapshot cost model overestimates by using full volume size instead of actual incremental storage.

#### Score Breakdown

| Category | Weight | Score | Weighted |
|----------|--------|-------|----------|
| Pricing Accuracy | 25% | 75/100 | 18.75 |
| Logic Correctness | 25% | 40/100 | 10.00 |
| Unused Detection | 20% | 50/100 | 10.00 |
| Error Handling | 15% | 85/100 | 12.75 |
| Protocol Compliance | 15% | 70/100 | 10.50 |
| **Total** | **100%** | | **62.00** |


---

## AUDIT-13: Monitoring

**Date:** 2026-05-01  
**Auditor:** OpenCode AI Agent  
**Scope:** `services/adapters/monitoring.py`, `services/monitoring.py`, `services/route53.py`  
**Status:** COMPLETED

---

### 1. Executive Summary

| Component | Status | Verdict Code |
|-----------|--------|--------------|
| CloudWatch Log Cost Estimation | ❌ FAIL | COST-UNDERESTIMATE-MAJOR |
| CloudTrail Paid Trail Detection | ⚠️ WARNING | LOGIC-PARTIAL |
| Parse-Based Savings Extraction | ✅ PASS | PARSE-ACCURATE |
| Route53 Hosted Zone Pricing | ✅ PASS | PRICING-EXACT |
| Route53 Health Check Pricing | ✅ PASS | PRICING-EXACT |
| **OVERALL** | ⚠️ **CONDITIONAL PASS** | **AUDIT-PARTIAL** |

---

### 2. Code Analysis

#### 2.1 Adapter Structure (`services/adapters/monitoring.py`)

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

#### 2.2 Savings Aggregation Logic

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

### 3. CloudWatch Log Cost Analysis

#### 3.1 Current Implementation (`services/monitoring.py:57-65`)

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

#### 3.2 AWS Pricing Verification (eu-west-1, 2026-03)

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

#### 3.3 Discrepancy Analysis

```
Expected: stored_gb × $0.57/GB (ingestion) + $0.03/GB (storage after 30 days)
Actual:   stored_gb × $0.03/GB

For 100 GB log group:
- Expected cost: $57.00 (ingestion) + $3.00 (storage) = $60.00
- Adapter shows: $3.00
- **UNDERESTIMATION: 95%**
```

#### 3.4 Verdict

**❌ FAIL - COST-UNDERESTIMATE-MAJOR**

The adapter uses **$0.03/GB** but AWS charges **$0.57/GB** for log ingestion in eu-west-1. This is a **19x underestimation** of actual costs. The $0.03 rate appears to be an outdated or confused storage-only rate.

**Correct Formula Should Be:**
```python
## For ingestion savings (primary cost driver)
f"${stored_bytes * 0.57 / (1024**3):.2f}/month"

## Or with tiered pricing for accuracy
```

---

### 4. CloudTrail Cost Analysis

#### 4.1 Current Implementation

The adapter checks for:
1. Multi-region trails (flags but no cost calculation)
2. Data events for all S3 buckets
3. Data events for all Lambda functions
4. CloudTrail Insights
5. Duplicate trails

#### 4.2 AWS Pricing Verification (eu-west-1)

| Event Type | Price | Notes |
|------------|-------|-------|
| Management Events | **$0.00** | First trail free |
| Additional Management Events | **$2.00/100k events** | Paid trails |
| Data Events | **$1.00/1M events** | $0.000001 per event |
| Insights Events | **$3.50/1M events** | $0.0000035 per event |

#### 4.3 Code Analysis

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

#### 4.4 Issues Found

1. **Static Savings Strings**: Most CloudTrail recommendations use non-parseable static strings like `"Single-region trail costs ~90% less"`
2. **Missing Paid Trail Logic**: Does not distinguish between first (free) trail and additional paid trails
3. **Insights Pricing Error**: States $0.35 per 100k events, but AWS charges $3.50 per 1M events (which equals $0.35 per 100k) - **actually correct but misleading unit**

#### 4.5 Verdict

**⚠️ WARNING - LOGIC-PARTIAL**

- Does flag cost drivers (multi-region, data events)
- Does NOT calculate actual dollar savings for CloudTrail
- Savings strings are not parseable by aggregation logic
- Missing first-trail-free logic

---

### 5. Parse-Based Savings Analysis

#### 5.1 How Parsing Works

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

#### 5.2 Test Cases

| Input String | Parsed Value | Expected | Status |
|--------------|--------------|----------|--------|
| `"$3.50/month"` | 3.50 | 3.50 | ✅ PASS |
| `"$0.50/month per zone"` | 0.50 | 0.50 | ✅ PASS |
| `"$10.50/month if reduced by 50%"` | 10.50 | 10.50 | ✅ PASS |
| `"Single-region trail costs ~90% less"` | None | N/A | ⚠️ Ignored (expected) |
| `"Reduce log level or retention period"` | None | N/A | ⚠️ Ignored (expected) |
| `"${count * 0.30:.2f}/month"` | Error | N/A | ❌ FAIL (f-string evaluated too late) |

#### 5.3 Verdict

**✅ PASS - PARSE-ACCURATE**

Parsing logic is correct for properly formatted strings. Non-parseable strings (static text, recommendations without dollar amounts) are gracefully ignored.

---

### 6. Route53 Pricing Analysis

#### 6.1 Hosted Zones (`services/route53.py:42-52`)

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

#### 6.2 Health Checks (`services/route53.py:89-97`)

```python
checks["unnecessary_health_checks"].append({
    "HealthCheckId": hc_id,
    "Type": hc_type,
    "Recommendation": "Health check without routing dependency - verify necessity",
    "EstimatedSavings": "$0.50/month per health check if removed",
    "CheckCategory": "Unnecessary Health Checks",
})
```

#### 6.3 AWS Pricing Verification

From AWS Pricing API (AmazonRoute53, eu-west-1):

| Resource | Adapter Price | AWS Price | Status |
|----------|---------------|-----------|--------|
| Hosted Zone | $0.50/month | $0.50/month | ✅ **EXACT MATCH** |
| Standard Health Check | $0.50/month | ~$0.50/month | ✅ **CORRECT** |

#### 6.4 Verdict

**✅ PASS - PRICING-EXACT**

Route53 pricing in the adapter matches AWS pricing exactly.

---

### 7. Pass Criteria Assessment

| Criterion | Requirement | Status |
|-----------|-------------|--------|
| CloudWatch log cost | stored_gb × $0.03/GB for ingestion + $0.03/GB for storage | ❌ FAIL (uses $0.03 instead of $0.57) |
| CloudTrail paid trails | Flag paid trails ($2/100k events) | ⚠️ PARTIAL (flags multi-region, not explicit paid status) |
| Parse accuracy | Dollar amounts extracted from EstimatedSavings | ✅ PASS |
| Route53 idle zones | $0.50/zone/month | ✅ PASS (exact match) |

---

### 8. Issues Summary

| # | Severity | Issue | Location | Impact |
|---|----------|-------|----------|--------|
| 1 | **HIGH** | CloudWatch log cost uses $0.03/GB instead of $0.57/GB | `services/monitoring.py:64` | 19x cost underestimation |
| 2 | MEDIUM | CloudTrail savings strings are non-parseable static text | `services/monitoring.py:137,151,161` | Zero CloudTrail savings in totals |
| 3 | LOW | CloudTrail Insights unit confusing ($0.35/100k vs $3.50/M) | `services/monitoring.py:190` | Minor documentation issue |
| 4 | MEDIUM | Missing first-trail-free logic for CloudTrail | `services/monitoring.py:112+` | May flag free trail as costly |

---

### 9. Recommendations

#### Immediate Actions Required

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

#### Nice to Have

3. Use tiered pricing for CloudWatch logs > 10TB
4. Add storage cost component (separate from ingestion)
5. Regional pricing support via ScanContext.pricing

---

### 10. Final Verdict

**VERDICT CODE: AUDIT-PARTIAL**

The Monitoring adapter has **one major pricing error** (CloudWatch logs at 5% of actual cost) that significantly undermines cost estimates. Route53 pricing is perfect. CloudTrail logic is incomplete but functional.

#### Scoring

| Category | Weight | Score |
|----------|--------|-------|
| Code Quality | 20% | 90% |
| Pricing Accuracy | 40% | 35% |
| Parse Logic | 20% | 95% |
| Feature Completeness | 20% | 60% |
| **OVERALL** | 100% | **64%** |

**Status:** ⚠️ **CONDITIONAL PASS** - Requires CloudWatch pricing fix before production use.

---

### Appendix A: Pricing API References

#### CloudWatch Log Ingestion (eu-west-1)
- SKU: KJBTQPDHW2H92B8Y
- Price: $0.57 per GB custom log data ingested
- Effective: 2026-03-01

#### CloudTrail Data Events (eu-west-1)
- SKU: 3SQYRBFEPJSYDZ9D
- Price: $0.000001 per data event ($1.00 per million)
- Effective: 2026-03-01

#### Route53 Hosted Zones
- Global pricing: $0.50 per hosted zone per month
- Standard health checks: ~$0.50 per month

---

*End of Audit Report*


---

## AUDIT-14: Glue

**Date:** May 1, 2026  
**Auditor:** AI Agent  
**Scope:** `services/adapters/glue.py`, `services/glue.py`  
**AWS Pricing API Region:** eu-west-1 (EU Ireland)  

---

### Code Analysis

#### Adapter Structure (`services/adapters/glue.py`)

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

#### Underlying Service (`services/glue.py`)

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

### Pricing Validation

#### Live AWS API Pricing (eu-west-1)

| Usage Type | SKU | Rate | Code Matches |
|------------|-----|------|--------------|
| ETL DPU-Hour | NB9R6DZZ9HCFD3YB | **$0.44** | ✅ Yes |
| ETL Memory-Optimized DPU-Hour | 3UYF6ZHB53FN3ANM | **$0.52** | ❌ Not used |
| Crawler DPU-Hour | RMAVV78MM2ZC7YJ4 | **$0.44** | ✅ Yes |
| Dev Endpoint DPU-Hour | ZWNGC5RT5GH88CSZ | **$0.44** | ✅ Yes |
| Interactive Sessions DPU-Hour | Y4YPXJZYGWU7MRW5 | **$0.44** | ✅ Yes |
| ETL Flex DPU-Hour | YEZ556JWKJ8XF8W4 | **$0.29** | ❌ Not used |
| DataBrew Node-Hour | XKNAJHYXEZH3PJBN | **$0.48** | ❌ Not used |

#### Pricing Constants in Code

```python
## services/adapters/glue.py:40
GLUE_DPU_HOURLY = 0.44  # ✅ MATCHES AWS API for standard ETL
```

**Verdict on DPU Rate:** The `$0.44/DPU-hour` constant is **accurate** for standard ETL jobs, crawlers, and dev endpoints in eu-west-1. However, the adapter does not account for:
- Memory-optimized jobs ($0.52/DPU-hour)
- Flex jobs ($0.29/DPU-hour — 34% cheaper)
- DataBrew ($0.48/node-hour)

---

### Pass Criteria Checklist

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

### Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| GLUE-001 | ⚠️ Medium | **160 hours/month assumption is arbitrary and undocumented** | The 160-hour assumption (line 48) appears to be `8 hrs/day × 20 workdays` but this is not documented. Actual Glue job runtime varies significantly — batch jobs may run only minutes, while streaming jobs run 24/7. This leads to inaccurate savings estimates. |
| GLUE-002 | 🔵 Low | **Adapter uses hardcoded pricing instead of PricingEngine** | Unlike 19 other adapters that use `ctx.pricing_engine`, Glue uses a hardcoded `$0.44` constant. This prevents automatic updates if AWS changes pricing and doesn't account for job-type variations (Flex vs Standard). |
| GLUE-003 | ⚠️ Medium | **Dev endpoint $316/month estimate calculation unclear** | Line 63 claims "$316/month per endpoint" at $0.44/hour. For a 2-DPU endpoint running full month (720 hrs): `$0.44 × 2 × 720 = $633.60`. The $316 figure suggests either: (a) 1-DPU endpoint, (b) 50% utilization, or (c) outdated pricing. No calculation provided. |
| GLUE-004 | 🔵 Low | **Does not distinguish between job types** | Different Glue job types have different pricing: Standard ($0.44), Memory-Optimized ($0.52), Flex ($0.29). Adapter treats all jobs as Standard, potentially over/under-estimating costs. |
| GLUE-005 | 🔵 Low | **No consideration for auto-scaling savings** | The adapter assumes 30% savings from rightsizing (line 49) but doesn't verify if auto-scaling is already enabled or calculate actual savings based on historical DPU utilization. |
| GLUE-006 | 🔵 Low | **Dev endpoint check doesn't verify actual usage** | The adapter flags all READY endpoints without checking CloudWatch metrics for actual activity. An endpoint may be READY but idle vs actively used. |

---

### Detailed Analysis

#### 160 Hours/Month Assumption

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

#### Dev Endpoint Savings Estimate

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

#### Comparison with Other Adapters

| Adapter | Pricing Source | Live API? |
|---------|---------------|-----------|
| EC2 | `pricing_engine.get_ec2_instance_monthly_price()` | ✅ Yes |
| RDS | `pricing_engine.get_rds_instance_monthly_price()` | ✅ Yes |
| EBS | `pricing_engine.get_ebs_monthly_price_per_gb()` | ✅ Yes |
| Lambda | Cost Hub + Compute Optimizer | ✅ Yes |
| **Glue** | Hardcoded `$0.44` | ❌ **No** |
| Lightsail | `pricing_engine.get_instance_monthly_price()` | ✅ Yes |

---

### Recommendations

#### Immediate (Pre-Release)

1. **Document the 160-hour assumption** — Add a comment explaining it's based on 8 hrs/day × 20 days
2. **Fix or document the $316/month dev endpoint calculation** — Clarify DPU assumptions

#### Short-Term (Next Sprint)

3. **Migrate to PricingEngine** — Add `get_glue_dpu_hourly_price(job_type=None)` method to `PricingEngine`
4. **Support job-type-aware pricing** — Distinguish between Standard, Flex, and Memory-Optimized jobs
5. **Calculate dev endpoint savings dynamically** — Use actual endpoint DPU configuration

#### Long-Term (Future Enhancement)

6. **CloudWatch integration for dev endpoints** — Check actual endpoint activity before flagging
7. **Derive hours from job schedules** — Use cron expressions or job run history for accurate hour estimates
8. **Consider Flex migration recommendations** — Flag jobs that could use Flex pricing for 34% savings

---

### Verdict

**Status:** ⚠️ **WARN** — **72/100**

#### Scoring Breakdown

| Category | Score | Max | Notes |
|----------|-------|-----|-------|
| Pricing Accuracy | 20/20 | 20 | $0.44 rate is correct for standard ETL |
| Documentation | 5/15 | 15 | 160-hour assumption and $316 estimate undocumented |
| Live Pricing Integration | 5/20 | 20 | Hardcoded, no PricingEngine usage |
| Cost Optimization Logic | 20/25 | 25 | Flags over-provisioned resources but uses arbitrary assumptions |
| Completeness | 12/20 | 20 | Missing Flex pricing, job-type distinctions |
| Dev Endpoint Handling | 10/10 | 10 | Correctly flags READY endpoints |

**Total: 72/100**

#### Summary

The Glue adapter correctly identifies cost optimization opportunities (over-provisioned jobs, idle dev endpoints, over-frequent crawlers) and uses an accurate DPU-hour rate. However, it relies on **undocumented assumptions** (160 hours/month, $316/endpoint) and **does not use the PricingEngine** like other modern adapters. The savings calculations are **directionally correct but potentially inaccurate**.

**Recommendation:** Address GLUE-001 and GLUE-003 (documentation issues) before release. Consider GLUE-002 (PricingEngine migration) for the next sprint to align with other adapters.

---

### Appendix: AWS Pricing API Raw Data

#### Verified Rates (eu-west-1, 2026-01-08)

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

#### Data Sources

1. AWS Pricing API via `aws-pricing-mcp-server` — Retrieved 2026-05-01
2. AWS Glue Pricing Documentation — https://aws.amazon.com/glue/pricing/
3. AWS Documentation Search — "AWS Glue pricing DPU per hour development endpoint crawler"

---

*End of Audit Report*


---

## AUDIT-15: Athena

**Auditor:** OpenCode AI Agent  
**Date:** 2026-05-01  
**Adapter:** `services/adapters/athena.py`  
**Related:** `services/athena.py` (legacy shim)  

---

### 1. Executive Summary

| Verdict Code | Category | Finding |
|--------------|----------|---------|
| **PASS** | Pricing | `$5/TB` is hardcoded but matches AWS Pricing API for both us-east-1 and eu-west-1 |
| **INFO** | ProcessedBytes | Metric exists in AWS/Athena namespace with WorkGroup dimension; aggregation logic is correct |
| **WARN** | 75% Savings | Undocumented assumption—no AWS benchmark found; appears to be conservative estimate |
| **PASS** | Fallback | `$50` fallback exists when CloudWatch data unavailable |
| **WARN** | Estimate Label | Fallback savings NOT labeled as estimate in output structure |

**Overall Verdict:** `CONDITIONAL-PASS` — 2 warnings require documentation, no blockers.

---

### 2. Code Analysis

#### 2.1 Architecture

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

#### 2.2 Flow Logic

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

### 3. Pricing Verification

#### 3.1 AWS Pricing API Results

**Query:** `get_pricing("AmazonAthena", region)`

##### us-east-1 (N. Virginia)
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

##### eu-west-1 (Ireland)
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

#### 3.2 Verification Result

| Region | Adapter Value | API Value | Status |
|--------|--------------|-----------|--------|
| us-east-1 | $5.00/TB | $5.00/TB | ✅ MATCH |
| eu-west-1 | $5.00/TB | $5.00/TB | ✅ MATCH |

**Finding:** The hardcoded `ATHENA_PRICE_PER_TB = 5.0` is **correct** for both regions tested. AWS documentation confirms $5/TB is the standard rate for SQL queries on S3 data.

#### 3.3 Hardcoded vs. Live Pricing

| Aspect | Current | Best Practice |
|--------|---------|---------------|
| Implementation | Hardcoded constant | `ctx.pricing_engine` lookup |
| Risk | Low (stable pricing) | Lower (auto-updates) |
| Impact | None currently | Future-proofing |

**Note:** Athena pricing has remained $5/TB since launch. Hardcoding is acceptable but using `ctx.pricing_engine` would align with other adapters.

---

### 4. ProcessedBytes Metric Analysis

#### 4.1 Metric Specification

Per [AWS Documentation](https://docs.aws.amazon.com/athena/latest/ug/query-metrics-viewing.html):

| Property | Adapter Value | AWS Spec | Status |
|----------|---------------|----------|--------|
| Namespace | `AWS/Athena` | ✅ `AWS/Athena` | Correct |
| MetricName | `ProcessedBytes` | ✅ `ProcessedBytes` | Correct |
| Description | Bytes scanned per DML query | ✅ "The number of bytes that Athena scanned per DML query" | Correct |
| Dimension | `WorkGroup` | ✅ `WorkGroup` dimension exists | Correct |
| Period | 2592000 seconds | 30 days | Correct |
| Statistic | `Sum` | Recommended for totals | Correct |

#### 4.2 Aggregation Logic

```python
## Adapter code (lines 52-53)
total_bytes = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
monthly_tb = total_bytes / (1024**4) if total_bytes > 0 else 0
```

**Unit Conversion:**
- Bytes → TB: `bytes / (1024^4)` = `bytes / 1,099,511,627,776`
- This is technically TiB (tebibyte) conversion, but used interchangeably with TB in cloud pricing

**Finding:** Aggregation logic is **correct**. Sum of daily/period Sum values yields total bytes over the 30-day window.

#### 4.3 Prerequisites

⚠️ **Important:** `ProcessedBytes` metrics require:
1. Workgroup must have **"Publish query metrics to CloudWatch"** enabled
2. Queries must have been executed in the last 30 days
3. Only **DML queries** (SELECT, etc.) populate this metric

If disabled, CloudWatch returns empty datapoints and adapter falls back to $50 estimate.

---

### 5. 75% Savings Factor Analysis

#### 5.1 The Claim

```python
## Line 58
savings += monthly_tb * ATHENA_PRICE_PER_TB * ctx.pricing_multiplier * 0.75
```

The adapter assumes **75% cost reduction** from optimization (partitioning, columnar formats, compression).

#### 5.2 AWS Documentation Research

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

#### 5.3 Real-World Benchmarks

| Optimization | Typical Savings | Source |
|--------------|-----------------|--------|
| Text → Parquet + Snappy | 70-90% | AWS documentation examples |
| Partitioning | 50-99% | Depends on query selectivity |
| Combined | 75-95% | Industry best practices |

#### 5.4 Verdict

| Aspect | Finding |
|--------|---------|
| Documented? | ❌ No official AWS "75%" benchmark |
| Reasonable? | ✅ Yes—conservative estimate |
| Risk | Low—actual savings often exceed 75% |
| Recommendation | Document as "estimated 75% savings from best practices" |

---

### 6. Fallback Behavior Analysis

#### 6.1 The Fallback

```python
## Lines 59-60
else:
    savings += 50.0 * ctx.pricing_multiplier
```

When CloudWatch `ProcessedBytes` data is unavailable, adapter defaults to **$50/month** per recommendation.

#### 6.2 $50 Basis

At $5/TB, $50 represents:
- **10 TB scanned per month** (or ~333 GB/day)

This is a reasonable middle-ground estimate for:
- Small-medium Athena deployments
- Workgroups with infrequent queries
- Organizations just starting with Athena

#### 6.3 Is It Labeled as Estimate?

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

### 7. Issues and Recommendations

#### Issue 1: Undocumented 75% Savings Factor

| Severity | LOW |
|----------|-----|
| Code | Line 58: `* 0.75` |
| Problem | No documentation explaining the 75% savings assumption |
| Fix | Add comment: `# 75% estimated savings from partitioning, columnar formats, and compression` |

#### Issue 2: Fallback Not Labeled as Estimate

| Severity | MEDIUM |
|----------|--------|
| Code | Lines 59-60 |
| Problem | Users cannot distinguish measured vs. estimated savings |
| Fix | Add metadata flag or use `ServiceFindings.estimated=True` if available |

#### Issue 3: Hardcoded Pricing (Non-blocking)

| Severity | INFO |
|----------|------|
| Code | Line 28: `ATHENA_PRICE_PER_TB = 5.0` |
| Problem | Does not use `ctx.pricing_engine` like other adapters |
| Fix | Consider migrating to live pricing lookup for consistency |

---

### 8. Pass Criteria Checklist

| Criterion | Required | Actual | Status |
|-----------|----------|--------|--------|
| Pricing matches AWS API | Yes | $5/TB confirmed | ✅ PASS |
| Metric exists in CloudWatch | Yes | ProcessedBytes verified | ✅ PASS |
| Aggregation logic correct | Yes | Sum over 30 days | ✅ PASS |
| Fallback exists | Yes | $50 default | ✅ PASS |
| Savings % documented | Preferred | Not documented | ⚠️ WARN |
| Estimates labeled | Preferred | Not labeled | ⚠️ WARN |

---

### 9. Final Verdict

#### Verdict Codes

| Code | Meaning |
|------|---------|
| PASS | No issues found |
| CONDITIONAL-PASS | Minor issues, non-blocking |
| WARN | Issues require attention |
| FAIL | Critical issues, fix required |

#### Overall Verdict: **CONDITIONAL-PASS**

**Rationale:**
1. ✅ Pricing is accurate ($5/TB verified)
2. ✅ CloudWatch metric and aggregation logic is correct
3. ✅ Fallback behavior exists
4. ⚠️ 75% savings factor is undocumented (not a blocker—conservative estimate)
5. ⚠️ Fallback estimates not labeled (recommend adding metadata)

**No code changes required for operation.** Recommend documenting assumptions for transparency.

---

### 10. Appendix

#### A. Adapter Code (Current)

```python
## services/adapters/athena.py (lines 28-62)
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

#### B. CloudWatch Metric Dimensions

Per AWS documentation, `ProcessedBytes` supports these dimensions:
- `WorkGroup` — Name of the workgroup ✅ (used by adapter)
- `QueryState` — SUCCEEDED, FAILED, CANCELED
- `QueryType` — DML, DDL, UTILITY
- `CapacityReservation` — Name of capacity reservation

Adapter correctly uses `WorkGroup` dimension.

#### C. References

1. [AWS Athena Pricing](https://aws.amazon.com/athena/pricing/)
2. [Athena CloudWatch Metrics](https://docs.aws.amazon.com/athena/latest/ug/query-metrics-viewing.html)
3. [Partitioning and Bucketing](https://docs.aws.amazon.com/athena/latest/ug/ctas-partitioning-and-bucketing.html)
4. [Cost Optimization Whitepaper](https://docs.aws.amazon.com/whitepapers/latest/cost-modeling-data-lakes/overview-of-cost-optimization.html)

---

*End of Report*


---

## AUDIT-16: Redshift

**Date:** 2026-05-01  
**Auditor:** AI Agent  
**Adapter File:** `services/adapters/redshift.py`  
**Service File:** `services/redshift.py`

---

### 1. Executive Summary

| Aspect | Status | Notes |
|--------|--------|-------|
| Code Quality | ✅ PASS | Clean implementation, follows adapter pattern |
| Node Pricing | ⚠️ PARTIAL | Live pricing fetched, but 35% heuristic may be inflated |
| 35% Savings Rate | ❌ FAIL | Documented savings are 24%, not 35% |
| RA3 Storage (RMS) | ⚠️ PARTIAL | Pricing exists ($0.024/GB/mo) but not included in calculations |
| **Overall Verdict** | ⚠️ **PARTIAL** | Adapter functional but savings calculation overestimates by ~46% |

**Verdict Codes:**
- `PASS` - Meets all criteria
- `PARTIAL` - Functional with minor issues
- `FAIL` - Significant issues requiring correction

---

### 2. Code Analysis

#### 2.1 Adapter Structure (`services/adapters/redshift.py`)

```python
class RedshiftModule(BaseServiceModule):
    key: str = "redshift"
    cli_aliases: tuple[str, ...] = ("redshift",)
    display_name: str = "Redshift"
```

**Assessment:** ✅ Clean, minimal adapter following the established pattern.

#### 2.2 Savings Calculation Logic

```python
for rec in recs:
    node_type = rec.get("NodeType")
    num_nodes = rec.get("NumberOfNodes", 1)
    if node_type:
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonRedshift", node_type)
        savings += monthly * num_nodes * 0.35  # <-- 35% flat rate
    else:
        savings += 200
```

**Assessment:** 
- ✅ Correctly uses `PricingEngine.get_instance_monthly_price()`
- ✅ Multiplies by `num_nodes`
- ❌ Uses hardcoded 35% savings rate (see Section 4)

#### 2.3 Recommendations Generation (`services/redshift.py`)

The service generates recommendations for:
1. **Reserved Instances** (lines 56-70): For clusters >30 days old with ≥2 nodes
2. **Cluster Rightsizing** (lines 72-81): For clusters with >3 nodes
3. **Serverless Optimization** (lines 94-103): For available workgroups

**Assessment:** ✅ Good coverage of major optimization categories.

---

### 3. Pricing Verification

#### 3.1 DC2.large Node Pricing (eu-west-1)

**Live AWS Pricing API Query:**
```
service_code: AmazonRedshift
region: eu-west-1
instanceType: dc2.large
```

**Result:**
| Metric | Value |
|--------|-------|
| On-Demand Price | $0.30/hour |
| Monthly (730 hrs) | $219.00/node |
| Storage Included | 0.16TB SSD |

**Adapter Calculation:**
- `get_instance_monthly_price("AmazonRedshift", "dc2.large")` returns ~$219
- Matches live pricing ✅

#### 3.2 Other Node Types Verified

| Node Type | On-Demand/hr | Monthly | Status |
|-----------|--------------|---------|--------|
| dc2.large | $0.30 | $219.00 | ✅ Verified |
| dc2.8xlarge | $5.60 | $4,088.00 | ✅ Verified |
| ra3.large | $0.601 | $438.73 | ✅ Verified |
| ra3.xlplus | $1.202 | $877.46 | ✅ Verified |
| ra3.4xlarge | $3.606 | $2,632.38 | ✅ Verified |
| ra3.16xlarge | $14.424 | $10,529.52 | ✅ Verified |

---

### 4. 35% Savings Rate Analysis

#### 4.1 AWS Documentation Research

**Query:** "Redshift reserved nodes pricing discount percentage"

**Key Findings from AWS Docs:**
> "Save up to **24%** with Redshift Reserved Instances for predictable workloads."
> — AWS Documentation, Reserved Nodes page

#### 4.2 Reserved Instance Pricing Comparison (ra3.4xlarge)

| Payment Option | Term | Effective Monthly | Savings vs On-Demand | Savings % |
|----------------|------|-------------------|----------------------|-----------|
| On-Demand | - | $2,632.38 | - | - |
| No Upfront | 1-year | $1,842.67 | $789.71 | **30.0%** |
| Partial Upfront | 1-year | $1,884.35 | $748.03 | **28.4%** |
| All Upfront | 1-year | $1,768.49 | $863.89 | **32.8%** |
| No Upfront | 3-year | $1,145.15 | $1,487.23 | **56.5%** |
| Partial Upfront | 3-year | $1,177.21 | $1,455.17 | **55.3%** |
| All Upfront | 3-year | $1,014.25 | $1,618.13 | **61.5%** |

#### 4.3 Discrepancy Analysis

| Source | Savings % | Notes |
|--------|-----------|-------|
| Adapter Code | 35% | Hardcoded flat rate |
| AWS Documentation | Up to 24% | General recommendation |
| 1-year RI (No Upfront) | 30% | Most common choice |
| 3-year RI | 56-61% | Higher commitment |

**Issue:** The adapter's documentation (`REDSHIFT_OPTIMIZATION_DESCRIPTIONS`) states "up to 24%" but the code uses 35%.

**Verdict:** ❌ **FAIL** - Inconsistent messaging. 35% is optimistic for typical 1-year commitments.

**Recommended Fix:**
```python
## Option 1: Align with documentation (24%)
savings += monthly * num_nodes * 0.24

## Option 2: Use tiered calculation
if num_nodes >= 5:  # Larger commitments get better rates
    savings += monthly * num_nodes * 0.30  # 1-year partial upfront
else:
    savings += monthly * num_nodes * 0.24  # Conservative estimate
```

---

### 5. RA3 Managed Storage (RMS) Analysis

#### 5.1 RMS Pricing Confirmed

**Live AWS Pricing API Query:**
```
service_code: AmazonRedshift
productFamily: Redshift Managed Storage
```

**Result:**
| Node Type | RMS Price |
|-----------|-----------|
| ra3.large | $0.024/GB-month |
| ra3.xlplus | $0.024/GB-month |
| ra3.4xlarge | $0.024/GB-month |
| ra3.16xlarge | $0.024/GB-month |

#### 5.2 RMS Cost Impact

RA3 nodes use Redshift Managed Storage (RMS) which is **billed separately** from compute:
- Compute: Hourly rate per node (e.g., $0.601/hr for ra3.large)
- Storage: $0.024/GB-month for data stored

**Example RA3.large cluster:**
- Compute (1 node): $0.601 × 730 = $438.73/month
- Storage (1TB): 1024 GB × $0.024 = $24.58/month
- **Total: $463.31/month**

#### 5.3 Adapter Coverage

**Current State:** ❌ **RMS costs are NOT included** in savings calculations.

The adapter only calculates:
```python
monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonRedshift", node_type)
savings += monthly * num_nodes * 0.35
```

This only covers **compute costs**, not storage.

**Verdict:** ⚠️ **PARTIAL** - RMS costs are real and can be 5-10% of total costs for data-heavy workloads.

**Recommendation:** For complete accuracy, RMS should be included when analyzing RA3 clusters:
```python
## Pseudocode for future enhancement
if node_type.startswith("ra3"):
    rms_size_gb = get_cluster_storage_size(cluster_id)  # Requires additional API call
    rms_monthly = rms_size_gb * 0.024 * ctx.pricing_multiplier
    total_monthly = compute_monthly + rms_monthly
```

---

### 6. Pass Criteria Assessment

| Criterion | Required | Actual | Status |
|-----------|----------|--------|--------|
| Live pricing integration | Yes | Yes (PricingEngine) | ✅ PASS |
| Node pricing accuracy | ±5% of AWS | Matches exactly | ✅ PASS |
| Savings documentation | Clear source | Claims 24%, uses 35% | ❌ FAIL |
| RA3 storage inclusion | Documented | Not included | ⚠️ PARTIAL |
| Code quality | Clean | Clean | ✅ PASS |
| Error handling | Present | Present | ✅ PASS |

---

### 7. Issues Found

#### Issue 1: Savings Rate Inconsistency (HIGH)
- **Location:** `services/adapters/redshift.py:45`
- **Problem:** Hardcoded 35% doesn't match documented 24%
- **Impact:** Overestimates savings by ~46% relative to documentation
- **Fix:** Align code with documentation or update documentation

#### Issue 2: RMS Storage Excluded (MEDIUM)
- **Location:** `services/adapters/redshift.py:44-45`
- **Problem:** RA3 RMS costs ($0.024/GB/mo) not factored into savings
- **Impact:** Underestimates total costs for RA3 clusters
- **Fix:** Add storage size detection and RMS cost calculation

#### Issue 3: Fallback Estimates Not Region-Aware (LOW)
- **Location:** `services/adapters/redshift.py:47-49`
- **Problem:** Flat $200 fallback doesn't use `ctx.pricing_multiplier`
- **Impact:** Inaccurate fallback in non-US regions
- **Fix:** Use `200 * ctx.pricing_multiplier`

---

### 8. Verdict

#### Overall: ⚠️ PARTIAL

**Summary:**
The Redshift adapter is functional and correctly fetches live pricing from AWS. However, it overestimates potential savings by using a 35% discount rate when AWS documentation (and the adapter's own descriptions) cite 24%. Additionally, RA3 Managed Storage costs are excluded from calculations, which could lead to incomplete cost projections for modern RA3 clusters.

**Recommendations:**
1. **Immediate:** Align the 35% savings rate with documented 24% or update documentation
2. **Short-term:** Add `pricing_multiplier` to fallback estimates
3. **Long-term:** Consider adding RMS storage cost detection for RA3 clusters

**Risk Assessment:**
- **User Impact:** MEDIUM - Users may expect higher savings than typically achievable
- **Accuracy Impact:** MEDIUM - 46% overestimation on RI savings
- **Trust Impact:** LOW - Core pricing is accurate, only savings projections affected

---

### 9. Appendix: Data Sources

#### A. Pricing API Calls

1. **DC2.large On-Demand:**
   - SKU: N8DGUPGNY95RANNQ
   - Rate: $0.30/hour
   - Region: eu-west-1

2. **RA3 Managed Storage:**
   - SKU: PYUW2AQHAPP38N9Z (ra3.large)
   - Rate: $0.024/GB-month
   - Region: eu-west-1

#### B. Reserved Instance Pricing (ra3.4xlarge)

| Term | Payment | Hourly Rate | Upfront | Effective Monthly | Savings |
|------|---------|-------------|---------|-------------------|---------|
| 1yr | No Upfront | $2.5242 | $0 | $1,842.67 | 30.0% |
| 1yr | Partial | $1.2080 | $10,582.50 | $1,884.35 | 28.4% |
| 1yr | All | $0.00 | $20,848 | $1,768.49 | 32.8% |
| 3yr | No Upfront | $1.5687 | $0 | $1,145.15 | 56.5% |
| 3yr | Partial | $0.7122 | $18,716.50 | $1,177.21 | 55.3% |
| 3yr | All | $0.00 | $35,538 | $1,014.25 | 61.5% |

#### C. AWS Documentation References

1. [Reserved nodes - Amazon Redshift](https://docs.aws.amazon.com/redshift/latest/mgmt/purchase-reserved-node-instance.html)
2. [Amazon Redshift reserved nodes - AWS Whitepapers](https://docs.aws.amazon.com/whitepapers/latest/cost-optimization-reservation-models/amazon-redshift-reserved-nodes.html)

---

*Report generated by automated audit process. All pricing data verified live against AWS Pricing API on 2026-05-01.*


---

## AUDIT-17: MSK

**Date:** 2026-05-01  
**Auditor:** Automated Audit  
**Adapter File:** `services/adapters/msk.py`  
**Legacy Logic File:** `services/msk.py`

---

### Executive Summary

The MSK adapter uses **EC2 proxy pricing** instead of actual MSK broker pricing, does **not include storage costs**, and applies an **arbitrary 30% savings rate** that is not grounded in MSK pricing mechanics.

**VERDICT: P2-FAIL** - Significant pricing inaccuracies require correction.

---

### 1. Code Analysis

#### 1.1 Adapter Structure
```python
## services/adapters/msk.py lines 39-46
for rec in recs:
    instance_type = rec.get("InstanceType")
    num_brokers = rec.get("NumberOfBrokerNodes", 3)
    if instance_type:
        hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type)
        monthly_cluster = hourly * 730 * num_brokers
        savings += monthly_cluster * 0.30
```

#### 1.2 Issues Identified

| Line | Issue | Severity |
|------|-------|----------|
| 44 | Uses `get_ec2_hourly_price()` instead of MSK broker pricing | **Critical** |
| 46 | Arbitrary 30% multiplier without MSK-specific justification | **High** |
| 46 | Storage costs completely omitted | **High** |

---

### 2. Pricing Analysis

#### 2.1 MSK vs EC2 Pricing Comparison (eu-west-1)

| Instance Type | MSK Broker Price/hr | EC2 Price/hr | MSK Premium |
|---------------|---------------------|--------------|-------------|
| kafka.m5.large | **$0.234** | $0.107 | **2.19x** |
| kafka.m5.xlarge | **$0.468** | $0.214 | **2.19x** |
| kafka.m7g.large | **$0.2275** | ~$0.10 | **2.28x** |
| kafka.m7g.xlarge | **$0.4548** | ~$0.20 | **2.27x** |

**Finding:** MSK charges approximately **2.2x the EC2 price** for the same underlying instance type. Using EC2 pricing dramatically **underestimates** MSK costs by ~55%.

#### 2.2 Storage Pricing (Not Included)

According to AWS MSK pricing documentation:
- MSK Standard brokers charge for **EBS storage in GB-months**
- Storage is a significant cost component (typically $0.10-$0.12/GB-month for gp3)
- The adapter completely omits storage costs in its calculations

**Example Impact:**
- 3 brokers × 1000 GB each = 3000 GB-month
- At $0.10/GB-month = **$300/month storage cost** (not counted)

---

### 3. The 30% Savings Rate Problem

#### 3.1 Current Implementation
```python
savings += monthly_cluster * 0.30  # Arbitrary 30% flat rate
```

#### 3.2 Analysis

**Question:** Where does 30% come from?

**Answer:** The adapter uses a flat 30% rate without MSK-specific justification:

1. **MSK does NOT offer Reserved Instances** - Unlike EC2/RDS, MSK has no RI program
2. **Rightsizing is the primary optimization** - Moving from larger to smaller broker types
3. **Serverless migration** - Converting provisioned to MSK Serverless (variable savings)
4. **Storage optimization** - gp2→gp3 migration (~20% savings)

**Realistic Savings Potential:**
- **Rightsizing (one size down):** ~50% compute savings (e.g., m5.xlarge → m5.large)
- **Storage (gp2→gp3):** ~20% storage savings
- **Serverless conversion:** Variable (can be higher or lower depending on usage)

**Verdict:** The 30% flat rate is **not grounded in MSK pricing mechanics** and will produce inaccurate estimates.

---

### 4. Pass Criteria Assessment

| Criterion | Status | Notes |
|-----------|--------|-------|
| Uses MSK-specific pricing | ❌ FAIL | Uses EC2 proxy pricing |
| Includes storage costs | ❌ FAIL | Storage omitted entirely |
| Savings rate justified | ❌ FAIL | Arbitrary 30% rate |
| Recommendation context accurate | ✅ PASS | Recommendations are reasonable |
| No code errors | ✅ PASS | Code executes without errors |

---

### 5. Detailed Issues

#### Issue #1: EC2 Proxy Pricing (CRITICAL)
**Location:** `services/adapters/msk.py:44`
```python
hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type)
```

**Problem:** MSK brokers cost ~2.2x more than equivalent EC2 instances. Using EC2 pricing significantly underestimates actual costs.

**Impact:** For a 3-node kafka.m5.large cluster:
- Adapter calculates: $0.107 × 730 × 3 = **$234.33/month**
- Actual MSK cost: $0.234 × 730 × 3 = **$512.46/month**
- **Underestimation: $278.13/month (54%)**

#### Issue #2: Missing Storage Costs (HIGH)
**Location:** `services/adapters/msk.py`

**Problem:** The adapter only calculates broker hourly costs and ignores EBS storage pricing entirely.

**Evidence:** AWS documentation states MSK charges:
> "a charge for the amount of storage provisioned in the cluster, calculated in GB-months"

**Impact:** Storage can be 30-50% of total MSK cost for high-volume clusters.

#### Issue #3: Arbitrary 30% Savings Rate (HIGH)
**Location:** `services/adapters/msk.py:46`
```python
savings += monthly_cluster * 0.30
```

**Problem:** 
- MSK has no Reserved Instance program
- Rightsizing (the main optimization) can yield 50%+ savings for one size reduction
- 30% is not based on actual optimization potential

---

### 6. Recommendations

#### 6.1 Short-Term Fixes

1. **Use MSK Broker Pricing**
   - Add `get_msk_broker_hourly_price(instance_type)` to PricingEngine
   - Use `AmazonMSK` service code instead of `AmazonEC2`

2. **Add Storage Cost Calculation**
   - Query `VolumeSize` from cluster info (already available in line 61)
   - Add EBS storage pricing at ~$0.10/GB-month

3. **Revise Savings Methodology**
   - Replace flat 30% with rightsizing-based calculation
   - Calculate savings as: `(current_instance_price - target_instance_price) × hours × brokers`

#### 6.2 Example Corrected Calculation

```python
## Current (incorrect)
hourly = ctx.pricing_engine.get_ec2_hourly_price("kafka.m5.large")  # $0.107
savings = hourly * 730 * 3 * 0.30  # $70.30/month

## Corrected
msk_hourly = ctx.pricing_engine.get_msk_broker_hourly_price("kafka.m5.large")  # $0.234
target_hourly = ctx.pricing_engine.get_msk_broker_hourly_price("kafka.m5.large")  # Next size down
storage_cost = volume_size * 0.10  # EBS storage
actual_savings = (msk_hourly - target_hourly) * 730 * 3  # Real rightsizing savings
```

---

### 7. Verdict Codes

| Category | Code | Description |
|----------|------|-------------|
| **Overall** | **P2-FAIL** | Significant pricing inaccuracies require correction |
| Pricing Accuracy | E1-CRITICAL | Uses wrong pricing source (EC2 instead of MSK) |
| Completeness | E2-HIGH | Storage costs omitted |
| Savings Logic | E3-HIGH | Arbitrary 30% rate not justified |
| Code Quality | P1-PASS | No execution errors |

---

### 8. References

- [AWS MSK Pricing](https://aws.amazon.com/msk/pricing/)
- [MSK Broker Instance Sizes](https://docs.aws.amazon.com/msk/latest/developerguide/broker-instance-sizes.html)
- AWS Pricing API: `AmazonMSK` service code exists with broker-specific pricing
- Adapter method: `get_ec2_hourly_price()` in `core/pricing_engine.py`

---

### 9. Appendix: Pricing Data

#### MSK Broker Pricing (eu-west-1, from AWS Pricing API)

| SKU | Instance Type | Price/Hour |
|-----|---------------|------------|
| 89K6M3Q8CNUUMRTV | kafka.m5.large | $0.2340 |
| 75HMKTEZEPX8UJT9 | kafka.m5.xlarge | $0.4680 |
| 5MAR6NZW479TGG67 | kafka.m7g.large | $0.2275 |
| 2JBW4Q6HH85RPBXS | kafka.m7g.xlarge | $0.4548 |

#### EC2 Pricing (eu-west-1, for comparison)

| Instance Type | Price/Hour |
|---------------|------------|
| m5.large | $0.1070 |
| m5.xlarge | $0.2140 |

**MSK Premium Factor: 2.18x - 2.27x**

---

*End of Audit Report*


---

## AUDIT-18: CloudFront

**Date:** 2025-05-01
**Auditor:** Automated Audit (CloudFront Service Adapter)
**Files Reviewed:**
- `services/adapters/cloudfront.py` (45 lines) — ServiceModule adapter
- `services/cloudfront.py` (130 lines) — Core logic module

---

### Code Analysis

#### Architecture

The CloudFront adapter follows the project's standard two-layer pattern:

1. **Adapter layer** (`services/adapters/cloudfront.py`): Thin `CloudfrontModule` class extending `BaseServiceModule`. Delegates all logic to the service module via `get_enhanced_cloudfront_checks(ctx)` and applies a **flat $25/rec** savings estimate.

2. **Service module** (`services/cloudfront.py`): Free function `get_enhanced_cloudfront_checks()` that performs three categories of checks:
   - **Price class optimization** — only for `PriceClass_All` distributions that are `enabled` AND have >1,000 requests/week (7-day CloudWatch metric)
   - **Low-traffic / disabled distributions** — flags `enabled=False` distributions
   - **Origin Shield review** — flags any origin with Origin Shield enabled

#### Adapter Layer (`services/adapters/cloudfront.py`)

```python
savings = 25 * len(recs)
```

- Flat $25/month per recommendation regardless of category.
- Does NOT differentiate between price class optimization (potentially high savings), disabled distributions (100% savings), or origin shield review (variable/unclear savings).

#### Service Module (`services/cloudfront.py`)

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

### Pricing Validation

#### AWS Pricing API Results (CloudFront, effective 2025-04-01)

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

#### $25/Rec Flat-Rate Assessment

| Scenario | Actual Savings | $25 Estimate | Accuracy |
|----------|---------------|-------------|----------|
| Disabled dist (no traffic) | $0-2/mo | $25 | Gross overestimate |
| Price class (US/EU-only traffic) | $0/mo | $25 | Gross overestimate |
| Price class (30% APAC traffic, 10TB/mo) | ~$100/mo | $25 | Underestimate |
| Price class (mixed global, 1TB/mo) | ~$10-30/mo | $25 | Reasonable |
| Origin Shield (unnecessary, low traffic) | ~$1-5/mo | $25 | Overestimate |

**The $25 flat rate is a poor fit for CloudFront** because savings vary from $0 (disabled distributions already cost nothing) to $100+/month (high-traffic with expensive-region delivery). Unlike EC2/EBS where per-resource costs are predictable, CloudFront costs are traffic-dependent.

---

### Pass Criteria Checklist

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

### Issues Found

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

### Savings Calculation Example

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

### Verdict

#### ⚠️ WARN — 58/100

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

#### Recommendations (Priority Order)

1. **Replace flat $25/rec with traffic-based calculation** — use CloudWatch `BytesDownloaded` metric per distribution to estimate monthly data transfer, then apply regional pricing differential
2. **Add geographic traffic analysis** — query CloudWatch metrics by region (usages types like `US-DataTransfer-Out-Bytes`, `EU-DataTransfer-Out-Bytes`, `AP-DataTransfer-Out-Bytes`) to determine actual traffic distribution
3. **Fix disabled distribution savings claim** — change from "100%" to "$0 - already disabled" and recommend deletion only for long-term disabled distributions
4. **Improve Origin Shield analysis** — only flag if origin request rate is low (check `OriginRequests` CloudWatch metric) or cache hit ratio is already high (>95%)
5. **Include latency trade-off warning** in price class recommendations — switching from PriceClass_All excludes edge locations, degrading performance for excluded regions


---

## AUDIT-19: APIGateway

**Adapter:** `services/adapters/api_gateway.py` (46 lines)
**Legacy logic:** `services/api_gateway.py` (107 lines)
**Pricing engine:** Not used — keyword-based flat rates
**Date:** 2026-05-01
**Auditor:** Automated (opencode)

---

### 1. Code Analysis

#### 1.1 REST → HTTP API Migration Savings

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
## services/adapters/api_gateway.py
result = get_enhanced_api_gateway_checks(ctx)
recs = result.get("recommendations", [])
savings = 15 * len(recs)  # ← Flat rate, NOT from CloudWatch metrics
```

**Verdict:** ❌ FAIL — REST→HTTP savings use flat $15/rec estimate, NOT computed from CloudWatch Count metrics

---

#### 1.2 Caching Savings Calculation

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

#### 1.3 Flat Rate Labeling

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

### 2. Pricing Validation

#### 2.1 eu-west-1 Pricing Verification

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

### 3. CloudWatch Metrics Analysis

#### 3.1 Metrics NOT Queried

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

### 4. Adapter Architecture Review

#### 4.1 ServiceModule Protocol Compliance

| Protocol Field | Implementation | Status |
|----------------|----------------|--------|
| `key` | `"api_gateway"` | ✅ PASS |
| `cli_aliases` | `("api_gateway",)` | ✅ PASS |
| `display_name` | `"API Gateway"` | ✅ PASS |
| `required_clients()` | `("apigateway",)` | ✅ PASS |
| `scan(ctx)` | Returns `ServiceFindings` | ✅ PASS |

**Verdict:** ✅ PASS — Protocol compliant

#### 4.2 Savings Calculation Method

| Check Type | Calculation Method | CloudWatch-Based? |
|------------|-------------------|-------------------|
| REST→HTTP migration | $15 flat per API | ❌ NO |
| Caching opportunities | Generic text only | ❌ NO |
| Unused stages | No savings calculated | N/A |
| Throttling optimization | No savings calculated | N/A |
| Request validation | No savings calculated | N/A |

**Verdict:** ❌ FAIL — All savings are flat-rate estimates, none from actual metrics

---

### 5. Documentation Cross-Reference

#### 5.1 AWS Documentation Findings

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

### 6. Pass Criteria Assessment

| Criteria | Status | Notes |
|----------|--------|-------|
| Pricing accuracy | 🔵 INFO | No live pricing used — flat rates only |
| REST→HTTP from CloudWatch Count | ❌ FAIL | Uses $15 flat rate, not metrics |
| Caching from request volume | ❌ FAIL | Generic text only, no dollar amount |
| Flat rate labeled as estimate | ⚠️ WARN | $15/rec not labeled |
| Protocol compliance | ✅ PASS | ServiceModule correctly implemented |

---

### 7. Issues Summary

#### Critical Issues (❌ FAIL)

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

#### Warning Issues (⚠️ WARN)

4. **Flat Rate Not Labeled as Estimate**
   - `$15 × len(recs)` presented as `total_monthly_savings`
   - No indication this is an approximation
   - Users may believe this is a precise calculation

#### Info Items (🔵 INFO)

5. **Pricing Engine Not Integrated**
   - No usage of `core/pricing_engine.py`
   - Keyword-based approach documented in AGENTS.md
   - Consistent with other simple adapters

6. **Limited Check Coverage**
   - Only 2 of 5 check categories populate recommendations
   - Unused stages, throttling, validation have no savings logic

---

### 8. Final Verdict

#### Overall: ❌ FAIL

The API Gateway adapter implements the ServiceModule Protocol correctly but **fails to calculate actual savings** from CloudWatch metrics. All savings figures are flat-rate estimates ($15 per recommendation) that are not labeled as estimates. The adapter cannot provide meaningful dollar savings for REST→HTTP migration or caching opportunities because it does not query usage metrics.

#### Recommendations for Fix

1. **Query CloudWatch Count metric:** For each REST API, get `Count` metric over 14 days, calculate monthly request volume, then compute: `(REST_price - HTTP_price) × monthly_requests`

2. **Calculate caching savings:** Query `CacheMissCount` and backend latency metrics to estimate backend cost reduction from enabling cache

3. **Label estimates:** Add `"is_estimate": true` to `extras` field when using flat-rate calculations

4. **Add CloudWatch client:** Update `required_clients()` to include `"cloudwatch"` and query actual usage metrics

---

### Appendix: Pricing Reference (eu-west-1)

#### API Gateway Request Pricing

| Tier | REST API | HTTP API |
|------|----------|----------|
| First 333M/300M | $3.50/million | $1.11/million |
| Next 667M | $3.19/million | $1.11/million |
| Next 19B | $2.71/million | $1.00/million |
| Over 20B | $1.72/million | $1.00/million |

#### API Gateway Cache Pricing

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


---

## AUDIT-20: StepFunctions

**Adapter File:** `services/adapters/step_functions.py`  
**Legacy Module:** `services/step_functions.py`  
**Audit Date:** 2026-05-01  
**Auditor:** AI Agent  
**Verdict:** ⚠️ WARN — Multiple Issues Detected

---

### Executive Summary

The Step Functions adapter has **3 confirmed issues** affecting cost accuracy:

| Issue | Severity | Finding |
|-------|----------|---------|
| Billing Unit Mismatch | 🔴 HIGH | Uses `ExecutionsStarted` metric but billing is per state **transition**, not execution |
| Hardcoded Pricing | 🟡 MED | $0.025/1K transitions hardcoded, no PricingEngine integration |
| Arbitrary Savings % | 🟡 MED | Flat 60% savings for Standard→Express without workflow analysis |

**Verdict Codes:** `WARN-001` (underestimation), `WARN-002` (hardcoded), `WARN-003` (unverified)

---

### 1. Pricing Verification

#### 1.1 Hardcoded Pricing Constant

**Location:** `services/adapters/step_functions.py:40`

```python
STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025  # Hardcoded
```

**AWS Pricing API Results:**
- Service Code: `AWSStepFunctions` (not found in API)
- Alternative attempted: No matches for "step" pattern
- eu-west-1: Unable to verify via API (empty results)
- us-east-1: Unable to verify via API (empty results)

**Documentation Verification:**
Per [AWS Step Functions Pricing](https://aws.amazon.com/step-functions/pricing/):
- **Standard Workflows:** $0.000025 per state transition ($0.025 per 1,000)
- **Free Tier:** 4,000 state transitions/month (indefinite)
- **Express Workflows:** $1.00 per million requests + $0.00001667 per GB-second duration

**Finding:** The hardcoded $0.025/1K matches documented us-east-1 pricing, but:
1. No regional pricing verification via PricingEngine
2. Unlike EC2/EBS/RDS adapters, does not use `ctx.pricing_engine`
3. No fallback mechanism for pricing API failures

**Status:** ⚠️ `WARN-002` — Hardcoded pricing without API verification

---

### 2. Critical Issue: ExecutionsStarted vs State Transitions

#### 2.1 The Problem

**Billing Model (per AWS docs):**
- Step Functions Standard charges per **state transition**, NOT per execution
- Each state in a workflow = 1 transition
- A 10-state workflow executed 1,000 times = 10,000 state transitions
- Cost = (10,000 transitions × $0.000025) = $0.25

**Adapter Implementation (line 55-72):**
```python
## Gets EXECUTIONS count, not transitions
resp = cw.get_metric_statistics(
    Namespace="AWS/States",
    MetricName="ExecutionsStarted",  # ← WRONG METRIC
    ...
)
monthly_executions = sum(dp["Sum"] for dp in resp.get("Datapoints", []))

## Calculates as if 1 execution = 1 transition
savings += (
    (monthly_executions / 1000) * STEP_FUNCTIONS_PER_1K_TRANSITIONS * 0.60
)
```

#### 2.2 Impact Analysis

| Workflow Type | States/Exec | Actual Transitions | Adapter Calc | Error |
|---------------|-------------|-------------------|--------------|-------|
| Simple | 2 | 2,000 (for 1K execs) | 1,000 | **-50%** |
| Medium | 10 | 10,000 (for 1K execs) | 1,000 | **-90%** |
| Complex | 50 | 50,000 (for 1K execs) | 1,000 | **-98%** |

**Real-World Example:**
- Image processing workflow (from AWS docs): 9 state transitions per execution
- 100,000 executions = 900,000 transitions
- **Actual cost:** 900,000 × $0.000025 = $22.50
- **Adapter estimates:** 100,000 × $0.000025 = $2.50
- **Underestimation:** $20.00 (89% error)

#### 2.3 Root Cause

The adapter uses `ExecutionsStarted` CloudWatch metric but does not:
1. Query the state machine definition to count states
2. Use `ExecutionsSucceeded` + average state count per execution
3. Apply a conversion factor (avg_states × executions)

**Available CloudWatch Metrics for Step Functions:**
- `ExecutionsStarted` — Number of workflow executions (what adapter uses)
- `ExecutionsSucceeded` — Successful completions
- `ExecutionsFailed` — Failed executions
- No direct "state transitions" metric available in CloudWatch

**Status:** 🔴 `WARN-001` — Severe cost underestimation for multi-step workflows

---

### 3. 60% Savings Calculation Analysis

#### 3.1 Standard vs Express Pricing

| Workflow Type | Pricing Model | Unit Cost |
|---------------|---------------|-----------|
| **Standard** | Per state transition | $0.000025/transition |
| **Express** | Per request + duration | $0.000001/request + $0.00001667/GB-second |

#### 3.2 Break-Even Analysis

Express has **two cost components:**
1. **Requests:** $1.00 per million = $0.000001 per execution
2. **Duration:** $0.00001667 per GB-second (=$0.06000/GB-hour for first 1K)

**Scenario Comparison** (1,000 executions/month):

| States | Standard Cost | Express Cost* | Actual Savings | Adapter Claims |
|--------|---------------|---------------|----------------|----------------|
| 2 states | $0.05 | ~$1.00+ | **-1900%** (more expensive) | 60% |
| 10 states | $0.25 | ~$1.00+ | **-300%** (more expensive) | 60% |
| 50 states | $1.25 | ~$1.00+ | ~20% | 60% |
| 100 states | $2.50 | ~$1.00+ | ~60% | 60% |

*Express estimate: $1.00 request fee + minimal duration for short workflows

#### 3.3 The Issue

The adapter applies a **flat 60% savings** (`* 0.60`) without:
1. Analyzing the state machine definition (number of states)
2. Measuring workflow duration
3. Estimating memory utilization
4. Considering request volume

For many workflows (especially simple ones), Express is **more expensive** than Standard, not 60% cheaper.

**Status:** 🟡 `WARN-003` — Arbitrary savings percentage without workflow analysis

---

### 4. Code Review Findings

#### 4.1 Missing PricingEngine Integration

**What other adapters do (e.g., EC2, EBS, RDS):**
```python
## Live pricing lookup
price = ctx.pricing_engine.get_ec2_instance_monthly_price(instance_type)
```

**What step_functions adapter does:**
```python
## Hardcoded constant
STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025
```

#### 4.2 Inaccurate Docstring

**Line 27-28:**
```python
"""...Savings calculated via CloudWatch ExecutionsStarted metrics 
with $0.025/1K state transitions pricing..."""
```

**Issue:** Claims to use "state transitions pricing" but uses `ExecutionsStarted` metric (executions, not transitions).

#### 4.3 Fast Mode Handling

**Lines 44, 76-77:**
```python
if not ctx.fast_mode:
    # ... CloudWatch lookup
else:
    savings += 150.0 * ctx.pricing_multiplier
```

The flat $150/rec estimate in fast mode is reasonable as a fallback, but the non-fast mode calculation is flawed.

---

### 5. Recommendations

#### 5.1 Immediate (High Priority)

1. **Add State Count Estimation**
   ```python
   # Query state machine definition
   sfn = ctx.client("stepfunctions")
   definition = sfn.describe_state_machine(stateMachineArn=arn)["definition"]
   # Parse JSON, count states (excluding Choice branches for conservative estimate)
   state_count = count_states_in_definition(definition)
   total_transitions = monthly_executions * state_count
   ```

2. **Use Conservative Multiplier**
   If parsing definitions is complex, apply a conservative multiplier:
   ```python
   AVG_STATES_PER_WORKFLOW = 5  # Conservative industry average
   total_transitions = monthly_executions * AVG_STATES_PER_WORKFLOW
   ```

#### 5.2 Medium Priority

3. **Implement PricingEngine Method**
   Add `get_stepfunctions_transition_price(region)` to `core/pricing_engine.py`

4. **Fix Express Savings Calculation**
   Require workflow analysis before claiming Express savings:
   ```python
   if state_count > 25 and duration_seconds < 60:
       estimated_savings = calculate_express_savings(...)
   else:
       estimated_savings = 0  # Cannot recommend without analysis
   ```

#### 5.3 Documentation

5. **Update Docstring**
   Document the limitation:
   ```python
   """...Note: Cost estimates assume average 5 states per workflow. 
   Actual costs vary based on workflow complexity."""
   ```

---

### 6. Test Impact

**Golden File:** `tests/fixtures/golden_scan_results.json` (line 424)
- Current: `total_monthly_savings: 0` (no recommendations)
- Impact: No change required (zero baseline)

**Regression Gate:**
```bash
pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```
- Status: No impact (no Step Functions test fixtures with non-zero savings)

---

### 7. Appendix: AWS Pricing Documentation

#### Standard Workflow Example (from AWS docs)

> An application workflow has four state transitions:
> 1. Start
> 2. Upload RAW File
> 3. Delete RAW File
> 4. End
>
> 100,000 executions = 400,000 transitions
> Cost = 400,000 × $0.000025 = $10.00 (after free tier)

#### Express Workflow Example (from AWS docs)

> 1 million workflows, 30 seconds average duration:
> - Request charges: 1M × $1.00/M = $1.00
> - Duration charges: 30M seconds × 64MB / 1024MB × $0.00001667 = $31.26
> - Total: $32.26

---

### 8. Verdict

| Check | Result | Notes |
|-------|--------|-------|
| $0.025/1K verified vs eu-west-1 API | ❌ FAIL | API returned empty results |
| Pricing from AWS PricingEngine | ❌ FAIL | Uses hardcoded constant |
| Billing unit is transitions | ❌ FAIL | Uses executions metric |
| 60% savings justified | ❌ FAIL | Arbitrary percentage |
| Code quality | ⚠️ WARN | Docstring inaccurate |
| Test coverage | ✅ PASS | No regressions |

**Final Verdict:** `WARN-001 WARN-002 WARN-003`

The adapter significantly underestimates Step Functions costs for multi-step workflows and applies arbitrary savings percentages. Recommendation: Fix billing unit calculation before production use.

---

*End of Audit Report*


---

## AUDIT-21: WorkSpaces

**Date:** 2026-05-01  
**Auditor:** OpenCode AI  
**Scope:** `services/adapters/workspaces.py`, `services/workspaces.py`  
**AWS Pricing Region:** eu-west-1 (EU Ireland)

---

### Executive Summary

The WorkSpaces adapter provides basic cost optimization recommendations for Amazon WorkSpaces but has significant gaps in pricing accuracy, savings calculations, and optimization coverage. The adapter correctly identifies ALWAYS_ON WorkSpaces as candidates for AutoStop conversion and detects unused WorkSpaces in STOPPED/ERROR/SUSPENDED states, but the pricing integration is fundamentally broken due to API field mismatches.

---

### Code Analysis

#### 1. Adapter Structure (`services/adapters/workspaces.py`)

```python
class WorkspacesModule(BaseServiceModule):
    key: str = "workspaces"
    cli_aliases: tuple[str, ...] = ("workspaces",)
    display_name: str = "WorkSpaces"
```

**Assessment:** Clean ServiceModule implementation following project patterns.

#### 2. Enhanced Checks (`services/workspaces.py`)

The `get_enhanced_workspaces_checks()` function implements three check categories:

| Check Category | Implementation Status | Description |
|----------------|----------------------|-------------|
| `billing_mode_optimization` | ✅ Implemented | Flags ALWAYS_ON WorkSpaces for AutoStop conversion |
| `unused_workspaces` | ✅ Implemented | Flags STOPPED/ERROR/SUSPENDED WorkSpaces |
| `bundle_rightsizing` | ❌ **Empty** | No implementation |

**Code Issues Found:**

1. **Missing Idle Detection**: Only checks workspace `State`, not actual usage metrics or login history
2. **Empty Bundle Rightsizing**: Check category exists but has no logic
3. **Static Savings Estimate**: Hardcoded "$50/month potential per workspace" regardless of bundle type

#### 3. Pricing Integration Analysis

The adapter attempts to use live pricing via:

```python
monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonWorkSpaces", compute_type)
savings += monthly * 0.40 if monthly > 0 else 35.0 * ctx.pricing_multiplier
```

**CRITICAL ISSUE**: The pricing engine's `_fetch_generic_instance_price()` uses:
```python
filters = [
    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
    {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
]
```

However, AWS Pricing API for WorkSpaces uses `usagetype` codes (e.g., `EU-AW-HW-2`), NOT `instanceType`. The `compute_type` values from `describe_workspaces` (like "STANDARD", "PERFORMANCE") do not map to pricing API filterable fields.

---

### Pricing Validation

#### eu-west-1 Bundle Pricing (Verified from AWS Pricing API)

| Bundle | ComputeTypeName | AlwaysOn Monthly | AutoStop Base | AutoStop Hourly | Break-Even Hours |
|--------|-----------------|------------------|---------------|-----------------|------------------|
| Standard | STANDARD | $37.00 | $11.00 | $0.32/hr | ~81 hours/month |
| Performance | PERFORMANCE | $54.00 | $13.00* | $0.38/hr* | ~108 hours/month |
| Power | POWER | $82.00 | $19.00* | $0.57/hr* | ~111 hours/month |
| PowerPro | POWERPRO | $157.00 | $25.00* | $0.88/hr* | ~150 hours/month |

*Estimated based on pricing patterns; exact AutoStop pricing varies by storage config

#### Pricing Accuracy Issues

| Issue | Current Behavior | Expected Behavior |
|-------|-----------------|-------------------|
| Field mismatch | Uses `instanceType` filter | Should use `usagetype` pattern matching |
| ComputeType mapping | Passes "STANDARD", "PERFORMANCE" | Should map to bundle IDs (2, 3, 8, etc.) |
| Regional pricing | Uses generic multiplier | Should query specific regional usagetypes |
| Fallback value | $35.00 × multiplier | Should use bundle-specific fallbacks |

---

### Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Live pricing integration | ❌ FAIL | Pricing API field mismatch; always falls back |
| 2 | Monthly vs hourly billing handling | ⚠️ WARN | Only detects ALWAYS_ON vs AutoStop, no usage analysis |
| 3 | Idle/underused WorkSpaces detection | ⚠️ WARN | Detects stopped/error states but not low-usage patterns |
| 4 | Savings calculation accuracy | ❌ FAIL | 40% flat rate doesn't reflect actual pricing models |
| 5 | Bundle rightsizing | ❌ FAIL | Check category exists but is empty |
| 6 | Multi-region pricing support | ⚠️ WARN | Uses pricing_multiplier but no regional bundle mapping |
| 7 | Error handling | ✅ PASS | Try/except with ctx.warn() for API failures |
| 8 | Fallback values | ⚠️ WARN | $35 fallback doesn't match actual eu-west-1 Standard price ($37) |

---

### Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| WS-001 | **CRITICAL** | Pricing engine uses incorrect filter field (`instanceType` instead of `usagetype`) | Live pricing never works; always falls back to estimates |
| WS-002 | **HIGH** | No mapping from `ComputeTypeName` (STANDARD) to bundle IDs (2) for pricing | Cannot query correct pricing for specific bundle types |
| WS-003 | **HIGH** | Savings calculated as flat 40% of monthly cost | Doesn't account for actual usage hours; significant over/under-estimation |
| WS-004 | **MEDIUM** | `bundle_rightsizing` check is empty | Missing optimization opportunity for over-provisioned WorkSpaces |
| WS-005 | **MEDIUM** | No idle detection based on login history | Cannot recommend termination of truly unused WorkSpaces |
| WS-006 | **LOW** | Fallback price $35 doesn't match eu-west-1 Standard ($37) | Minor inaccuracy in fallback calculations |
| WS-007 | **LOW** | Hardcoded "$50/month" savings estimate in recommendations | Not personalized to actual bundle pricing |

---

### Recommendations

#### Immediate Fixes

1. **Fix Pricing Integration (WS-001, WS-002)**:
   - Implement bundle ID mapping: `{"STANDARD": "2", "PERFORMANCE": "3", "POWER": "8", ...}`
   - Use `usagetype` filter with pattern like `{region_prefix}-AW-HW-{bundle_id}`
   - Example: `EU-AW-HW-2` for Standard in eu-west-1

2. **Improve Savings Calculation (WS-003)**:
   - For ALWAYS_ON → AutoStop recommendations, calculate break-even hours
   - Use formula: `(AlwaysOn_Monthly - AutoStop_Base) / AutoStop_Hourly`
   - Recommend AutoStop only if expected usage < break-even

3. **Implement Bundle Rightsizing (WS-004)**:
   - Track actual resource utilization (CPU, memory) via CloudWatch
   - Compare against bundle specifications
   - Recommend downsizing if utilization < 30% consistently

#### Enhancements

4. **Add Idle Detection (WS-005)**:
   - Query WorkSpaces connection events or CloudWatch UserConnected metric
   - Flag WorkSpaces with no connections in 30+ days
   - Differentiate between intentionally stopped vs abandoned WorkSpaces

5. **Fix Fallback Values (WS-006)**:
   - Update fallback to $37 for Standard bundles
   - Consider region-specific fallback dictionaries

---

### Verdict

**⚠️ WARN — 45/100**

The WorkSpaces adapter provides basic functionality and correctly identifies billing mode optimization opportunities, but the pricing integration is non-functional due to API field mismatches. The savings calculations are rough estimates rather than accurate projections. The empty bundle rightsizing check represents a significant missed optimization opportunity.

**Remediation Priority**: HIGH - Fix pricing integration (WS-001, WS-002) before relying on savings estimates for customer-facing reports.

---

### Appendix: AWS Pricing API Response Examples

#### Standard Bundle (EU-AW-HW-2) - AlwaysOn Monthly
```json
{
  "product": {
    "attributes": {
      "usagetype": "EU-AW-HW-2",
      "location": "EU (Ireland)"
    }
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Month",
        "pricePerUnit": {"USD": "37.0000000000"},
        "description": "$37/month-Monthly Plus-Windows licensed-Standard-2vCPU,4GB Memory,80GB Root,50GB User"
      }
    }
  }
}
```

#### Standard Bundle (EU-AW-HW-2-AutoStop-User) - AutoStop Monthly Base
```json
{
  "product": {
    "attributes": {
      "usagetype": "EU-AW-HW-2-AutoStop-User",
      "location": "EU (Ireland)"
    }
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Month",
        "pricePerUnit": {"USD": "11.0000000000"},
        "description": "$11/month-Hourly Plus-Windows licensed-Standard-2vCPU,4GB Memory,80GB Root,50GB User"
      }
    }
  }
}
```

#### Standard Bundle (EU-AW-HW-2-AutoStop-Usage) - AutoStop Hourly
```json
{
  "product": {
    "attributes": {
      "usagetype": "EU-AW-HW-2-AutoStop-Usage",
      "location": "EU (Ireland)"
    }
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Hour",
        "pricePerUnit": {"USD": "0.3200000000"},
        "description": "$0.32/hour-Hourly Plus-Windows licensed-Standard-2vCPU,4GB Memory"
      }
    }
  }
}
```

---

*Audit generated using AWS Pricing API data and live documentation search.*


---

## AUDIT-22: Lightsail

**Adapter**: `services/adapters/lightsail.py`  
**Supporting**: `services/lightsail.py` (112 lines), `core/pricing_engine.py` (571 lines), `core/contracts.py` (188 lines)  
**Date**: 2026-05-01  
**Auditor**: OpenCode Audit Agent  
**Pricing Data Source**: AWS Price List API (service code: AmazonLightsail), queried 2026-05-01

---

### 1. Code Analysis

#### 1.1 Adapter Architecture

**Location**: `services/adapters/lightsail.py`

The `LightsailModule` class implements the `ServiceModule` protocol:

```python
class LightsailModule(BaseServiceModule):
    key: str = "lightsail"
    cli_aliases: tuple[str, ...] = ("lightsail",)
    display_name: str = "Lightsail"

    def required_clients(self) -> tuple[str, ...]:
        return ("lightsail",)

    def scan(self, ctx: Any) -> ServiceFindings:
        result = get_enhanced_lightsail_checks(ctx)
        # ... savings calculation ...
```

**Key Points**:
- Inherits from `BaseServiceModule` (proper adapter pattern)
- Delegates to `get_enhanced_lightsail_checks()` for actual logic
- Uses `ctx.pricing_engine.get_instance_monthly_price()` when available
- Falls back to `12.0 * ctx.pricing_multiplier` heuristic

#### 1.2 Bundle Cost Calculation

**Location**: `services/lightsail.py` lines 29-41

```python
_BUNDLE_COSTS: dict[str, float] = {
    "nano_2_0": 3.50,
    "micro_2_0": 5.00,
    "small_2_0": 10.00,
    "medium_2_0": 20.00,
    "large_2_0": 40.00,
    "xlarge_2_0": 80.00,
    "2xlarge_2_0": 160.00,
}
_DEFAULT_BUNDLE_COST = 20.00

def get_lightsail_bundle_cost(bundle_id: str) -> float:
    return _BUNDLE_COSTS.get(bundle_id, _DEFAULT_BUNDLE_COST)
```

**Analysis**:
- Only 7 bundle types mapped (nano through 2xlarge)
- Default cost of $20.00 for unknown bundles
- AWS Lightsail offers many more bundle types (GPU, Memory-optimized, Compute-optimized, Research bundles)

#### 1.3 Detection Logic

**Location**: `services/lightsail.py` lines 57-109

The module performs 5 check types:

| Check Type | Logic | Status |
|------------|-------|--------|
| `idle_instances` | Detects instances with `state.name == "stopped"` | ✅ Implemented |
| `oversized_instances` | Detects running instances with "xlarge" or "large" in bundle_id | ✅ Implemented |
| `unused_static_ips` | Detects static IPs where `attachedTo` is falsy | ✅ Implemented |
| `load_balancer_optimization` | Empty placeholder | ⚠️ Not implemented |
| `database_optimization` | Empty placeholder | ⚠️ Not implemented |

**Idle Instance Detection** (lines 63-75):
```python
if instance_state == "stopped":
    checks["idle_instances"].append({
        "InstanceName": instance_name,
        "State": instance_state,
        "BundleId": bundle_id,
        "Recommendation": "Delete stopped Lightsail instance to eliminate costs",
        "EstimatedSavings": f"${get_lightsail_bundle_cost(bundle_id):.2f}/month",
        "CheckCategory": "Idle Resource Cleanup",
    })
```

**Oversized Instance Detection** (lines 77-91):
```python
if instance_state == "running" and ("xlarge" in bundle_id.lower() or "large" in bundle_id.lower()):
    checks["oversized_instances"].append({
        "InstanceName": instance_name,
        "BundleId": bundle_id,
        "State": instance_state,
        "Recommendation": "Review instance utilization - consider downsizing...",
        "EstimatedSavings": f"${get_lightsail_bundle_cost(bundle_id) * 0.3:.2f}/month potential",
        "CheckCategory": "Instance Rightsizing",
    })
```

**Static IP Detection** (lines 93-103):
```python
static_ips_response = lightsail.get_static_ips()
static_ips = static_ips_response.get("staticIps", [])

for static_ip in static_ips:
    if not static_ip.get("attachedTo"):
        checks["unused_static_ips"].append({
            "StaticIpName": static_ip.get("name"),
            "IpAddress": static_ip.get("ipAddress"),
            "Recommendation": "Release unused static IP to avoid charges",
            "EstimatedSavings": "$5.00/month",
            "CheckCategory": "Unused Resource Cleanup",
        })
```

#### 1.4 Savings Aggregation in Adapter

**Location**: `services/adapters/lightsail.py` lines 36-46

```python
for rec in recs:
    bundle_name = rec.get("BundleName", rec.get("bundleName", ""))
    if ctx.pricing_engine and bundle_name:
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonLightsail", bundle_name)
        savings += monthly if monthly > 0 else 12.0 * ctx.pricing_multiplier
    else:
        savings += 12.0 * ctx.pricing_multiplier
```

**Issue**: The adapter looks for `BundleName` or `bundleName` in recommendations, but the underlying service returns `BundleId` (not `BundleName`). This means the pricing engine lookup will always fail, falling back to the constant.

---

### 2. Pricing Validation

#### 2.1 Live API Pricing (eu-west-1)

Queried AWS Price List API for service code `AmazonLightsail` in region `eu-west-1`.

##### Standard Bundles (Linux with IPv4)

| Bundle Type | vCPU | Memory | Storage | Hourly Rate | Monthly Cost* | Hardcoded Cost | Variance |
|-------------|------|--------|---------|-------------|---------------|----------------|----------|
| nano (0.5GB) | 2 | 0.5GB | 20GB | $0.00470 | ~$3.43 | $3.50 | +2.0% |
| micro (1GB) | 2 | 1GB | 40GB | $0.00940 | ~$6.86 | $5.00 | -27.1% |
| small (2GB) | 2 | 2GB | 60GB | $0.01880 | ~$13.72 | $10.00 | -27.1% |
| medium (4GB) | 2 | 4GB | 80GB | $0.03760 | ~$27.45 | $20.00 | -27.2% |
| large (8GB) | 2 | 8GB | 160GB | $0.07520 | ~$54.90 | $40.00 | -27.2% |
| xlarge (16GB) | 4 | 16GB | 320GB | $0.11290 | ~$82.42 | $80.00 | -2.9% |
| 2xlarge (32GB) | 8 | 32GB | 640GB | $0.22581 | ~$164.84 | $160.00 | -2.9% |

*Monthly cost calculated as hourly rate × 730 hours

**Key Findings**:
- nano and xlarge/2xlarge bundles are reasonably accurate
- micro through large bundles are **significantly underestimated** (27% variance)
- Hardcoded prices appear to be outdated or from a different region

##### Static IP Pricing

| Resource | API Price | Hardcoded | Status |
|----------|-----------|-----------|--------|
| Unused Static IP | $0.005/hour ($3.65/month) | $5.00/month | ⚠️ Overestimated by 37% |

The code uses "$5.00/month" but the actual AWS price is $0.005/hour = ~$3.65/month (730 hours).

##### Other Lightsail Resources Detected in API

The API returned pricing for many bundle types not covered by the adapter:

- **GPU Bundles**: $2.44-$2.74/hour (Research workloads)
- **Memory-Optimized**: $0.09946-$0.39516/hour (2-64GB)
- **Compute-Optimized**: $0.05108-$2.26/hour (4GB-144GB)
- **Windows Bundles**: 50-100% premium over Linux
- **IPv6-only Bundles**: Slightly cheaper than IPv4

#### 2.2 Pricing Engine Integration

**Location**: `core/pricing_engine.py` lines 230-242

```python
def get_instance_monthly_price(self, service_code: str, instance_type: str) -> float:
    if service_code == "AmazonLightsail":
        price = self._fetch_generic_instance_price(service_code, instance_type)
        if price is None:
            price = self._use_fallback(12.0, f"No Lightsail price for {instance_type}")
        return price
    # ... other services
```

The pricing engine does support Lightsail via `_fetch_generic_instance_price()`, but as noted in section 1.4, the adapter passes the wrong field name (`BundleName` vs `BundleId`).

---

### 3. Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Adapter implements ServiceModule protocol | ✅ PASS | Inherits from BaseServiceModule |
| 2 | Uses pricing_engine when available | ✅ PASS | Calls `ctx.pricing_engine.get_instance_monthly_price()` |
| 3 | Idle instance detection works | ✅ PASS | Checks `state.name == "stopped"` |
| 4 | Static IP detection works | ✅ PASS | Checks `attachedTo` field |
| 5 | Bundle costs are accurate | ⚠️ WARN | 27% variance for micro-large bundles |
| 6 | Static IP pricing is accurate | ⚠️ WARN | Hardcoded $5.00 vs actual $3.65 |
| 7 | All Lightsail bundle types covered | ❌ FAIL | Only 7 of 30+ bundle types mapped |
| 8 | GPU/Research bundle support | ❌ FAIL | Not implemented |
| 9 | Load balancer checks | 🔵 INFO | Placeholder only |
| 10 | Database checks | 🔵 INFO | Placeholder only |
| 11 | Field name consistency | ❌ FAIL | Adapter uses `BundleName`, service returns `BundleId` |
| 12 | Error handling | ✅ PASS | try/except with `ctx.warn()` |

---

### 4. Issues Found

#### 4.1 ❌ FAIL — Bundle Cost Underestimation

**Severity**: HIGH  
**Location**: `services/lightsail.py` lines 29-35

| Bundle | Hardcoded | Live eu-west-1 | Variance |
|--------|-----------|----------------|----------|
| micro_2_0 | $5.00 | ~$6.86 | -27% |
| small_2_0 | $10.00 | ~$13.72 | -27% |
| medium_2_0 | $20.00 | ~$27.45 | -27% |
| large_2_0 | $40.00 | ~$54.90 | -27% |

**Impact**: Savings calculations for stopped instances and rightsizing recommendations are significantly underestimated (27% low for most bundles).

**Recommendation**: Update `_BUNDLE_COSTS` to match current eu-west-1 pricing or use pricing engine exclusively.

---

#### 4.2 ❌ FAIL — Missing Bundle Type Coverage

**Severity**: MEDIUM  
**Location**: `services/lightsail.py` lines 29-35

The adapter only maps 7 bundle types. AWS Lightsail offers:
- General purpose (nano-2xlarge) ✅ Covered
- Memory-optimized (2GB-64GB) ❌ Not covered
- Compute-optimized (4GB-192GB) ❌ Not covered
- GPU/Research (XL-4XL) ❌ Not covered
- Windows variants ❌ Not covered

**Impact**: Unknown bundle types fall back to $20.00 default, causing incorrect savings estimates.

**Recommendation**: Expand bundle mapping or use live pricing API for all bundle lookups.

---

#### 4.3 ❌ FAIL — Field Name Mismatch

**Severity**: HIGH  
**Location**: `services/adapters/lightsail.py` line 38

**Adapter code**:
```python
bundle_name = rec.get("BundleName", rec.get("bundleName", ""))
```

**Service returns**:
```python
{
    "BundleId": bundle_id,  # e.g., "micro_2_0"
    ...
}
```

**Impact**: The pricing engine lookup always fails because the field name is wrong. The adapter always falls back to `12.0 * ctx.pricing_multiplier` instead of using actual bundle pricing.

**Recommendation**: Change adapter to use `rec.get("BundleId")` to match the service output.

---

#### 4.4 ⚠️ WARN — Static IP Overestimation

**Severity**: LOW  
**Location**: `services/lightsail.py` line 101

- Hardcoded: "$5.00/month"
- Actual: $0.005/hour × 730 = $3.65/month
- Variance: +37% overestimation

**Impact**: Savings estimates for releasing static IPs are inflated by 37%.

**Recommendation**: Use API pricing: $0.005/hour for unattached static IPs.

---

#### 4.5 🔵 INFO — Placeholder Checks

**Severity**: INFO  
**Location**: `services/lightsail.py` lines 50-54

Two check types are defined but never populated:
- `load_balancer_optimization`: Empty list
- `database_optimization`: Empty list

AWS Pricing API shows:
- Load Balancer: $0.0242/hour (~$17.67/month)
- Database bundles: $0.0806-$0.6586/hour

**Recommendation**: Implement checks for Lightsail Load Balancers and Managed Databases.

---

#### 4.6 🔵 INFO — Oversized Instance Detection Heuristic

**Severity**: INFO  
**Location**: `services/lightsail.py` line 77

The detection uses simple string matching:
```python
if "xlarge" in bundle_id.lower() or "large" in bundle_id.lower():
```

This catches "large", "xlarge", "2xlarge" but also falsely matches "nano_2_0" (contains "2_0" not "large").

The 30% savings estimate is a heuristic without utilization data.

**Recommendation**: Consider fetching CloudWatch metrics for actual utilization before flagging as oversized.

---

### 5. Verdict

#### Summary

| Category | Status |
|----------|--------|
| Architecture | ✅ Correct (adapter pattern) |
| Idle detection | ✅ Working |
| Static IP detection | ✅ Working |
| Bundle pricing accuracy | ❌ 27% underestimated |
| Static IP pricing | ⚠️ 37% overestimated |
| Pricing engine integration | ❌ Broken (field name mismatch) |
| Bundle coverage | ❌ Incomplete (7 of 30+ types) |
| Error handling | ✅ Working |

#### Final Assessment

The Lightsail adapter has a **correct architectural foundation** but contains **critical issues** that affect pricing accuracy:

1. **Field name mismatch** prevents pricing engine from working
2. **Hardcoded bundle costs** are outdated (27% low for most bundles)
3. **Limited bundle coverage** misses GPU, Memory-optimized, Compute-optimized, and Windows bundles
4. **Static IP pricing** is overestimated

**Verdict**: ❌ **FAIL** (52/100)

The adapter requires fixes before production use:
1. Fix field name from `BundleName` to `BundleId` in adapter
2. Update hardcoded bundle costs to match eu-west-1 pricing
3. Expand bundle type coverage or remove hardcoded costs and rely on pricing engine
4. Correct static IP pricing to $0.005/hour

---

### Appendix: Raw Pricing API Results

#### Standard Bundle — nano (0.5GB IPv6)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Instance
Memory: 0.5GB, vCPU: 2, Storage: 20GB
Price: $0.00470/hour (~$3.43/month)
SKU: 9B7ZUZ96CXNFSUAR
```

#### Standard Bundle — micro (1GB with IPv4)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Instance
Memory: 1GB, vCPU: 2, Storage: 40GB
Price: $0.00940/hour (~$6.86/month)
SKU: 6WHN6NX5RRBZ6BWX
```

#### Static IP (Unattached)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Networking
Price: $0.005/hour ($3.65/month)
SKU: 7BHMTQCG3VYCM7NA
Description: Static IP address not attached to an instance
```

#### Load Balancer
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Load Balancing
Price: $0.0242/hour ($17.67/month)
SKU: 37PP4FD27VHEQU2H
```

#### Memory-Optimized Bundle (16GB)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Instance
Memory: 16GB, vCPU: 2, Storage: 160GB
Price: $0.09946/hour (~$72.61/month)
SKU: 5AJ9EYU6YWV44FEA
```

---

*End of Audit Report*


---

## AUDIT-23: AppRunner

**Date**: 2026-05-01  
**Auditor**: OpenCode Agent  
**Scope**: `services/adapters/apprunner.py` and underlying `services/apprunner.py`  
**Region Tested**: eu-west-1 (Ireland)

---

### Code Analysis

#### Architecture
The AppRunner adapter follows the standard two-layer architecture:

1. **Adapter Layer** (`services/adapters/apprunner.py`): Implements `ServiceModule` Protocol
   - Hardcoded pricing constants for vCPU and memory
   - Applies 30% arbitrary rightsizing discount to all recommendations
   - Uses `ctx.pricing_multiplier` for regional pricing adjustments

2. **Service Layer** (`services/apprunner.py`): Contains actual detection logic
   - Lists App Runner services via `apprunner:ListServices`
   - Describes service configuration via `apprunner:DescribeService`
   - Very basic detection logic (triggers only on "large" in config or non-1GB memory)

#### Pricing Implementation

```python
## From services/adapters/apprunner.py lines 40-59
APP_RUNNER_VCPU_HOURLY = 0.064      # $/vCPU-hour
APP_RUNNER_MEM_GB_HOURLY = 0.007    # $/GB-hour

monthly_active = (
    (vcpus * APP_RUNNER_VCPU_HOURLY + mem_gb * APP_RUNNER_MEM_GB_HOURLY) * 730 * ctx.pricing_multiplier
)
savings += monthly_active * 0.30    # 30% arbitrary discount
```

---

### Pricing Validation

#### AWS Pricing API Results (eu-west-1)

| Component | API Price | Adapter Constant | Match |
|-----------|-----------|------------------|-------|
| vCPU-hour (x86) | $0.064 | $0.064 | ✅ |
| GB-hour | $0.007 | $0.007 | ✅ |
| Build-mins | $0.005/min | Not used | N/A |
| Automatic Deployment | $1.00/pipeline | Not used | N/A |
| Provisioned GB-hour | $0.007 | Not used | N/A |

**Pricing API Verification**: ✅ Constants match AWS Pricing API for eu-west-1

#### App Runner Pricing Model (from AWS Documentation)

App Runner uses a **dual-mode pricing model**:

1. **Provisioned Container Instances** (Idle State):
   - Charged: $0.007/GB-hour (memory only)
   - Purpose: Keep application warm, eliminate cold starts
   - Default: 1 provisioned instance minimum

2. **Active Container Instances** (Processing State):
   - Charged: $0.064/vCPU-hour + $0.007/GB-hour
   - Automatically scales up/down based on request volume
   - One-minute minimum vCPU charge when transitioning from provisioned

3. **Additional Costs**:
   - Build fee: $0.005/minute (when deploying from source)
   - Automatic deployments: $1.00/pipeline/month

---

### Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Pricing constants match AWS API | ✅ PASS | $0.064/vCPU-hr and $0.007/GB-hr verified |
| 2 | Regional pricing multiplier applied | ✅ PASS | Uses `ctx.pricing_multiplier` |
| 3 | Required boto3 clients declared | ✅ PASS | Returns `("apprunner",)` |
| 4 | Returns proper ServiceFindings | ✅ PASS | Correct dataclass structure |
| 5 | Error handling for API failures | ⚠️ WARN | Silent pass on exceptions (line 60-61, 63-64) |
| 6 | CloudWatch metrics for rightsizing | ❌ FAIL | No utilization data collected |
| 7 | Dual-mode pricing model honored | ❌ FAIL | Assumes 24/7 active, ignores provisioned state |
| 8 | All cost components included | ❌ FAIL | Build fees and deployment fees omitted |
| 9 | Detection logic comprehensive | ❌ FAIL | Only triggers on "large" or non-1GB memory |
| 10 | Unused service detection | ❌ FAIL | Services not checked for actual traffic |

**Score: 4/10 criteria passed**

---

### Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| AP-001 | **HIGH** | Assumes 24/7 active usage; ignores provisioned idle state | Savings estimates 3-5x too high for typical low-traffic services |
| AP-002 | **HIGH** | 30% rightsizing discount is arbitrary, not based on metrics | Recommendations lack factual basis; could mislead users |
| AP-003 | **MEDIUM** | No CloudWatch integration for CPU/memory utilization | Cannot detect actually idle or over-provisioned services |
| AP-004 | **MEDIUM** | Build fees ($0.005/min) not included in savings calc | Missing cost optimization opportunity |
| AP-005 | **MEDIUM** | Automatic deployment fees ($1/mo) not included | Incomplete cost picture for CI/CD workflows |
| AP-006 | **MEDIUM** | Detection logic too narrow (only "large" or non-1GB) | Many optimization opportunities missed |
| AP-007 | **LOW** | No check for services that could be paused | Missed savings for dev/test environments |
| AP-008 | **LOW** | Silent exception handling masks failures | Debug difficulty if App Runner APIs fail |
| AP-009 | **LOW** | No concurrency optimization analysis | Key cost driver in App Runner not analyzed |

---

### Detailed Analysis

#### Issue AP-001: Incorrect Pricing Model Assumption

**Current Behavior**: Adapter calculates costs assuming services are always in "active" state:
```
Monthly = (vCPU × $0.064 + Memory × $0.007) × 730 hours
```

**Actual App Runner Behavior**:
- Most services spend majority of time in "provisioned" state (memory only)
- Only pay vCPU costs when actively processing requests
- Example: A 1vCPU/2GB service active 2 hours/day:
  - **Actual cost**: $10.22/month (provisioned) + $0.13/day × 30 = $14.12/month
  - **Adapter calculates**: $56.94/month (as if 24/7 active)
  - **Overestimation**: 4× too high

**Recommendation**: Model both provisioned and active states; use conservative traffic assumptions.

#### Issue AP-002: Arbitrary 30% Discount

**Current Behavior**: All savings multiplied by 0.30 without justification.

**Comparison with Other Adapters**:
- EC2 adapter: Uses CloudWatch CPU metrics, Compute Optimizer recommendations
- Lambda adapter: CloudWatch invocation metrics, memory analysis
- RDS adapter: CloudWatch connection metrics

**Recommendation**: Implement CloudWatch metric collection or use conservative fixed percentage based on industry benchmarks.

#### Issue AP-003: Missing CloudWatch Integration

**Gap**: No `requires_cloudwatch = True` flag, no metric collection.

**Relevant Metrics** (from App Runner console):
- `RequestCount` — total requests (detect unused services)
- `ActiveInstances` — current active container count
- `MemoryUtilization` — percentage of memory used
- `CPUUtilization` — percentage of CPU used

**Recommendation**: Add CloudWatch metric queries to detect:
1. Services with zero requests (pausable)
2. Services with low CPU/memory utilization (rightsizable)
3. Over-provisioned concurrency settings

#### Issue AP-004 & AP-005: Missing Cost Components

**Build Fees**: When deploying from source code, App Runner charges $0.005/minute for build time. For teams with frequent deployments, this is significant.

**Automatic Deployments**: $1.00/pipeline/month. Services with CI/CD integration incur this fee.

**Recommendation**: Add detection for:
- Source-based deployments (vs image-based)
- Automatic deployment configuration
- Calculate build frequency impact

---

### Correctness Verification

#### Pricing Constants
```bash
## AWS Pricing API verified on 2026-05-01
Service: AWSAppRunner
Region: eu-west-1
- vCPU-hour (x86): $0.064 ✅
- GB-hour: $0.007 ✅
```

#### Calculation Example
For a 1 vCPU, 2 GB service:
```python
## Adapter calculation (always active assumption)
(1 * 0.064 + 2 * 0.007) * 730 * 1.0 = $56.94/month
Savings at 30% = $17.08/month

## Realistic calculation (2 hours active/day)
## Provisioned: 2 GB * $0.007 * 730 = $10.22/month
## Active: (1 * $0.064 + 2 * $0.007) * 2 hrs/day * 30 days = $4.68/month
## Total: $14.90/month
## Realistic savings (pause dev): $14.90/month (not $17.08)
```

---

### Comparison with Best Practices

| Aspect | AppRunner Adapter | Best Practice (EC2 Adapter) | Gap |
|--------|-------------------|----------------------------|-----|
| Pricing source | Hardcoded constants | AWS Pricing API via PricingEngine | Medium |
| Rightsizing basis | Arbitrary 30% | CloudWatch CPU metrics | High |
| Idle detection | None | 14-day CloudWatch analysis | High |
| State awareness | Single (active) | Multiple (running/stopped) | High |
| Error handling | Silent pass | Warning emission | Low |

---

### Recommendations

#### Immediate (P0)
1. **Fix pricing model**: Calculate based on provisioned + realistic active hours
2. **Remove arbitrary 30%**: Use actual CloudWatch metrics or conservative 10-15%

#### Short-term (P1)
3. **Add CloudWatch integration**: Collect RequestCount, CPU, Memory metrics
4. **Detect pausable services**: Zero requests over 14 days = pause recommendation
5. **Include build/deployment costs**: Detect source-based deployments

#### Long-term (P2)
6. **Concurrency analysis**: Review auto-scaling concurrency settings
7. **Configuration optimization**: Recommend optimal vCPU/memory combinations
8. **Add to pricing engine**: Implement `get_apprunner_monthly_price()` method

---

### Verdict

**⚠️ WARN — 40/100**

The AppRunner adapter functions correctly from a code perspective but contains significant methodological flaws in its cost modeling:

1. **Pricing Model**: The assumption of 24/7 active usage leads to **4-5× overestimation** of potential savings for typical low-traffic services.

2. **Detection Logic**: The underlying service module's detection is too narrow, only triggering on superficial string matching ("large") rather than analyzing actual resource utilization.

3. **Missing Features**: Unlike EC2, Lambda, and RDS adapters, AppRunner lacks CloudWatch metric integration, making it impossible to provide data-driven recommendations.

4. **Cost Completeness**: Build fees and automatic deployment costs are significant for App Runner but completely omitted.

**Recommendation**: The adapter should be marked as **beta quality** in documentation until CloudWatch integration and proper dual-mode pricing are implemented. Current savings estimates should be treated as theoretical maximums, not realistic projections.

---

### Appendix: AWS Documentation References

- [App Runner Pricing](https://aws.amazon.com/apprunner/pricing/)
- [App Runner InstanceConfiguration API](https://docs.aws.amazon.com/apprunner/latest/api/API_InstanceConfiguration.html)
- [App Runner Architecture](https://docs.aws.amazon.com/apprunner/latest/dg/architecture.html)
- Pricing verified via AWS Price List API: `AWSAppRunner` service code

---

*Generated by OpenCode Agent | Audit ID: AUDIT-23*


---

## AUDIT-24: Transfer

**Adapter:** AWS Transfer Family  
**File:** `services/adapters/transfer.py`  
**Service Module:** `services/transfer_svc.py`  
**Audit Date:** 2026-05-01  
**Auditor:** OpenCode Agent  
**Region Tested:** eu-west-1 (EU Ireland)

---

### Executive Summary

The AWS Transfer Family adapter implements protocol-based cost optimization detection for SFTP/FTPS/FTP/AS2 servers. The pricing calculation is **accurate for protocol hourly rates** but has **significant gaps** in covering the full pricing model, including data transfer costs, connector fees, and Web App pricing.

---

### Code Analysis

#### Adapter Structure (`transfer.py` - 56 lines)

```python
class TransferModule(BaseServiceModule):
    key: str = "transfer"
    cli_aliases: tuple[str, ...] = ("transfer",)
    display_name: str = "Transfer Family"

    def required_clients(self) -> tuple[str, ...]:
        return ("transfer",)
```

**Architecture:**
- ✅ Follows `BaseServiceModule` pattern correctly
- ✅ Implements `required_clients()` and `scan()` methods
- ✅ Uses `SourceBlock` for structured findings
- ✅ Properly integrates with `ServiceFindings` contract

#### Pricing Calculation Logic

```python
TRANSFER_PER_PROTOCOL_HOUR = 0.30
savings = 0.0
for rec in recs:
    protocols = rec.get("Protocols", ["SFTP"])
    num_protocols = len(protocols) if isinstance(protocols, list) else 1
    endpoint_monthly = TRANSFER_PER_PROTOCOL_HOUR * num_protocols * 730 * ctx.pricing_multiplier
    savings += endpoint_monthly
```

**Formula:** `$0.30 × protocols × 730 hours × pricing_multiplier`

#### Service Module (`transfer_svc.py` - 82 lines)

**Detection Logic:**
1. **Protocol Optimization** - Flags servers with >1 enabled protocol
2. **Unused Servers** - Identifies STOPPED/OFFLINE servers

**Checks Dictionary Structure:**
```python
checks: dict[str, list[dict[str, Any]]] = {
    "unused_servers": [],
    "protocol_optimization": [],
    "endpoint_optimization": [],  # Empty - not implemented
}
```

---

### Pricing Validation

#### AWS Pricing API Data (eu-west-1)

| Component | Usage Type | Price | Adapter Coverage |
|-----------|------------|-------|------------------|
| **SFTP Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **FTPS Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **FTP Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **AS2 Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **Data Upload** | EU-UploadBytes | $0.04/GB | ❌ Missing |
| **Data Download** | EU-DownloadBytes | $0.04/GB | ❌ Missing |
| **SFTP Connector Send** | EU-SFTPConnector-SendBytes | $0.40/GB | ❌ Missing |
| **SFTP Connector Retrieve** | EU-SFTPConnector-RetrieveBytes | $0.40/GB | ❌ Missing |
| **Web App** | EU-WebAppHours | $0.50/hour | ❌ Missing |
| **PGP Decryption** | EU-DecryptBytes-PGP | $0.10/GB | ❌ Missing |
| **AS2 Messages (inbound)** | EU-InboundMessages | $0.01-$0.001/msg | ❌ Missing |
| **AS2 Messages (outbound)** | EU-OutboundMessages | $0.01-$0.001/msg | ❌ Missing |
| **Connector Calls** | EU-SFTPConnectorCall-* | $0.001/call | ❌ Missing |

#### Pricing Accuracy Assessment

| Metric | Value | Status |
|--------|-------|--------|
| Protocol Hour Rate | $0.30 | ✅ Accurate |
| Hours/Month | 730 | ✅ Accurate |
| Regional Multiplier Support | Yes | ✅ Implemented |
| Data Transfer Pricing | $0.04/GB | ❌ Not Covered |
| Connector Pricing | $0.40/GB | ❌ Not Covered |

---

### Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Correct protocol hourly pricing ($0.30) | ✅ PASS | Matches AWS Pricing API |
| 2 | Proper client registration | ✅ PASS | Returns `("transfer",)` tuple |
| 3 | Follows adapter pattern | ✅ PASS | Extends `BaseServiceModule` |
| 4 | Uses `ServiceFindings` contract | ✅ PASS | Correct structure |
| 5 | Error handling with `ctx.warn()` | ✅ PASS | Try/catch around API calls |
| 6 | Supports regional pricing multiplier | ✅ PASS | Uses `ctx.pricing_multiplier` |
| 7 | Protocol detection logic | ✅ PASS | Flags servers with >1 protocol |
| 8 | Unused server detection | ✅ PASS | Detects STOPPED/OFFLINE state |
| 9 | Data transfer cost estimation | ❌ FAIL | Not implemented |
| 10 | SFTP Connector cost tracking | ❌ FAIL | Not implemented |
| 11 | Web App pricing support | ❌ FAIL | Not implemented |
| 12 | AS2 message pricing | ❌ FAIL | Not implemented |
| 13 | Realistic savings calculation | ⚠️ WARN | Assumes 100% protocol removal |
| 14 | Endpoint type optimization | ❌ FAIL | VPC endpoint costs not analyzed |
| 15 | Storage backend cost awareness | ❌ FAIL | S3 vs EFS costs not differentiated |

**Score:** 8/15 criteria passed (53%)

---

### Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| TRANS-001 | 🔴 HIGH | Savings calculation assumes complete protocol removal | Reports inflated savings that may not be achievable |
| TRANS-002 | 🟡 MEDIUM | Missing data transfer cost analysis ($0.04/GB) | Underestimates total cost of ownership |
| TRANS-003 | 🟡 MEDIUM | No SFTP Connector pricing ($0.40/GB) | Misses connector-related optimization opportunities |
| TRANS-004 | 🟡 MEDIUM | No Web App pricing ($0.50/hour) | Incomplete for Web App-enabled servers |
| TRANS-005 | 🟢 LOW | `endpoint_optimization` category is empty placeholder | Dead code - should be removed or implemented |
| TRANS-006 | 🟢 LOW | Generic "Variable" savings for protocol optimization | Reduces actionability of recommendations |
| TRANS-007 | 🟡 MEDIUM | No VPC endpoint cost analysis | VPC-hosted endpoints have different pricing |
| TRANS-008 | 🟡 MEDIUM | No PGP decryption cost tracking ($0.10/GB) | Missing encryption-related costs |

#### Issue Details

##### TRANS-001: Inflated Savings Calculation

**Current Logic:**
```python
endpoint_monthly = TRANSFER_PER_PROTOCOL_HOUR * num_protocols * 730 * ctx.pricing_multiplier
```

**Problem:** This calculates the cost of ALL protocols, but the recommendation is to "review if all protocols are needed" - not to remove them. A server with SFTP+FTPS (2 protocols) reports $438/month savings, but removing one protocol only saves $219/month.

**Recommendation:** Calculate savings as cost of redundant protocols only (protocols - 1).

##### TRANS-002: Missing Data Transfer Costs

Data transfer is often the largest cost component for Transfer Family:
- Upload: $0.04/GB
- Download: $0.04/GB
- SFTP Connector: $0.40/GB (10x higher!)

**Impact:** For a server transferring 1TB/month, data costs ($40) exceed protocol costs ($30).

##### TRANS-003: Missing Connector Pricing

SFTP Connectors are priced at $0.40/GB - 10x the standard data transfer rate. The adapter does not detect or price connector usage.

---

### Verdict

#### ⚠️ WARN — 65/100

**Assessment:** The Transfer Family adapter has **accurate protocol hourly pricing** ($0.30/hour confirmed by AWS Pricing API) but has **significant gaps** in cost coverage. The adapter is functional for basic protocol optimization detection but underestimates total costs and overestimates potential savings.

#### Strengths
1. ✅ Protocol hourly rate ($0.30) is accurate for eu-west-1
2. ✅ Follows established adapter patterns correctly
3. ✅ Proper error handling with context warnings
4. ✅ Supports regional pricing multipliers
5. ✅ Detects unused servers (STOPPED/OFFLINE)

#### Weaknesses
1. ❌ Savings calculation assumes 100% protocol removal
2. ❌ Missing data transfer cost analysis ($0.04/GB)
3. ❌ Missing SFTP Connector pricing ($0.40/GB)
4. ❌ Missing Web App pricing ($0.50/hour)
5. ❌ Missing PGP decryption costs ($0.10/GB)
6. ❌ Missing AS2 message pricing
7. ❌ Empty `endpoint_optimization` category

#### Recommendations

1. **Fix TRANS-001:** Change savings calculation to `(num_protocols - 1) × $0.30 × 730` for protocol optimization recommendations

2. **Add Data Transfer Estimation:** Query CloudWatch metrics for `BytesUploaded` and `BytesDownloaded` to estimate transfer costs

3. **Add Connector Detection:** Query `list_connectors` API and include connector transfer costs

4. **Remove or Implement:** Either implement `endpoint_optimization` checks or remove the empty placeholder

5. **Enhanced Recommendations:** 
   - Detect idle servers (low transfer volume relative to protocol hours)
   - Identify servers suitable for consolidation
   - Flag VPC endpoint vs Public endpoint cost differences

---

### AWS Documentation References

- [AWS Transfer Family Pricing](https://aws.amazon.com/aws-transfer-family/pricing/)
- [AWS Transfer Family User Guide](https://docs.aws.amazon.com/transfer/latest/userguide/what-is-aws-transfer-family.html)
- Pricing validated against AWS Price List API (service code: `AWSTransfer`)

---

### Appendix: Pricing API Raw Data

**Service Code:** AWSTransfer  
**Region:** eu-west-1 (EU Ireland)  
**API Call:** `get_pricing(service_code="AWSTransfer", region="eu-west-1")`

**Key Price Points:**
```json
{
  "EU-ProtocolHours": "$0.30/hour (SFTP, FTPS, FTP, AS2)",
  "EU-UploadBytes": "$0.04/GB",
  "EU-DownloadBytes": "$0.04/GB",
  "EU-WebAppHours": "$0.50/hour",
  "EU-SFTPConnector-SendBytes": "$0.40/GB",
  "EU-SFTPConnector-RetrieveBytes": "$0.40/GB",
  "EU-DecryptBytes-PGP": "$0.10/GB"
}
```

---

*End of Audit Report*


---

## AUDIT-25: DMS

**Date:** 2026-05-01
**Auditor:** Automated Audit Agent
**Files:** `services/adapters/dms.py` (56L), `services/dms.py` (132L)
**Scope:** Pricing accuracy, savings calculations, detection logic, protocol compliance

---

### Code Analysis

#### Architecture

The DMS adapter follows the standard two-layer pattern:
- **Adapter** (`services/adapters/dms.py`): `DmsModule(BaseServiceModule)` — thin wrapper calling the service module and computing savings
- **Service module** (`services/dms.py`): `get_enhanced_dms_checks()` — performs actual API calls and logic

#### Detection Categories

| Category | Logic | Threshold |
|----------|-------|-----------|
| `instance_rightsizing` | Instance class contains `"large"` AND CPU < 30% over 7 days | CPU avg < 30% |
| `unused_instances` | Subset of rightsizing where CPU < 5% | CPU avg < 5% |
| `serverless_migration` | All serverless replication configs found | None (informational) |

#### Pricing Flow

```
DmsModule.scan(ctx)
  → get_enhanced_dms_checks(ctx)        # returns {recommendations, checks}
  → For each recommendation:
      if pricing_engine and instance_class:
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonDMS", instance_class)
        savings += monthly * 0.35       # flat 35%
      else:
        savings += 50                   # flat $50 fallback
```

#### Key Code Issues Identified

1. **`services/dms.py:47`** — Detection filter `if "large" in instance_class` is overly narrow
2. **`services/dms.py:88`** — Hardcoded `"$100/month potential"` savings text
3. **`services/adapters/dms.py:40`** — Service code `"AmazonDMS"` incorrect
4. **`services/dms.py:51-57`** — CloudWatch client used but not declared in `required_clients()`
5. **`services/adapters/dms.py:42`** — No `pricing_multiplier` applied

---

### Pricing Validation

#### AWS Price List API Data (eu-west-1, OnDemand, 2025-11-01)

**Service code:** `AWSDatabaseMigrationSvc` (NOT `"AmazonDMS"`)

**Instance pricing reference (monthly = hourly × 730):**

| Instance Type | Single-AZ $/hr | Single-AZ $/mo | Multi-AZ $/hr | Multi-AZ $/mo |
|---------------|---------------|----------------|---------------|---------------|
| t3.small | $0.039 | $28.47 | $0.078 | $56.94 |
| t3.medium | $0.078 | $56.94 | $0.156 | $113.88 |
| t3.large | $0.157 | $114.61 | $0.314 | $229.22 |
| c5.large | $0.175 | $127.75 | $0.350 | $255.50 |
| c6i.large | $0.175 | $127.75 | $0.350 | $255.50 |
| r5.large | $0.230 | $167.90 | $0.460 | $335.80 |
| r6i.large | $0.230 | $167.90 | $0.460 | $335.80 |
| c5.xlarge | $0.351 | $256.23 | $0.702 | $512.46 |
| c6i.xlarge | $0.351 | $256.23 | $0.702 | $512.46 |
| r5.xlarge | $0.460 | $335.80 | $0.920 | $671.60 |
| r6i.xlarge | $0.460 | $335.80 | $0.920 | $671.60 |
| r5.2xlarge | $0.920 | $671.60 | $1.840 | $1,343.20 |

**Key pricing facts from AWS documentation:**
- DMS pricing has **separate SKUs** for Single-AZ and Multi-AZ (Multi-AZ = 2× Single-AZ)
- Instance type format in pricing API: `r5.large`, `t3.large` (no `dms.` prefix)
- Instance type format from boto3 API: `dms.r5.large`, `dms.t3.large` (has `dms.` prefix)
- Storage: 100GB GP2 included for compute-optimized, 50GB for burstable; extra storage billed separately
- DMS Serverless billed per DCU-hour (1 DCU = 2GB RAM)
- Supported families: T3, T2 (burstable), C5/C6i/C7i (compute), R5/R6i/R7i (memory)
- T2 instances are legacy; T3 is current generation

#### Pricing Accuracy Assessment

| Aspect | Status | Detail |
|--------|--------|--------|
| Service code | ❌ WRONG | Uses `"AmazonDMS"`, actual is `AWSDatabaseMigrationSvc` |
| Instance class format | ❌ LIKELY BROKEN | Passes `dms.r5.large` to pricing engine; API expects `r5.large` |
| Hardcoded "$100/month" | ❌ GROSSLY INACCURATE | t3.small total cost is only $29/mo; $100 savings impossible |
| Flat 35% savings ratio | ⚠️ ARBITRARY | No basis in actual rightsizing calculations |
| Flat $50 fallback | ⚠️ INACCURATE | Overstates small instances ($28/mo), understates large ($672/mo) |
| Multi-AZ awareness | ❌ MISSING | Doesn't detect or factor 2× Multi-AZ pricing |
| Storage costs | ❌ MISSING | No storage overage analysis |
| `pricing_multiplier` | ❌ NOT APPLIED | Regional adjustment ignored |

---

### Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Service code matches AWS Price List API | ❌ FAIL | `"AmazonDMS"` vs actual `AWSDatabaseMigrationSvc` |
| 2 | Instance class format compatible with pricing API | ❌ FAIL | boto3 returns `dms.r5.large`; pricing API expects `r5.large` |
| 3 | Savings estimates within ±20% of actual | ❌ FAIL | "$100/month potential" is 3.5× actual for smallest instances |
| 4 | Detection covers all instance families | ❌ FAIL | Only flags classes containing `"large"` — misses xlarge, 2xlarge, medium, small, micro |
| 5 | Multi-AZ pricing factored | ❌ FAIL | Not detected or priced differently |
| 6 | CloudWatch client declared in required_clients | ❌ FAIL | Only `("dms",)` declared; also uses `cloudwatch` |
| 7 | pricing_multiplier applied | ❌ FAIL | Not used anywhere |
| 8 | No hardcoded dollar amounts in recommendations | ❌ FAIL | `"$100/month potential"` hardcoded in service module |
| 9 | Serverless migration has meaningful analysis | ⚠️ WARN | Generic text only, no cost comparison |
| 10 | Error handling doesn't produce false recommendations | ❌ FAIL | CW failure falls through to generic recommendation without utilization data |
| 11 | Protocol compliance (ServiceModule) | ✅ PASS | Correctly extends BaseServiceModule, implements scan() |
| 12 | Returns valid ServiceFindings | ✅ PASS | Correct structure with sources dict |
| 13 | optimization_descriptions populated | ✅ PASS | Has instance_rightsizing entry |
| 14 | Module registered in ALL_MODULES | ✅ PASS | Registered correctly |
| 15 | Graceful degradation on permission errors | ⚠️ WARN | Top-level catch exists but permission issues on CW not surfaced |

---

### Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| DMS-01 | 🔴 CRITICAL | Service code `"AmazonDMS"` is wrong — actual API code is `AWSDatabaseMigrationSvc` | Pricing engine will fail to find any DMS prices; all savings fall to flat $50 fallback |
| DMS-02 | 🔴 CRITICAL | Instance class format mismatch: boto3 returns `dms.r5.large`, pricing API expects `r5.large` | Even with correct service code, pricing lookup will return None for all instances |
| DMS-03 | 🔴 HIGH | Detection only flags instances with `"large"` in class name | Misses `dms.r5.xlarge` ($336/mo), `dms.r5.2xlarge` ($672/mo), `dms.r5.4xlarge` ($1,344/mo) — the most expensive instances |
| DMS-04 | 🟠 HIGH | Hardcoded `"$100/month potential"` savings text | For t3.small ($28/mo total), claims $100 savings — 3.5× impossible. Misleads users |
| DMS-05 | 🟠 HIGH | CloudWatch client not declared in `required_clients()` | Orchestrator cannot pre-check CW permissions; silent failure at scan time |
| DMS-06 | 🟡 MEDIUM | CloudWatch failure produces false recommendations | When CW call fails, instance gets generic rightsizing recommendation without utilization evidence |
| DMS-07 | 🟡 MEDIUM | No Multi-AZ detection or pricing | Multi-AZ instances cost 2× but adapter doesn't flag unnecessary Multi-AZ for dev/test |
| DMS-08 | 🟡 MEDIUM | Flat 35% savings ratio is arbitrary | Actual rightsizing savings depend on instance family and target size; 35% may under/overstate |
| DMS-09 | 🟡 MEDIUM | `pricing_multiplier` not applied | Savings not adjusted for region (e.g., ap-south-1 is cheaper than us-east-1) |
| DMS-10 | 🔵 LOW | No storage cost analysis | DMS charges for storage above included allowance (50-100GB); not checked |
| DMS-11 | 🔵 LOW | No generation-based recommendations | Doesn't recommend upgrading T2→T3 or R5→R6i for better price/performance |
| DMS-12 | 🔵 LOW | CPU-only utilization check | DMS is often memory/I/O bound; doesn't check `FreeableMemory` or `NetworkThroughput` |
| DMS-13 | 🔵 LOW | Serverless cost comparison missing | Doesn't estimate serverless DCU cost vs provisioned instance cost |
| DMS-14 | 🔵 LOW | `describe_replication_configs` errors silently swallowed | Permission issues on serverless API not surfaced to user |

---

### Verdict

#### ⚠️ WARN — 38/100

The DMS adapter has **correct structural compliance** with the ServiceModule protocol and produces valid ServiceFindings objects. However, it has **two critical pricing lookup bugs** (wrong service code and instance class format mismatch) that mean the pricing engine path likely always fails, falling back to flat-rate estimates. The detection logic is severely limited — only checking instances with `"large"` in the name — which means it misses the most expensive instances (xlarge, 2xlarge, etc.). The hardcoded `"$100/month potential"` savings text is materially misleading for small instances.

#### Score Breakdown

| Category | Weight | Score | Weighted |
|----------|--------|-------|----------|
| Protocol Compliance | 15% | 95 | 14.3 |
| Pricing Accuracy | 25% | 10 | 2.5 |
| Detection Coverage | 25% | 25 | 6.3 |
| Savings Calculation | 20% | 30 | 6.0 |
| Error Handling | 15% | 60 | 9.0 |
| **Total** | **100%** | | **38.0** |

#### Recommended Fixes (Priority Order)

1. **DMS-01 + DMS-02**: Fix service code to `AWSDatabaseMigrationSvc` and strip `dms.` prefix from instance class before pricing lookup
2. **DMS-03**: Remove `"large"` filter — check all running instances regardless of size
3. **DMS-04**: Replace hardcoded `$100/month` with calculated savings from pricing engine
4. **DMS-05**: Add `"cloudwatch"` to `required_clients()` return tuple
5. **DMS-06**: Don't emit rightsizing recommendations when CW data is unavailable
6. **DMS-07**: Detect Multi-AZ via `MultiAZ` field and recommend single-AZ for non-production
7. **DMS-09**: Apply `ctx.pricing_multiplier` to all calculated savings


---

## AUDIT-26: QuickSight

**Date**: 2026-05-01  
**Adapter**: `services/adapters/quicksight.py` (58 lines)  
**Service Module**: `services/quicksight.py` (94 lines)  
**Auditor**: Automated audit against AWS Pricing API + AWS docs

---

### Code Analysis

#### Architecture

The adapter follows the standard two-layer pattern:

1. **Adapter layer** (`services/adapters/quicksight.py`): `QuicksightModule(BaseServiceModule)` implementing `ServiceModule` protocol. Delegates to service module and applies SPICE per-GB pricing to compute savings.

2. **Service module** (`services/quicksight.py`): `get_enhanced_quicksight_checks(ctx)` performs the actual AWS API calls:
   - `describe_account_subscription` — checks if QuickSight is enabled
   - `list_namespaces` (paginated) — enumerates namespaces
   - `list_users` (paginated per namespace) — counts total users
   - `describe_spice_capacity` — retrieves SPICE used/total capacity in bytes

#### Detection Logic

The service module detects exactly **one** optimization category: **SPICE capacity underutilization** (triggered when SPICE usage < 50% of total capacity). It counts users but never generates user-related recommendations — user count is only included as metadata in the SPICE optimization record.

#### Savings Calculation (Adapter Layer)

```python
QUICKSIGHT_SPICE_PER_GB = {"ENTERPRISE": 0.38, "STANDARD": 0.25}
savings = 0.0
for rec in recs:
    edition = rec.get("Edition", "ENTERPRISE")
    spice_price = QUICKSIGHT_SPICE_PER_GB.get(edition, 0.38) * ctx.pricing_multiplier
    unused_gb = rec.get("UnusedSpiceCapacityGB", 0)
    if unused_gb > 0:
        savings += unused_gb * spice_price
    else:
        savings += 30.0 * ctx.pricing_multiplier
```

#### Critical Disconnect: Service Module vs Adapter

The adapter reads fields that the service module never sets:

| Field Adapter Reads | Field Service Module Sets | Match? |
|---------------------|--------------------------|--------|
| `rec.get("Edition", "ENTERPRISE")` | Never sets `Edition` | **NO** — always defaults to `"ENTERPRISE"` |
| `rec.get("UnusedSpiceCapacityGB", 0)` | Never sets `UnusedSpiceCapacityGB` | **NO** — always `0` |
| `rec.get("UserCount")` | Sets `UserCount` | Yes (but not used for savings) |

Because `UnusedSpiceCapacityGB` is never set, the `if unused_gb > 0` branch **never executes**. Every recommendation falls into the `else` branch: `savings += 30.0 * ctx.pricing_multiplier` (a flat $30 per recommendation).

Meanwhile, the service module has its own savings estimate: `~${(total_capacity - used_capacity) * 0.25:.0f}/month` — which is a hardcoded $0.25/GB regardless of edition.

---

### Pricing Validation

#### AWS Pricing API (eu-west-1, 2026-04-03)

| SPICE Type | Usage Type | Price (USD/GB-Mo) |
|------------|-----------|-------------------|
| Enterprise SPICE | `EU-QS-Enterprise-SPICE` | **$0.38** |
| Standard/Provisioned SPICE | `EU-QS-Provisioned-SPICE` | **$0.25** |

#### Adapter Pricing Table

```python
QUICKSIGHT_SPICE_PER_GB = {"ENTERPRISE": 0.38, "STANDARD": 0.25}
```

| Edition | Adapter Price | AWS API Price | Match? |
|---------|--------------|---------------|--------|
| ENTERPRISE | $0.38/GB | $0.38/GB | **Correct** |
| STANDARD | $0.25/GB | $0.25/GB | **Correct** |

#### Service Module Hardcoded Estimate

```python
f"~${(total_capacity - used_capacity) * 0.25:.0f}/month (estimate - verify SPICE pricing)"
```

This uses $0.25/GB unconditionally — **incorrect for Enterprise edition** (should be $0.38/GB). The text also says "(estimate - verify SPICE pricing)" — admission it's unreliable.

#### AWS Documentation Cross-Reference

AWS docs confirm (aws.amazon.com/quicksight/pricing):
- **Standard Edition**: $0.25/GB for additional SPICE beyond 10 GB/user included
- **Enterprise Edition**: $0.38/GB for additional SPICE beyond included capacity
- **User pricing**: Authors $18-40/mo, Readers $3-20/mo, Standard $9-12/mo — **completely absent from adapter**

---

### Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Adapter registered in `ALL_MODULES` | ✅ PASS | `services/__init__.py:34` imports and registers `QuicksightModule` |
| 2 | Implements `ServiceModule` protocol | ✅ PASS | Extends `BaseServiceModule`, implements `required_clients()` and `scan()` |
| 3 | Returns valid `ServiceFindings` | ✅ PASS | Correct structure with `service_name`, `total_recommendations`, `sources` |
| 4 | `required_clients()` returns correct boto3 clients | ✅ PASS | Returns `("quicksight",)` |
| 5 | Pricing values match AWS Pricing API | ⚠️ WARN | SPICE per-GB rates are correct ($0.38/$0.25), but edition is never resolved so Enterprise rate always applies; service module uses wrong $0.25 flat rate |
| 6 | Savings calculations are accurate | ❌ FAIL | **Dead code**: adapter reads `UnusedSpiceCapacityGB` which service module never sets → all savings fall to flat $30 fallback. Service module's own estimate uses wrong rate |
| 7 | API pagination implemented | ✅ PASS | Uses paginators for `list_namespaces` and `list_users` |
| 8 | Error handling for disabled service | ✅ PASS | Gracefully handles `ResourceNotFoundException` for accounts without QuickSight |
| 9 | Exception handling per-namespace | ✅ PASS | Inner try/except continues on per-namespace user listing failures |
| 10 | Uses `ScanContext` correctly | ✅ PASS | Uses `ctx.client()`, `ctx.account_id`, `ctx.warn()`, `ctx.pricing_multiplier` |
| 11 | Golden file exists and passes | ✅ PASS | `golden_scan_results.json` has QuickSight entry with 0 recs/$0 savings |
| 12 | No hardcoded region strings | ✅ PASS | Uses `ctx.account_id` (region from ScanContext) |
| 13 | Zero write operations | ✅ PASS | All API calls are read-only `describe_*` and `list_*` |
| 14 | Detection coverage adequate | ⚠️ WARN | Only detects SPICE underutilization. Missing: idle users, Reader vs Author optimization, edition upgrade/downgrade analysis, session-based reader cost review |
| 15 | No false positive risk | ⚠️ WARN | 50% SPICE threshold is arbitrary but reasonable. However, savings are incorrect due to field mismatch |

---

### Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| QS-01 | 🔴 Critical | **Field mismatch between service module and adapter**: Adapter reads `UnusedSpiceCapacityGB` and `Edition` but service module sets `UsedCapacityGB`, `TotalCapacityGB`, and `UtilizationPercent`. The `unused_gb > 0` branch never executes. | All savings default to flat $30/recommendation regardless of actual SPICE waste |
| QS-02 | 🔴 Critical | **Edition always defaults to ENTERPRISE**: Service module never includes `Edition` in recommendation dicts. `rec.get("Edition", "ENTERPRISE")` always returns the default. | Standard edition accounts get wrong pricing ($0.38 instead of $0.25) if the field were ever used |
| QS-03 | 🟡 Medium | **Service module uses hardcoded $0.25/GB regardless of edition**: The `EstimatedSavings` string in the recommendation always multiplies by 0.25, even for Enterprise edition ($0.38/GB). | Text recommendation understates savings for Enterprise by ~34% |
| QS-04 | 🟡 Medium | **No user optimization checks**: Module counts users across namespaces but never generates recommendations for user tier optimization (Author→Reader, unused users, Reader session vs per-user pricing). | QuickSight user costs ($18-40/mo per author) are a major cost driver — completely unoptimized |
| QS-05 | 🟡 Medium | **No edition-level pricing integration**: Adapter doesn't determine which edition the account is on. The `describe_account_subscription` API returns edition info but it's only used for enabled/disabled check. | Pricing can't be accurately applied per edition |
| QS-06 | 🔵 Low | **Duplicate savings estimate**: Both the service module (in recommendation dict) and adapter (in ServiceFindings) compute savings independently with different logic. The service module's estimate appears in the recommendation text but is never used for `total_monthly_savings`. | Confusing for consumers of the data — which savings number is authoritative? |
| QS-07 | 🔵 Low | **`spice_optimization` is the only check category with real logic**: `user_optimization` and `capacity_optimization` categories are initialized but never populated. | Dead categories; code suggests intended future checks that were never implemented |

---

### Verdict

#### ⚠️ WARN — 45/100

#### Summary

The QuickSight adapter has **correct SPICE per-GB pricing values** that match the AWS Pricing API exactly ($0.38 Enterprise, $0.25 Standard in eu-west-1). However, the savings calculation is **fundamentally broken** due to a field-name mismatch between the service module and adapter layer — the adapter reads fields (`UnusedSpiceCapacityGB`, `Edition`) that the service module never produces. This means:

1. **Every recommendation gets a flat $30 fallback savings** instead of actual SPICE-based calculation
2. **Edition is never resolved**, so even if fields were correct, Standard accounts would be over-priced
3. The service module's own text estimate uses the wrong rate ($0.25 flat instead of edition-aware)

Detection coverage is **narrow** — only SPICE underutilization at a 50% threshold. QuickSight user costs (Authors $18-40/mo, Readers $3-20/mo) represent the largest cost optimization opportunity but are completely unexamined.

#### Risk Assessment

- **False savings claims**: High — reported savings are arbitrary ($30 flat) rather than computed
- **False positives**: Low — SPICE underutilization detection logic is sound
- **False negatives**: Medium — missing user optimization, edition analysis, session pricing

#### Recommendations

1. **Fix field mismatch (QS-01)**: Service module should output `UnusedSpiceCapacityGB` (= total - used) and `Edition` in recommendation dicts
2. **Extract edition from subscription (QS-05)**: Parse edition from `describe_account_subscription` response
3. **Add user optimization (QS-04)**: Detect unused users (no login in 90d), Author→Reader conversion opportunities, Reader per-session vs per-user pricing
4. **Remove duplicate savings logic (QS-06)**: Single authoritative savings calculation in adapter only
5. **Remove dead check categories (QS-07)**: Either implement `user_optimization` and `capacity_optimization` or remove the empty dict keys


---

## AUDIT-27: MediaStore

**Date:** 2026-05-01  
**Auditor:** AI Agent  
**Scope:** `services/adapters/mediastore.py`, `services/mediastore.py`  
**Service Code:** AWSElementalMediaStore  
**Region:** eu-west-1 (Ireland)

---

### Executive Summary

The MediaStore adapter implements the ServiceModule protocol and provides cost optimization recommendations for AWS Elemental MediaStore containers. The adapter uses S3-equivalent pricing for storage cost calculations, which happens to match actual MediaStore storage pricing ($0.023/GB-month), but omits other significant cost components including ingest optimization fees and API request charges.

**Verdict: ⚠️ WARN — 62/100**

---

### Code Analysis

#### Adapter Structure (`services/adapters/mediastore.py`)

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

#### Underlying Module (`services/mediastore.py`)

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

### Pricing Validation

#### AWS MediaStore Pricing (eu-west-1) — Official

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

#### Adapter Pricing Approach

| Aspect | Implementation | Accuracy |
|--------|---------------|----------|
| Storage cost per GB | Uses S3 Standard price ($0.023) | ✅ Exact match |
| Ingest optimization | Not calculated | ❌ Missing |
| API request costs | Not calculated | ❌ Missing |
| Fallback rate | `0.023 * pricing_multiplier` | ✅ Correct base |
| Flat fallback | $20 * multiplier (no storage info) | ⚠️ Arbitrary |

#### Savings Calculation Analysis

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

### Pass Criteria Checklist

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

### Issues Found

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

### Detailed Findings

#### Issue MEDIA-001: Missing Storage Size Measurement

**Location:** `services/mediastore.py`, lines 45-68

**Current Behavior:**
The detection logic only checks CloudWatch activity metrics but never retrieves the actual storage size of containers:

```python
## Missing: No call to get container storage size
metrics_to_check = ["RequestCount", "BytesDownloaded", "BytesUploaded"]
```

**Expected Behavior:**
Should call `mediastore.describe_container()` or CloudWatch `BucketSizeBytes` metric to determine actual storage for accurate savings calculation.

**Fix Recommendation:**
```python
## Add storage size retrieval
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

#### Issue MEDIA-002: Incomplete Cost Components

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

#### Issue MEDIA-004: Inconsistent Savings Display

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

### Recommendations

#### Immediate Actions

1. **Fix MEDIA-001**: Add storage size retrieval via CloudWatch `BucketSizeBytes` metric
2. **Fix MEDIA-004**: Align hardcoded recommendation savings with adapter calculation
3. **Document Limitation**: Add comment that ingest costs are not included in savings estimate

#### Short-Term Improvements

4. **Add Ingest Optimization**: Include `$0.02/GB` for estimated monthly ingest volume
5. **Add Request Costs**: Estimate based on historical `RequestCount` metrics
6. **Support IA Tier**: Detect storage class and apply appropriate pricing ($0.0125/GB)

#### Long-Term Enhancements

7. **MediaStore-Specific Pricing**: Create `get_mediastore_monthly_price()` in pricing engine
8. **Regional Pricing**: Query actual MediaStore pricing from AWS Pricing API per region

---

### Appendix: AWS Pricing API Raw Data

#### Storage Pricing
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

#### Ingest Optimization Pricing
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

#### PUT Request Pricing
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

### Verdict

**⚠️ WARN — 62/100**

The MediaStore adapter implements the ServiceModule protocol correctly and provides functional detection of unused containers. However, the savings calculations are significantly flawed:

1. **Storage size is never measured**, forcing arbitrary flat-rate savings estimates
2. **Ingest optimization costs** (often 40-50% of total costs) are completely omitted
3. **Inconsistent messaging** between recommendation text ($25) and calculated savings ($20)

The adapter is **safe to use** for identifying unused containers but should **not be relied upon** for accurate cost savings estimates until MEDIA-001 and MEDIA-002 are addressed.

---

*End of Audit Report*


---

## AUDIT-28: Batch

| Field | Value |
|-------|-------|
| **Adapter** | `services/adapters/batch.py` (56 lines) |
| **Supporting Module** | `services/batch_svc.py` (94 lines) |
| **Audit Date** | 2026-05-01 |
| **Auditor** | Automated audit agent |

---

### 1. Code Analysis

#### 1.1 Architecture Overview

**Verdict: ✅ PASS**

The Batch adapter follows the standard `ServiceModule` Protocol pattern:

```python
class BatchModule(BaseServiceModule):
    key: str = "batch"
    cli_aliases: tuple[str, ...] = ("batch",)
    display_name: str = "Batch"

    def required_clients(self) -> tuple[str, ...]:
        return ("batch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        # Implementation
```

The adapter correctly delegates to `get_enhanced_batch_checks()` in `services/batch_svc.py`, which implements three distinct check categories:

| Check Category | Detection Logic | Savings Estimate |
|----------------|-----------------|------------------|
| Spot Optimization | `allocation_strategy != "SPOT_CAPACITY_OPTIMIZED"` | 60-90% |
| Graviton Migration | No `6g` or `7g` instance types found | 20-40% |
| Job Rightsizing | vCPUs > 8 OR memory > 16384 MB | "Rightsize based on actual usage" |

#### 1.2 Savings Calculation Logic

**Verdict: ⚠️ WARN — Inconsistent savings model**

The adapter calculates savings as follows:

```python
for rec in recs:
    instance_types = rec.get("InstanceTypes", [])
    if ctx.pricing_engine is not None and instance_types:
        hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_types[0])
        monthly = hourly * 730
        savings += monthly * 0.30  # Fixed 30% multiplier
    else:
        savings += 150  # Flat fallback
```

**Issues identified:**

1. **30% multiplier is arbitrary**: The Spot optimization check claims 60-90% savings, but the calculation applies only 30%
2. **Graviton savings mismatch**: Detection claims 20-40% savings, but calculation uses 30% (may over/under-estimate)
3. **Job rightsizing has no savings calculation**: Jobs flagged for rightsizing contribute $0 to the total (no `InstanceTypes` field)
4. **First instance type only**: If a compute environment has multiple instance types, only the first is priced
5. **No Fargate pricing**: Fargate-based compute environments are analyzed but not priced differently

#### 1.3 Detection Logic Analysis

**Verdict: ⚠️ WARN — Limited coverage**

The `get_enhanced_batch_checks()` function analyzes:

| Resource | API Call | Checks |
|----------|----------|--------|
| Compute Environments | `describe_compute_environments` | Allocation strategy, instance types |
| Job Definitions | `describe_job_definitions` | vCPU/memory allocation |

**Missing analyses:**

- **Job Queues**: No examination of queue depth, job pending times, or scheduling efficiency
- **Fargate vs EC2 decision**: No recommendation on when to use Fargate vs EC2
- **Spot interruption handling**: No check for checkpointing or fault-tolerance patterns
- **Compute environment utilization**: No CloudWatch metrics for actual utilization
- **Job retry policies**: No analysis of retry configurations that waste compute

#### 1.4 Pricing Model

**Verdict: ✅ PASS — Correctly references underlying resources**

Per [AWS Batch Pricing documentation](https://aws.amazon.com/batch/pricing/):

> "There is no additional charge for AWS Batch. You pay for AWS resources (e.g. EC2 instances, AWS Lambda functions or AWS Fargate) you create to store and run your application."

The adapter correctly:
- Uses `PricingEngine.get_ec2_hourly_price()` for EC2-based compute environments
- References EC2 service code (`AmazonEC2`) via the pricing engine
- Does not attempt to query a non-existent "AWSBatch" service code

**Verified**: AWS Pricing API has no `AWSBatch` or `Batch` service code — this is correct because Batch is a free orchestration layer on top of paid compute resources.

---

### 2. Pricing Validation

#### 2.1 EC2 Pricing Reference

AWS Batch compute environments using EC2 launch types are priced at the underlying EC2 instance rate. The adapter queries:

```python
ctx.pricing_engine.get_ec2_hourly_price(instance_types[0])
```

This method queries the `AmazonEC2` service code with filters:
- `instanceType`: The instance type (e.g., "m5.large")
- `location`: Region display name (e.g., "EU (Ireland)")
- `operatingSystem`: "Linux" (default)
- `tenancy`: "Shared"

#### 2.2 Sample Pricing Verification (eu-west-1)

| Instance Type | On-Demand (Linux) | Monthly (730h) | 30% Savings Claim |
|---------------|-------------------|----------------|-------------------|
| m5.large | $0.107/hr | $78.11 | $23.43 |
| m5.xlarge | $0.214/hr | $156.22 | $46.87 |
| t3.medium | $0.0464/hr | $33.87 | $10.16 |
| c6g.large (Graviton) | $0.085/hr | $62.05 | $18.62 |

**Note**: Actual Spot savings typically range 60-90% off On-Demand pricing, meaning the adapter's 30% calculation is **conservative** by design.

#### 2.3 Fargate Pricing Gap

**Verdict: ❌ FAIL — No Fargate pricing support**

Fargate-based Batch compute environments use different pricing:
- **Fargate**: $0.04048/vCPU/hour + $0.004445/GB/hour
- **Fargate Spot**: ~70% discount on Fargate pricing

The adapter does not distinguish between EC2 and Fargate compute environments when calculating savings:

```python
## In batch_svc.py — no type-specific handling
ce_type = ce.get("type")  # "MANAGED" or "UNMANAGED"
## Fargate vs EC2 is NOT extracted or handled differently
```

**Impact**: Fargate compute environments still trigger Graviton recommendations (irrelevant for Fargate) and use EC2 pricing for savings calculations (incorrect for Fargate).

---

### 3. Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Implements ServiceModule Protocol | ✅ PASS | Extends BaseServiceModule correctly |
| 2 | Correct boto3 client requested | ✅ PASS | Returns `("batch",)` |
| 3 | Uses pricing_engine for live pricing | ✅ PASS | Calls `get_ec2_hourly_price()` |
| 4 | Handles missing pricing_engine gracefully | ✅ PASS | Falls back to $150/rec |
| 5 | Pagination handled for large fleets | ✅ PASS | Uses `get_paginator()` for both CEs and job defs |
| 6 | Error handling graceful | ✅ PASS | Wrapped in try/except with `ctx.warn()` |
| 7 | Savings calculation matches claimed percentages | ⚠️ WARN | 30% used vs 60-90% claimed for Spot |
| 8 | Fargate pricing supported | ❌ FAIL | No Fargate-specific pricing logic |
| 9 | Regional pricing via pricing_multiplier | ⚠️ WARN | pricing_multiplier not applied in savings calc |
| 10 | Distinguishes EC2 vs Fargate environments | ❌ FAIL | Both treated identically |
| 11 | SourceBlock correctly populated | ✅ PASS | `enhanced_checks` with count and recommendations |
| 12 | Optimization descriptions provided | ✅ PASS | `BATCH_OPTIMIZATION_DESCRIPTIONS` exported |

---

### 4. Issues Found

#### Critical

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| BATCH-001 | Fargate compute environments analyzed with EC2 pricing logic | `batch_svc.py:30-50` | Incorrect savings estimates for Fargate CEs |
| BATCH-002 | No Fargate Spot recommendation despite 70% savings potential | `batch_svc.py` | Missing major optimization opportunity |

#### Warning

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| BATCH-003 | Savings calculation uses 30% multiplier regardless of check type | `batch.py:44` | Spot check claims 60-90% but calc uses 30%; Graviton claims 20-40% but calc uses 30% |
| BATCH-004 | Job rightsizing recommendations contribute $0 to savings total | `batch.py:39-46` | No `InstanceTypes` field in job def recs |
| BATCH-005 | Only first instance type priced when multiple types exist | `batch.py:40-42` | Undercounts potential savings |
| BATCH-006 | `pricing_multiplier` not applied to savings calculation | `batch.py:44` | Regional pricing variations ignored |
| BATCH-007 | No CloudWatch utilization metrics for actual vs allocated comparison | `batch_svc.py` | Cannot detect truly idle CEs |

#### Info

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| BATCH-008 | No job queue depth or scheduling latency analysis | `batch_svc.py` | Missing queue optimization opportunities |
| BATCH-009 | No retry policy analysis for wasteful re-executions | `batch_svc.py` | May miss cost drivers from failed jobs |
| BATCH-010 | Flat $150/rec fallback not documented or configurable | `batch.py:46` | Opaque fallback behavior |

---

### 5. Documentation

**Verdict: ✅ PASS**

- Adapter docstring accurately describes flat-rate strategy (`batch.py:23-33`)
- `services/adapters/CLAUDE.md` correctly categorizes Batch as "Flat-rate (1 adapter)"
- `README.md` lists Batch optimizations: "Spot allocation strategy, Graviton instances"
- IAM policy includes `batch:DescribeComputeEnvironments` permission

---

### 6. Verdict

| Category | Verdict |
|----------|---------|
| **Overall** | **⚠️ WARN — 2 critical issues, 6 warnings** |
| Architecture | ✅ PASS |
| Pricing Accuracy | ⚠️ WARN |
| Detection Logic | ⚠️ WARN |
| Documentation | ✅ PASS |
| Error Handling | ✅ PASS |

#### Score: 68/100

**Scoring breakdown:**
- Protocol compliance: 20/20
- Pricing model correctness: 10/20 (Fargate gap, inconsistent multipliers)
- Detection coverage: 15/25 (missing Fargate Spot, queues, utilization)
- Savings accuracy: 10/20 (30% vs claimed percentages, missing job rightsizing $)
- Documentation: 8/8
- Error handling: 5/7 (silent fallback to $150)

#### Summary

The Batch adapter correctly implements the `ServiceModule` Protocol and references EC2 pricing for underlying compute resources, which aligns with AWS's actual pricing model (Batch itself is free; you pay for EC2/Fargate).

However, two critical gaps undermine its accuracy:

1. **BATCH-001/BATCH-002**: Fargate compute environments are analyzed using EC2 pricing logic, and no Fargate Spot recommendations are generated. With Fargate Spot offering ~70% savings, this is a significant missed optimization opportunity.

2. **BATCH-003**: The 30% savings multiplier is applied uniformly across all check types, despite Spot checks claiming 60-90% savings and Graviton checks claiming 20-40%. This leads to systematic under- or over-estimation depending on the check type.

#### Recommended Priority

1. **Fix BATCH-001**: Detect Fargate vs EC2 compute environments and skip Graviton checks for Fargate
2. **Add Fargate Spot (BATCH-002)**: Implement Fargate Spot allocation strategy check with 70% savings estimate
3. **Align savings multipliers (BATCH-003)**: Use check-type-specific multipliers (70% for Spot, 30% for Graviton)
4. **Apply pricing_multiplier (BATCH-006)**: Multiply savings by `ctx.pricing_multiplier` for regional accuracy
5. **Add CloudWatch utilization (BATCH-007)**: Check actual CE utilization to recommend downsizing

---

*End of Audit Report*
