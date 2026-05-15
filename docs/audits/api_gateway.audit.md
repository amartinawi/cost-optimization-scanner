# Audit: api_gateway Adapter

**Adapter**: `services/adapters/api_gateway.py` (48 lines — thin)
**Legacy shim**: `services/api_gateway.py` (127 lines)
**ALL_MODULES index**: 14
**Renderer path**: **Phase A** (`api_gateway` is in `_PHASE_A_SERVICES`)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 4 (3 MEDIUM, 1 LOW) |
| L2 Calculation | **CONDITIONAL-PASS** | 3 (2 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 1 (LOW) |
| **Overall**    | **WARN** | substantially improved since prior audit (was FAIL); pricing now live-CW-backed |

**Note**: This adapter is the most recently-improved of the lot. Old `docs/audits/SUMMARY.md` line 56 calls it FAIL with flat `$15/api`; that code is gone. Caching finding was correctly removed per scope rule. **The pricing model is now CW-metric-backed and correct.**

---

## Pre-flight facts

- **Required clients**: `("apigateway", "cloudwatch")` ✓
- **Flags**: `requires_cloudwatch=False` (default) — **lies**; `reads_fast_mode=False` (default) — **lies** (shim *does* check `ctx.fast_mode` at line 73)
- **Sources**: `{"enhanced_checks"}` — but only the `rest_vs_http` category actively emits (4 other categories empty)
- **Pricing**: `REST_PER_M = 3.50`, `HTTP_PER_M = 1.00`, `SAVINGS_PER_M = 2.50` — module-const in shim, matches AWS list

---

## L1 — Technical findings

### L1-001 `requires_cloudwatch=False` but shim uses CW  [MEDIUM]
- Evidence: `services/api_gateway.py:77, 80`
- **Recommended fix**: set `requires_cloudwatch=True`

### L1-002 `reads_fast_mode=False` but shim *honors* `ctx.fast_mode`  [MEDIUM]
- Evidence: `services/api_gateway.py:73` — `if not ctx.fast_mode:`
- This is the *good* inverse of the other adapters — shim correctly skips CW in fast mode. Flag must reflect it
- **Recommended fix**: set `reads_fast_mode=True`

### L1-003 Module-level `print()` + adapter banner `print()`  [MEDIUM]
- `services/api_gateway.py:18`, `services/adapters/api_gateway.py:36`
- **Recommended fix**: `logger.debug`

### L1-004 Silent `except Exception: pass` at line 110-111  [LOW]
- Resource-lookup errors swallowed
- **Recommended fix**: `ctx.warn` + AccessDenied classification

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| `rest_vs_http.EstimatedMonthlySavings` | `live` (CW Count) × `module-const` ($2.50/M delta) | `services/api_gateway.py:93-95` | YES — REST list $3.50/M, HTTP list $1.00/M, delta $2.50/M | AWS API Gateway pricing page |
| Adapter `pricing_multiplier` application | applied on shim-const path | line 40 | YES — correct per L2.3.2 |

### L2-001 REST/HTTP pricing doesn't model tiers  [MEDIUM]
- **Evidence**: AWS pricing:
  - REST: $3.50/M first 333M, $2.80/M next 667M, $2.38 next 19B, $1.87 above 20B
  - HTTP: $1.00/M first 300M, $0.90/M above
- Shim uses flat first-tier rates. For large APIs (1B+ requests/month) this overstates savings
- **Recommended fix**: model the first-tier as a conservative ceiling, OR add tier breakpoints

### L2-002 Caching opportunity finding correctly removed  [LOW / POSITIVE]
- Evidence: shim lines 113-117 — explicit comment explaining why removed (cache *adds* API Gateway cost, net savings depends on backend; quantification impossible without backend data)
- **Status**: PASS — exemplary scope-rule compliance

### L2-003 `monthly_requests = 0.0` on CW failure  [LOW]
- Evidence: line 90-91. If CW fails (perm denied, throttle), savings silently become 0 instead of "unknown"
- **Recommended fix**: emit a `pricing_warning` field when CW fails so users can distinguish "no traffic" from "no data"

---

## L3 — Reporting findings

### L3-001 `optimization_descriptions` declares 5 categories but only 1 emits  [LOW]
- The 4 empty categories (`unused_stages`, `caching_opportunities`, `throttling_optimization`, `request_validation`) are removed per scope rule but their `optimization_descriptions` entries remain
- **Recommended fix**: trim the descriptions dict to match actual emissions

---

## External validation

| Finding | Source | Result |
|---|---|---|
| L2-001 | AWS API Gateway pricing page | REST $3.50/M tier-1 + tiered breakpoints — confirms shim's tier-1 constant |
| L2-002 (cache removed) | scope rule | removed correctly |

---

## Recommended next steps

1. **[MEDIUM] L1-001 / L1-002** — Set `requires_cloudwatch=True`, `reads_fast_mode=True` to match shim behavior.
2. **[MEDIUM] L1-003** — Drop the 2 prints.
3. **[MEDIUM] L2-001** — Model REST/HTTP tier breakpoints (or document the conservative first-tier assumption in the rec text).
4. **[LOW] L1-004** — Add `ctx.warn` + AccessDenied classification at the 2 silent except sites.
5. **[LOW] L2-003** — Add `pricing_warning` field when CW fails.
6. **[LOW] L3-001** — Trim `optimization_descriptions` to actual emissions.
