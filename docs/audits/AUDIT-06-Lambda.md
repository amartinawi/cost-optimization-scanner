# AUDIT-06: Lambda Service Adapter

| Field | Value |
|-------|-------|
| **Date** | 2026-05-01 |
| **Adapter** | `services/adapters/lambda_svc.py` (63 lines) |
| **Legacy shim** | `services/lambda_svc.py` (241 lines) |
| **Auditor** | Automated audit agent |
| **Overall Verdict** | **FAIL** — 5 findings (2 critical, 2 major, 1 minor) |

---

## 1. Cost Hub Savings Pass-Through

**Verdict: [V-1] PASS — No pricing_multiplier applied to Cost Hub estimates**

The adapter sums Cost Hub `estimatedMonthlySavings` directly (line 42):

```python
savings += sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)
```

This is correct. Cost Optimization Hub returns region-specific dollar amounts. The adapter does **not** multiply by `ctx.pricing_multiplier`. No code path applies `pricing_multiplier` to Cost Hub data anywhere in this adapter.

---

## 2. Flat-Rate Savings — Arbitrary and Undocumented

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

### Problems

| Issue | Detail |
|-------|--------|
| **No source documentation** | The $50/$10/$5 values have no comment, no doc reference, and no link to AWS pricing. They appear to be arbitrary guesses. |
| **Keyword fragility** | Matching relies on exact substrings (`"Up to 90%"`, `"memory optimization"`, `"Eliminate unused costs"`). Any change in the legacy shim's `EstimatedSavings` text silently drops savings to $0. |
| **No function-level pricing** | A 128MB Lambda function and a 10,240MB function get the same flat rate. AWS Lambda pricing is $0.0000166667/GB-second + $0.20/million requests — savings scale with memory size and invocation count. |
| **Wrong for provisioned concurrency** | "Up to 90%" maps to $50, but actual provisioned concurrency costs vary wildly (a function with 100 provisioned concurrent executions at 1GB could cost $3,500/month). |

### What the legacy shim emits

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

## 3. Lambda Pricing Model — No Live Pricing Used

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

## 4. Double-Counting Analysis

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

## 5. Missing Savings Categories

**Verdict: [V-5] MINOR — ARM migration and VPC cost savings not quantified**

The ARM migration check (line 198-232 in legacy shim) correctly gates on:
- x86_64 architecture
- ARM-compatible runtime
- Minimum 10 weekly invocations

But the adapter assigns $0 savings because `"20% cost reduction with ARM architecture"` doesn't match any keyword. ARM/Graviton provides a documented **20% price-performance improvement** per AWS. The VPC check similarly produces $0.

---

## Verdict Summary

| ID | Finding | Severity | Verdict Code |
|----|---------|----------|-------------|
| V-1 | Cost Hub savings pass-through | — | **PASS** |
| V-2 | Flat rates are undocumented magic numbers | Critical | **FAIL** |
| V-3 | No live Lambda pricing API used | Critical | **FAIL** |
| V-4 | No dedup between Cost Hub and enhanced checks | Major | **FAIL** |
| V-5 | ARM/VPC savings not quantified | Minor | **WARN** |

---

## Recommendations

### R-1: Replace keyword matching with function-level savings calculation
Use `ctx.pricing_engine` (needs a new Lambda method) or inline Lambda pricing constants:

```python
LAMBDA_PRICE_PER_GB_SEC = 0.0000166667  # x86 on-demand
LAMBDA_ARM_PRICE_PER_GB_SEC = 0.0000133334
LAMBDA_PRICE_PER_1K_REQUESTS = 0.0000002
```

Then calculate per-function savings based on actual memory size, invocation count, and duration.

### R-2: Add deduplication by FunctionName
Before summing, build a set of function names from Cost Hub recommendations and exclude matching functions from enhanced checks (or vice versa):

```python
cost_hub_fns = {rec.get("resourceArn", "").split(":")[-1] for rec in cost_hub_recs}
for rec in enhanced_recs:
    if rec.get("FunctionName") not in cost_hub_fns:
        # count and calculate savings
```

### R-3: Add PricingEngine method for Lambda
Add `get_lambda_duration_price_per_gb_sec(architecture)` and `get_lambda_request_price_per_1m()` to `core/pricing_engine.py`, then use them in both the adapter and legacy shim.

### R-4: Document flat-rate origins
If flat rates must remain as fallback, add comments citing the source (e.g., "Based on median Lambda function savings observed in AWS Cost Explorer for us-east-1, 2025").

---

## File Inventory

| File | Role | Lines |
|------|------|-------|
| `services/adapters/lambda_svc.py` | ServiceModule adapter | 63 |
| `services/lambda_svc.py` | Legacy analysis logic | 241 |
| `services/advisor.py` | Cost Optimization Hub fetcher | 153 |
| `core/scan_context.py` | ScanContext with pricing_multiplier | 72 |
| `core/pricing_engine.py` | No Lambda method exists | 517 |

---

## AWS Documentation References

- [Lambda cost and performance optimization (Serverless Lens)](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/cost-and-performance-optimization.html) — recommends AWS Compute Optimizer for memory rightsizing
- [Lambda pricing](https://aws.amazon.com/lambda/pricing/) — $0.0000166667/GB-sec (x86), $0.0000133334/GB-sec (arm64), $0.20/1M requests
- [Lambda Power Tuning](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/net-serverless.html) — recommended tool for finding optimal memory configuration
