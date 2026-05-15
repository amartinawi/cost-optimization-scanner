# Audit: sagemaker Adapter

**Adapter**: `services/adapters/sagemaker.py` (447 lines — self-contained)
**ALL_MODULES index**: 31
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 (1 CRITICAL, 1 MEDIUM) |
| L2 Calculation | **FAIL** | 5 (1 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | CW Period invalid (idle-endpoint check unreachable) + 1.15× SageMaker markup × pricing_multiplier double-multiply on live path + 30% consolidation factor invented |

---

## Pre-flight facts

- **Required clients**: `("sagemaker", "cloudwatch")` ✓
- **Flags**: `requires_cloudwatch=True` ✓, `reads_fast_mode=True` ✓
- **Sources**: 4 (`idle_endpoints`, `idle_notebooks`, `spot_training`, `multi_model_consolidation`)
- **Pricing**: live `get_sagemaker_instance_monthly` × `1.15 markup` × pricing_multiplier

---

## L1 — Technical findings

### L1-001 CloudWatch `Period = days * 86400` invalid  [CRITICAL]
- **Evidence**: line 49 — `period = days * 86400 = 7 * 86400 = 604800`. CW `get_metric_statistics` max Period is 86400; values above silently fail
- **Why it matters**: `_get_cloudwatch_invocations_sum` always returns `None` → `_check_idle_endpoints` never emits a rec (line 122-124 short-circuits when invocations None)
- **Recommended fix**: same as Aurora L1-002 — set `Period=86400`, aggregate datapoints in code

### L1-002 Adapter banner `print()` + 12+ silent `except: pass`  [MEDIUM]
- Lines 41, 63, 75, 79, 88, 96, 156, 178, 204, 229, 244, 255, 287, 322 — silent error swallow throughout
- **Recommended fix**: route through `ctx.warn` / `ctx.permission_issue`

---

## L2 — Calculation findings

### Source-classification table

| Recommendation | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Idle endpoint | `live × 1.15 markup × pricing_multiplier` | line 40, 142 | **CRITICAL FAIL** — double-multiplier on live path; 1.15 SageMaker markup is undocumented |
| Idle notebook | same path | line 186-190 | same FAIL |
| Spot training | `derived` (`on_demand_cost × SPOT_SAVINGS_RATE=0.70 × multiplier`) | line 268 | **WARN** — 70% midpoint OK; but `on_demand_cost = (live_monthly × 1.15)/730 × hours × multiplier` → triple-pricing through |
| Multi-model consolidation | `derived` (`(N-1) × instance_monthly × 0.30 × multiplier`) | line 334 | **FAIL** — 30% factor invented; multi-model consolidation savings vary widely |

### L2-001 Live × 1.15 × `pricing_multiplier` triple-pricing  [CRITICAL]
- **Evidence**:
  - line 38-40 — `price = get_sagemaker_instance_monthly(...)` (live) then `× SAGEMAKER_INSTANCE_MULTIPLIER (1.15)`
  - line 142 — `monthly_savings = instance_monthly * pricing_multiplier`
- The `× 1.15` is a SageMaker-vs-EC2 markup constant (SageMaker charges ~10-15% premium for managed ML platform). If `get_sagemaker_instance_monthly` queries `AmazonSageMaker` service code, it already includes this premium — **don't re-multiply**.
- AND the live result × `pricing_multiplier` is double-multiplier per L2.3.1
- **Why it matters**: every SageMaker savings is `1.15 × regional_multiplier ×` actual. In us-east-1 (multiplier 1.0): savings 1.15× inflated. In eu-west-1 (1.08): 1.242× inflated
- **Recommended fix**: drop `* SAGEMAKER_INSTANCE_MULTIPLIER` from `_get_instance_monthly`; drop `* pricing_multiplier` from idle-endpoint/notebook paths (live values already region-correct)

### L2-002 30% multi-model consolidation factor invented  [HIGH]
- **Evidence**: line 334 — `(len(group) - 1) × instance_monthly × CONSOLIDATION_SAVINGS_RATE × multiplier`. The 30% has no documented basis; SageMaker multi-model endpoints can save up to 100% of redundant instance hours
- **Recommended fix**: simulate actual consolidation — `(N - target_count) × instance_monthly` where `target_count` is computed from per-model invocation counts

### L2-003 70% spot savings + invented factors combine non-linearly  [HIGH]
- **Evidence**: line 268 — `savings = on_demand_cost × SPOT_SAVINGS_RATE × pricing_multiplier`. SageMaker Managed Spot Training actual savings 50-90% depending on job interruption risk
- **Status**: 70% is conservative midpoint, defensible; but combined with the 1.15 markup and double-multiplier, the final number is layered wrongness

### L2-004 `_get_instance_monthly` returns 0 on Pricing API miss → rec savings = 0  [MEDIUM]
- **Evidence**: lines 139-142 — if Pricing API returns 0, `monthly_savings = 0.0`. Adapter still emits the rec but with $0
- **Recommended fix**: skip rec entirely + `pricing_warning` field

### L2-005 `pricing_multiplier` applied on derived path (mixed)  [LOW]
- **Evidence**: line 268 — spot path is fundamentally `live × markup × pricing_multiplier`. Same issue as L2-001 cascades

---

## L3 — Reporting findings

**None** — strong `stat_cards`, `grouping`, descriptions.

---

## Recommended next steps

1. **[CRITICAL] L1-001** — Fix CW Period (≤86400, aggregate in code).
2. **[CRITICAL] L2-001** — Drop `× 1.15` markup; drop `× pricing_multiplier` on live paths. Same fix as elasticache/opensearch/dms/lambda etc.
3. **[HIGH] L2-002** — Replace 30% consolidation factor with simulated multi-model target count.
4. **[MEDIUM] L1-002** — Classify the 12+ silent except sites.
5. **[MEDIUM] L2-004** — Skip recs when Pricing API returns 0 + `pricing_warning`.
