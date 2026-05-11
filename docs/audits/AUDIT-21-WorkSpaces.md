# AUDIT-21: WorkSpaces Adapter Audit

**Date:** 2026-05-01  
**Auditor:** OpenCode AI  
**Scope:** `services/adapters/workspaces.py`, `services/workspaces.py`  
**AWS Pricing Region:** eu-west-1 (EU Ireland)

---

## Executive Summary

The WorkSpaces adapter provides basic cost optimization recommendations for Amazon WorkSpaces but has significant gaps in pricing accuracy, savings calculations, and optimization coverage. The adapter correctly identifies ALWAYS_ON WorkSpaces as candidates for AutoStop conversion and detects unused WorkSpaces in STOPPED/ERROR/SUSPENDED states, but the pricing integration is fundamentally broken due to API field mismatches.

---

## Code Analysis

### 1. Adapter Structure (`services/adapters/workspaces.py`)

```python
class WorkspacesModule(BaseServiceModule):
    key: str = "workspaces"
    cli_aliases: tuple[str, ...] = ("workspaces",)
    display_name: str = "WorkSpaces"
```

**Assessment:** Clean ServiceModule implementation following project patterns.

### 2. Enhanced Checks (`services/workspaces.py`)

The `get_enhanced_workspaces_checks()` function implements three check categories:

| Check Category | Implementation Status | Description |
|----------------|----------------------|-------------|
| `billing_mode_optimization` | ✅ Implemented | Flags ALWAYS_ON WorkSpaces for AutoStop conversion |
| `unused_workspaces` | ✅ Implemented | Flags STOPPED/ERROR/SUSPENDED WorkSpaces |
| `bundle_rightsizing` | ❌ **Empty** | No implementation |

**Code Issues Found:**

1. **Missing Idle Detection**: Only checks workspace `State`, not actual usage metrics or login history
2. **Empty Bundle Rightsizing**: Check category exists but has no logic
3. **Static Savings Estimate**: Hardcoded "$50/month potential per workspace" regardless of bundle type

### 3. Pricing Integration Analysis

The adapter attempts to use live pricing via:

```python
monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonWorkSpaces", compute_type)
savings += monthly * 0.40 if monthly > 0 else 35.0 * ctx.pricing_multiplier
```

**CRITICAL ISSUE**: The pricing engine's `_fetch_generic_instance_price()` uses:
```python
filters = [
    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
    {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
]
```

However, AWS Pricing API for WorkSpaces uses `usagetype` codes (e.g., `EU-AW-HW-2`), NOT `instanceType`. The `compute_type` values from `describe_workspaces` (like "STANDARD", "PERFORMANCE") do not map to pricing API filterable fields.

---

## Pricing Validation

### eu-west-1 Bundle Pricing (Verified from AWS Pricing API)

| Bundle | ComputeTypeName | AlwaysOn Monthly | AutoStop Base | AutoStop Hourly | Break-Even Hours |
|--------|-----------------|------------------|---------------|-----------------|------------------|
| Standard | STANDARD | $37.00 | $11.00 | $0.32/hr | ~81 hours/month |
| Performance | PERFORMANCE | $54.00 | $13.00* | $0.38/hr* | ~108 hours/month |
| Power | POWER | $82.00 | $19.00* | $0.57/hr* | ~111 hours/month |
| PowerPro | POWERPRO | $157.00 | $25.00* | $0.88/hr* | ~150 hours/month |

*Estimated based on pricing patterns; exact AutoStop pricing varies by storage config

### Pricing Accuracy Issues

| Issue | Current Behavior | Expected Behavior |
|-------|-----------------|-------------------|
| Field mismatch | Uses `instanceType` filter | Should use `usagetype` pattern matching |
| ComputeType mapping | Passes "STANDARD", "PERFORMANCE" | Should map to bundle IDs (2, 3, 8, etc.) |
| Regional pricing | Uses generic multiplier | Should query specific regional usagetypes |
| Fallback value | $35.00 × multiplier | Should use bundle-specific fallbacks |

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Live pricing integration | ❌ FAIL | Pricing API field mismatch; always falls back |
| 2 | Monthly vs hourly billing handling | ⚠️ WARN | Only detects ALWAYS_ON vs AutoStop, no usage analysis |
| 3 | Idle/underused WorkSpaces detection | ⚠️ WARN | Detects stopped/error states but not low-usage patterns |
| 4 | Savings calculation accuracy | ❌ FAIL | 40% flat rate doesn't reflect actual pricing models |
| 5 | Bundle rightsizing | ❌ FAIL | Check category exists but is empty |
| 6 | Multi-region pricing support | ⚠️ WARN | Uses pricing_multiplier but no regional bundle mapping |
| 7 | Error handling | ✅ PASS | Try/except with ctx.warn() for API failures |
| 8 | Fallback values | ⚠️ WARN | $35 fallback doesn't match actual eu-west-1 Standard price ($37) |

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| WS-001 | **CRITICAL** | Pricing engine uses incorrect filter field (`instanceType` instead of `usagetype`) | Live pricing never works; always falls back to estimates |
| WS-002 | **HIGH** | No mapping from `ComputeTypeName` (STANDARD) to bundle IDs (2) for pricing | Cannot query correct pricing for specific bundle types |
| WS-003 | **HIGH** | Savings calculated as flat 40% of monthly cost | Doesn't account for actual usage hours; significant over/under-estimation |
| WS-004 | **MEDIUM** | `bundle_rightsizing` check is empty | Missing optimization opportunity for over-provisioned WorkSpaces |
| WS-005 | **MEDIUM** | No idle detection based on login history | Cannot recommend termination of truly unused WorkSpaces |
| WS-006 | **LOW** | Fallback price $35 doesn't match eu-west-1 Standard ($37) | Minor inaccuracy in fallback calculations |
| WS-007 | **LOW** | Hardcoded "$50/month" savings estimate in recommendations | Not personalized to actual bundle pricing |

---

## Recommendations

### Immediate Fixes

1. **Fix Pricing Integration (WS-001, WS-002)**:
   - Implement bundle ID mapping: `{"STANDARD": "2", "PERFORMANCE": "3", "POWER": "8", ...}`
   - Use `usagetype` filter with pattern like `{region_prefix}-AW-HW-{bundle_id}`
   - Example: `EU-AW-HW-2` for Standard in eu-west-1

2. **Improve Savings Calculation (WS-003)**:
   - For ALWAYS_ON → AutoStop recommendations, calculate break-even hours
   - Use formula: `(AlwaysOn_Monthly - AutoStop_Base) / AutoStop_Hourly`
   - Recommend AutoStop only if expected usage < break-even

3. **Implement Bundle Rightsizing (WS-004)**:
   - Track actual resource utilization (CPU, memory) via CloudWatch
   - Compare against bundle specifications
   - Recommend downsizing if utilization < 30% consistently

### Enhancements

4. **Add Idle Detection (WS-005)**:
   - Query WorkSpaces connection events or CloudWatch UserConnected metric
   - Flag WorkSpaces with no connections in 30+ days
   - Differentiate between intentionally stopped vs abandoned WorkSpaces

5. **Fix Fallback Values (WS-006)**:
   - Update fallback to $37 for Standard bundles
   - Consider region-specific fallback dictionaries

---

## Verdict

**⚠️ WARN — 45/100**

The WorkSpaces adapter provides basic functionality and correctly identifies billing mode optimization opportunities, but the pricing integration is non-functional due to API field mismatches. The savings calculations are rough estimates rather than accurate projections. The empty bundle rightsizing check represents a significant missed optimization opportunity.

**Remediation Priority**: HIGH - Fix pricing integration (WS-001, WS-002) before relying on savings estimates for customer-facing reports.

---

## Appendix: AWS Pricing API Response Examples

### Standard Bundle (EU-AW-HW-2) - AlwaysOn Monthly
```json
{
  "product": {
    "attributes": {
      "usagetype": "EU-AW-HW-2",
      "location": "EU (Ireland)"
    }
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Month",
        "pricePerUnit": {"USD": "37.0000000000"},
        "description": "$37/month-Monthly Plus-Windows licensed-Standard-2vCPU,4GB Memory,80GB Root,50GB User"
      }
    }
  }
}
```

### Standard Bundle (EU-AW-HW-2-AutoStop-User) - AutoStop Monthly Base
```json
{
  "product": {
    "attributes": {
      "usagetype": "EU-AW-HW-2-AutoStop-User",
      "location": "EU (Ireland)"
    }
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Month",
        "pricePerUnit": {"USD": "11.0000000000"},
        "description": "$11/month-Hourly Plus-Windows licensed-Standard-2vCPU,4GB Memory,80GB Root,50GB User"
      }
    }
  }
}
```

### Standard Bundle (EU-AW-HW-2-AutoStop-Usage) - AutoStop Hourly
```json
{
  "product": {
    "attributes": {
      "usagetype": "EU-AW-HW-2-AutoStop-Usage",
      "location": "EU (Ireland)"
    }
  },
  "terms": {
    "OnDemand": {
      "priceDimensions": {
        "unit": "Hour",
        "pricePerUnit": {"USD": "0.3200000000"},
        "description": "$0.32/hour-Hourly Plus-Windows licensed-Standard-2vCPU,4GB Memory"
      }
    }
  }
}
```

---

*Audit generated using AWS Pricing API data and live documentation search.*
