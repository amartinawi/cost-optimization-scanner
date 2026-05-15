# Audit: dynamodb Adapter

**Adapter**: `services/adapters/dynamodb.py` (82 lines)
**Legacy shim**: `services/dynamodb.py` (502 lines) — analysis lives here
**ALL_MODULES index**: 6
**Renderer path**: **Phase B** — both sources route to `_render_dynamodb_enhanced_checks` (`reporter_phase_b.py:2184-2185`)
**Date**: 2026-05-15
**Auditor**: automated (per `docs/audits/AUDIT_PROMPT.md`)

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **FAIL** | 7 (1 CRITICAL, 4 HIGH, 2 MEDIUM) |
| L2 Calculation | **FAIL** | 6 (2 CRITICAL, 3 HIGH, 1 MEDIUM) |
| L3 Reporting   | **WARN** | 4 (2 HIGH, 1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | on-demand rate 10× too high + provisioned rate 13% too high + protocol lies about CW |

---

## Pre-flight facts

- **Adapter class**: `DynamoDbModule` at `services/adapters/dynamodb.py:16`
- **Declared `required_clients`**: `("dynamodb",)` — **lies**: shim calls `ctx.client("cloudwatch")` at six sites
- **Declared `requires_cloudwatch`**: `False` (default inherited from `BaseServiceModule`) — **lies**: shim hits CloudWatch GetMetricStatistics
- **Declared `reads_fast_mode`**: `True` — **lies**: `fast_mode` never referenced anywhere in adapter or shim
- **Sources emitted**: `{"dynamodb_table_analysis", "enhanced_checks"}`
- **Pricing methods consumed**: **none** from `PricingEngine`. Adapter and shim hold parallel hardcoded constants — see L2-001
- **Test files**: `tests/test_offline_scan.py`, `tests/test_regression_snapshot.py`, `tests/test_reporter_snapshots.py`

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `cloudwatch`  [CRITICAL]
- **Check**: L1.1.4 — required_clients must list **every** boto3 service used inside scan()
- **Evidence**: `services/adapters/dynamodb.py:26` returns `("dynamodb",)`; shim calls `ctx.client("cloudwatch")` at `services/dynamodb.py:144, 288, 374` plus `cloudwatch.get_metric_statistics(...)` at lines 147, 156, 292, 302, 378, 388
- **Why it matters**: `ClientRegistry` constructs clients lazily through the declared `required_clients` set. Omitting `cloudwatch` here means (i) IAM previews under `--scan-only=dynamodb` show the wrong policy surface, (ii) any cross-service permission audit (`docs/audits/SUMMARY.md` X1) gets the wrong adapter→action mapping, (iii) if `ClientRegistry` were ever tightened to refuse undeclared clients, dynamodb would crash mid-scan. Today the call works only because `ClientRegistry.client()` accepts any name
- **Recommended fix**: change return to `("dynamodb", "cloudwatch")`

### L1-002 `requires_cloudwatch` flag is False but shim is CloudWatch-heavy  [HIGH]
- **Check**: L1.1.5 — flag must reflect code
- **Evidence**: default value `False` from `BaseServiceModule` (`services/_base.py:24`) is not overridden on `DynamoDbModule`. Shim makes 6 `get_metric_statistics` calls per table
- **Why it matters**: `cost_optimizer.py` and orchestrator can use this flag to short-circuit CW-bound work in `--fast` mode; with the lie, DynamoDB silently keeps making CW calls in fast scans. Also confuses the scope-of-IAM audit
- **Recommended fix**: set `requires_cloudwatch: bool = True` on `DynamoDbModule`

### L1-003 `reads_fast_mode` flag is True but `ctx.fast_mode` is never read  [HIGH]
- **Check**: L1.1.6 — flag must reflect code
- **Evidence**: `services/adapters/dynamodb.py:22` declares `reads_fast_mode: bool = True`; `grep "fast_mode" services/dynamodb.py services/adapters/dynamodb.py` → only the adapter declaration; **no consumer**
- **Why it matters**: protocol contract drift — orchestrator may believe the adapter trims CW work in fast mode when it actually doesn't. Combined with L1-002 (CW is heavily used), this is a double misrepresentation
- **Recommended fix**: either (a) make `get_dynamodb_table_analysis` and `get_enhanced_dynamodb_checks` skip CW calls when `ctx.fast_mode` is True (preferred — fast mode should be honest about cost), or (b) flip to `reads_fast_mode = False`

### L1-004 Module-level `print()` calls in adapter and shim  [HIGH]
- **Evidence**:
  - `services/adapters/dynamodb.py:41` — adapter banner
  - `services/dynamodb.py:15` — **module-level** print fires on every import
- **Recommended fix**: route adapter banner to `logger.debug`; delete the module-level print

### L1-005 No `AccessDenied` / `UnauthorizedOperation` discrimination  [HIGH]
- **Check**: L1.2.3 — IAM gaps must route through `ctx.permission_issue`
- **Evidence**: every `except` in `services/dynamodb.py` routes to `ctx.warn` (lines 191, 195, 491, 494) or to a swallow-and-fallback path (`except Exception: pass` at line 177, 346, 463). Zero references to `ctx.permission_issue` or `AccessDenied` discrimination
- **Why it matters**: accounts missing `dynamodb:DescribeTable`, `dynamodb:ListTables`, or `cloudwatch:GetMetricStatistics` produce blank warnings. The HTML "Permission Issues" section stays empty
- **Recommended fix**: classify exceptions; route to `ctx.permission_issue(action="dynamodb:DescribeTable")` etc.

### L1-006 Three silent `except Exception` blocks substitute fabricated values  [HIGH]
- **Check**: L1.2.2 — every except must record or re-raise
- **Evidence**:
  - `services/dynamodb.py:177-178` — CW fails → `table_info["EstimatedMonthlyCost"] = 25.0` (invented)
  - `services/dynamodb.py:346-347` — CW fails → `recommendation_text = "High provisioned capacity - CloudWatch analysis recommended"` (no warn)
  - `services/dynamodb.py:463-478` — CW fails → emits a placeholder recommendation that says "CloudWatch analysis required" (no warn)
- **Why it matters**: a permission denial on `cloudwatch:GetMetricStatistics` looks indistinguishable from "metrics not present yet" — the user has no way to know the analysis is broken. The `$25.0` fallback at line 176/178 also seeps into `total_monthly_savings`
- **Recommended fix**: replace each `except Exception:` with classification + `ctx.warn`/`ctx.permission_issue`. Remove the $25 fabrication

### L1-007 `total_count` extras missing — `extras.table_counts` instead of `total_count`  [MEDIUM]
- **Check**: L2.5.3 / L3.1.1 — `ServiceFindings.total_count` should be set when the adapter has a meaningful scan-population count
- **Evidence**: `services/adapters/dynamodb.py:65-81` returns `ServiceFindings` without setting `total_count=`; the table population count lives in `extras.table_counts.total`. Sibling adapters (S3, EBS, EC2) set `total_count` directly so regression tests can assert it
- **Why it matters**: `tests/test_regression_snapshot.py::TestServiceFindings::test_ami_has_total_count` exists for AMI; the equivalent assertion for DynamoDB would need to dig into `extras` instead of reading the canonical field. Drift in invariant
- **Recommended fix**: set `total_count=dynamodb_data.get("total_tables", 0)` on the `ServiceFindings` return

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| `dynamodb_table_analysis` provisioned EstimatedMonthlyCost | `module-const` ($0.107/RCU-mo, $0.537/WCU-mo) | `services/dynamodb.py:70-71, 123-125` | **FAIL** — AWS list price is $0.0949/RCU-mo, $0.4745/WCU-mo → **+12.7% / +13.2% inflated** | AWS Pricing API → $0.00013/RCU-hr × 730 = $0.0949; $0.00065/WCU-hr × 730 = $0.4745 |
| `dynamodb_table_analysis` on-demand EstimatedMonthlyCost | `module-const` ($0.00000125/RCU-req, $0.00000625/WCU-req) | `services/dynamodb.py:170-171` | **CRITICAL FAIL** — **10× too high**. AWS list is $0.125 per *million* RRU = $0.000000125/RRU; $0.625/M WRU = $0.000000625/WRU. Shim is off by an order of magnitude | AWS Pricing API → $0.0000001250 USD/RRU, $0.0000006250 USD/WRU |
| `dynamodb_table_analysis` on-demand fallback | `arbitrary` ($25.0) | `services/dynamodb.py:176, 178` | **FAIL** — invented constant when CW returns no data points OR fails | n/a |
| Adapter savings rollup, provisioned path | `derived` (`monthly × 0.23`) | `services/adapters/dynamodb.py:57-58` | **FAIL** — `0.23` has no documented basis; reserved-capacity discount is 53-76% (`docs/audits/SUMMARY.md` and shim line 367), not 23% | n/a |
| Adapter savings rollup, non-provisioned path | `derived` (`cost × 0.30`) | `services/adapters/dynamodb.py:60-61` | **FAIL** — `0.30` is an arbitrary multiplier applied to whichever cost the shim happened to produce (which is already 10× too high on the on-demand path) | n/a |
| `enhanced_checks.*.EstimatedSavings` | `arbitrary` (strings like "53-76% vs On-Demand", "Up to 60%", "Variable based on actual usage", "100% of table costs", "40-80% on storage costs") | `services/dynamodb.py:276, 355, 367, 440, 455, 473, 486` | **FAIL** — every enhanced-check rec emits a non-dollar string; none contribute to `total_monthly_savings` (adapter does not read `EstimatedMonthlySavings` from them); scope-rule violation | n/a |

### L2-001 Provisioned RCU/WCU rates 13% above AWS list price  [HIGH]
- **Check**: L2.1 / L2.2 — `module-const` must match AWS-published price
- **Evidence**: shim and adapter together hold four constants:
  - `services/dynamodb.py:70` — `_PROVISIONED_RCU_COST = 0.107` ($/RCU-mo)
  - `services/dynamodb.py:71` — `_PROVISIONED_WCU_COST = 0.537` ($/WCU-mo)
  - `services/adapters/dynamodb.py:48` — `DYNAMODB_RCU_HOURLY = 0.000147` ($/RCU-hr)
  - `services/adapters/dynamodb.py:49` — `DYNAMODB_WCU_HOURLY = 0.000735` ($/WCU-hr)

  All four are pre-2017 prices. AWS lowered DynamoDB rates and the current published values (verified via Pricing API) are:
  - RCU: $0.00013/hr ($0.0949/mo at 730 hrs)
  - WCU: $0.00065/hr ($0.4745/mo at 730 hrs)
- **Why it matters**: every provisioned-table cost the shim reports is **+13% inflated**. Because the adapter then multiplies by `* 0.23`, the savings are also +13% inflated. Same direction on every account
- **Recommended fix**: replace all four constants with a `PricingEngine.get_dynamodb_rcu_hourly()`/`get_dynamodb_wcu_hourly()` method. Or at minimum, update the constants and add a comment with the effective date

### L2-002 On-demand RCU/WCU rates **10× above** AWS list price  [CRITICAL]
- **Check**: L2.1 / L2.2.1 — unit error
- **Evidence**: `services/dynamodb.py:170-171`:
  ```python
  on_demand_rcu = 0.00000125
  on_demand_wcu = 0.00000625
  ```
  AWS publishes the price **per million** request units: $0.125 per million RRU = `$0.000000125/RRU`; $0.625 per million WRU = `$0.000000625/WRU`. The shim's constants have **one fewer zero** than AWS publishes — i.e. shim treats the price as $/single-unit when AWS actually charges $/million-units divided by million. Math at line 172-173 then multiplies by raw request counts → result is **10× over-stated**
- **Why it matters**: an on-demand table consuming 1M requests/day reads at $0.125/day actual = $3.81/month. Shim says $38.10/month. Adapter takes that and multiplies by `0.30` to claim $11.43/month savings. Reality: max possible savings on that table is `$3.81 × 0.30 = $1.14/month`. **Off by 10×.**
- **Recommended fix**: change the two constants to `0.000000125` and `0.000000625` (add one zero). Better — add `PricingEngine.get_dynamodb_on_demand_read_unit_price()` / `..._write_unit_price()` methods that pull from the Pricing API live

### L2-003 Arbitrary `* 0.23` and `* 0.30` savings multipliers in adapter  [HIGH]
- **Check**: L2.1 — `arbitrary` values forbidden
- **Evidence**: `services/adapters/dynamodb.py:57-61`:
  ```python
  if rcu > 0 or wcu > 0:
      monthly_current = (rcu * DYNAMODB_RCU_HOURLY + wcu * DYNAMODB_WCU_HOURLY) * 730 * ctx.pricing_multiplier
      savings += monthly_current * 0.23
  else:
      cost = rec.get("EstimatedMonthlyCost", 0)
      savings += cost * 0.30
  ```
- **Why it matters**: the **0.23** appears to represent "post-reserved-capacity cost ratio" (77% savings, close to AWS-advertised 76% max). But: (i) only some recs are reserved-capacity candidates; (ii) reserved capacity requires `rcu ≥ 100 AND wcu ≥ 100` per shim line 360 — the adapter applies 0.23 to **every** provisioned table; (iii) `0.30` for non-provisioned is undocumented; (iv) the same rec count appears in both source-blocks via different routes, risking double-counting (see L2-005). Combined effect: savings number is neither a worst-case nor a best-case but a uniform 23%/30% smear with no AWS basis
- **Recommended fix**: derive savings per opportunity: rightsize → `(current_capacity − consumed_capacity × 1.2) × rate`; reserved → `current_cost × 0.66` (real AWS published 66% midpoint of 53-76%); empty table → `current_cost × 1.0`. Same `_SAVINGS_FACTORS` pattern as S3

### L2-004 Adapter does not consume `enhanced_checks.EstimatedSavings`  [HIGH]
- **Check**: L2.5.1 / L2.5.3 — `total_recommendations` must align with `total_monthly_savings`
- **Evidence**: `services/adapters/dynamodb.py:63` — `total_recs = len(opt_opps) + len(enhanced_recs)` but only `opt_opps` contribute to `savings`. None of `enhanced_recs` does (none has `EstimatedMonthlySavings`; adapter doesn't even attempt to read)
- **Why it matters**: same metric-divergence as AMI/EFS — `total_recommendations` includes all the enhanced-check rows (reserved-capacity, unused tables, data-lifecycle, billing-mode-optimization) but `total_monthly_savings` ignores them. For an account whose primary opportunity is reserved capacity, the dollar figure is silently incomplete
- **Recommended fix**: quantify `EstimatedMonthlySavings` on every emitted rec (per scope rule); sum them on line 51-61

### L2-005 Double-count risk: same table appears in both sources  [MEDIUM]
- **Check**: L2.5.2 — dedupe across sources
- **Evidence**: `get_dynamodb_table_analysis` and `get_enhanced_dynamodb_checks` both call `dynamodb.describe_table` for every table. A provisioned table with `read_capacity > 100` appears in `dynamodb_table_analysis.optimization_opportunities` AND in `enhanced_checks.over_provisioned_capacity` AND (if `wcu >= 100`) in `enhanced_checks.reserved_capacity`. No `TableName`-based dedupe across sources
- **Why it matters**: same table can contribute up to 3× to `total_recommendations` (counted but not double-summed for $ because enhanced doesn't contribute to $ today — see L2-004). Once L2-004 is fixed, the double-count becomes a $ overstatement
- **Recommended fix**: dedupe by `TableName` before summing; if a table appears in multiple sources, take the max-savings option or split clearly (e.g., capacity-rightsizing is mutually exclusive with reserved-capacity since the latter requires `≥100 RCU` post-rightsize)

### L2-006 `pricing_multiplier` correctly applied to module-const provisioned path  [LOW]
- **Check**: L2.3.2
- **Evidence**: `services/adapters/dynamodb.py:57` — `... * 730 * ctx.pricing_multiplier` ✓. However shim's `EstimatedMonthlyCost` at line 126 does **NOT** apply pricing_multiplier (the shim has no multiplier argument on this path), so the on-demand `cost * 0.30` at adapter line 61 misses the regional adjustment
- **Status**: PASS for the provisioned path; refers L2-001/L2-003 for the missed multiplier on on-demand

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonDynamoDB, us-east-1, productFamily="Provisioned IOPS"` | RCU $0.00013/hr (SKU 4V475Q49DCKGXQZ2), WCU $0.00065/hr (SKU R6PXMNYCEDGZ2EYN) | confirms shim constants are 13% above AWS list |
| L2-002 | `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonDynamoDB, us-east-1, productFamily="Amazon DynamoDB PayPerRequest Throughput"` | Read $0.125/M RRU (SKU 4W4ZMC46EHE8XTTZ), Write $0.625/M WRU (SKU FGXVD96DKJMUASY3) | refutes shim's per-unit rates — adapter is 10× too high |
| L2-001 | math | `0.00013 × 730 = $0.0949/RCU-mo`; shim has $0.107 | (0.107 - 0.0949)/0.0949 ≈ 12.7% over | confirms |
| L2-002 | math | shim applies `avg_requests × 0.00000125`; should be `avg_requests × 0.000000125` | factor of 10 error | confirms |

---

## L3 — Reporting findings

### L3-001 Renderer's case-sensitive substring match silently drops most recs  [HIGH]
- **Check**: L3.2.2 — fields the renderer reads must match the adapter's strings exactly
- **Evidence**: `reporter_phase_b.py:748-757`:
  - looks for `"Switch to On-Demand"` and `"Switch to Provisioned"` substrings (capital `S`)
  - shim emits `"switch to Provisioned mode"` at `services/dynamodb.py:438` (lowercase `s`)
  - shim emits `"Consider On-Demand billing for unpredictable workloads"` at `services/dynamodb.py:135` — does NOT contain `"Switch to On-Demand"`
- **Why it matters**: provisioned tables flagged for migration to On-Demand fail the `"Switch to On-Demand" in str(opportunities)` test, then fail every subsequent `elif`, then hit line 759 `else: continue` — **the rec is silently dropped from the rendered HTML**. The recommendation count still increments at the adapter layer but the table card never appears
- **Recommended fix**: replace substring matching with explicit `CheckCategory` mapping. The shim already emits canonical category strings (`"DynamoDB Over-Provisioned Capacity"`, `"DynamoDB Reserved Capacity"`, `"DynamoDB Billing Mode - Metric-Backed"`, etc.); use them as dict keys

### L3-002 "On-Demand to Provisioned" group captures over-provisioned recs by accident  [HIGH]
- **Check**: L3.2.2 — substring routing leaks
- **Evidence**: same renderer line 750: `"Provisioned" in check_category` is True for both `"DynamoDB Over-Provisioned Capacity"` (means *reduce* provisioned) and `"DynamoDB Reserved Capacity"` (provisioned tables). Both get routed to `"On-Demand to Provisioned"` group whose hardcoded heading reads "Switch to Provisioned mode for predictable workloads (Save 20-60%)" — the opposite of the actual recommendation for over-provisioned tables
- **Why it matters**: a table flagged for capacity reduction is rendered with a heading recommending it switch *to* the mode it's already in. User-facing wrongness
- **Recommended fix**: same as L3-001 — explicit category map

### L3-003 `SOURCE_TYPE_MAP` for `enhanced_checks` reads "Metric Backed" but most categories aren't  [MEDIUM]
- **Check**: L3.2.x — labeling honesty
- **Evidence**: `reporter_phase_b.py:2104` — `"dynamodb_table_analysis": "Metric Backed"` (and generic `enhanced_checks` defaults to "Metric Backed"). Of the 7 enhanced categories, only `"DynamoDB Billing Mode - Metric-Backed"` (line 441) is truly metric-backed; the other 6 (`unused_tables`, `reserved_capacity`, `data_lifecycle`, etc.) are static rules
- **Why it matters**: same pattern that S3 fixed in v2 (`reporter_phase_b.py:1881 ("s3", "enhanced_checks"): "Static Analysis"`). DynamoDB needs the same split or override
- **Recommended fix**: add `("dynamodb", "enhanced_checks"): "Mixed (Static + Metric Backed)"` or split into two sources

### L3-004 No `priority`/`severity` set; size/utilization signals lost  [LOW]
- **Check**: L3.2.3
- **Evidence**: zero `priority`/`severity` fields set across all 7 categories. A table with 90% over-provisioning and a table with 5% over-provisioning render identically
- **Recommended fix**: derive `priority` from `(read_utilization, write_utilization)` for over-provisioned; `priority="high"` for `< 20%`, `"medium"` for `20-50%`

---

## Cross-layer red flags

**None auto-FAIL** under §4. However:
- **L1-001 (CRITICAL)** — `required_clients()` lies about cloudwatch usage. Per §4.3 "The adapter bypasses ClientRegistry" is auto-FAIL; this is a contract violation in spirit if not letter (ClientRegistry still issues the client because it's lenient, but the declared surface is wrong)
- **L2-002 (CRITICAL)** — on-demand request-unit pricing is 10× too high. Every on-demand-table savings figure on every account is wrong by an order of magnitude
- Combined, these two findings push overall to **FAIL**

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/dynamodb.py services/dynamodb.py
N806 Variable `DYNAMODB_RCU_HOURLY` in function should be lowercase  --> services/adapters/dynamodb.py:48:9
N806 Variable `DYNAMODB_WCU_HOURLY` in function should be lowercase  --> services/adapters/dynamodb.py:49:9
Found 2 errors.

# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='dynamodb'); print('key:',m.key,'aliases:',m.cli_aliases,'clients:',m.required_clients(),'cw:',m.requires_cloudwatch,'fast:',m.reads_fast_mode)"
key: dynamodb
aliases: ('dynamodb',)
clients: ('dynamodb',)           ← LIE: shim uses cloudwatch
cw: False                         ← LIE: shim calls cloudwatch.get_metric_statistics 6×
fast: True                        ← LIE: fast_mode never read

# 3. fast_mode usage (none)
$ grep -n "fast_mode\|ctx.fast_mode" services/dynamodb.py services/adapters/dynamodb.py
services/adapters/dynamodb.py:22:    reads_fast_mode: bool = True
(only the declaration, no consumer)

# 4. Offline + 5. Regression / snapshot tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k dynamodb -v
============================= 4 passed, 122 deselected in 0.06s ==============================

# 6. AWS price vs shim constants (live API)
RCU provisioned:      AWS $0.00013/hr × 730 = $0.0949/mo; shim has $0.107/mo  → +12.7% inflated
WCU provisioned:      AWS $0.00065/hr × 730 = $0.4745/mo; shim has $0.537/mo  → +13.2% inflated
RCU on-demand:        AWS $0.000000125/RRU;                  shim has $0.00000125  → 10× inflated
WCU on-demand:        AWS $0.000000625/WRU;                  shim has $0.00000625  → 10× inflated
```

Tests pass against the recorded fixture, which was generated with the same flawed constants — the 10×-on-demand and 13%-provisioned errors are masked by the golden.

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L2-002** — Fix the on-demand request-unit constants (`0.00000125` → `0.000000125`, `0.00000625` → `0.000000625`). One-character fix per constant. Re-capture the golden afterward and confirm the on-demand-table cost figures drop by 90%.
2. **[CRITICAL] L1-001** — Add `"cloudwatch"` to `required_clients()`. One-line fix.
3. **[HIGH] L2-001** — Update provisioned-capacity constants to AWS list price (or, preferably, route through `PricingEngine.get_dynamodb_rcu_hourly()`). Adds ~13% savings honesty.
4. **[HIGH] L1-002 / L1-003** — Flip `requires_cloudwatch` to True; either honor `fast_mode` or flip `reads_fast_mode` to False. Match flags to actual behavior.
5. **[HIGH] L2-003** — Replace `* 0.23` / `* 0.30` with per-opportunity factor dict (S3 v2 pattern). Document each factor against an AWS-published savings range.
6. **[HIGH] L2-004** — Quantify `EstimatedMonthlySavings` on every enhanced-check rec; remove the placeholder strings ("Variable based on actual usage", "Up to 60%", etc.) per scope rule.
7. **[HIGH] L3-001 / L3-002** — Replace substring matching in `_render_dynamodb_enhanced_checks` with explicit `CheckCategory` → group map. Today most recs are silently dropped or misgrouped.
8. **[HIGH] L1-005** — Add `AccessDenied` classification at the 4 `ctx.warn` sites and the 3 silent `except Exception:` sites.
9. **[MEDIUM] L2-005** — Dedupe by `TableName` across sources before summing savings.
10. **[MEDIUM] L1-006** — Remove the `$25.0` invented fallback; emit zero with a `pricing_warning` field instead.
11. **[MEDIUM] L3-003** — Override `SOURCE_TYPE_MAP` for `("dynamodb", "enhanced_checks")` to "Mixed (Static + Metric Backed)".
12. **[MEDIUM] L1-004** — Drop the module-level print in `services/dynamodb.py:15`.
13. **[MEDIUM] L1-007** — Set `total_count=` on the ServiceFindings return.
14. **[LOW] L3-004** — Derive `priority` from utilization tiers.
15. **[LOW] ruff** — Lowercase `DYNAMODB_RCU_HOURLY` / `WCU_HOURLY` per N806 (or move to module-const at the top of `services/dynamodb.py`).
16. **[INFO]** — Re-capture goldens after CRITICAL fixes; current goldens mask the 10× on-demand bug.
