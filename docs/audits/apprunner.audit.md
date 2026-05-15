# Audit: apprunner Adapter

**Adapter**: `services/adapters/apprunner.py` (93 lines)
**ALL_MODULES index**: 20
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| L2 Calculation | **FAIL** | 5 (3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | hardcoded 12% rightsize + 160 active-hour assumption + dual-mode App Runner pricing modeled simplistically |

---

## Pre-flight facts

- **Required clients**: `("apprunner",)` — **omits `cloudwatch`** (used in `_estimate_active_hours`)
- **Flags**: `reads_fast_mode=True` ✓ (honors fast_mode at line 30)
- **Sources**: `{"enhanced_checks"}`
- **Pricing constants**: vCPU $0.064/hr ✓, memory $0.007/GB-hr ✓ (both match AWS list)

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `cloudwatch`  [HIGH]
- **Evidence**: line 27 returns `("apprunner",)`; `_estimate_active_hours` uses `cloudwatch` (line 33)
- **Recommended fix**: `("apprunner", "cloudwatch")`; set `requires_cloudwatch=True`

### L1-002 Two `print()` calls (banner + CW warning)  [MEDIUM]
- Lines 53, 57
- **Recommended fix**: `logger.debug` / `ctx.warn`

### L1-003 CloudWatch logic in adapter, not shim  [MEDIUM]
- **Evidence**: `_estimate_active_hours` belongs in `services/apprunner.py:get_enhanced_apprunner_checks`
- **Recommended fix**: move to shim

---

## L2 — Calculation findings

### Source-classification table

| Component | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| vCPU rate $0.064/hr | `module-const` | line 12 | YES | AWS list price |
| Memory rate $0.007/GB-hr | `module-const` | line 13 | YES | AWS list price |
| `DEFAULT_ACTIVE_HOURS_PER_MONTH = 160` | `arbitrary` | line 14 | **FAIL** — invented |
| `RIGHTSIZING_SAVINGS_RATE = 0.12` | `arbitrary` | line 15 | **FAIL** — 12% factor uniform across all rightsizing recs |
| Provisioned monthly | `derived` (`mem_gb × $0.007 × 730 × multiplier`) | line 77 | **WARN** — assumes 100% provisioned uptime |

### L2-001 12% rightsizing factor uniform  [HIGH]
- **Evidence**: line 82 — `monthly_cost * RIGHTSIZING_SAVINGS_RATE`. App Runner rightsizing can range 0-75% depending on CPU/memory utilization
- **Recommended fix**: derive from measured CPU util (already collected by `_estimate_active_hours` but not passed through)

### L2-002 `DEFAULT_ACTIVE_HOURS_PER_MONTH = 160` arbitrary  [HIGH]
- **Evidence**: line 14 — 160 hours/month ≈ 5.3 hr/day. The CPU-utilization-tier branching at lines 48-51 multiplies this by 0.5 for low-util — also arbitrary
- **Recommended fix**: use actual CW `ActiveInstances` or `RunningTasks` metric for billed hours

### L2-003 Provisioned vs Active pricing model simplified  [HIGH]
- **Evidence**: App Runner has two pricing modes:
  - **Provisioned** ($0.007/GB-hr, billed 24/7 when service exists, even idle)
  - **Active** (vCPU + memory, billed when request being processed)
  Adapter sums both fully: `provisioned (730 hrs)` + `active (active_hours)`. But for "no-traffic" services, the actual cost is **only** provisioned memory; for "always-on" services, the active rate replaces provisioned for those hours
- **Recommended fix**: model `cost = mem_gb × $0.007 × 730 + (vcpus × $0.064 + mem_gb × $0.007) × active_hours` — but subtract `mem_gb × $0.007 × active_hours` to avoid double-counting memory during active periods

### L2-004 `pricing_multiplier` applied on module-const path correctly  [LOW / POSITIVE]
- **Evidence**: lines 77, 79 — multiplier applied
- **Status**: PASS

### L2-005 Adapter recomputes pricing the shim could pre-compute  [MEDIUM]
- **Evidence**: vCPU/Memory parsing at lines 67-76 belongs in shim
- **Recommended fix**: emit `EstimatedMonthlySavings` numeric from shim; adapter just sums

---

## L3 — Reporting findings

**None** — adapter sets sources cleanly.

---

## Recommended next steps

1. **[HIGH] L2-001 / L2-002 / L2-003** — Replace 12%/160-hour/dual-mode model with per-rec computed cost from actual CW metrics.
2. **[HIGH] L1-001** — Add `"cloudwatch"` to `required_clients()`.
3. **[MEDIUM] L1-002 / L1-003** — Move CW logic to shim; route prints through `ctx.warn`.
4. **[MEDIUM] L2-005** — Pre-compute savings in shim, not adapter.
