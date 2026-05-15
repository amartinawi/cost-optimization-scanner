# Audit: containers Adapter

**Adapter**: `services/adapters/containers.py` (134 lines)
**Legacy shim**: `services/containers.py` (~700 lines)
**ALL_MODULES index**: 8
**Renderer path**: **Phase B** — `("containers", "enhanced_checks"): _render_containers_enhanced_checks`, `("containers", "cost_optimization_hub"): _render_cost_hub_source`, `("containers", "compute_optimizer"): _render_compute_optimizer_source`
**Date**: 2026-05-15
**Auditor**: automated

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **FAIL** | 5 (1 CRITICAL, 3 HIGH, 1 MEDIUM) |
| L2 Calculation | **FAIL** | 7 (1 CRITICAL, 4 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| **Overall**    | **FAIL** | required_clients lies about CW + string-token savings dispatch + 5 invented fallback constants |

---

## Pre-flight facts

- **Adapter class**: `ContainersModule` at `services/adapters/containers.py:13`
- **Declared `required_clients`**: `("ecs", "eks", "ecr", "compute-optimizer")` — **omits `cloudwatch`**. Shim uses CW heavily (lines 385, 478, 501, etc.)
- **Declared `requires_cloudwatch`**: `False` (default) — **lies**
- **Sources emitted**: `{"enhanced_checks", "cost_optimization_hub", "compute_optimizer"}`
- **Pricing constants**:
  - `FARGATE_VCPU_HOURLY = 0.04048` (function-local, ruff N806)
  - `FARGATE_MEM_GB_HOURLY = 0.004445`
  - 5 invented fallback constants: `150.0`, `75.0`, `25.0`, `100.0`, `60.0`
- **Test files**: `tests/test_offline_scan.py`, `tests/test_regression_snapshot.py`, `tests/test_reporter_snapshots.py`

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `cloudwatch`  [CRITICAL]
- **Check**: L1.1.4
- **Evidence**: `services/adapters/containers.py:22` returns `("ecs", "eks", "ecr", "compute-optimizer")`; shim uses `ctx.client("cloudwatch")` at `services/containers.py:385, 478, 501` plus `cloudwatch.get_metric_statistics` calls for ECS CPU/memory utilization (lines 389, 402) and EKS analysis (482, 505, 515)
- **Recommended fix**: change to `("ecs", "eks", "ecr", "compute-optimizer", "cloudwatch")`; set `requires_cloudwatch = True`

### L1-002 `requires_cloudwatch` flag is False but shim is CW-heavy  [HIGH]
- **Evidence**: default False inherited from `BaseServiceModule`
- **Recommended fix**: set `requires_cloudwatch: bool = True` on the class

### L1-003 Three `print()` calls — two in error paths instead of `ctx.warn`  [HIGH]
- **Evidence**:
  - `services/adapters/containers.py:37` — adapter banner
  - `services/adapters/containers.py:42` — `print(f"Warning: [containers] container services analysis failed: {e}")`
  - `services/adapters/containers.py:48` — `print(f"Warning: [containers] enhanced checks failed: {e}")`
- **Why it matters**: a top-level shim failure (likely AccessDenied at ECS list-clusters) silently degrades to "empty container_data" with `print` instead of `ctx.warn` / `ctx.permission_issue`. JSON `scan_warnings`/`permission_issues` stay empty
- **Recommended fix**: replace each `print` with `ctx.warn(..., service="containers")` and classify AccessDenied → `ctx.permission_issue`

### L1-004 Adapter uses string-token matching against `EstimatedSavings` and `Recommendation` to dispatch savings math  [HIGH]
- **Check**: L1.3.x — protocol contract; brittle string-matching for $ math
- **Evidence**: `services/adapters/containers.py:60-95`:
  ```python
  savings_str = rec.get("EstimatedSavings", "")
  recommendation = rec.get("Recommendation", "").lower()
  ...
  if "spot" in recommendation or "spot instances" in savings_str.lower():
      savings += monthly_ondemand * 0.70 if monthly_ondemand > 0 else 150.0 * ctx.pricing_multiplier
  elif "rightsizing" in savings_str.lower() ... :
      savings += monthly_ondemand * 0.30 ...
  ```
- **Why it matters**: a rec generator at `services/containers.py:553` emits `"EstimatedSavings": "30-60% cost reduction based on measured over-provisioning"` — contains "30-60" not "rightsizing"; routes to the `else:` branch (`monthly_ondemand × 0.30` or $60 flat). One typo in either side desyncs $ math from intent. The shim emits `CheckCategory` for every rec (e.g. `"ECS Container Insights Required"`) — that's the structured field to dispatch on
- **Recommended fix**: dispatch on `CheckCategory` with a dict; remove the `EstimatedSavings`-string token grep

### L1-005 Module-level / inline `print()` in shim & inconsistent paginators  [MEDIUM]
- **Evidence**: shim has its own banner print at module-load time (assumed pattern across all shims). `ecs.list_clusters`, `eks.list_clusters`, `ecr.describe_repositories` — need to verify paginator usage; only partially audited here
- **Recommended fix**: full shim review under follow-up

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| Fargate x86 vCPU rate | `module-const` `0.04048` | `services/adapters/containers.py:52` | YES — matches AWS list price | AWS Pricing API → SKU `8CESGAFWKAJ98PME` Fargate vCPU $0.0404800000/hr |
| Fargate x86 memory rate | `module-const` `0.004445` | `services/adapters/containers.py:53` | YES — matches | AWS Pricing API → SKU `PBZNQUSEXZUC34C9` Fargate Memory $0.0044450000/GB-hr |
| ARM Fargate rates | **not modeled** | TODO at line 54-55 | **WARN** — adapter doesn't honor `cpuArchitecture`; tasks on Graviton get x86-priced savings | AWS Pricing API → ARM vCPU $0.03238 (20% cheaper), ARM mem $0.00356 (20% cheaper) |
| Windows Fargate rates | **not modeled** | n/a | **WARN** — Windows Fargate is 15% more expensive for vCPU + Windows OS license fee | AWS Pricing API → Windows vCPU $0.04655 + $0.046 OS license/hr |
| Spot savings | `derived` (`monthly × 0.70`) | line 82-83 | **WARN** — 70% is mid-band of AWS's published 50-70% Spot savings; OK as a documented approximation **if** marked as such | AWS docs: ECS Spot capacity provider — "savings up to 70%" |
| Rightsizing savings | `derived` (`monthly × 0.30`) | line 84-89 | **FAIL** — 30% is arbitrary; the actual delta is `(over_provisioned − measured_usage)` which the shim already collects via CW (lines 389-402) but adapter ignores | n/a |
| Lifecycle savings | `arbitrary` (`$25/mo flat × multiplier`) | line 90-91 | **FAIL** — ECR lifecycle savings depend entirely on image-storage GB and lifecycle-policy aggressiveness; flat $25 has no AWS basis | n/a |
| Unused cluster savings | `derived` or `arbitrary` (`monthly_ondemand` or `$100`) | line 92-93 | **CONDITIONAL** — `monthly_ondemand` path is fine when Fargate task config is known; $100 fallback for clusters with no service is invented | n/a |
| Generic else branch | `derived` (`monthly × 0.30`) or `arbitrary` (`$60`) | line 94-95 | **FAIL** — same 30% issue as rightsizing |
| Cost Hub `estimatedMonthlySavings` | `aws-api` | line 102-104 | YES | AWS Cost Hub returns flat float at top level |
| Compute Optimizer ECS savings | `aws-api` (pre-normalized) | line 109-110, `services/advisor.py:_normalize_ecs_co_rec:276` | **WARN** — normalizer applies `pricing_multiplier` though AWS CO already returns region-priced values (see Lambda L2-003) | n/a |

### L2-001 Five invented fallback constants when `monthly_ondemand <= 0`  [HIGH]
- **Check**: L2.1 — `arbitrary` values forbidden
- **Evidence**: `services/adapters/containers.py:83, 89, 91, 93, 95` — the `if monthly_ondemand > 0 else <CONST>` pattern produces $150, $75, $25, $100, $60 (× multiplier) for recs missing Cpu/Memory/TaskCount fields. None of the five constants is documented or derived from anything AWS publishes
- **Why it matters**: a cluster with no service info (which is **exactly** the case the unused-cluster check flags) contributes $100/month savings — guess, not data. Multiply by 10 unused clusters and the headline number is $1000 of synthetic savings
- **Recommended fix**: when `Cpu` / `Memory` are missing, emit `EstimatedMonthlySavings = 0.0` and a `pricing_warning: "task config unknown"` — do not fabricate

### L2-002 Rightsize savings use flat 30% not measured-usage delta  [CRITICAL]
- **Check**: L2.1 / L2.5 — `derived` only when documented
- **Evidence**: `services/adapters/containers.py:84-89` — `monthly_ondemand × 0.30` for any rec whose savings string contains "rightsizing" or whose recommendation says "rightsize" / "over-provisioned". Shim collects actual CW CPU + memory utilization at `services/containers.py:389-402, 505-515` but those numbers are not passed through to the adapter for savings computation. Shim emits an `EstimatedSavings: "30-60% cost reduction based on measured over-provisioning"` STRING (line 553) but never a numeric `EstimatedMonthlySavings` field
- **Why it matters**: the shim measured 5% CPU + 20% memory utilization on a task — the actual rightsize savings is ~75-80%, not 30%. Conservative flat 30% understates by 2-3× for genuinely over-provisioned tasks AND overstates for tasks already running at 60-70% utilization
- **Recommended fix**: shim should compute `actual_savings = current_cost × (1 − max(measured_cpu, measured_mem) × 1.2)` and emit it as a numeric field. Adapter then sums the numeric, not a string-dispatched multiplier

### L2-003 `cost_optimization_hub` source double-counts vs `enhanced_checks`  [HIGH]
- **Check**: L2.5.2 — dedupe across sources
- **Evidence**: a single ECS service that is over-provisioned can appear in:
  1. `enhanced_checks` (shim emits a rightsizing rec from CW analysis)
  2. `cost_optimization_hub` (orchestrator hands the CoH `EcsService` finding for the same service)
  3. `compute_optimizer` (CO emits its own task-size recommendation)
  Adapter sums savings from all three without dedup. Unlike Lambda (which has a dedup block at `services/adapters/lambda_svc.py:78-90`, even if buggy), Containers has **no dedup at all**
- **Why it matters**: 3× double-count of the same opportunity on every multi-source ECS service
- **Recommended fix**: dedupe by `serviceName`/`taskDefinitionArn` across the three sources before summing

### L2-004 ECR lifecycle savings hardcoded $25/month  [HIGH]
- **Check**: L2.1
- **Evidence**: `services/adapters/containers.py:90-91` — `savings += 25.0 * ctx.pricing_multiplier` for any rec mentioning "lifecycle". ECR pricing is $0.10/GB-month for image storage. Real lifecycle savings is `unused_image_GB × $0.10` × pricing_multiplier
- **Recommended fix**: shim should query `ecr.describe_image_scan_findings` / `ecr.describe_repository_storage_metrics` to compute actual savings. Or at minimum, multiply by `repository_count × avg_image_GB × $0.10` with documented assumptions

### L2-005 Compute Optimizer normalizer double-applies `pricing_multiplier`  [HIGH]
- **Check**: L2.3.1
- **Evidence**: `services/advisor.py:276` — `_normalize_ecs_co_rec` returns `"estimatedMonthlySavings": round(savings * pricing_multiplier, 2)`. Same bug as Lambda — CO API already returns region-priced savings
- **Recommended fix**: drop `* pricing_multiplier` in `_normalize_ecs_co_rec`

### L2-006 `pricing_multiplier` correctly applied to module-const Fargate path  [LOW]
- **Check**: L2.3.2
- **Evidence**: line 79 — `monthly_ondemand` includes `* ctx.pricing_multiplier` ✓
- **Status**: PASS

### L2-007 ARM Fargate (Graviton) not modeled  [MEDIUM]
- **Check**: L2.1 — pricing-model gap
- **Evidence**: TODO at `services/adapters/containers.py:54-55`. Shim does not inspect `runtimePlatform.cpuArchitecture`. ARM Fargate is ~20% cheaper than x86 (verified)
- **Why it matters**: customers running ARM Fargate get their task cost overstated by 20%, leading to inflated savings on every rec

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| Fargate x86 vCPU rate | `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonECS, us-east-1, cputype=perCPU` | $0.04048/hr (SKU 8CESGAFWKAJ98PME) | confirms shim constant |
| Fargate x86 memory rate | same | `AmazonECS, us-east-1, memorytype=perGB, usagetype=USE1-Fargate-GB-Hours` | $0.004445/GB-hr (SKU PBZNQUSEXZUC34C9) | confirms shim constant |
| ARM rates (L2-007) | same | `usagetype=USE1-Fargate-ARM-vCPU-Hours` | $0.03238 vCPU, $0.00356 GB | refutes "x86 prices cover all tasks" |
| L2-005 | AWS CO docs | https://docs.aws.amazon.com/compute-optimizer/latest/ug/view-ecs-service-recommendations.html | "based on on-demand pricing in the resource's Region" | refutes the multiplier in `_normalize_ecs_co_rec` |

---

## L3 — Reporting findings

### L3-001 `_render_containers_enhanced_checks` has a hardcoded category whitelist  [HIGH]
- **Check**: L3.2.4 — every emitted `CheckCategory` must have a rendering path
- **Evidence**: `reporter_phase_b.py:791-820` defines 7 explicit groups. Shim emits at least 8 categories. Recs whose `CheckCategory` is not in the whitelist route to `"Other Optimizations"` and lose their group label
- **Recommended fix**: extend the whitelist or replace with descriptor-driven routing

### L3-002 No `EstimatedMonthlySavings` numeric field on shim recs  [MEDIUM]
- **Check**: L3.1.5 / scope rule
- **Evidence**: every shim emission site sets `EstimatedSavings` as a string but never `EstimatedMonthlySavings` as a float. JSON consumers cannot read the figure programmatically
- **Recommended fix**: emit both — string for display, float for downstream

### L3-003 No `priority`/`severity` field; severity signal lost  [MEDIUM]
- **Recommended fix**: derive from utilization (rightsize) or cluster cost (unused)

---

## Cross-layer red flags

**None auto-FAIL** under §4. But:
- **L1-001 (CRITICAL)** — required_clients omits CW
- **L2-001 (5 invented constants)** + **L2-002 (flat 30% rightsizing)** + **L2-003 (no cross-source dedup)** — stack to systematic over-statement

Overall **FAIL**.

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/containers.py services/containers.py
N806 Variable `FARGATE_VCPU_HOURLY` in function should be lowercase
N806 Variable `FARGATE_MEM_GB_HOURLY` in function should be lowercase

# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='containers'); print('clients:', m.required_clients()); print('cw:', m.requires_cloudwatch); print('fast:', m.reads_fast_mode)"
clients: ('ecs', 'eks', 'ecr', 'compute-optimizer')   ← LIE: shim uses cloudwatch
cw: False                                              ← LIE
fast: False

# 3. Offline + 5. Regression / snapshot tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k containers -v
============================= 4 passed, 122 deselected in 0.06s ==============================

# 6. AWS prices (live API)
Fargate x86 vCPU:      $0.04048/hr     ← matches adapter constant
Fargate x86 memory:    $0.004445/GB-hr ← matches adapter constant
Fargate ARM vCPU:      $0.03238/hr     ← 20% cheaper, adapter doesn't model
Fargate ARM memory:    $0.00356/GB-hr  ← 20% cheaper, adapter doesn't model
Fargate Windows vCPU:  $0.04655/hr     ← +15%, adapter doesn't model
```

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L1-001** — Add `"cloudwatch"` to `required_clients()`; set `requires_cloudwatch = True`.
2. **[CRITICAL] L2-002** — Compute rightsize savings from CW-measured CPU/memory utilization (shim already collects); drop the flat 30% multiplier.
3. **[HIGH] L1-003 / L1-004** — Replace 3 `print` sites with `ctx.warn`/`ctx.permission_issue`; replace `EstimatedSavings`-string token dispatch with `CheckCategory` dict.
4. **[HIGH] L2-001** — Stop fabricating $150 / $75 / $25 / $100 / $60 fallbacks; emit `EstimatedMonthlySavings = 0.0` + `pricing_warning` when input fields are missing.
5. **[HIGH] L2-003** — Dedup `cost_optimization_hub` ∪ `enhanced_checks` ∪ `compute_optimizer` by `serviceName` / `taskDefinitionArn` before summing.
6. **[HIGH] L2-004** — Replace flat $25 ECR lifecycle savings with `image_storage_GB × $0.10/GB-month`.
7. **[HIGH] L2-005** — Drop `* pricing_multiplier` from `_normalize_ecs_co_rec`.
8. **[MEDIUM] L2-007** — Add ARM / Windows Fargate price branches.
9. **[MEDIUM] L3-001** — Extend Phase B handler whitelist or move to descriptor routing.
10. **[MEDIUM] L3-002** — Emit `EstimatedMonthlySavings` numeric field on every rec.
11. **[LOW] L3-003** — Set `priority`.
12. **[LOW] ruff N806** — Constants → module-level UPPER_CASE.
