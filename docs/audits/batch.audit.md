# Audit: batch Adapter

**Adapter**: `services/adapters/batch.py` (71 lines)
**ALL_MODULES index**: 27
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 1 |
| L2 Calculation | **FAIL** | 4 (1 CRITICAL, 1 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | live × pricing_multiplier double-multiply + $150 fallback + Graviton 10% understates |

**Note**: dispatches on `CheckCategory` (good — better than string matching used by other adapters).

---

## Pre-flight facts

- **Required clients**: `("batch",)` — should include `ec2` for instance describe?
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `get_ec2_hourly_price(instance_type)` (live)

---

## L1 — Technical findings

### L1-001 Adapter print  [LOW]

---

## L2 — Calculation findings

### L2-001 Live × `pricing_multiplier` double-multiply  [CRITICAL]
- **Evidence**: line 59 — `hourly × 730 × rate × multiplier`. `hourly` from live `get_ec2_hourly_price` is region-correct
- **Recommended fix**: drop `* multiplier` on live path

### L2-002 Graviton 10% factor understates AWS published 20%  [HIGH]
- **Evidence**: line 49 — `rate = 0.10` for Graviton. AWS Graviton EC2 instances are ~20% cheaper than x86 (m5 vs m6g, etc.)
- **Recommended fix**: use 0.20

### L2-003 $150 fallback constant when instance_types missing  [MEDIUM]
- **Evidence**: line 61
- **Recommended fix**: emit 0 + warning

### L2-004 `CheckCategory` dispatch (good pattern)  [LOW / POSITIVE]
- **Evidence**: lines 45-53. Clean structured dispatch on canonical category strings — not string-token grep
- **Status**: PASS — model for other adapters to follow

---

## L3 — Reporting findings

**None**.

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Drop double-multiplier on live path.
2. **[HIGH] L2-002** — Fix Graviton to 20%.
3. **[MEDIUM] L2-003** — Drop $150 fallback.
4. **[LOW] L1-001** — Drop print.
