# DynamoDB Adapter Audit Report

**Audit ID:** AUDIT-07-DynamoDB
**Date:** 2026-05-01
**Auditor:** OpenCode Agent (glm-5.1)
**Adapter:** `services/adapters/dynamodb.py` (81 lines)
**Legacy Module:** `services/dynamodb.py` (439 lines)

---

## Executive Summary

| # | Check | Severity | Verdict Code | Finding |
|---|-------|----------|--------------|---------|
| 1 | ProvisionedThroughput key mismatch | **CRITICAL** | `BUG-KEY-DYNAMODB-001` | Adapter reads nested key that doesn't exist; hourly pricing path is dead code |
| 2 | On-demand EstimatedMonthlyCost=0 | **HIGH** | `BUG-SAVINGS-DYNAMODB-002` | On-demand tables always contribute $0 savings |
| 3 | RCU/WCU pricing constants | **MEDIUM** | `WARN-PRICE-DYNAMODB-003` | Constants 11.6% below actual eu-west-1 rates |
| 4 | 23% reserved capacity discount | **LOW** | `NOTE-DISCOUNT-DYNAMODB-004` | Conservative vs AWS documented 54-77% |
| 5 | Table detection coverage | **PASS** | `PASS-DETECT-DYNAMODB-005` | Both provisioned and on-demand tables scanned |
| 6 | Shim pricing constants | **MEDIUM** | `WARN-SHIM-DYNAMODB-006` | Legacy module uses 2.33x inflated monthly rates |

**Overall Verdict:** `FAIL` — Critical key-mismatch bug renders the adapter's intended pricing path unreachable. All savings flow through the unintended fallback path using inflated legacy constants.

---

## 1. CRITICAL: ProvisionedThroughput Key Mismatch

### The Bug

The adapter reads `ProvisionedThroughput` as a **nested key** from the shim output, but the shim places `ReadCapacityUnits` and `WriteCapacityUnits` at the **top level** of the dict.

**Adapter (lines 51-54):**
```python
throughput = rec.get("ProvisionedThroughput", {})  # Always returns {}
rcu = throughput.get("ReadCapacityUnits", 0)         # Always 0
wcu = throughput.get("WriteCapacityUnits", 0)         # Always 0
```

**Shim output structure (`services/dynamodb.py` lines 106-116):**
```python
table_info = {
    "TableName": table_name,
    "BillingMode": ...,
    "TableStatus": ...,
    "ItemCount": ...,
    "TableSizeBytes": ...,
    "ReadCapacityUnits": 0,       # <-- TOP LEVEL, not nested
    "WriteCapacityUnits": 0,       # <-- TOP LEVEL, not nested
    "EstimatedMonthlyCost": 0,
    "OptimizationOpportunities": [],
}
```

The shim reads `ProvisionedThroughput` from the **raw AWS API response** (line 119) and flattens `ReadCapacityUnits`/`WriteCapacityUnits` into the top-level dict. The adapter expects the raw AWS shape but receives the flattened shim shape.

### Consequence

The `if rcu > 0 or wcu > 0:` branch (lines 56-58) is **never entered** for ANY table. Every table falls through to the else branch:

```python
else:
    cost = rec.get("EstimatedMonthlyCost", 0)
    savings += cost * 0.30
```

This means:
- **The adapter's own RCU/WCU hourly constants ($0.00013/$0.00065) are dead code** — never used
- **The `pricing_multiplier` is never applied** to DynamoDB savings
- **The 23% reserved capacity discount is never applied** — 30% is used instead
- All savings come from the shim's `EstimatedMonthlyCost × 0.30`

**Verdict Code:** `BUG-KEY-DYNAMODB-001` — Critical key-mismatch, dead pricing path.

---

## 2. On-Demand Tables: $0 Savings Bug

### Root Cause

The shim initializes `EstimatedMonthlyCost = 0` for ALL tables (line 114), and only populates it for PROVISIONED tables (lines 123-126). On-demand tables never get a cost estimate.

**Shim flow:**
```python
# Line 114: initialized for ALL tables
table_info["EstimatedMonthlyCost"] = 0

# Lines 118-126: ONLY provisioned tables
if table_info["BillingMode"] == "PROVISIONED":
    monthly_cost = (RCU * 0.25) + (WCU * 1.25)
    table_info["EstimatedMonthlyCost"] = round(monthly_cost, 2)  # ✅ Set
else:
    analysis["on_demand_tables"].append(table_name)
    # ❌ EstimatedMonthlyCost stays at 0
```

**Combined with Bug #1**, the adapter savings calculation becomes:
- **Provisioned tables:** `savings += EstimatedMonthlyCost × 0.30` (non-zero, but using inflated shim constants)
- **On-demand tables:** `savings += 0 × 0.30 = $0`

### Impact

- Accounts with only on-demand DynamoDB tables report **$0 DynamoDB savings**
- The enhanced checks (`get_enhanced_dynamodb_checks`) DO produce recommendations for billing mode optimization, but these carry only text-based `EstimatedSavings` strings ("Up to 60%"), not numeric values that flow to the grand total
- No `$50 flat rate` fallback exists in the current code — the task description referenced this but it is absent

**Verdict Code:** `BUG-SAVINGS-DYNAMODB-002` — On-demand tables produce $0 savings.

---

## 3. RCU/WCU Pricing Constants Verification

### Adapter Constants (Dead Code — See Bug #1)

```python
DYNAMODB_RCU_HOURLY = 0.00013   # line 47
DYNAMODB_WCU_HOURLY = 0.00065   # line 48
```

### AWS Pricing API — eu-west-1 (Verified 2025-08-28)

| Metric | Adapter | AWS Actual | Delta |
|--------|---------|------------|-------|
| RCU/hour | $0.000130 | $0.000147 | **-11.6% LOW** |
| WCU/hour | $0.000650 | $0.000735 | **-11.6% LOW** |

**Source:** AWS Price List API, service code `AmazonDynamoDB`
- RCU: SKU `J7FH9ZNSN6J2RR9Q`, usagetype `EU-ReadCapacityUnit-Hrs`
- WCU: SKU `36CGV8QUHJZG65HD`, usagetype `EU-WriteCapacityUnit-Hrs`

### Monthly Impact (If Constants Were Actually Used)

For a table with 100 RCU + 50 WCU:
```
Adapter: (100 × 0.00013 + 50 × 0.00065) × 730 = $33.29/month
AWS:     (100 × 0.000147 + 50 × 0.000735) × 730 = $37.59/month
Gap:     $4.30/month per table (11.6% understated)
```

**Verdict Code:** `WARN-PRICE-DYNAMODB-003` — Constants stale but currently dead code due to Bug #1.

---

## 4. Reserved Capacity Discount Analysis

### Adapter Uses 23% (Dead Code)

```python
savings += monthly_current * 0.23   # line 58 — never reached
```

### AWS Documentation

Per [DynamoDB Reserved Capacity docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/reserved-capacity.html):

> "You can save up to **54% off standard rates for a one-year term** and **77% off standard rates for a three-year term**."

### AWS Pricing API — Actual Reserved Rates (eu-west-1)

#### RCU Reserved Pricing

| Term | Hourly Rate | Upfront/Unit | Effective Monthly | Savings vs On-Demand |
|------|------------|--------------|-------------------|---------------------|
| On-Demand | $0.000147 | — | $0.1073 | — |
| 1-year | $0.000029 | $0.339 | $0.0494 | **53.9%** |
| 3-year | $0.000018 | $0.4068 | $0.0244 | **77.2%** |

#### WCU Reserved Pricing

| Term | Hourly Rate | Upfront/Unit | Effective Monthly | Savings vs On-Demand |
|------|------------|--------------|-------------------|---------------------|
| On-Demand | $0.000735 | — | $0.5366 | — |
| 1-year | $0.000145 | $1.695 | $0.2471 | **53.9%** |
| 3-year | $0.000092 | $2.034 | $0.1237 | **76.9%** |

Effective monthly = (hourly × 730) + (upfront ÷ months in term)

### Assessment

| Metric | 1-Year Actual | 3-Year Actual | Adapter (23%) |
|--------|--------------|---------------|---------------|
| Discount Rate | 54% | 77% | 23% |
| % of 1-year rate | — | — | 42.6% |
| % of 3-year rate | — | — | 29.9% |

The 23% is **not documented** as a specific DynamoDB Reserved Capacity tier. It appears to be a conservative placeholder. The adapter's own description text says "53-76%" which contradicts the 23% calculation.

**Verdict Code:** `NOTE-DISCOUNT-DYNAMODB-004` — 23% conservative; dead code anyway due to Bug #1.

---

## 5. Table Detection: Provisioned vs On-Demand

### Coverage Analysis

Both billing modes are scanned in the shim:

```python
# services/dynamodb.py lines 118-145
if table_info["BillingMode"] == "PROVISIONED":
    analysis["provisioned_tables"].append(table_name)
    # Gets RCU/WCU, EstimatedMonthlyCost, optimization flags
else:
    analysis["on_demand_tables"].append(table_name)
    # Gets monitoring recommendation only
```

### Enhanced Checks (`get_enhanced_dynamodb_checks`)

| Check Category | Target | CloudWatch? | Numeric Savings? |
|----------------|--------|-------------|------------------|
| `billing_mode_optimization` | On-demand → Provisioned | Yes (14-day) | No (text only) |
| `capacity_rightsizing` | Provisioned | Yes (7-day) | No (text only) |
| `reserved_capacity` | High-capacity provisioned | No | No (text: "53-76%") |
| `data_lifecycle` | Tables >10GB | No | No (text: "40-80%") |
| `unused_tables` | Empty tables | No | No (text: "100%") |
| `over_provisioned_capacity` | RCU/WCU >100 | Yes (7-day) | No (text: "Variable") |
| `global_tables_optimization` | Not implemented | — | — |

The enhanced checks produce recommendations with **text-based `EstimatedSavings`** strings that do NOT flow into the `total_monthly_savings` dollar figure. Only the `optimization_opportunities` from `get_dynamodb_table_analysis` contribute to savings.

**Verdict Code:** `PASS-DETECT-DYNAMODB-005` — Both table types scanned. Enhanced checks are text-only.

---

## 6. Shim Pricing Constants (Actually Used)

Because of Bug #1, the ACTUAL savings path uses the shim's `EstimatedMonthlyCost`:

**Shim constants (`services/dynamodb.py` lines 70-71):**
```python
_PROVISIONED_RCU_COST: float = 0.25   # per unit/month
_PROVISIONED_WCU_COST: float = 1.25   # per unit/month
```

### Comparison with AWS Actual

| Metric | Shim Monthly | AWS Actual Monthly | Ratio |
|--------|-------------|-------------------|-------|
| RCU/unit/month | $0.25 | $0.1073 | **2.33x over** |
| WCU/unit/month | $1.25 | $0.5366 | **2.33x over** |

The shim constants are **2.33× the actual AWS provisioned capacity rates** in eu-west-1.

### Effective Savings Calculation

Since Bug #1 forces all provisioned tables through `EstimatedMonthlyCost × 0.30`:

```
Actual savings reported = shim_monthly_cost × 0.30
                        = (RCU × 0.25 + WCU × 1.25) × 0.30
```

For a table with 100 RCU + 50 WCU:
```
Shim monthly: 100 × 0.25 + 50 × 1.25 = $87.50
Adapter savings: $87.50 × 0.30 = $26.25

AWS actual monthly: $37.59
True 1-year reserved savings: $37.59 × 0.54 = $20.27

Overstatement: $26.25 vs $20.27 = +29.5%
```

The inflated shim constants + 30% discount rate overstates savings vs. what would be computed with accurate pricing + proper discount rate.

**Verdict Code:** `WARN-SHIM-DYNAMODB-006` — Legacy shim constants 2.33x actual, producing overstated savings.

---

## 7. Data Flow Diagram

```
AWS DescribeTable API
        │
        ▼
┌─────────────────────────────────┐
│  services/dynamodb.py (shim)    │
│                                 │
│  table_info = {                 │
│    "ReadCapacityUnits": N,  ←──┼── TOP LEVEL
│    "WriteCapacityUnits": N, ←──┼── TOP LEVEL
│    "EstimatedMonthlyCost": N   │
│  }                              │
│                                 │
│  (NO "ProvisionedThroughput"    │
│   nested key in output)         │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  adapters/dynamodb.py           │
│                                 │
│  throughput = rec.get(          │
│    "ProvisionedThroughput", {}) │
│       ↑                         │
│       └── ALWAYS {} ────────────┼── BUG: key doesn't exist
│                                 │
│  rcu = throughput.get(          │
│    "ReadCapacityUnits", 0)      │
│       └── ALWAYS 0 ─────────────┼── BUG: dead path
│                                 │
│  if rcu > 0 or wcu > 0:        │
│    ← NEVER ENTERED              │
│  else:                          │
│    cost = EstimatedMonthlyCost  │
│    savings += cost × 0.30       │
│                                 │
│  Result:                        │
│  - Provisioned: shim_cost×0.30 │
│  - On-demand: $0.00             │
└─────────────────────────────────┘
```

---

## 8. Recommendations

| Priority | # | Action | Impact |
|----------|---|--------|--------|
| **P0** | 1 | Fix key mismatch: change `rec.get("ProvisionedThroughput", {})` to read top-level `ReadCapacityUnits`/`WriteCapacityUnits` directly | Activates correct pricing path |
| **P0** | 2 | Populate `EstimatedMonthlyCost` for on-demand tables in shim (use CloudWatch consumption data or on-demand pricing API) | On-demand savings flow to total |
| **P1** | 3 | Update RCU/WCU hourly constants: `$0.000147`, `$0.000735` for eu-west-1 | 11.6% more accurate |
| **P1** | 4 | Migrate to `ctx.pricing_engine` for live DynamoDB pricing lookup (like EC2/EBS/RDS adapters) | Region-accurate pricing |
| **P2** | 5 | Replace 23% hardcode with 54% (1-year) or make configurable | Aligned with AWS docs |
| **P2** | 6 | Fix shim monthly constants ($0.25/$1.25) to match AWS actual ($0.107/$0.537) | Correct EstimatedMonthlyCost |
| **P3** | 7 | Add free tier awareness (first 25 RCU/WCU) | Accurate for small tables |

---

## 9. Verdict Codes Summary

| Code | Severity | Description | Status |
|------|----------|-------------|--------|
| `BUG-KEY-DYNAMODB-001` | **CRITICAL** | `ProvisionedThroughput` key mismatch — hourly pricing path is dead code | Must fix |
| `BUG-SAVINGS-DYNAMODB-002` | **HIGH** | On-demand `EstimatedMonthlyCost` always 0 — $0 savings reported | Must fix |
| `WARN-PRICE-DYNAMODB-003` | **MEDIUM** | RCU/WCU constants 11.6% below eu-west-1 actual | Update when fixing #1 |
| `NOTE-DISCOUNT-DYNAMODB-004` | **LOW** | 23% discount conservative vs AWS 54-77% | Currently dead code |
| `PASS-DETECT-DYNAMODB-005` | **PASS** | Both provisioned and on-demand tables scanned | OK |
| `WARN-SHIM-DYNAMODB-006` | **MEDIUM** | Legacy shim constants 2.33× actual AWS rates | Align with pricing engine |

---

## 10. Overall Verdict

**Status:** `FAIL` 🔴

The adapter contains a **critical key-mismatch bug** (`BUG-KEY-DYNAMODB-001`) that renders its intended pricing calculation path completely unreachable. All dollar savings flow through an unintended fallback using inflated legacy constants, and on-demand tables produce $0 savings regardless of actual optimization potential.

### What Works
- Table detection for both billing modes
- Enhanced checks with CloudWatch metric analysis
- Correct data structure shape for HTML report rendering
- Proper exception handling and warning propagation

### What Doesn't Work
- The adapter's own RCU/WCU hourly pricing constants (dead code)
- The `pricing_multiplier` regional adjustment (never applied)
- The 23% reserved capacity discount (never applied)
- On-demand table dollar savings (always $0)

### Safe to Use?
The adapter produces **non-zero savings** for provisioned tables via the fallback path (`EstimatedMonthlyCost × 0.30`), but these savings are computed using 2.33× inflated constants and a different discount rate (30% vs intended 23%). The numbers that appear in reports are therefore overstated by approximately 29.5% for provisioned tables, and on-demand tables are completely missing from savings totals.

---

## Appendix A: AWS Pricing API Evidence

### On-Demand Provisioned Rates (eu-west-1)

```
Service:     AmazonDynamoDB
Region:      eu-west-1 (EU Ireland)
Fetched:     2025-08-28 pricing data

RCU Provisioned:
  SKU:       J7FH9ZNSN6J2RR9Q
  UsageType: EU-ReadCapacityUnit-Hrs
  Rate:      $0.000147/RCU-hour (beyond 18,600 free tier)
  Free tier: $0.00 for first 18,600 RCU-hours/month

WCU Provisioned:
  SKU:       36CGV8QUHJZG65HD
  UsageType: EU-WriteCapacityUnit-Hrs
  Rate:      $0.000735/WCU-hour (beyond 18,600 free tier)
  Free tier: $0.00 for first 18,600 WCU-hours/month
```

### Reserved Capacity Rates (eu-west-1)

```
RCU 1-year Heavy Utilization:
  SKU:       J7FH9ZNSN6J2RR9Q.YTVHEVGPBZ
  Hourly:    $0.000029/RCU-hour
  Upfront:   $0.339/RCU

RCU 3-year Heavy Utilization:
  SKU:       J7FH9ZNSN6J2RR9Q.VG8YD49WWM
  Hourly:    $0.000018/RCU-hour
  Upfront:   $0.4068/RCU

WCU 1-year Heavy Utilization:
  SKU:       36CGV8QUHJZG65HD.YTVHEVGPBZ
  Hourly:    $0.000145/WCU-hour
  Upfront:   $1.695/WCU

WCU 3-year Heavy Utilization:
  SKU:       36CGV8QUHJZG65HD.VG8YD49WWM
  Hourly:    $0.000092/WCU-hour
  Upfront:   $2.034/WCU
```

## Appendix B: AWS Documentation Evidence

Source: [DynamoDB Reserved Capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/reserved-capacity.html)

> "Reserved capacity is purchased in allocations of 100 WCUs or 100 RCUs. The smallest reserved capacity offering is 100 capacity units (reads or writes). DynamoDB reserved capacity is offered as either a one-year commitment or in select Regions as a three-year commitment. You can save up to **54% off standard rates for a one-year term** and **77% off standard rates for a three-year term**."

Confirmed: The API data shows effective savings of 53.9% (1-year) and 77.1% (3-year), consistent with AWS documentation.

---

*Report generated by OpenCode Agent (glm-5.1) following AWS Cost Scanner audit protocol.*
*Pricing data sourced from AWS Price List API (publication date 2025-08-28).*
