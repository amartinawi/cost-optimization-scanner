# AUDIT-03: RDS Service Adapter

**Date**: 2026-05-01  
**Auditor**: Automated audit via AWS Pricing API + source code review  
**Files**: `services/adapters/rds.py` (67 lines), `services/rds.py` (528 lines), `core/pricing_engine.py` (571 lines)  
**Region tested**: eu-west-1  

---

## Executive Summary

| Verdict | Count | Details |
|---------|-------|---------|
| ❌ FAIL | 2 | Storage volumeType mapping broken; Aurora backup price incorrect |
| ⚠️ WARN | 3 | Fallback constants outdated; Multi-AZ savings overestimated; Aurora dual-tier ignored |
| ✅ PASS | 4 | Compute pricing correct; Protocol compliance; parse_dollar_savings; snapshot logic |
| 🔵 INFO | 3 | Architecture notes; no engine-specific RI analysis; non-prod detection heuristic |

---

## 1. Compute Pricing Verification

### 1.1 db.t3.medium MySQL — Single-AZ vs Multi-AZ ✅ PASS

| Configuration | Hourly (USD) | Monthly (730 hrs) | Source |
|---|---|---|---|
| db.t3.medium MySQL **Single-AZ** | $0.072 | $52.56 | AWS Pricing API |
| db.t3.medium MySQL **Multi-AZ** | $0.144 | $105.12 | AWS Pricing API |
| **Ratio** | **2.00×** | **2.00×** | Exact doubling confirmed |

> Multi-AZ is exactly 2× Single-AZ for db.t3.medium MySQL in eu-west-1. This matches the industry-standard expectation.

### 1.2 Aurora MySQL db.r5.large ✅ PASS

| SKU | Hourly | Description |
|-----|--------|-------------|
| Standard | $0.320 | `RDS db.r5.large Single-AZ instance hour running Aurora MySQL` |
| IO-Optimized | $0.416 | `RDS db.r5.large IO-optimized Single-AZ instance hour running Aurora MySQL` |

> Two distinct pricing tiers exist for Aurora MySQL. The adapter's `get_instance_monthly_price("AmazonRDS", "db.r5.large")` returns the **first** API result. Which tier is returned depends on API ordering — this is non-deterministic.

### 1.3 Compute Optimizer Savings Extraction ✅ PASS

- `services/adapters/rds.py:49` — `co_recs` savings via `r.get("estimatedMonthlySavings", 0)` — correct float extraction from Compute Optimizer.
- `services/adapters/rds.py:52-53` — `enhanced_recs` savings via `parse_dollar_savings(est)` — correct regex `\$(\d+\.?\d*)` extraction.

---

## 2. Storage Pricing — CRITICAL BUG ❌ FAIL

### 2.1 volumeType Mapping Broken ❌ FAIL

**Location**: `core/pricing_engine.py:380-388` (`_fetch_rds_storage_price`)

```python
def _fetch_rds_storage_price(self, storage_type: str, *, multi_az: bool = False) -> float | None:
    deployment_option = "Multi-AZ" if multi_az else "Single-AZ"
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
        {"Type": "TERM_MATCH", "Field": "volumeType", "Value": storage_type.upper()},  # ← BUG
        {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment_option},
    ]
    return self._call_pricing_api("AmazonRDS", filters)
```

**The code sends `storage_type.upper()`** → `"GP2"`, `"GP3"`, `"IO1"`, `"IO2"`

**Actual AWS Pricing API volumeType values** (confirmed via `get_pricing_attribute_values`):

| Adapter input | Code sends | Actual API value | Match? |
|---|---|---|---|
| `"gp2"` | `"GP2"` | `"General Purpose"` or `"General Purpose (SSD)"` | ❌ NEVER |
| `"gp3"` | `"GP3"` | `"General Purpose-GP3"` | ❌ NEVER |
| `"io1"` | `"IO1"` | `"Provisioned IOPS"` or `"Provisioned IOPS (SSD)"` | ❌ NEVER |
| `"io2"` | `"IO2"` | `"Provisioned IOPS-IO2"` | ❌ NEVER |

**Impact**: Every RDS storage price lookup via the live API returns `None`, silently falling back to hardcoded constants. The Multi-AZ differentiation in `deploymentOption` is correct but never exercised because the `volumeType` filter always fails first.

**Required mapping**:

```python
_RDS_VOLUME_TYPE_MAP = {
    "gp2": "General Purpose (SSD)",  # or "General Purpose"
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS (SSD)",  # or "Provisioned IOPS"
    "io2": "Provisioned IOPS-IO2",
}
```

### 2.2 Actual vs Fallback Storage Prices ⚠️ WARN

| Type | Fallback constant | Actual API (eu-west-1) | Delta |
|---|---|---|---|
| gp2 | $0.115/GB | $0.127/GB | **+10.4% under** |
| gp3 | $0.115/GB | $0.127/GB | **+10.4% under** |
| io1 | $0.200/GB | $0.138/GB | **-31.0% OVER** |
| io2 | not in fallback (uses 0.115) | $0.138/GB | **+20.2% under** |

> The io1 fallback ($0.200) overestimates by 31% — all io1 storage savings recommendations are inflated. The gp2/gp3 fallback ($0.115) underestimates by 10% — savings appear smaller than reality.

### 2.3 Multi-AZ Storage Price Verification ⚠️ WARN

The `multi_az` parameter is correctly threaded through:
- `pricing_engine.py:200-218` — `get_rds_monthly_storage_price_per_gb(storage_type, multi_az=)` 
- `pricing_engine.py:208` — cache key includes `"Multi-AZ"` / `"Single-AZ"`
- `pricing_engine.py:381` — `deploymentOption` filter set correctly

**But**: Since the volumeType mapping is broken (see 2.1), the `deploymentOption` filter never takes effect. Both Single-AZ and Multi-AZ return the same fallback value.

**Confirmed Multi-AZ storage prices** (eu-west-1, AWS Pricing API):

| Type | Single-AZ | Multi-AZ | Ratio |
|---|---|---|---|
| GP2 (`General Purpose (SSD)`) | $0.127/GB | $0.253/GB | **1.99×** |
| GP3 (`General Purpose-GP3`) | $0.127/GB | $0.254/GB | **2.00×** |
| io1 (`Provisioned IOPS (SSD)`) | $0.138/GB | $0.276/GB | **2.00×** |

Since the fallback returns the same value for both, Multi-AZ storage costs are underreported by ~50%.

---

## 3. Backup & Snapshot Pricing

### 3.1 Backup Storage Price — Aurora vs Non-Aurora ❌ FAIL

**Location**: `core/pricing_engine.py:220-232` (`get_rds_backup_storage_price_per_gb`)

The API returns multiple `Storage Snapshot` products at different prices:

| Engine | $/GB/month |
|---|---|
| MySQL / PostgreSQL / MariaDB / Oracle / SQL Server / Db2 | $0.095 |
| **Aurora MySQL** | **$0.021** |
| **Aurora PostgreSQL** | **$0.021** |

The code filters only by `productFamily=Storage Snapshot` and takes the **first result**, which is always $0.095 (non-Aurora). Aurora backup costs are overestimated by **4.5×**.

**Impact**: Aurora cluster snapshot cleanup recommendations (lines 478-516 in `services/rds.py`) show inflated savings.

### 3.2 Snapshot Cost Estimation ✅ PASS

**Location**: `services/rds.py:417-435` (DB snapshots), `services/rds.py:486-516` (Aurora cluster snapshots)

Formula: `allocated_storage_gb × backup_price_per_gb`

- Correctly uses `ctx.pricing_engine.get_rds_backup_storage_price_per_gb()` when available
- Falls back to `0.095 × pricing_multiplier` on API failure
- Described as "coarse estimate" — appropriate since actual snapshot size ≠ allocated storage

---

## 4. Savings Calculation Patterns

### 4.1 parse_dollar_savings() Usage ✅ PASS

**Location**: `services/_savings.py:8-16`

```python
def parse_dollar_savings(savings_str: str) -> float:
    match = re.search(r"\$(\d+\.?\d*)", savings_str)
    return float(match.group(1)) if match else 0.0
```

Applied in `services/adapters/rds.py:52-53` with guard `"$" in est and "/month" in est`.

**Verified patterns that parse correctly**:
- `"$12.50/month"` → 12.50 ✅
- `"~50% of instance cost"` → 0.0 (no `$` sign, correctly skipped) ✅
- `"65-75% of compute costs"` → 0.0 (no `$` sign, correctly skipped) ✅
- `"Up to 60% savings"` → 0.0 (correctly skipped) ✅

### 4.2 Non-Dollar Savings — Recommendations Without Quantified Savings ⚠️ WARN

Several check categories produce recommendations with non-quantified savings strings:

| Check | EstimatedSavings | Parsed |
|---|---|---|
| `multi_az_unnecessary` | `"~50% of instance cost"` | 0.0 |
| `non_prod_scheduling` | `"65-75% of compute costs"` | 0.0 |
| `aurora_serverless_candidates` | `"Pay only for capacity used (up to 90%...)"` | 0.0 |
| `reserved_instances` | `"Up to 60% savings..."` | 0.0 |
| `idle_databases` (stopped) | `"Storage costs continue..."` | 0.0 |

These contribute recommendations to the count but **$0.00** to `total_monthly_savings`. Users may see high recommendation counts with low total savings, reducing credibility.

### 4.3 gp2→gp3 Storage Savings ⚠️ WARN

**Location**: `services/rds.py:272-293`

```python
gp2_price = ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp2", multi_az=multi_az)
monthly_cost = allocated_storage * gp2_price
savings = monthly_cost * 0.20  # hardcoded 20%
```

- Since the API lookup always fails (volumeType bug), this uses the fallback $0.115/GB
- The 20% savings factor is hardcoded — actual gp2 vs gp3 prices are identical ($0.127 each in eu-west-1)
- **In eu-west-1, there are NO savings from gp2→gp3 on a per-GB basis** — the benefit is in included IOPS (gp3 includes 3,000 IOPS vs gp2's baseline). The savings come from avoiding Provisioned IOPS charges, not from a storage price difference.

---

## 5. Aurora vs Non-Aurora Handling

### 5.1 Dual Aurora Pricing Tiers 🔵 INFO

The API returns two distinct Aurora MySQL instance prices for db.r5.large:
- **Standard**: $0.320/hr
- **IO-Optimized**: $0.416/hr (30% premium)

The adapter uses `get_instance_monthly_price("AmazonRDS", instance_class)` which takes the first API result — this is non-deterministic. Aurora IO-Optimized is a separate deployment choice that should be explicitly handled.

### 5.2 Aurora-Specific Checks ✅ PASS

- Aurora Serverless v2 candidate detection: Checks for small instance classes (`db.t3.small`, `db.t3.medium`, `db.t4g.small`, `db.t4g.medium`) ✅
- Aurora cluster Serverless v2 migration check: Detects clusters without `ServerlessV2ScalingConfiguration` ✅
- Aurora storage type review: Flags `storageType == "aurora"` for I/O-Optimized review ✅

---

## 6. Architecture & Protocol Compliance ✅ PASS

| Aspect | Status | Notes |
|---|---|---|
| `ServiceModule` Protocol | ✅ | Implements `required_clients()` and `scan()` |
| `ServiceFindings` return | ✅ | Returns typed `ServiceFindings` with 2 sources |
| Source decomposition | ✅ | `compute_optimizer` + `enhanced_checks` SourceBlocks |
| `optimization_descriptions` | ✅ | 4 description categories provided |
| `extras` metadata | ✅ | Includes `instance_counts` |
| Client requirements | ✅ | `("rds", "compute-optimizer")` declared |
| Error resilience | ✅ | All AWS API calls wrapped in try/except with fallbacks |

---

## 7. Non-Prod Detection Heuristic 🔵 INFO

**Location**: `services/rds.py:192-228`

Multi-AZ review uses a two-tier detection:
1. **Tag-based**: Checks `Environment`/`Stage`/`Env` tags
2. **Name-based fallback**: Scans DB identifier for `dev`, `test`, `staging`, `qa` substrings

This is a reasonable heuristic but may produce false positives (e.g., a production DB named `order-devotion-db`).

---

## 8. Findings Summary

| # | Severity | Category | Finding |
|---|---|---|---|
| F-01 | ❌ CRITICAL | Pricing | `volumeType` filter sends `"GP2"`/`"GP3"`/`"IO1"` but API expects `"General Purpose (SSD)"`/`"General Purpose-GP3"`/`"Provisioned IOPS (SSD)"`. All RDS storage API lookups fail silently → always uses fallback. |
| F-02 | ❌ HIGH | Pricing | `get_rds_backup_storage_price_per_gb()` returns $0.095 for all engines. Aurora backup is $0.021/GB (4.5× cheaper). Aurora snapshot savings are overstated. |
| F-03 | ⚠️ MEDIUM | Pricing | Fallback constants are outdated: io1 at $0.200 vs actual $0.138 (+31% over); gp2/gp3 at $0.115 vs actual $0.127 (-10% under). |
| F-04 | ⚠️ MEDIUM | Accuracy | gp2→gp3 savings hardcoded at 20%. In many regions, gp2 and gp3 have identical per-GB pricing. Savings are from IOPS, not storage. |
| F-05 | ⚠️ LOW | Completeness | Non-quantified savings estimates (`"~50% of instance cost"`, `"65-75%"`) contribute to recommendation count but $0.00 to total savings. |
| F-06 | 🔵 INFO | Architecture | No engine-specific Reserved Instance analysis. RI recommendations are generic ("Up to 60% savings") without checking existing RI coverage. |
| F-07 | 🔵 INFO | Edge case | Aurora IO-Optimized tier ($0.416/hr vs $0.320/hr standard) not differentiated — `get_instance_monthly_price` returns whichever API result comes first. |

---

## 9. AWS Documentation Cross-Reference

Per AWS docs (confirmed via search):

1. **Multi-AZ pricing**: "Multi-AZ deployments are priced per DB instance-hour consumed" — confirmed 2× Single-AZ for compute. Storage also doubles.
2. **Reserved Instances**: "Save up to 69% over On-Demand rates" — RI recommendations cite "up to 60%", slightly conservative but reasonable.
3. **Backup pricing**: "Backup storage associated with automated database backups and active database snapshots, billed in GB-month" — $0.095 for non-Aurora, $0.021 for Aurora in eu-west-1.
4. **Storage types**: RDS supports gp2, gp3, io1, io2, Magnetic — all confirmed in API volumeType values.

---

## 10. Recommended Remediation Priority

| Priority | Finding | Fix |
|---|---|---|
| **P0** | F-01 | Add `_RDS_VOLUME_TYPE_MAP` dict in `pricing_engine.py` and use it in `_fetch_rds_storage_price` instead of `storage_type.upper()` |
| **P1** | F-02 | Add `engine` parameter to `get_rds_backup_storage_price_per_gb()` and filter by `databaseEngine` for Aurora-specific pricing |
| **P2** | F-03 | Update `FALLBACK_RDS_STORAGE_GB_MONTH` to current eu-west-1 values: `{"gp2": 0.127, "gp3": 0.127, "io1": 0.138, "io2": 0.138}` |
| **P3** | F-04 | Calculate gp2→gp3 savings based on IOPS included rather than flat 20% storage discount |
| **P4** | F-05 | Add per-instance dollar savings estimates for Multi-AZ and non-prod scheduling checks using live pricing |
