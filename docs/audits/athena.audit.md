# Audit: athena Adapter

**Adapter**: `services/adapters/athena.py` (78 lines)
**ALL_MODULES index**: 26
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| L2 Calculation | **WARN** | 3 (1 MEDIUM, 2 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** — pricing correct, 75% factor documented, CW-backed; main issue is $50 invented fallback + CW omitted from required_clients |

---

## Pre-flight facts

- **Required clients**: `("athena",)` — **omits `cloudwatch`** (used at line 41)
- **Flags**: `reads_fast_mode=True` ✓ (honored at line 35)
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `ATHENA_PRICE_PER_TB = 5.0` ✓ matches AWS list

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `cloudwatch`  [HIGH]
- **Evidence**: line 24; CW used at line 41
- **Recommended fix**: `("athena", "cloudwatch")`; set `requires_cloudwatch=True`

### L1-002 CloudWatch logic in adapter, not shim  [MEDIUM]
- Lines 40-59 — should be in `services/athena.py`

### L1-003 Adapter banner + CW-failure `print()`  [MEDIUM]
- Lines 27, 58

---

## L2 — Calculation findings

### L2-001 `$50` fallback when CW fails or fast_mode  [MEDIUM]
- **Evidence**: lines 66, 68. Invented constant
- **Recommended fix**: emit 0 + `pricing_warning`

### L2-002 75% savings factor for Parquet/ORC conversion  [LOW]
- **Evidence**: line 63 — `* 0.75`. Documented inline as "70-91% compression for columnar formats". Defensible
- **Status**: PASS — documented well

### L2-003 Single rec contributes whole-account savings  [LOW]
- **Evidence**: each rec gets `monthly_tb × $5/TB × 0.75` — but `monthly_tb` is queried at workgroup level. If multiple recs flag the same workgroup (e.g., "use Parquet" and "use partition pruning"), savings double-count
- **Recommended fix**: dedupe by `WorkGroup`

---

## L3 — Reporting findings

**None**.

---

## Recommended next steps

1. **[HIGH] L1-001** — Add `"cloudwatch"` to `required_clients()`; set `requires_cloudwatch=True`.
2. **[MEDIUM] L1-002** — Move CW logic to shim.
3. **[MEDIUM] L2-001** — Drop $50 fallback.
4. **[LOW] L1-003** — Drop prints.
5. **[LOW] L2-003** — Dedupe by WorkGroup.
