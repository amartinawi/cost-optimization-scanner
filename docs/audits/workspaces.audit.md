# Audit: workspaces Adapter

**Adapter**: `services/adapters/workspaces.py` (67 lines)
**ALL_MODULES index**: 23
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 1 |
| L2 Calculation | **FAIL** | 4 (1 CRITICAL, 2 HIGH, 1 MEDIUM) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | `get_instance_monthly_price("AmazonWorkSpaces", ...)` structurally dead — WorkSpaces uses `bundle` filter not `instanceType` |

---

## Pre-flight facts

- **Required clients**: `("workspaces",)`
- **Sources**: `{"enhanced_checks"}`
- **Pricing path**: `get_instance_monthly_price("AmazonWorkSpaces", bundle_id)` — **structurally broken** (Pricing API uses `bundle`, not `instanceType`)

---

## L1 — Technical findings

### L1-001 Adapter print line 40  [LOW]

---

## L2 — Calculation findings

### L2-001 `get_instance_monthly_price("AmazonWorkSpaces", ...)` is structurally dead  [CRITICAL]
- **Evidence**: line 54. `_fetch_generic_instance_price` at `core/pricing_engine.py:661-669` filters by `{"Field": "instanceType", "Value": instance_type}`. AWS WorkSpaces Pricing API attributes (verified via MCP `get_pricing_service_attributes`): `bundle`, `bundleGroup`, `bundleDescription`, `memory`, `vcpu`, `runningMode`, `operatingSystem` — **no `instanceType`**. The filter never matches; call returns 0
- **Why it matters**: same shape as Lightsail (L2-001). 100% of WorkSpaces savings fall through to the `$35 * pricing_multiplier` invented constant when `EstimatedSavingsAmount` is not set by the shim. Adapter's "live pricing" claim is fictional
- **Recommended fix**: add `PricingEngine._fetch_workspaces_bundle_price(bundle_id, running_mode)` using `bundle` + `runningMode` filters; or have shim emit `EstimatedSavingsAmount` directly with live-pricing math

### L2-002 `* 0.40` AlwaysOn→AutoStop factor invented  [HIGH]
- **Evidence**: line 55. Actual savings depend on user workday (AutoStop = $0.30/hour per workspace, AlwaysOn = ~$25-$65/month flat). For 8-hour workday (~160 hr/mo): AutoStop ~$48 vs AlwaysOn ~$36 — AutoStop is sometimes MORE expensive. Flat 40% doesn't reflect this trade-off
- **Recommended fix**: compute actual AlwaysOn vs AutoStop delta from CW `UserConnected` metric

### L2-003 $35 fallback in two paths  [HIGH]
- **Evidence**: lines 55, 57
- **Recommended fix**: emit 0 + warning when bundle pricing unavailable

### L2-004 Bundle ID mapping uses numeric strings  [MEDIUM]
- **Evidence**: line 49 — `WORKSPACE_BUNDLE_MAP.get(compute_type, "2")`. AWS Pricing API filters on `bundle` attribute which is the SKU bundle name like `"Standard"`, `"Performance"`, `"Power"`, `"PowerPro"`, `"Graphics.g4dn"`, etc., not numeric IDs
- **Recommended fix**: map `compute_type` directly to API-compatible `bundle` attribute values

---

## External validation

| Finding | Tool | Result |
|---|---|---|
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing_service_attributes("AmazonWorkSpaces")` | returns `["bundle","bundleGroup","memory","vcpu","runningMode",...]` — confirms no `instanceType` attribute |

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Add WorkSpaces-aware pricing method to `PricingEngine` using `bundle` filter.
2. **[HIGH] L2-002** — Replace 0.40 factor with actual AlwaysOn vs AutoStop delta computed from CW `UserConnected` metric.
3. **[HIGH] L2-003** — Drop $35 fallback.
4. **[MEDIUM] L2-004** — Map `compute_type` to AWS-compatible bundle attribute values.
5. **[LOW] L1-001** — Drop adapter print.
