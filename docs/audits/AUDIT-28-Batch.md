# AUDIT-28: Batch Service Adapter

| Field | Value |
|-------|-------|
| **Adapter** | `services/adapters/batch.py` (56 lines) |
| **Supporting Module** | `services/batch_svc.py` (94 lines) |
| **Audit Date** | 2026-05-01 |
| **Auditor** | Automated audit agent |

---

## 1. Code Analysis

### 1.1 Architecture Overview

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

### 1.2 Savings Calculation Logic

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

### 1.3 Detection Logic Analysis

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

### 1.4 Pricing Model

**Verdict: ✅ PASS — Correctly references underlying resources**

Per [AWS Batch Pricing documentation](https://aws.amazon.com/batch/pricing/):

> "There is no additional charge for AWS Batch. You pay for AWS resources (e.g. EC2 instances, AWS Lambda functions or AWS Fargate) you create to store and run your application."

The adapter correctly:
- Uses `PricingEngine.get_ec2_hourly_price()` for EC2-based compute environments
- References EC2 service code (`AmazonEC2`) via the pricing engine
- Does not attempt to query a non-existent "AWSBatch" service code

**Verified**: AWS Pricing API has no `AWSBatch` or `Batch` service code — this is correct because Batch is a free orchestration layer on top of paid compute resources.

---

## 2. Pricing Validation

### 2.1 EC2 Pricing Reference

AWS Batch compute environments using EC2 launch types are priced at the underlying EC2 instance rate. The adapter queries:

```python
ctx.pricing_engine.get_ec2_hourly_price(instance_types[0])
```

This method queries the `AmazonEC2` service code with filters:
- `instanceType`: The instance type (e.g., "m5.large")
- `location`: Region display name (e.g., "EU (Ireland)")
- `operatingSystem`: "Linux" (default)
- `tenancy`: "Shared"

### 2.2 Sample Pricing Verification (eu-west-1)

| Instance Type | On-Demand (Linux) | Monthly (730h) | 30% Savings Claim |
|---------------|-------------------|----------------|-------------------|
| m5.large | $0.107/hr | $78.11 | $23.43 |
| m5.xlarge | $0.214/hr | $156.22 | $46.87 |
| t3.medium | $0.0464/hr | $33.87 | $10.16 |
| c6g.large (Graviton) | $0.085/hr | $62.05 | $18.62 |

**Note**: Actual Spot savings typically range 60-90% off On-Demand pricing, meaning the adapter's 30% calculation is **conservative** by design.

### 2.3 Fargate Pricing Gap

**Verdict: ❌ FAIL — No Fargate pricing support**

Fargate-based Batch compute environments use different pricing:
- **Fargate**: $0.04048/vCPU/hour + $0.004445/GB/hour
- **Fargate Spot**: ~70% discount on Fargate pricing

The adapter does not distinguish between EC2 and Fargate compute environments when calculating savings:

```python
# In batch_svc.py — no type-specific handling
ce_type = ce.get("type")  # "MANAGED" or "UNMANAGED"
# Fargate vs EC2 is NOT extracted or handled differently
```

**Impact**: Fargate compute environments still trigger Graviton recommendations (irrelevant for Fargate) and use EC2 pricing for savings calculations (incorrect for Fargate).

---

## 3. Pass Criteria Checklist

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

## 4. Issues Found

### Critical

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| BATCH-001 | Fargate compute environments analyzed with EC2 pricing logic | `batch_svc.py:30-50` | Incorrect savings estimates for Fargate CEs |
| BATCH-002 | No Fargate Spot recommendation despite 70% savings potential | `batch_svc.py` | Missing major optimization opportunity |

### Warning

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| BATCH-003 | Savings calculation uses 30% multiplier regardless of check type | `batch.py:44` | Spot check claims 60-90% but calc uses 30%; Graviton claims 20-40% but calc uses 30% |
| BATCH-004 | Job rightsizing recommendations contribute $0 to savings total | `batch.py:39-46` | No `InstanceTypes` field in job def recs |
| BATCH-005 | Only first instance type priced when multiple types exist | `batch.py:40-42` | Undercounts potential savings |
| BATCH-006 | `pricing_multiplier` not applied to savings calculation | `batch.py:44` | Regional pricing variations ignored |
| BATCH-007 | No CloudWatch utilization metrics for actual vs allocated comparison | `batch_svc.py` | Cannot detect truly idle CEs |

### Info

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| BATCH-008 | No job queue depth or scheduling latency analysis | `batch_svc.py` | Missing queue optimization opportunities |
| BATCH-009 | No retry policy analysis for wasteful re-executions | `batch_svc.py` | May miss cost drivers from failed jobs |
| BATCH-010 | Flat $150/rec fallback not documented or configurable | `batch.py:46` | Opaque fallback behavior |

---

## 5. Documentation

**Verdict: ✅ PASS**

- Adapter docstring accurately describes flat-rate strategy (`batch.py:23-33`)
- `services/adapters/CLAUDE.md` correctly categorizes Batch as "Flat-rate (1 adapter)"
- `README.md` lists Batch optimizations: "Spot allocation strategy, Graviton instances"
- IAM policy includes `batch:DescribeComputeEnvironments` permission

---

## 6. Verdict

| Category | Verdict |
|----------|---------|
| **Overall** | **⚠️ WARN — 2 critical issues, 6 warnings** |
| Architecture | ✅ PASS |
| Pricing Accuracy | ⚠️ WARN |
| Detection Logic | ⚠️ WARN |
| Documentation | ✅ PASS |
| Error Handling | ✅ PASS |

### Score: 68/100

**Scoring breakdown:**
- Protocol compliance: 20/20
- Pricing model correctness: 10/20 (Fargate gap, inconsistent multipliers)
- Detection coverage: 15/25 (missing Fargate Spot, queues, utilization)
- Savings accuracy: 10/20 (30% vs claimed percentages, missing job rightsizing $)
- Documentation: 8/8
- Error handling: 5/7 (silent fallback to $150)

### Summary

The Batch adapter correctly implements the `ServiceModule` Protocol and references EC2 pricing for underlying compute resources, which aligns with AWS's actual pricing model (Batch itself is free; you pay for EC2/Fargate).

However, two critical gaps undermine its accuracy:

1. **BATCH-001/BATCH-002**: Fargate compute environments are analyzed using EC2 pricing logic, and no Fargate Spot recommendations are generated. With Fargate Spot offering ~70% savings, this is a significant missed optimization opportunity.

2. **BATCH-003**: The 30% savings multiplier is applied uniformly across all check types, despite Spot checks claiming 60-90% savings and Graviton checks claiming 20-40%. This leads to systematic under- or over-estimation depending on the check type.

### Recommended Priority

1. **Fix BATCH-001**: Detect Fargate vs EC2 compute environments and skip Graviton checks for Fargate
2. **Add Fargate Spot (BATCH-002)**: Implement Fargate Spot allocation strategy check with 70% savings estimate
3. **Align savings multipliers (BATCH-003)**: Use check-type-specific multipliers (70% for Spot, 30% for Graviton)
4. **Apply pricing_multiplier (BATCH-006)**: Multiply savings by `ctx.pricing_multiplier` for regional accuracy
5. **Add CloudWatch utilization (BATCH-007)**: Check actual CE utilization to recommend downsizing

---

*End of Audit Report*
