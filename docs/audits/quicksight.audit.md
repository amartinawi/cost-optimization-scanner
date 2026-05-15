# Audit: quicksight Adapter

**Adapter**: `services/adapters/quicksight.py` (58 lines)
**ALL_MODULES index**: 19
**Renderer path**: Generic per-rec
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **FAIL** | 4 (1 CRITICAL, 1 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | Standard $0.25 wrong (AWS lists $0.38 for both editions); shim uses $0.25 flat, adapter uses $0.38 — inconsistent |

**Note**: prior AUDIT-26 finding (adapter reads fields shim never sets) has been **FIXED** — `Edition` + `UnusedSpiceCapacityGB` are now emitted by `services/quicksight.py:68-69`.

---

## Pre-flight facts

- **Required clients**: `("quicksight",)`
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `QUICKSIGHT_SPICE_PER_GB = {"ENTERPRISE": 0.38, "STANDARD": 0.25}` (adapter); shim hardcodes `$0.25/GB` (line 76)

---

## L1 — Technical findings

### L1-001 Adapter banner `print()`  [LOW]

### L1-002 Shim's "QuickSight not enabled" uses `print()` instead of `ctx.permission_issue`  [MEDIUM]
- **Evidence**: `services/quicksight.py:88`
- **Recommended fix**: silently skip or `ctx.warn` with skip-rationale

---

## L2 — Calculation findings

### L2-001 SPICE Standard $0.25/GB is wrong; AWS lists $0.38 for BOTH editions  [CRITICAL]
- **Evidence**:
  - Adapter line 39: `{"ENTERPRISE": 0.38, "STANDARD": 0.25}`
  - Shim line 76: `(total_capacity - used_capacity) * 0.25` — uses $0.25 flat regardless of edition
  - AWS QuickSight pricing page: "**$0.38 per GB / month** for SPICE capacity" — same rate for Standard and Enterprise
- **Why it matters**: shim under-states by 34% (`(0.38 − 0.25) / 0.38`) on every account; adapter mismatches shim on Standard editions, and the $0.38/$0.25 split itself is a fiction. The two layers compute different numbers — `EstimatedSavings` string in shim says one thing; adapter's `total_monthly_savings` says another
- **Recommended fix**: replace both with `SPICE_PRICE_PER_GB = 0.38` (single value); align shim and adapter

### L2-002 `PurchaseMode` field doesn't return "ENTERPRISE"/"STANDARD"  [HIGH]
- **Evidence**: shim line 69 — `"Edition": capacity_config.get("PurchaseMode", "ENTERPRISE")`. `PurchaseMode` in DescribeSpiceCapacity returns values like `"ANNUAL"` / `"MONTHLY"` (per AWS API docs), not edition identifiers. Edition lives in `describe_account_settings.Edition` (separate API)
- **Why it matters**: adapter looks up `QUICKSIGHT_SPICE_PER_GB.get(edition, 0.38)` — `"ANNUAL"` and `"MONTHLY"` aren't keys, so it falls through to default $0.38. Currently correct *by accident* of the dict's default. If shim ever sets a real edition, it would break
- **Recommended fix**: shim should call `quicksight.describe_account_settings(AwsAccountId=...)` to get actual `Edition`

### L2-003 `$30/month` fallback when `unused_gb <= 0`  [MEDIUM]
- **Evidence**: adapter line 48 — `savings += 30.0 * ctx.pricing_multiplier`. If shim emits a rec without `UnusedSpiceCapacityGB`, $30 invented. No documented basis
- **Recommended fix**: emit 0 + `pricing_warning`

### L2-004 `pricing_multiplier` correctly applied on module-const path  [LOW / POSITIVE]
- **Evidence**: line 43 — `spice_price * ctx.pricing_multiplier`. SPICE pricing is regional; AWS Pricing API would be preferable but module-const + multiplier is acceptable per L2.3.2
- **Status**: PASS

---

## L3 — Reporting findings

**None** — adapter reads the fields shim sets; prior AUDIT-26 mismatch resolved.

---

## External validation

| Finding | Source | Result |
|---|---|---|
| L2-001 | AWS QuickSight pricing page | "$0.38 per GB/month for SPICE capacity" — refutes Standard $0.25 |
| L2-002 | AWS QuickSight API docs `DescribeSpiceCapacity` | `PurchaseMode` returns `ANNUAL`/`MONTHLY` (not edition); edition via `DescribeAccountSettings.Edition` |

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Replace `QUICKSIGHT_SPICE_PER_GB` dict with single $0.38 constant; align shim's $0.25 line 76 with $0.38.
2. **[HIGH] L2-002** — Use `DescribeAccountSettings.Edition` for the edition field.
3. **[MEDIUM] L2-003** — Drop $30 fallback.
4. **[MEDIUM] L1-002** — `print("ℹ️ QuickSight not enabled")` → silent skip or `ctx.warn`.
5. **[LOW] L1-001** — Drop adapter print.
