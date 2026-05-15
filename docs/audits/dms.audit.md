# Audit: dms Adapter

**Adapter**: `services/adapters/dms.py` (93 lines)
**ALL_MODULES index**: 18
**Renderer path**: **Phase A** (`dms` is in `_PHASE_A_SERVICES`)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **FAIL** | 6 (1 CRITICAL, 3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | live × multiplier double-multiply + flat $50 invented + Multi-AZ savings ignore actual instance cost |

---

## Pre-flight facts

- **Required clients**: `("dms", "cloudwatch")` ✓
- **Flags**: `reads_fast_mode=True`
- **Sources**: dynamic per-shim-checks + `multi_az_review` if non-prod Multi-AZ detected
- **Pricing**: service code `AWSDatabaseMigrationSvc` ✓ (per the prior `docs/audits/SUMMARY.md` AUDIT-25 fix). `instance_class.replace("dms.", "")` to strip DMS prefix

---

## L1 — Technical findings

### L1-001 Adapter `print()` line 36  [LOW]
### L1-002 Adapter logic for Multi-AZ should be in shim  [MEDIUM]
- Lines 50-71 — adapter constructs `multi_az_review` recs by inspecting `Tags` + `ReplicationInstanceIdentifier` for "dev/test/staging/sandbox/nonprod" keywords. This belongs in shim where the rest of the analysis lives
- **Recommended fix**: move Multi-AZ detection to `services/dms.py:get_enhanced_dms_checks`

---

## L2 — Calculation findings

### Source-classification table

| Rec | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Live rec savings | `live × 0.35 × multiplier` | line 43-46 | **CRITICAL FAIL** — double-multiplier; live path already region-correct |
| Missing-instance-class fallback | `module-const` ($50 × multiplier) | line 48 | **HIGH FAIL** — invented constant |
| Multi-AZ non-prod savings | `module-const` ($50 × multiplier per rec) | line 71 | **HIGH FAIL** — Multi-AZ doubles price; savings should be `monthly_instance_cost × 0.5` (per-instance), not $50 flat |

### L2-001 Live × `pricing_multiplier` double-multiply  [CRITICAL]
- **Evidence**: line 46 — `savings += monthly * 0.35 * ctx.pricing_multiplier`. `get_instance_monthly_price` returns region-correct values
- **Recommended fix**: drop `* ctx.pricing_multiplier` from line 46

### L2-002 35% savings factor arbitrary  [HIGH]
- **Evidence**: line 46. DMS rightsizing savings vary widely — from 0% (already-sized) to 75% (T3 → Serverless). Flat 35% has no basis
- **Recommended fix**: per-`CheckCategory` factor

### L2-003 $50 fallback constants in two places  [HIGH]
- **Evidence**: line 48 (no instance-class fallback), line 71 (Multi-AZ savings). DMS dms.t3.micro is ~$25/mo, dms.r5.4xlarge is ~$1800/mo. Flat $50 is wrong by 1.5-36×
- **Recommended fix**: use shim's instance-class price lookup; emit 0 + warning when class missing

### L2-004 Multi-AZ savings ignore actual instance cost  [HIGH]
- **Evidence**: line 71 — `len(multi_az_recs) * 50 * ctx.pricing_multiplier`. Multi-AZ DMS doubles the instance cost. Switching to Single-AZ saves exactly 50% of the actual monthly price (which is known from `monthly` calculation at line 43)
- **Recommended fix**: compute Multi-AZ savings as `monthly × 0.5` where `monthly` is the live price for the same instance class

### L2-005 `"AWSDatabaseMigrationSvc"` service code  [LOW / POSITIVE]
- **Evidence**: line 44 — correct AWS service code (fixed in prior audit). Confirmed via `mcp__aws-pricing-mcp-server__get_pricing_service_codes` listing
- **Status**: PASS

### L2-006 `instance_class.replace("dms.", "")` to strip prefix  [MEDIUM]
- **Evidence**: line 44. DMS uses `dms.t3.micro` etc. AWS Pricing API SKU uses `dms.t3.micro` (with prefix) — need to verify; if Pricing API expects no prefix the strip is correct, otherwise the lookup fails
- **Recommended fix**: verify against `mcp__aws-pricing-mcp-server__get_pricing_attribute_values("AWSDatabaseMigrationSvc", "instanceType")` — if values include `dms.` prefix, REMOVE the strip; otherwise keep

---

## L3 — Reporting findings

**None** — Phase A renderer handles `dms`; sources dynamically named.

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Drop `* ctx.pricing_multiplier` on live path.
2. **[HIGH] L2-002 / L2-003 / L2-004** — Per-category factors; compute Multi-AZ savings from actual monthly.
3. **[MEDIUM] L1-002** — Move Multi-AZ detection to shim.
4. **[MEDIUM] L2-006** — Verify `dms.` prefix handling in Pricing API call.
5. **[LOW] L1-001** — Drop adapter print.
