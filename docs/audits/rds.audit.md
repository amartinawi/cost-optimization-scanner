# Audit: rds Adapter

**Adapter**: `services/adapters/rds.py` (82 lines)
**Legacy shim**: `services/rds.py` (528 lines)
**ALL_MODULES index**: 3
**Renderer path**: Phase B — 2 dedicated handlers (`compute_optimizer`, `enhanced_checks`)
**Date**: 2026-05-12
**Auditor**: automated (multi-layer audit prompt v2, external-validation enforced)

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (2 MEDIUM, 1 LOW) |
| L2 Calculation | **WARN** | 7 (3 HIGH, 3 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 1 (LOW, pre-existing print) |
| **Overall**    | **WARN** | 11 findings |

`WARN` — no FAIL-class or CRITICAL findings remain after the EC2-round fixes propagated into the shared helpers (`compute_optimizer_savings`, `parse_dollar_savings`). The 3 HIGH L2 findings are all symptoms of the **same** root cause: three RDS recommendation categories still emit percentage-only `EstimatedSavings` strings that now parse to **$0** (after the EC2 round's L2-004 fix removed the $50 fallback). Real savings can be quantified, but require a new `get_rds_instance_monthly_price` method on `PricingEngine` (the existing generic one is unreliable for RDS — see L2-RDS-008).

---

## Pre-flight facts

- **Adapter class**: `RdsModule` at `services/adapters/rds.py:18`
- **Required boto3 clients**: `("rds", "compute-optimizer")`
- **`requires_cloudwatch`**: `False` — verified by `grep cloudwatch services/rds.py` (0 hits). RDS adapter does **not** use CloudWatch. ✓
- **`reads_fast_mode`**: `False` — no expensive enrichment that needs gating. ✓
- **CLI alias resolution**: `resolve_cli_keys(ALL_MODULES, {'rds'}, None) → {'rds'}` (PASS)
- **Sources emitted**: `compute_optimizer`, `enhanced_checks`
- **Extras**: `instance_counts` (dict: total/running/stopped + per-engine breakdown)
- **`stat_cards`**: empty tuple
- **`optimization_descriptions`**: `RDS_OPTIMIZATION_DESCRIPTIONS` (5 entries — idle_databases, rds_optimization, instance_rightsizing, reserved_instances, storage_optimization)
- **Pricing methods consumed (from `PricingEngine`)**:
  - `get_rds_monthly_storage_price_per_gb(storage_type, multi_az=)` — used by gp2→gp3 migration
  - `get_rds_backup_storage_price_per_gb()` — used by backup retention and old snapshots
  - Compute Optimizer savings now via `compute_optimizer_savings()` (fixed in commit `79f0bdb`)
- **Test fixture**: `tests/fixtures/recorded_aws_responses/compute-optimizer/get_rds_database_recommendations.json` — empty (`rdsDBRecommendations: []`)
- **Golden savings snapshot**: `tests/fixtures/reporter_snapshots/savings/rds.txt` = `0.000000`

---

## External validation evidence

| Tool | Call | Result | Used by |
|---|---|---|---|
| `mcp__aws-pricing-mcp-server__get_pricing_service_attributes` | `AmazonRDS` | Filterable attributes include `instanceType`, `databaseEngine`, `deploymentOption` (Single-AZ / Multi-AZ), `licenseModel`, `storageMedia`, `engineCode`, `engineMajorVersion`, `volumeType`, etc. | confirms RDS pricing requires engine + deployment to disambiguate |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonRDS / us-east-1`, `instanceType=db.t3.medium + engine=PostgreSQL + deployment ANY_OF [Single-AZ, Multi-AZ]` | Single-AZ = **$0.072/h** ($52.56/mo @ 730h); Multi-AZ = **$0.145/h** ($105.85/mo). **Ratio = 2.01×**. | Confirms Multi-AZ ≈ 2× Single-AZ assumption used by `_fetch_rds_storage_price`; quantifies L2-RDS-001 fix impact |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonRDS / us-east-1`, `productFamily=Storage Snapshot` | 37 SKUs at `$0.095/GB-Mo`. Description text: "_per additional GB-month of backup storage exceeding free allocation_". | L2-RDS-007 — free tier exists and is unmodeled |
| `WebSearch` | "AWS RDS automated backup storage free tier 100% of total provisioned storage" | AWS rule: free backup storage = 100% of total provisioned database storage in region. Charged $0.095/GB-Mo above that. | L2-RDS-007 |
| Code probe | `grep "cloudwatch" services/rds.py` | 0 hits | Confirms `requires_cloudwatch=False` is correct |
| Code probe | `python3 -c "from services import ALL_MODULES; ..."` | `cw=False, fast=False, clients=('rds', 'compute-optimizer'), class=RdsModule` | Pre-flight |

---

## L1 — Technical findings

### L1-RDS-001  `print()` statements in scan path  [MEDIUM]

- **Check**: L1.5 hygiene (clean stdout/stderr discipline)
- **Evidence**:
  - `services/rds.py:87` — `print("🔍 [services/rds.py] RDS module active")` (top-of-function diagnostic)
  - `services/rds.py:136, 437, 516, 518, 521` — five `print(f"Warning: …")` calls inside `except` blocks
  - `services/adapters/rds.py:41, 47, 54, 60` — four `print()` calls in adapter `try/except` blocks
  - `reporter_phase_b.py:439` — `print(f"⚠️ Could not parse grouped savings …")` (renderer)
- **Why it matters**: Operator-relevant warnings never reach `scan_warnings[]` in the JSON output; they're emitted to stdout where `python3 cli.py … | jq` callers lose them. Same pattern as resolved L1-004 in EC2.
- **Fix**: Replace top-of-function diagnostic with `logger.debug(...)`; replace warning `print(...)` calls with `ctx.warn(..., service="rds")` (or `ctx.permission_issue` when applicable) — matches the EC2 fix pattern.

### L1-RDS-002  Silent except blocks not routed to ctx  [MEDIUM]

- **Check**: L1.2.2 (no silent error swallowing)
- **Evidence**:
  - `services/adapters/rds.py:46-47` — `except Exception as e: print(...)` for Compute Optimizer check failure
  - `services/adapters/rds.py:53-54` — same for enhanced checks
  - `services/adapters/rds.py:59-60` — same for instance count
  - `services/rds.py:223-228` — `except Exception:` in Multi-AZ tag lookup; no log entry, just falls through to name-based heuristic
  - `services/rds.py:436-437, 515-516, 517-518, 520-521` — `except Exception as e: print(...)` for snapshot / cluster / cluster-snapshot / enhanced paths
- **Why it matters**: AccessDenied on `rds:DescribeDBSnapshots` produces zero old-snapshot recs with no signal in `ctx._permission_issues`. Operators cannot distinguish "no snapshots" from "no permission".
- **Fix**: Route `ClientError` with `Code in {UnauthorizedOperation, AccessDenied}` to `ctx.permission_issue(...)`. Other exceptions → `ctx.warn(...)`. Same pattern as resolved L1-003 in EC2.

### L1-RDS-003  Multi-AZ tag-lookup fallback is silent and opaque  [LOW]

- **Check**: L1.2.2 / L1.5 (debuggability of fallback paths)
- **Evidence**: `services/rds.py:223-228` — when `list_tags_for_resource` raises any exception (permission denied, throttling, malformed ARN, network), the code silently falls back to a name-substring heuristic. No `ctx.warn`, no `logger.debug` — the operator has no way to tell that classifications below this point are heuristic-based rather than tag-based.
- **Why it matters**: A non-prod DB named `prod-analytics-replica-1` would be misclassified as non-prod, producing a recommendation to disable Multi-AZ that could break production redundancy.
- **Fix**: Log the fallback at `logger.debug` level with the resource ARN. The fallback itself is acceptable; the silence is not.

---

## L2 — Calculation findings

### Source classification table

| Recommendation category | Source key | Source class | Evidence | Acceptable? | External |
|---|---|---|---|---|---|
| Compute Optimizer recs | `compute_optimizer` | `aws-api` (live) | `services/adapters/rds.py:63` via `compute_optimizer_savings` | YES | boto3 ref already validated in EC2 audit |
| Multi-AZ Unnecessary | `enhanced_checks` | **percentage string → $0** | `services/rds.py:240` `EstimatedSavings: "~50% of instance cost"` | **NO** — see L2-RDS-001 | `get_pricing(db.t3.medium PostgreSQL)`: Multi-AZ $0.145/h vs Single-AZ $0.072/h (2.01× ratio) |
| Backup Retention Excessive | `enhanced_checks` | `live` ✓ | `services/rds.py:262` uses `get_rds_backup_storage_price_per_gb()` | YES (formula caveat in L2-RDS-007) | `get_pricing(Storage Snapshot)`: $0.095/GB-Mo above free tier |
| Storage Optimization (gp2→gp3) | `enhanced_checks` | `live` ✓ | `services/rds.py:274` uses `get_rds_monthly_storage_price_per_gb("gp2", multi_az=)` | YES — multi-AZ-aware | live API |
| Storage Optimization (io1/io2/gp3 review) | `enhanced_checks` | **string → $0** | `services/rds.py:306` `EstimatedSavings: "Requires workload analysis…"` | WARN (informational, acceptable) | n/a |
| Non-Production Scheduling | `enhanced_checks` | **percentage string → $0** | `services/rds.py:330` `EstimatedSavings: "65-75% of compute costs"` | **NO** — see L2-RDS-002 | live RDS price × 0.65 would quantify |
| Idle Database (stopped) | `enhanced_checks` | string → $0 | `services/rds.py:346` `EstimatedSavings: "Storage costs continue while stopped"` | WARN (acceptable — compute is already saved by being stopped) | n/a |
| Instance Rightsizing (db.t*) | `enhanced_checks` | string → $0 | `services/rds.py:362` `EstimatedSavings: "Requires CloudWatch analysis…"` | WARN (acceptable — defers to Compute Optimizer) | n/a |
| Reserved Instances | `enhanced_checks` | **percentage string → $0** | `services/rds.py:378` `EstimatedSavings: "Up to 60% savings for 1-3 year commitments"` | **NO** — see L2-RDS-003 | RI savings depend on commitment + payment; ~40% for 1-yr no-upfront |
| Aurora Serverless candidates | `enhanced_checks` | **percentage string → $0** | `services/rds.py:398` `"Pay only for capacity used (up to 90% for idle periods)"` | WARN (acceptable — variable workload, no usage data) | n/a |
| Old Snapshots | `enhanced_checks` | `live` ✓ | `services/rds.py:428-431` uses `get_rds_backup_storage_price_per_gb()` | YES (free-tier caveat in L2-RDS-007) | live API |
| Aurora Serverless v2 (cluster) | `enhanced_checks` | **percentage string → $0** | `services/rds.py:454` `"20-90% cost reduction for variable workloads"` | WARN (acceptable — no usage data) | n/a |
| Aurora Storage Optimization | `enhanced_checks` | string → $0 | `services/rds.py:472` `"Potential I/O cost reduction…"` | WARN (acceptable) | n/a |
| Aurora Cluster Snapshots | `enhanced_checks` | `live` ✓ | `services/rds.py:503-506` uses `get_rds_backup_storage_price_per_gb()` | YES | live API |

### External validation log

| Finding ID | Tool | Call | Result | Confirms |
|---|---|---|---|---|
| L2-RDS-001 | `get_pricing` | RDS db.t3.medium PostgreSQL Single-AZ vs Multi-AZ us-east-1 | Single = $0.072/h; Multi = $0.145/h; ratio = **2.01×** | Multi-AZ is ~2× Single-AZ → eliminating Multi-AZ saves ~50% of the **Multi-AZ** instance price (≈ Single-AZ price) |
| L2-RDS-002 | (deferred to L2-RDS-008) | n/a | Would need `get_rds_instance_monthly_price(engine, class, multi_az)` | Cannot quantify without that method |
| L2-RDS-003 | (deferred to L2-RDS-008) | n/a | Same | Same |
| L2-RDS-007 | `WebSearch` + `get_pricing` | "AWS RDS free tier 100% of provisioned storage" | AWS rule: free backup storage = 100% of total provisioned DB storage in region; $0.095/GB-Mo above | Backup-retention formula ignores free tier |

### L2-RDS-001  Multi-AZ Unnecessary saving renders as $0  [HIGH]

- **Check**: L2.1 source attribution (`arbitrary` after L2-004 fix)
- **Evidence**:
  - `services/rds.py:240` — `EstimatedSavings: "~50% of instance cost"`
  - After EC2 round's L2-004 fix to `services/_savings.py`, `parse_dollar_savings("~50% of instance cost") → 0.0`
  - Adapter aggregation `if "$" in est and "/month" in est` at `services/adapters/rds.py:66` further filters this out: contribution to `total_monthly_savings` = **$0**
- **External**: live AWS Pricing API for `db.t3.medium PostgreSQL` shows Multi-AZ = $0.145/h, Single-AZ = $0.072/h. Switching from Multi-AZ to Single-AZ on a non-prod DB saves **$53.29/month** for one db.t3.medium — completely unreported today.
- **Why it matters**: Multi-AZ is the single largest non-rightsizing cost optimization in RDS. A typical multi-DB non-prod environment with 5–10 Multi-AZ databases hides $250–$1000/month of recoverable spend in the headline `total_monthly_savings`. The recommendation itself (the warning to disable Multi-AZ in non-prod) is still emitted and counted in `total_recommendations`, so the report is **internally inconsistent**: "10 recommendations, $0 savings."
- **Fix**:
  1. Add `PricingEngine.get_rds_instance_monthly_price(engine, instance_class, *, multi_az=False)` that filters on `databaseEngine`, `instanceType`, `deploymentOption`, `licenseModel="No license required"` (or `licenseModel="License included"` for SQL Server/Oracle BYOL paths).
  2. In `services/rds.py:230` block, compute `multi_az_price = get_rds_instance_monthly_price(engine, class, multi_az=True)`, `single_az_price = get_rds_instance_monthly_price(engine, class, multi_az=False)`, then `savings = multi_az_price - single_az_price`.
  3. Emit `EstimatedSavings: f"${savings:.2f}/month with single-AZ"`.

### L2-RDS-002  Non-Prod Scheduling saving renders as $0  [HIGH]

- **Check**: L2.1
- **Evidence**: `services/rds.py:330` — `EstimatedSavings: "65-75% of compute costs"`. Same parse-to-$0 result.
- **External**: same `get_rds_instance_monthly_price` would quantify this.
- **Why it matters**: Same magnitude as L2-RDS-001 (dev/test DBs running 24/7 are often Single-AZ but expensive; scheduling 12h/day saves ~50%, 8h/day saves ~67%). Unreported today.
- **Fix**: After L2-RDS-008's new pricing method exists, emit `f"${monthly_price * 0.65:.2f}/month with nights/weekends shutdown"`.

### L2-RDS-003  Reserved Instances saving renders as $0  [HIGH]

- **Check**: L2.1
- **Evidence**: `services/rds.py:378` — `EstimatedSavings: "Up to 60% savings for 1-3 year commitments"`. Parses to $0.
- **External**: same dependency on `get_rds_instance_monthly_price`. Typical 1-yr no-upfront RI savings is ~38–40%; 3-yr all-upfront is ~60%. Use 0.40 as a conservative quantification.
- **Why it matters**: RI recommendations are issued for **every** available DB instance (`services/rds.py:368`), so this finding compounds heavily.
- **Fix**: Same as L2-RDS-002 — emit `f"${monthly_price * 0.40:.2f}/month with 1-yr no-upfront RI"`.

### L2-RDS-004  Aurora Serverless candidates: $0 acceptable  [LOW]

- **Check**: L2.1
- **Evidence**: `services/rds.py:398` — `EstimatedSavings: "Pay only for capacity used (up to 90% for idle periods)"`. Parses to $0.
- **External**: Aurora Serverless v2 pricing is `$0.06/ACU-hour` (already in `PricingEngine` as `get_aurora_acu_hourly`). Without idle-percentage data per workload, the saving cannot be honestly quantified.
- **Why it matters**: Less than L2-RDS-001/002/003. The string is descriptive prose, not a misleading number.
- **Fix**: Reword to `"$0.00/month - quantify after measuring idle hours (Aurora Serverless v2 charges per ACU-hour)"` to match the project-wide format and avoid confusing a parser.

### L2-RDS-005  Aurora Serverless v2 cluster: $0 acceptable  [LOW]

- **Check**: L2.1
- **Evidence**: `services/rds.py:454` — `EstimatedSavings: "20-90% cost reduction for variable workloads"`. Parses to $0.
- **Why it matters**: Same as L2-RDS-004.
- **Fix**: Same reword.

### L2-RDS-006  Aurora Storage Optimization: $0 acceptable  [LOW]

- **Check**: L2.1
- **Evidence**: `services/rds.py:472` — `EstimatedSavings: "Potential I/O cost reduction for high-throughput workloads"`. Parses to $0.
- **Why it matters**: Defers to operator's workload analysis. Acceptable.
- **Fix**: Reword to project-wide `$0.00/month - …` format.

### L2-RDS-007  Backup-retention formula ignores free-tier rule  [MEDIUM]

- **Check**: L2.2.5 (free-tier deductions)
- **Evidence**:
  - `services/rds.py:247-253`:
    ```python
    extra_backup_days = backup_retention - 7
    backup_price = get_rds_backup_storage_price_per_gb()      # $0.095/GB-Mo
    backup_savings = allocated_storage * backup_price * extra_backup_days / 30
    ```
  - **External**: AWS rule (confirmed via `WebSearch` + AWS Pricing API description text): _"free backup storage equal to 100% of total provisioned database storage in each AWS Region. … you will only be charged for backup storage that exceeds this amount."_
  - The formula assumes 100% of `allocated_storage` is billed daily, which is only true when the regional backup pool is already fully consumed.
  - Also: the formula uses `extra_backup_days / 30` which implies "save (allocated_storage × price) per day saved from retention", over-counting because backup storage is incremental, not full-copy-per-day.
- **Why it matters**: A 100 GB DB with 14-day retention reports `100 × 0.095 × 7 / 30 = $2.22/month`. Real value is often closer to $0 (because the DB's own 100 GB of provisioned storage already covers the free tier). For multi-DB accounts the formula may **understate** (when many small DBs each get their own free quota that pools regionally) or **overstate** (when allocated storage is large but actual data is small).
- **Fix**: At minimum, document the assumption ("estimate assumes regional backup free tier is fully consumed"). For correctness, the formula needs an account-level free-tier offset:
  ```python
  regional_provisioned = sum(allocated_storage_of_each_db_in_region)
  regional_backup_used = sum(actual_backup_size_of_each_db)  # not available from describe_db_instances
  ```
  Latter requires `cloudwatch:GetMetricStatistics` on `AWS/RDS/TotalBackupStorageBilled` — out of scope without adding CloudWatch dependency. Recommend keeping the current formula with an updated description: `f"${backup_savings:.2f}/month (estimate, assumes free tier exhausted)"`.

### L2-RDS-008  PricingEngine lacks an RDS-specific instance pricing method  [MEDIUM]

- **Check**: L2.1 source readiness for downstream fixes
- **Evidence**:
  - `core/pricing_engine.py:286-298` exposes `get_instance_monthly_price(service_code, instance_type)` which filters only on `instanceType` + `location`. For `AmazonRDS`, this leaves engine, deployment, and license model un-disambiguated — `_call_pricing_api` would return the first matching SKU (effectively arbitrary).
  - L2-RDS-001/002/003 fixes all need `get_rds_instance_monthly_price(engine, instance_class, *, multi_az)`.
- **Why it matters**: Blocks all three HIGH fixes. Cannot resolve L2-RDS-001/002/003 without this method.
- **Fix** (new PricingEngine method):
  ```python
  def get_rds_instance_monthly_price(
      self, engine: str, instance_class: str, *, multi_az: bool = False
  ) -> float:
      """$/month for an RDS DB instance, engine-and-deployment-aware."""
      engine_label = {  # map engine -> Price List 'databaseEngine' value
          "mysql": "MySQL",
          "postgres": "PostgreSQL",
          "mariadb": "MariaDB",
          "oracle-se2": "Oracle",
          "sqlserver-ex": "SQL Server",
          # ... extend as needed
      }.get(engine.lower(), "MySQL")
      filters = [
          {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
          {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine_label},
          {"Type": "TERM_MATCH", "Field": "deploymentOption",
           "Value": "Multi-AZ" if multi_az else "Single-AZ"},
          {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
          {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": "No license required"},
      ]
      hourly = self._call_pricing_api("AmazonRDS", filters)
      return hourly * 730 if hourly is not None else FALLBACK_RDS_INSTANCE_MONTHLY
  ```

### L2-RDS-009  No dedup between Compute Optimizer + enhanced checks  [MEDIUM]

- **Check**: L2.5.2 (no double-counting)
- **Evidence**: `services/adapters/rds.py:62-67`:
  ```python
  savings += sum(compute_optimizer_savings(r) for r in co_recs)
  for rec in enhanced_recs:
      est = rec.get("EstimatedSavings", "")
      if "$" in est and "/month" in est:
          savings += parse_dollar_savings(est)
  ```
  A single DB instance can produce a Compute Optimizer rightsizing rec **and** an enhanced "Reserved Instances" rec **and** an enhanced "Multi-AZ Unnecessary" rec.
- **Why it matters**: Today the issue is **dormant** — enhanced RI/Multi-AZ recs contribute $0 (percentage strings; see L2-RDS-001/002/003). The moment those fixes land, the same db.t3.medium PostgreSQL Multi-AZ instance would contribute:
  - Compute Optimizer: $X (e.g. $30/mo for downsize)
  - Multi-AZ disable: $53/mo
  - Reserved Instance: ~$42/mo (Single-AZ × 0.40)
  - **Total: $125/mo for one instance** — but only one of these is achievable (they're mutually exclusive remediations).
- **Fix**: Build a per-instance dedup map keyed by `(DBInstanceIdentifier, engine)`. For instances appearing in multiple sources, retain only the highest-savings source. Add a unit test asserting `sum(per-rec saving) ≤ max-source-saving-per-instance × num_instances`.

---

## L3 — Reporting findings

### Field-presence table

| Source | Renderer | Required fields | Notes |
|---|---|---|---|
| `compute_optimizer` | `_render_rds_compute_optimizer` (`reporter_phase_b.py:339`) | `resourceArn`, `instanceFinding`, `instanceFindingReasonCodes`, `engine`, `engineVersion`, `currentDBInstanceClass`, `instanceRecommendationOptions[].dbInstanceClass`, `instanceRecommendationOptions[].savingsOpportunity.estimatedMonthlySavings.value` | Reads **same** nested path used by `compute_optimizer_savings` ✓ |
| `enhanced_checks` | `_render_rds_enhanced_checks` (`reporter_phase_b.py:405`) | `CheckCategory`, `Recommendation`, `EstimatedSavings`, `DBInstanceIdentifier` / `DBClusterIdentifier` / `SnapshotId`, `engine`, `engineVersion`, `instanceFinding` / `storageFinding` | Renderer **already** sums numeric per-rec `EstimatedSavings` strings (line 432-437) and displays the total — this is the right pattern; just needs the strings to be numeric. After L2-RDS-001/002/003 fixes, the renderer needs no changes. |

### L3-RDS-001  `print()` in renderer parse-error path  [LOW]

- **Check**: L3.1.4 / L1.4 hygiene
- **Evidence**: `reporter_phase_b.py:439` — `print(f"⚠️ Could not parse grouped savings '{savings_str}': {str(e)}")` inside `_render_rds_enhanced_checks`.
- **Why it matters**: Same stdout-pollution issue as L1-RDS-001 but in the renderer. Defensive `try/except ValueError` for float parsing is fine — the log channel is the issue.
- **Fix**: Replace with `logger.debug(...)`. Renderer never has a `ctx` to route through, so logging is the right channel.

---

## Cross-layer red flags

**None.** No bypass of `ClientRegistry`. No cross-account ARNs (all ARNs use `ctx.account_id`). No writes to AWS. No `ALL_MODULES` mutation. No untraceable dollars — every dollar in `total_monthly_savings` today is either from a live PricingEngine call or from `compute_optimizer_savings`. The **gap** is that several recs that SHOULD produce dollars currently produce $0 — but those are unreported, not arbitrary.

---

## Verification log

```
# Module probe
$ python3 -c "from services import ALL_MODULES; m = next(x for x in ALL_MODULES if x.key=='rds'); print(m.requires_cloudwatch, m.reads_fast_mode, m.required_clients())"
False False ('rds', 'compute-optimizer')

# CW probe (RDS doesn't call CloudWatch)
$ grep -n "cloudwatch" services/rds.py
(no hits — confirms requires_cloudwatch=False is correct)

# Print scan-path search
$ grep -n "print(" services/rds.py services/adapters/rds.py | wc -l
11

# parse_dollar_savings against RDS strings
'~50% of instance cost'                                        → 0.0   (L2-RDS-001)
'65-75% of compute costs'                                      → 0.0   (L2-RDS-002)
'Up to 60% savings for 1-3 year commitments'                   → 0.0   (L2-RDS-003)
'Pay only for capacity used (up to 90% for idle periods)'      → 0.0   (L2-RDS-004)
'$2.22/month in backup storage (estimate - beyond free tier)'  → 2.22  ✓
'$15.18/month'                                                 → 15.18 ✓

# Live RDS Pricing API
db.t3.medium PostgreSQL Single-AZ us-east-1:  $0.072/h → $52.56/mo
db.t3.medium PostgreSQL Multi-AZ  us-east-1:  $0.145/h → $105.85/mo
Multi-AZ / Single-AZ ratio:                    2.01×       (validates the ~2× rule)
RDS backup storage above free tier:           $0.095/GB-Mo

# Regression gate
$ pytest tests/test_offline_scan.py tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -k rds
4 passed, 122 deselected
```

---

## Recommended next steps (prioritized)

### HIGH — fix to recover material recommendation $ value

1. **L2-RDS-008** — Add `PricingEngine.get_rds_instance_monthly_price(engine, instance_class, *, multi_az)`. Blocks the next two.
2. **L2-RDS-001** — Multi-AZ Unnecessary: emit `f"${multi_az_price - single_az_price:.2f}/month with single-AZ"`.
3. **L2-RDS-002** — Non-Prod Scheduling: emit `f"${monthly_price * 0.65:.2f}/month with nights/weekends shutdown"`.
4. **L2-RDS-003** — Reserved Instances: emit `f"${monthly_price * 0.40:.2f}/month with 1-yr no-upfront RI"`.

### MEDIUM

5. **L2-RDS-009** — Dedup Compute Optimizer + enhanced checks per `DBInstanceIdentifier`. Implement **before** landing L2-RDS-001/002/003 so the headline doesn't triple-count.
6. **L2-RDS-007** — Document the backup-retention formula's assumption that the regional free tier is exhausted. Update the rec's description text.
7. **L1-RDS-001** — Replace 11 `print()` calls with `logger.*` / `ctx.warn` / `ctx.permission_issue` (EC2-round pattern).
8. **L1-RDS-002** — Route `except` blocks through `ctx.permission_issue` for AccessDenied and `ctx.warn` for everything else.

### LOW

9. **L1-RDS-003** — Log Multi-AZ tag-lookup fallback at `logger.debug` so the heuristic path is debuggable.
10. **L2-RDS-004/005/006** — Reword variable-workload strings to the project-wide `$0.00/month - …` format.
11. **L3-RDS-001** — Replace renderer `print` with `logger.debug`.

---

## What this audit did **not** cover

- IAM policy completeness for RDS actions (out of scope — covered by README's IAM section).
- > 10,000 DB instance throttling (load test scope).
- Aurora **performance** profiling (this audit is cost-only; Aurora storage/IO recommendations are detection-level).
- Cross-account / Organizations scans.

---

## Sources

- AWS RDS Pricing — https://aws.amazon.com/rds/pricing/
- AWS RDS Backup Storage FAQ — https://aws.amazon.com/rds/faqs/
- AWS Database Blog: Demystifying Amazon RDS backup storage costs — https://aws.amazon.com/blogs/database/demystifying-amazon-rds-backup-storage-costs/
- AWS Pricing API (via MCP): `mcp__aws-pricing-mcp-server__get_pricing` for `AmazonRDS / us-east-1` (db.t3.medium PostgreSQL Single-AZ + Multi-AZ; Storage Snapshot family)
- AWS Pricing API attributes (via MCP): `get_pricing_service_attributes('AmazonRDS')` — confirms `databaseEngine`, `deploymentOption`, `licenseModel` are the required filter dimensions
