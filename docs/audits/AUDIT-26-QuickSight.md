# AUDIT-26: QuickSight Adapter Audit

**Date**: 2026-05-01  
**Adapter**: `services/adapters/quicksight.py` (58 lines)  
**Service Module**: `services/quicksight.py` (94 lines)  
**Auditor**: Automated audit against AWS Pricing API + AWS docs

---

## Code Analysis

### Architecture

The adapter follows the standard two-layer pattern:

1. **Adapter layer** (`services/adapters/quicksight.py`): `QuicksightModule(BaseServiceModule)` implementing `ServiceModule` protocol. Delegates to service module and applies SPICE per-GB pricing to compute savings.

2. **Service module** (`services/quicksight.py`): `get_enhanced_quicksight_checks(ctx)` performs the actual AWS API calls:
   - `describe_account_subscription` — checks if QuickSight is enabled
   - `list_namespaces` (paginated) — enumerates namespaces
   - `list_users` (paginated per namespace) — counts total users
   - `describe_spice_capacity` — retrieves SPICE used/total capacity in bytes

### Detection Logic

The service module detects exactly **one** optimization category: **SPICE capacity underutilization** (triggered when SPICE usage < 50% of total capacity). It counts users but never generates user-related recommendations — user count is only included as metadata in the SPICE optimization record.

### Savings Calculation (Adapter Layer)

```python
QUICKSIGHT_SPICE_PER_GB = {"ENTERPRISE": 0.38, "STANDARD": 0.25}
savings = 0.0
for rec in recs:
    edition = rec.get("Edition", "ENTERPRISE")
    spice_price = QUICKSIGHT_SPICE_PER_GB.get(edition, 0.38) * ctx.pricing_multiplier
    unused_gb = rec.get("UnusedSpiceCapacityGB", 0)
    if unused_gb > 0:
        savings += unused_gb * spice_price
    else:
        savings += 30.0 * ctx.pricing_multiplier
```

### Critical Disconnect: Service Module vs Adapter

The adapter reads fields that the service module never sets:

| Field Adapter Reads | Field Service Module Sets | Match? |
|---------------------|--------------------------|--------|
| `rec.get("Edition", "ENTERPRISE")` | Never sets `Edition` | **NO** — always defaults to `"ENTERPRISE"` |
| `rec.get("UnusedSpiceCapacityGB", 0)` | Never sets `UnusedSpiceCapacityGB` | **NO** — always `0` |
| `rec.get("UserCount")` | Sets `UserCount` | Yes (but not used for savings) |

Because `UnusedSpiceCapacityGB` is never set, the `if unused_gb > 0` branch **never executes**. Every recommendation falls into the `else` branch: `savings += 30.0 * ctx.pricing_multiplier` (a flat $30 per recommendation).

Meanwhile, the service module has its own savings estimate: `~${(total_capacity - used_capacity) * 0.25:.0f}/month` — which is a hardcoded $0.25/GB regardless of edition.

---

## Pricing Validation

### AWS Pricing API (eu-west-1, 2026-04-03)

| SPICE Type | Usage Type | Price (USD/GB-Mo) |
|------------|-----------|-------------------|
| Enterprise SPICE | `EU-QS-Enterprise-SPICE` | **$0.38** |
| Standard/Provisioned SPICE | `EU-QS-Provisioned-SPICE` | **$0.25** |

### Adapter Pricing Table

```python
QUICKSIGHT_SPICE_PER_GB = {"ENTERPRISE": 0.38, "STANDARD": 0.25}
```

| Edition | Adapter Price | AWS API Price | Match? |
|---------|--------------|---------------|--------|
| ENTERPRISE | $0.38/GB | $0.38/GB | **Correct** |
| STANDARD | $0.25/GB | $0.25/GB | **Correct** |

### Service Module Hardcoded Estimate

```python
f"~${(total_capacity - used_capacity) * 0.25:.0f}/month (estimate - verify SPICE pricing)"
```

This uses $0.25/GB unconditionally — **incorrect for Enterprise edition** (should be $0.38/GB). The text also says "(estimate - verify SPICE pricing)" — admission it's unreliable.

### AWS Documentation Cross-Reference

AWS docs confirm (aws.amazon.com/quicksight/pricing):
- **Standard Edition**: $0.25/GB for additional SPICE beyond 10 GB/user included
- **Enterprise Edition**: $0.38/GB for additional SPICE beyond included capacity
- **User pricing**: Authors $18-40/mo, Readers $3-20/mo, Standard $9-12/mo — **completely absent from adapter**

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Adapter registered in `ALL_MODULES` | ✅ PASS | `services/__init__.py:34` imports and registers `QuicksightModule` |
| 2 | Implements `ServiceModule` protocol | ✅ PASS | Extends `BaseServiceModule`, implements `required_clients()` and `scan()` |
| 3 | Returns valid `ServiceFindings` | ✅ PASS | Correct structure with `service_name`, `total_recommendations`, `sources` |
| 4 | `required_clients()` returns correct boto3 clients | ✅ PASS | Returns `("quicksight",)` |
| 5 | Pricing values match AWS Pricing API | ⚠️ WARN | SPICE per-GB rates are correct ($0.38/$0.25), but edition is never resolved so Enterprise rate always applies; service module uses wrong $0.25 flat rate |
| 6 | Savings calculations are accurate | ❌ FAIL | **Dead code**: adapter reads `UnusedSpiceCapacityGB` which service module never sets → all savings fall to flat $30 fallback. Service module's own estimate uses wrong rate |
| 7 | API pagination implemented | ✅ PASS | Uses paginators for `list_namespaces` and `list_users` |
| 8 | Error handling for disabled service | ✅ PASS | Gracefully handles `ResourceNotFoundException` for accounts without QuickSight |
| 9 | Exception handling per-namespace | ✅ PASS | Inner try/except continues on per-namespace user listing failures |
| 10 | Uses `ScanContext` correctly | ✅ PASS | Uses `ctx.client()`, `ctx.account_id`, `ctx.warn()`, `ctx.pricing_multiplier` |
| 11 | Golden file exists and passes | ✅ PASS | `golden_scan_results.json` has QuickSight entry with 0 recs/$0 savings |
| 12 | No hardcoded region strings | ✅ PASS | Uses `ctx.account_id` (region from ScanContext) |
| 13 | Zero write operations | ✅ PASS | All API calls are read-only `describe_*` and `list_*` |
| 14 | Detection coverage adequate | ⚠️ WARN | Only detects SPICE underutilization. Missing: idle users, Reader vs Author optimization, edition upgrade/downgrade analysis, session-based reader cost review |
| 15 | No false positive risk | ⚠️ WARN | 50% SPICE threshold is arbitrary but reasonable. However, savings are incorrect due to field mismatch |

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| QS-01 | 🔴 Critical | **Field mismatch between service module and adapter**: Adapter reads `UnusedSpiceCapacityGB` and `Edition` but service module sets `UsedCapacityGB`, `TotalCapacityGB`, and `UtilizationPercent`. The `unused_gb > 0` branch never executes. | All savings default to flat $30/recommendation regardless of actual SPICE waste |
| QS-02 | 🔴 Critical | **Edition always defaults to ENTERPRISE**: Service module never includes `Edition` in recommendation dicts. `rec.get("Edition", "ENTERPRISE")` always returns the default. | Standard edition accounts get wrong pricing ($0.38 instead of $0.25) if the field were ever used |
| QS-03 | 🟡 Medium | **Service module uses hardcoded $0.25/GB regardless of edition**: The `EstimatedSavings` string in the recommendation always multiplies by 0.25, even for Enterprise edition ($0.38/GB). | Text recommendation understates savings for Enterprise by ~34% |
| QS-04 | 🟡 Medium | **No user optimization checks**: Module counts users across namespaces but never generates recommendations for user tier optimization (Author→Reader, unused users, Reader session vs per-user pricing). | QuickSight user costs ($18-40/mo per author) are a major cost driver — completely unoptimized |
| QS-05 | 🟡 Medium | **No edition-level pricing integration**: Adapter doesn't determine which edition the account is on. The `describe_account_subscription` API returns edition info but it's only used for enabled/disabled check. | Pricing can't be accurately applied per edition |
| QS-06 | 🔵 Low | **Duplicate savings estimate**: Both the service module (in recommendation dict) and adapter (in ServiceFindings) compute savings independently with different logic. The service module's estimate appears in the recommendation text but is never used for `total_monthly_savings`. | Confusing for consumers of the data — which savings number is authoritative? |
| QS-07 | 🔵 Low | **`spice_optimization` is the only check category with real logic**: `user_optimization` and `capacity_optimization` categories are initialized but never populated. | Dead categories; code suggests intended future checks that were never implemented |

---

## Verdict

### ⚠️ WARN — 45/100

### Summary

The QuickSight adapter has **correct SPICE per-GB pricing values** that match the AWS Pricing API exactly ($0.38 Enterprise, $0.25 Standard in eu-west-1). However, the savings calculation is **fundamentally broken** due to a field-name mismatch between the service module and adapter layer — the adapter reads fields (`UnusedSpiceCapacityGB`, `Edition`) that the service module never produces. This means:

1. **Every recommendation gets a flat $30 fallback savings** instead of actual SPICE-based calculation
2. **Edition is never resolved**, so even if fields were correct, Standard accounts would be over-priced
3. The service module's own text estimate uses the wrong rate ($0.25 flat instead of edition-aware)

Detection coverage is **narrow** — only SPICE underutilization at a 50% threshold. QuickSight user costs (Authors $18-40/mo, Readers $3-20/mo) represent the largest cost optimization opportunity but are completely unexamined.

### Risk Assessment

- **False savings claims**: High — reported savings are arbitrary ($30 flat) rather than computed
- **False positives**: Low — SPICE underutilization detection logic is sound
- **False negatives**: Medium — missing user optimization, edition analysis, session pricing

### Recommendations

1. **Fix field mismatch (QS-01)**: Service module should output `UnusedSpiceCapacityGB` (= total - used) and `Edition` in recommendation dicts
2. **Extract edition from subscription (QS-05)**: Parse edition from `describe_account_subscription` response
3. **Add user optimization (QS-04)**: Detect unused users (no login in 90d), Author→Reader conversion opportunities, Reader per-session vs per-user pricing
4. **Remove duplicate savings logic (QS-06)**: Single authoritative savings calculation in adapter only
5. **Remove dead check categories (QS-07)**: Either implement `user_optimization` and `capacity_optimization` or remove the empty dict keys
