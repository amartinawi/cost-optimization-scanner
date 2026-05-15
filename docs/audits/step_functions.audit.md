# Audit: step_functions Adapter

**Adapter**: `services/adapters/step_functions.py` (98 lines)
**Legacy shim**: `services/step_functions.py`
**ALL_MODULES index**: 15
**Renderer path**: **Phase A** (`step_functions` is in `_PHASE_A_SERVICES`)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| L2 Calculation | **FAIL** | 5 (1 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | invented $150/month idle fallback + 60% Standard→Express factor incorrect model + Express pricing model fundamentally different |

---

## Pre-flight facts

- **Required clients**: `("states",)` — **omits `cloudwatch`** (used at line 56)
- **Flags**: `requires_cloudwatch=False` — **lies**; `reads_fast_mode=True` — **correct** (line 48 honors it)
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `$0.025/1K transitions` (Standard) hardcoded; `0.60` savings factor for Standard→Express

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `cloudwatch`  [HIGH]
- **Evidence**: line 24 returns `("states",)`; line 56 uses `cloudwatch`
- **Recommended fix**: change to `("states", "cloudwatch")`

### L1-002 `requires_cloudwatch=False` but adapter uses CW directly  [MEDIUM]
- **Recommended fix**: set `requires_cloudwatch=True`

### L1-003 Adapter does its own CloudWatch lookup (should be in shim)  [MEDIUM]
- **Evidence**: lines 53-72 — adapter handles CW logic when `monthly_executions <= 0` from rec. This logic belongs in the shim alongside other CW metric fetches
- **Recommended fix**: move CW fetch to `services/step_functions.py:get_enhanced_step_functions_checks`; adapter should just consume `MonthlyExecutions`

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Standard→Express migration | `live` (CW ExecutionsStarted) × `module-const` ($0.025/1K) × `0.60` | line 81-86 | **FAIL** — 60% factor is invented; Express has a different pricing model entirely | Express = $1/M requests + $0.00001667/GB-s, not a percentage of Standard |
| Idle / no-execution fallback | `module-const` ($150/month) | line 88, 90 | **FAIL** — `STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK = 150.0` is invented; idle state machines have **no** ongoing cost (Step Functions bills per-transition only) |
| `AVG_STATES_PER_EXECUTION = 5` | `arbitrary` | line 44, 79 | **WARN** — invented average; shim should expose actual state count |
| `eligible_for_migration` threshold | `derived` (state_count > 25 AND avg_duration < 60s) | line 76 | **WARN** — 25-state / 60-s thresholds for Express eligibility are not AWS-documented rules; Express limits are 5-min duration max and limited state types |

### L2-001 `$150/month idle fallback` invented — Step Functions has NO idle cost  [CRITICAL]
- **Evidence**: line 88 — `savings += STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK * ctx.pricing_multiplier` (= $150/month) when `monthly_executions == 0`. Also line 90 when in fast-mode
- **Why it matters**: AWS Step Functions bills **only** for state transitions (Standard) or requests + duration (Express). A state machine with **zero executions has zero cost**. The $150 fallback fabricates savings on inactive state machines
- **Recommended fix**: when `monthly_executions == 0`, emit `EstimatedMonthlySavings = 0.0` with rec text "Inactive state machine — verify if still needed (no ongoing cost)"

### L2-002 60% Standard→Express savings factor models wrong pricing  [HIGH]
- **Evidence**: line 84 — `* 0.60`. AWS Express pricing is:
  - $1 per million requests
  - $0.00001667 per GB-second for duration
  Standard pricing is `$0.025 per 1000 transitions`. The two models can't be related by a constant ratio — the savings depend on:
  - State transitions per execution
  - Average execution duration
  - Memory allocated
- **Why it matters**: for a workflow with 10 transitions per execution, 100 ms duration, 64 MB memory:
  - Standard: 10 × $0.025 / 1000 = $0.00025 per execution
  - Express: $1 / M requests = $0.000001 per request, + $0.00001667 × 0.064 × 0.1 = $0.00000011 per execution
  - Actual savings ≈ 99% (not 60%)
  For longer workflows (60+ states, 60+ seconds), Express may not even be eligible
- **Recommended fix**: compute Express cost from request count + memory + avg duration; subtract from Standard cost to get actual delta

### L2-003 `pricing_multiplier` applied to both live-CW-derived AND module-const paths  [HIGH]
- **Evidence**:
  - Live path: line 85 `* ctx.pricing_multiplier` after `(monthly_transitions / 1000) * STEP_FUNCTIONS_PER_1K_TRANSITIONS` — but `$0.025/1K` is a module-const, so multiplier is **correct** here per L2.3.2
  - Fallback path: line 88 — multiplier applied to invented $150 constant — correct per L2.3.2
- **Status**: PASS on the pricing_multiplier application (no double-multiply); the issue is L2-002 (wrong factor) compounds with the multiplier

### L2-004 `AVG_STATES_PER_EXECUTION = 5` averages a per-workflow variable  [MEDIUM]
- **Evidence**: line 44, 79 — every execution is assumed to be 5 transitions. Shim should pass actual `StateCount` per state machine (which is available from `states.describe_state_machine.definition`)
- **Recommended fix**: shim should parse the state machine definition JSON and emit `StateCount` accurately

### L2-005 Adapter falls back to $150 in `fast_mode` regardless of actual executions  [LOW]
- **Evidence**: line 89-90 — `else: savings += STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK * ctx.pricing_multiplier`. Fast mode skips CW lookup but uses the invented $150 fallback for EVERY rec
- **Why it matters**: a fast-mode scan over 100 state machines reports `$15,000/month` of "savings" — pure fabrication
- **Recommended fix**: when fast_mode, emit 0 + `pricing_warning: "fast mode — re-run without --fast for execution-count-based savings"`

### External validation

| Finding | Source | Result |
|---|---|---|
| L2-001 | AWS Step Functions pricing page | "You are charged based on the number of state transitions" — refutes idle cost |
| L2-002 | AWS Step Functions pricing page | Standard = $25/M transitions; Express = $1/M requests + duration — confirms different models |

---

## L3 — Reporting findings

**None significant** — uses Phase A renderer correctly; `optimization_descriptions` provided.

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Remove `$150/month idle fallback` (lines 11, 88, 90). Emit 0 instead.
2. **[HIGH] L2-002** — Replace `× 0.60` with actual Express cost from request × duration × memory; compute Standard cost from real transition count; emit delta.
3. **[HIGH] L1-001** — Add `"cloudwatch"` to `required_clients()`; set `requires_cloudwatch=True`.
4. **[MEDIUM] L1-003** — Move CW fetch to shim.
5. **[MEDIUM] L2-004** — Parse `definition` JSON for `StateCount`.
6. **[LOW] L2-005** — Fast-mode should not fabricate $150 fallback.
