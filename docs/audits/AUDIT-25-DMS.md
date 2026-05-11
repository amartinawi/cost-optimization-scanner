# AUDIT-25: DMS Adapter Audit

**Date:** 2026-05-01
**Auditor:** Automated Audit Agent
**Files:** `services/adapters/dms.py` (56L), `services/dms.py` (132L)
**Scope:** Pricing accuracy, savings calculations, detection logic, protocol compliance

---

## Code Analysis

### Architecture

The DMS adapter follows the standard two-layer pattern:
- **Adapter** (`services/adapters/dms.py`): `DmsModule(BaseServiceModule)` — thin wrapper calling the service module and computing savings
- **Service module** (`services/dms.py`): `get_enhanced_dms_checks()` — performs actual API calls and logic

### Detection Categories

| Category | Logic | Threshold |
|----------|-------|-----------|
| `instance_rightsizing` | Instance class contains `"large"` AND CPU < 30% over 7 days | CPU avg < 30% |
| `unused_instances` | Subset of rightsizing where CPU < 5% | CPU avg < 5% |
| `serverless_migration` | All serverless replication configs found | None (informational) |

### Pricing Flow

```
DmsModule.scan(ctx)
  → get_enhanced_dms_checks(ctx)        # returns {recommendations, checks}
  → For each recommendation:
      if pricing_engine and instance_class:
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonDMS", instance_class)
        savings += monthly * 0.35       # flat 35%
      else:
        savings += 50                   # flat $50 fallback
```

### Key Code Issues Identified

1. **`services/dms.py:47`** — Detection filter `if "large" in instance_class` is overly narrow
2. **`services/dms.py:88`** — Hardcoded `"$100/month potential"` savings text
3. **`services/adapters/dms.py:40`** — Service code `"AmazonDMS"` incorrect
4. **`services/dms.py:51-57`** — CloudWatch client used but not declared in `required_clients()`
5. **`services/adapters/dms.py:42`** — No `pricing_multiplier` applied

---

## Pricing Validation

### AWS Price List API Data (eu-west-1, OnDemand, 2025-11-01)

**Service code:** `AWSDatabaseMigrationSvc` (NOT `"AmazonDMS"`)

**Instance pricing reference (monthly = hourly × 730):**

| Instance Type | Single-AZ $/hr | Single-AZ $/mo | Multi-AZ $/hr | Multi-AZ $/mo |
|---------------|---------------|----------------|---------------|---------------|
| t3.small | $0.039 | $28.47 | $0.078 | $56.94 |
| t3.medium | $0.078 | $56.94 | $0.156 | $113.88 |
| t3.large | $0.157 | $114.61 | $0.314 | $229.22 |
| c5.large | $0.175 | $127.75 | $0.350 | $255.50 |
| c6i.large | $0.175 | $127.75 | $0.350 | $255.50 |
| r5.large | $0.230 | $167.90 | $0.460 | $335.80 |
| r6i.large | $0.230 | $167.90 | $0.460 | $335.80 |
| c5.xlarge | $0.351 | $256.23 | $0.702 | $512.46 |
| c6i.xlarge | $0.351 | $256.23 | $0.702 | $512.46 |
| r5.xlarge | $0.460 | $335.80 | $0.920 | $671.60 |
| r6i.xlarge | $0.460 | $335.80 | $0.920 | $671.60 |
| r5.2xlarge | $0.920 | $671.60 | $1.840 | $1,343.20 |

**Key pricing facts from AWS documentation:**
- DMS pricing has **separate SKUs** for Single-AZ and Multi-AZ (Multi-AZ = 2× Single-AZ)
- Instance type format in pricing API: `r5.large`, `t3.large` (no `dms.` prefix)
- Instance type format from boto3 API: `dms.r5.large`, `dms.t3.large` (has `dms.` prefix)
- Storage: 100GB GP2 included for compute-optimized, 50GB for burstable; extra storage billed separately
- DMS Serverless billed per DCU-hour (1 DCU = 2GB RAM)
- Supported families: T3, T2 (burstable), C5/C6i/C7i (compute), R5/R6i/R7i (memory)
- T2 instances are legacy; T3 is current generation

### Pricing Accuracy Assessment

| Aspect | Status | Detail |
|--------|--------|--------|
| Service code | ❌ WRONG | Uses `"AmazonDMS"`, actual is `AWSDatabaseMigrationSvc` |
| Instance class format | ❌ LIKELY BROKEN | Passes `dms.r5.large` to pricing engine; API expects `r5.large` |
| Hardcoded "$100/month" | ❌ GROSSLY INACCURATE | t3.small total cost is only $29/mo; $100 savings impossible |
| Flat 35% savings ratio | ⚠️ ARBITRARY | No basis in actual rightsizing calculations |
| Flat $50 fallback | ⚠️ INACCURATE | Overstates small instances ($28/mo), understates large ($672/mo) |
| Multi-AZ awareness | ❌ MISSING | Doesn't detect or factor 2× Multi-AZ pricing |
| Storage costs | ❌ MISSING | No storage overage analysis |
| `pricing_multiplier` | ❌ NOT APPLIED | Regional adjustment ignored |

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Service code matches AWS Price List API | ❌ FAIL | `"AmazonDMS"` vs actual `AWSDatabaseMigrationSvc` |
| 2 | Instance class format compatible with pricing API | ❌ FAIL | boto3 returns `dms.r5.large`; pricing API expects `r5.large` |
| 3 | Savings estimates within ±20% of actual | ❌ FAIL | "$100/month potential" is 3.5× actual for smallest instances |
| 4 | Detection covers all instance families | ❌ FAIL | Only flags classes containing `"large"` — misses xlarge, 2xlarge, medium, small, micro |
| 5 | Multi-AZ pricing factored | ❌ FAIL | Not detected or priced differently |
| 6 | CloudWatch client declared in required_clients | ❌ FAIL | Only `("dms",)` declared; also uses `cloudwatch` |
| 7 | pricing_multiplier applied | ❌ FAIL | Not used anywhere |
| 8 | No hardcoded dollar amounts in recommendations | ❌ FAIL | `"$100/month potential"` hardcoded in service module |
| 9 | Serverless migration has meaningful analysis | ⚠️ WARN | Generic text only, no cost comparison |
| 10 | Error handling doesn't produce false recommendations | ❌ FAIL | CW failure falls through to generic recommendation without utilization data |
| 11 | Protocol compliance (ServiceModule) | ✅ PASS | Correctly extends BaseServiceModule, implements scan() |
| 12 | Returns valid ServiceFindings | ✅ PASS | Correct structure with sources dict |
| 13 | optimization_descriptions populated | ✅ PASS | Has instance_rightsizing entry |
| 14 | Module registered in ALL_MODULES | ✅ PASS | Registered correctly |
| 15 | Graceful degradation on permission errors | ⚠️ WARN | Top-level catch exists but permission issues on CW not surfaced |

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| DMS-01 | 🔴 CRITICAL | Service code `"AmazonDMS"` is wrong — actual API code is `AWSDatabaseMigrationSvc` | Pricing engine will fail to find any DMS prices; all savings fall to flat $50 fallback |
| DMS-02 | 🔴 CRITICAL | Instance class format mismatch: boto3 returns `dms.r5.large`, pricing API expects `r5.large` | Even with correct service code, pricing lookup will return None for all instances |
| DMS-03 | 🔴 HIGH | Detection only flags instances with `"large"` in class name | Misses `dms.r5.xlarge` ($336/mo), `dms.r5.2xlarge` ($672/mo), `dms.r5.4xlarge` ($1,344/mo) — the most expensive instances |
| DMS-04 | 🟠 HIGH | Hardcoded `"$100/month potential"` savings text | For t3.small ($28/mo total), claims $100 savings — 3.5× impossible. Misleads users |
| DMS-05 | 🟠 HIGH | CloudWatch client not declared in `required_clients()` | Orchestrator cannot pre-check CW permissions; silent failure at scan time |
| DMS-06 | 🟡 MEDIUM | CloudWatch failure produces false recommendations | When CW call fails, instance gets generic rightsizing recommendation without utilization evidence |
| DMS-07 | 🟡 MEDIUM | No Multi-AZ detection or pricing | Multi-AZ instances cost 2× but adapter doesn't flag unnecessary Multi-AZ for dev/test |
| DMS-08 | 🟡 MEDIUM | Flat 35% savings ratio is arbitrary | Actual rightsizing savings depend on instance family and target size; 35% may under/overstate |
| DMS-09 | 🟡 MEDIUM | `pricing_multiplier` not applied | Savings not adjusted for region (e.g., ap-south-1 is cheaper than us-east-1) |
| DMS-10 | 🔵 LOW | No storage cost analysis | DMS charges for storage above included allowance (50-100GB); not checked |
| DMS-11 | 🔵 LOW | No generation-based recommendations | Doesn't recommend upgrading T2→T3 or R5→R6i for better price/performance |
| DMS-12 | 🔵 LOW | CPU-only utilization check | DMS is often memory/I/O bound; doesn't check `FreeableMemory` or `NetworkThroughput` |
| DMS-13 | 🔵 LOW | Serverless cost comparison missing | Doesn't estimate serverless DCU cost vs provisioned instance cost |
| DMS-14 | 🔵 LOW | `describe_replication_configs` errors silently swallowed | Permission issues on serverless API not surfaced to user |

---

## Verdict

### ⚠️ WARN — 38/100

The DMS adapter has **correct structural compliance** with the ServiceModule protocol and produces valid ServiceFindings objects. However, it has **two critical pricing lookup bugs** (wrong service code and instance class format mismatch) that mean the pricing engine path likely always fails, falling back to flat-rate estimates. The detection logic is severely limited — only checking instances with `"large"` in the name — which means it misses the most expensive instances (xlarge, 2xlarge, etc.). The hardcoded `"$100/month potential"` savings text is materially misleading for small instances.

### Score Breakdown

| Category | Weight | Score | Weighted |
|----------|--------|-------|----------|
| Protocol Compliance | 15% | 95 | 14.3 |
| Pricing Accuracy | 25% | 10 | 2.5 |
| Detection Coverage | 25% | 25 | 6.3 |
| Savings Calculation | 20% | 30 | 6.0 |
| Error Handling | 15% | 60 | 9.0 |
| **Total** | **100%** | | **38.0** |

### Recommended Fixes (Priority Order)

1. **DMS-01 + DMS-02**: Fix service code to `AWSDatabaseMigrationSvc` and strip `dms.` prefix from instance class before pricing lookup
2. **DMS-03**: Remove `"large"` filter — check all running instances regardless of size
3. **DMS-04**: Replace hardcoded `$100/month` with calculated savings from pricing engine
4. **DMS-05**: Add `"cloudwatch"` to `required_clients()` return tuple
5. **DMS-06**: Don't emit rightsizing recommendations when CW data is unavailable
6. **DMS-07**: Detect Multi-AZ via `MultiAZ` field and recommend single-AZ for non-production
7. **DMS-09**: Apply `ctx.pricing_multiplier` to all calculated savings
