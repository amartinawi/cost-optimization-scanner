# Audit: file_systems Adapter

**Adapter**: `services/adapters/file_systems.py` (94 lines)
**Legacy shim**: `services/efs_fsx.py` (607 lines) — all analysis lives here
**ALL_MODULES index**: 4
**Renderer path**: **Phase A** (`reporter_phase_a.render_file_systems` at `reporter_phase_a.py:25`); `file_systems` is in `_PHASE_A_SERVICES` (`reporter_phase_b.py:2203`)
**Date**: 2026-05-15
**Auditor**: automated (per `docs/audits/AUDIT_PROMPT.md`)

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **FAIL** | 5 (1 HIGH, 3 HIGH, 1 MEDIUM) |
| L2 Calculation | **FAIL** | 7 (1 CRITICAL, 4 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 4 (2 HIGH, 1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | double-multiplier + arbitrary 0.40 + scope-rule violations |

---

## Pre-flight facts

- **Adapter class**: `FileSystemsModule` at `services/adapters/file_systems.py:19`
- **Required boto3 clients**: `("efs", "fsx")`
- **Flags**: `requires_cloudwatch=False`, `reads_fast_mode=False` — matches code
- **Sources emitted**: `{"efs_lifecycle_analysis", "fsx_optimization_analysis", "enhanced_checks"}`
- **Pricing methods consumed**: `ctx.pricing_engine.get_efs_monthly_price_per_gb()` — gated behind a `cost == 0` branch that is **never reached** because `_estimate_efs_cost` always returns non-zero (L2-005). No FSx live-pricing method exists in PricingEngine; FSx uses module-const dict at `services/efs_fsx.py:106-111`
- **Test files**: `tests/test_offline_scan.py`, `tests/test_regression_snapshot.py`, `tests/test_reporter_snapshots.py`

---

## L1 — Technical findings

### L1-001 Module-level `print()` calls in adapter and shim  [HIGH]
- **Check**: violates `~/.claude/rules/python/hooks.md`; L1.2.2-adjacent
- **Evidence**:
  - `services/adapters/file_systems.py:43` — `print("\U0001f50d [services/adapters/file_systems.py] File Systems module active")`
  - `services/efs_fsx.py:14` — **module-level** `print(...)` that fires every time the module is imported (test runs, CLI listing, anything)
- **Why it matters**: stdout pollution at import time; banner appears in every regression-test run output (see `tests/test_offline_scan.py` test session header), and module-level side-effects make caching/lazy-loading impossible
- **Recommended fix**: delete the module-level print; route the adapter banner to `logger.debug`

### L1-002 FSx `describe_file_systems` not paginated  [HIGH]
- **Check**: L1.2.1 — list operations must use paginators
- **Evidence**:
  - `services/efs_fsx.py:283` — `fs_response = fsx.describe_file_systems()` (no paginator)
  - `services/efs_fsx.py:379` — same call, again no paginator (in `get_fsx_optimization_analysis`)
  - `services/efs_fsx.py:522` — same call, again no paginator (in `get_enhanced_efs_fsx_checks`)
  - Verified via `boto3.client('fsx').can_paginate('describe_file_systems')` → `True`
- **Why it matters**: accounts with >100 FSx file systems (default page size) silently drop trailing pages — invisible to all three call sites, three times per scan. EFS calls correctly use the paginator (lines 168, 231, 447); FSx is the outlier
- **Recommended fix**: replace each `fsx.describe_file_systems()` with `fsx.get_paginator("describe_file_systems").paginate()`. (FileCaches `describe_file_caches` correctly has **no** paginator per `can_paginate=False`, so leave that call as-is)

### L1-003 No `AccessDenied` / `UnauthorizedOperation` discrimination  [HIGH]
- **Check**: L1.2.3 — IAM gaps must route through `ctx.permission_issue`
- **Evidence**: every except site in `services/efs_fsx.py` routes to `ctx.warn(...)` — never to `ctx.permission_issue`. The shim's six `except Exception as e: ctx.warn(...)` sites at lines 213-214, 267-268, 270-271, 358-359, 419-420, 515-516, 517-518, 599-600 all collapse permission denials into generic warnings
- **Why it matters**: accounts missing `efs:DescribeLifecycleConfiguration`, `fsx:DescribeFileSystems`, `fsx:DescribeFileCaches`, or `efs:DescribeFileSystems` get a warning blob instead of a structured permission-issue record. The HTML "Permission Issues" section stays empty even when IAM is the root cause
- **Recommended fix**: classify exceptions via `e.response["Error"]["Code"]` against `{"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}` → `ctx.permission_issue(action="efs:DescribeFileSystems")` etc.

### L1-004 Adapter logic mutates result-dict shape based on isinstance check  [MEDIUM]
- **Check**: L1.3.x / Protocol-conformance
- **Evidence**: `services/adapters/file_systems.py:52-61` — the adapter handles **two different return shapes** from `get_efs_lifecycle_analysis` and `get_fsx_optimization_analysis` (either `list` directly or `dict` with `"recommendations"` key). The shim functions actually always return `list[dict]` (signatures at `services/efs_fsx.py:227, 375`) — the dict branch is dead code
- **Why it matters**: defensive code that masks contract drift. If the shim ever changes return shape, the adapter silently uses a different code path. Either lock the shim's return type with a unit test or remove the dict branch
- **Recommended fix**: delete the `if isinstance(... , list) else ...` branches; rely on the signatures `-> list[dict[str, Any]]`

### L1-005 `extras` payload contains non-JSON-native objects pre-serialization  [LOW]
- **Check**: L1.3.x / L3.1.4
- **Evidence**: `services/efs_fsx.py:206` — `"CreationTime": fs["CreationTime"].isoformat()` (good); `services/efs_fsx.py:329, 352` — same (good). All `CreationTime` values are converted. **However**: `extras={"efs_counts": efs_counts, "fsx_counts": fsx_counts}` at `services/adapters/file_systems.py:93` — `efs_counts["unused_systems"]` is a list of dicts including iso-stringified `CreationTime`, OK. `total_size_gb` is rounded to 2 decimals — OK. No `Decimal`/`datetime` leakage detected. Passes
- **Status**: PASS — recorded as a positive control

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| `efs_lifecycle_analysis` — `EstimatedMonthlyCost` | `module-const` (`0.30/$0.025`, `0.16/$0.0133`) | `services/efs_fsx.py:94-102` | **WARN** — constants match AWS price (verified) but `0.2 * standard + 0.8 * ia` distribution is invented | AWS Pricing API EFS storage prices → all 4 base prices confirm us-east-1 (see external log) |
| `fsx_optimization_analysis` — `EstimatedMonthlyCost` | `module-const` (4 dicts × 2-3 storage types) | `services/efs_fsx.py:106-116` | **CONDITIONAL** — Lustre SSD $0.154, Windows SSD $0.13, ONTAP SSD $0.144, OpenZFS SSD $0.20 — verifiable but missing FSx for NetApp ONTAP Capacity Pool, Lustre Persistent vs Scratch differentiation, Windows MAZ vs SAZ | AWS FSx pricing pages would need separate validation per file-system type |
| `efs_idle_systems.EstimatedSavings` | `module-const` (`$size_gb * 0.30`) | `services/efs_fsx.py:498` | **FAIL** — flat $0.30/GB assumes Standard tier always; ignores One Zone ($0.16); never multiplied by `pricing_multiplier`; emitted as STRING into `EstimatedSavings` only (no numeric `EstimatedMonthlySavings` field) | n/a — code-only |
| `efs_archive_storage.EstimatedSavings` | `arbitrary` ("Up to 94% for cold data") | `services/efs_fsx.py:471` | **FAIL** — percentage placeholder, no $ amount; scope-rule violation | n/a |
| `efs_one_zone_migration.EstimatedSavings` | `arbitrary` ("47% cost reduction") | `services/efs_fsx.py:484` | **FAIL** — percentage placeholder; scope-rule violation | n/a |
| `efs_throughput_optimization.EstimatedSavings` | `arbitrary` ("20-50% on throughput costs") | `services/efs_fsx.py:511` | **FAIL** — percentage range placeholder; scope-rule violation | n/a |
| `fsx_intelligent_tiering.EstimatedSavings` | `arbitrary` ("Significant for infrequently accessed data") | `services/efs_fsx.py:541` | **FAIL** — qualitative placeholder, "Significant" is meaningless to a dollar-aggregator | n/a |
| `fsx_storage_type_optimization.EstimatedSavings` | `arbitrary` ("~85% storage cost reduction") | `services/efs_fsx.py:557` | **FAIL** — percentage placeholder | n/a |
| `fsx_data_deduplication.EstimatedSavings` | `arbitrary` ("30-80% storage capacity reduction") | `services/efs_fsx.py:568` | **FAIL** — percentage range | n/a |
| `fsx_single_az_migration.EstimatedSavings` | `arbitrary` ("~50% cost reduction") | `services/efs_fsx.py:581` | **FAIL** — percentage placeholder | n/a |
| `fsx_backup_retention.EstimatedSavings` | `arbitrary` ("Reduce backup storage costs") | `services/efs_fsx.py:595` | **FAIL** — qualitative placeholder | n/a |
| Adapter savings rollup `cost * 0.40` | `derived` from cost | `services/adapters/file_systems.py:72, 77` | **FAIL** — undocumented 40% multiplier applied uniformly to every EFS and FSx record's `EstimatedMonthlyCost` to synthesize savings. Multiplier has no AWS basis | n/a |

### L2-001 `pricing_multiplier` **double-applied** on FSx path  [CRITICAL]
- **Check**: L2.3.1 — must not double-apply multiplier
- **Evidence**:
  - `services/efs_fsx.py:116` — `_estimate_fsx_cost` returns `capacity_gb * storage_price * pricing_multiplier` (multiplier already applied)
  - `services/adapters/file_systems.py:73-76`:
    ```python
    for rec in fsx_recs:
        cost = rec.get("EstimatedMonthlyCost", 0)
        if ctx.pricing_multiplier:        # 1.0 is truthy → always True
            cost *= ctx.pricing_multiplier   # ← SECOND multiplication
        savings += cost * 0.40
    ```
- **Why it matters**: in `eu-west-1` (multiplier ≈ 1.08) the FSx cost is multiplied to `1.08 × 1.08 ≈ 1.166×` reality — savings are 16.6% over-reported. In `ap-northeast-1` (multiplier ≈ 1.14) → `1.30×` over-reported. The bug is hidden in us-east-1 because `1.0 × 1.0 = 1.0`. Identical pattern is **NOT** present on the EFS path at line 65-72 (which simply takes the rec's `EstimatedMonthlyCost` as-is) — only FSx is double-multiplied
- **Recommended fix**: delete the `if ctx.pricing_multiplier: cost *= ctx.pricing_multiplier` block in the FSx loop. `_estimate_fsx_cost` already applies the multiplier (correctly, per L2.3.2 since FSx is module-const)

### L2-002 Adapter savings rollup uses arbitrary `* 0.40` multiplier  [HIGH]
- **Check**: L2.1 — `arbitrary` values forbidden in `total_monthly_savings`
- **Evidence**: `services/adapters/file_systems.py:72, 77` — `savings += cost * 0.40` for every EFS and FSx rec, regardless of opportunity type (lifecycle, idle, archive, throughput, dedup, etc.). The 0.40 has no AWS-documented basis. EFS Standard→IA delta is `(0.30-0.025)/0.30 = 0.917` (92%, not 40%); EFS Standard→Archive is `(0.30-0.008)/0.30 = 0.973`; FSx Windows SSD→HDD is `(0.13-0.08)/0.13 = 0.385` (close to 40% but coincidental)
- **Why it matters**: a uniformly applied savings multiplier across heterogeneous opportunity classes is precisely the "percentage-range estimate without per-account baseline" pattern the cost-only scope rule (added 2026-05-14) was added to ban. Same shape as the EBS adapter's old `cost × 0.40` that was removed
- **Recommended fix**: model per-opportunity savings the way S3 does — `EFS_SAVINGS_FACTORS = {"lifecycle_missing_ia": 0.92, "lifecycle_missing_archive": 0.97, "idle": 1.00, "one_zone_migration": 0.47, ...}` grounded in AWS pricing-page documentation, applied per rec, not as a global 0.40

### L2-003 EFS Archive tier ($0.008/GB-Mo) not modeled  [HIGH]
- **Check**: L2.1 — pricing model gap for a documented AWS price
- **Evidence**: `services/efs_fsx.py:94-102` — `_estimate_efs_cost` only knows about Standard ($0.30) and IA ($0.025). EFS Archive ($0.008/GB-Mo) and EFS Elastic-Throughput IA ($0.016/GB-Mo) are listed by AWS pricing but absent from the dict. The shim emits an `efs_archive_storage` recommendation (line 465) but cannot quantify the savings because it can't price the destination tier
- **Why it matters**: archive savings are the largest single optimization the adapter recommends (Standard → Archive = 97% reduction) and the adapter ships zero dollar amount on those recs — they appear in `total_recommendations` but contribute $0 to `total_monthly_savings`. Same metric-divergence as the AMI `unused_amis` finding
- **Recommended fix**: add Archive tier price to `_estimate_efs_cost` and compute savings as `(current_tier_price - archive_price) × size_gb × archive_eligible_fraction`. Alternative: add `get_efs_monthly_price_per_gb(tier="archive")` to PricingEngine and use it

### L2-004 Eight enhanced-check categories emit non-dollar `EstimatedSavings`  [HIGH]
- **Check**: cost-only scope rule (`docs/audits/AUDIT_PROMPT.md` §Scope Rule)
- **Evidence**: 8 of 10 enhanced-check rec generators emit strings like "Up to 94% for cold data" (line 471), "47% cost reduction" (484), "20-50% on throughput costs" (511), "Significant for infrequently accessed data" (541), "~85% storage cost reduction" (557), "30-80% storage capacity reduction" (568), "~50% cost reduction" (581), "Reduce backup storage costs" (595). Only `efs_idle_systems` (line 498) emits a `$N.NN/month` string — and even that is flat $0.30/GB without multiplier
- **Why it matters**: per scope rule "Every check must produce a concrete account-specific $ saving … percentage-range estimate without per-account baseline" → REMOVE. The 8 categories should either be quantified (preferred) or removed. Currently they inflate `total_recommendations` while contributing nothing to `total_monthly_savings`
- **Recommended fix**: same pattern as L2-002 — compute per-rec savings from `_estimate_efs_cost`/`_estimate_fsx_cost` + a documented factor. Emit `EstimatedSavings: "$X.YY/month"` and a numeric `EstimatedMonthlySavings` field. The adapter at line 79 already reads `EstimatedMonthlySavings` — it just isn't populated

### L2-005 `pricing_engine.get_efs_monthly_price_per_gb()` is unreachable dead code  [MEDIUM]
- **Check**: L2.1 / L2.4 — live pricing must be reachable when configured
- **Evidence**: `services/adapters/file_systems.py:67-71`:
  ```python
  if ctx.pricing_engine is not None and cost == 0:
      size_gb = rec.get("SizeGB", rec.get("StorageCapacity", 0))
      if size_gb > 0:
          price = ctx.pricing_engine.get_efs_monthly_price_per_gb()
          cost = size_gb * price
  ```
  `cost` comes from `rec["EstimatedMonthlyCost"]` which is always set by `_estimate_efs_cost` (services/efs_fsx.py:260). `_estimate_efs_cost` returns `(0.2 * std + 0.8 * ia) * mult` which is `0` only when `size_gb == 0`. But the branch's own guard `if size_gb > 0` then trips. So the live-pricing path is effectively dead
- **Why it matters**: a reader scanning the adapter sees "pricing_engine is used" and trusts the live path. Reality: every dollar comes from the hardcoded shim constants. Audit summary (`docs/audits/SUMMARY.md` line 47) lists this adapter as "PricingEngine (per-GB)" — which is technically true for the branch but materially false for the actual numbers
- **Recommended fix**: invert the branch — make `_estimate_efs_cost` consult `ctx.pricing_engine.get_efs_monthly_price_per_gb()` first and only fall back to the module-const dict when the engine returns 0 or raises. The S3 adapter pattern at `services/s3.py:521-547` is a good template

### L2-006 `pricing_multiplier` correctly applied on EFS module-const path  [LOW]
- **Check**: L2.3.2 — module-const path applies multiplier
- **Evidence**: `services/efs_fsx.py:102` — `return base_cost * pricing_multiplier` ✓ for EFS. `services/efs_fsx.py:116` — same for FSx ✓
- **Status**: PASS — recorded as a positive control. The double-multiply (L2-001) is the adapter's fault, not the shim's

### L2-007 `_estimate_efs_cost` 80/20 IA/Standard split is invented  [HIGH]
- **Check**: L2.1 — `derived` only when ratio is documented
- **Evidence**: `services/efs_fsx.py:101` — `(size_gb * 0.2 * standard_price) + (size_gb * 0.8 * ia_price)` assumes every EFS file system is 20% Standard / 80% IA, regardless of whether a lifecycle policy exists. For an account that has **no lifecycle policy enabled** (the very condition the rec is recommending against), 100% of the data lives in Standard — so the "current cost" is *understated* by ≈64%, which means the savings delta presented to the user is **smaller** than the real opportunity
- **Why it matters**: the recommendation reads "enable lifecycle policy to save 80%" but the dollar figure attached to it assumes the policy is already enabled at 80% IA. The two halves contradict each other
- **Recommended fix**: split into two estimates — `current_cost = size_gb * standard_price * pricing_multiplier` (no policy) vs `projected_cost = size_gb * (0.2 * std + 0.8 * ia) * pricing_multiplier` (after policy enabled). Savings = current − projected. Conditional on `HasIAPolicy` flag the shim already collects (line 244)

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-003 / L2-007 | `mcp__aws-pricing-mcp-server__get_pricing` | `service_code=AmazonEFS, region=us-east-1, productFamily=Storage` | $0.30 Standard, $0.025 Standard-IA, $0.16 One Zone, $0.0133 One Zone-IA, **$0.008 Archive**, $0.016 IA-ElasticThroughput (all $/GB-Mo) | confirms shim's 4 base constants are numerically right and pins the missing Archive + IA-ET prices to add |
| L2-002 | shim source vs price math | `_estimate_efs_cost(100GB, 1.0, False) = 4.0` vs adapter savings = `4.0 × 0.40 = $1.60/mo` | adapter says lifecycle policy saves $1.60/mo on a 100 GB file system; actual Standard→IA on that file is `100 × (0.30 − 0.025) = $27.50/mo` (assuming 100% migration) | refutes the 0.40 multiplier; understates savings by ~17× on the lifecycle case |
| L1-002 | `boto3.client("fsx").can_paginate("describe_file_systems")` | live boto3 introspection | `True` | confirms paginator exists; shim should use it |
| L1-002 | `boto3.client("fsx").can_paginate("describe_file_caches")` | live boto3 introspection | `False` | confirms NO paginator for FileCaches → current `fsx.describe_file_caches()` call is correct |

---

## L3 — Reporting findings

### Field-presence table

| Source | Renderer | Required fields | Missing / mis-routed fields |
|---|---|---|---|
| `efs_lifecycle_analysis` | `render_file_systems` (Phase A) — routed to "EFS No Lifecycle" group | `FileSystemId`, `Name`, `SizeGB`, `HasIAPolicy` | OK |
| `fsx_optimization_analysis` | Phase A — routed to "FSx Optimization" group when `FileSystemType` set, else **lost** | `FileSystemId`, `Name`, `SizeGB`, `FileSystemType` | `FileCacheId` recs (`services/efs_fsx.py:407`) have no `FileSystemId` → `seen_fs.get("Unknown")` collision → all FileCache recs collapse into one bucket |
| `enhanced_checks` | Phase A — routed by `CheckCategory` to 4 buckets; 7 of 10 categories fall through to "EFS Optimization" catch-all | `CheckCategory` | "Idle EFS File System", "EFS Throughput Optimization", "FSx Intelligent-Tiering", "FSx Storage Type Optimization", "FSx Data Deduplication", "FSx Single-AZ Migration", "FSx Backup Retention" all collapse into a single "EFS Optimization" group despite the FSx ones being unrelated to EFS |

### L3-001 7 of 10 enhanced-check categories route to wrong group  [HIGH]
- **Check**: L3.2.4 — `optimization_descriptions` must cover every distinct `CheckCategory`
- **Evidence**: `reporter_phase_a.py:68-75`:
  ```python
  for rec in enhanced_recs:
      category = rec.get("CheckCategory", "")
      if category == "EFS Archive Storage Missing":   # matches "EFS Archive Storage Missing"
          ...
      elif category == "EFS One Zone Migration":      # matches "EFS One Zone Migration"
          ...
      else:                                            # ← catches everything else
          grouped_fs.setdefault("EFS Optimization", []).append(rec)
  ```
  The shim emits 10 distinct `CheckCategory` values (line 472, 485, 499, 512, 542, 558, 569, 582, 596 — plus the 2 above). 7 fall into the generic `"EFS Optimization"` bucket — labeled "EFS" even when the rec is about FSx Lustre or FSx Windows
- **Why it matters**: a FSx Windows deduplication recommendation renders as `<h4>EFS Optimization (1 file system)</h4><p><strong>Recommendation:</strong> Review FSx configuration ...</p>` — header lies, recommendation text is generic, and the user can't tell what to actually do. The 4 `optimization_descriptions` keys exposed at `services/efs_fsx.py:49-90` (`fsx_storage_optimization`, `fsx_capacity_rightsizing`, `fsx_ontap_features`, `fsx_lustre_optimization`, `file_cache_optimization`) are **never used by the Phase A renderer** — the renderer only knows the 4 hardcoded groups
- **Recommended fix**: extend the `if/elif` chain in `render_file_systems` to cover all 10 `CheckCategory` values, OR refactor to drive group routing from a category→bucket dict, OR delete the unused `optimization_descriptions` entries

### L3-002 FileCache recs collapse into single `seen_fs["Unknown"]` slot  [HIGH]
- **Check**: L3.2.2 — renderer key resolution must reflect the rec's identifier
- **Evidence**: `reporter_phase_a.py:48-53`:
  ```python
  for rec in lifecycle_recs:
      fs_id = rec.get("FileSystemId", "Unknown")
      if fs_id not in seen_fs:
          seen_fs[fs_id] = rec
      elif rec.get("Name") and rec.get("Name") != "Unnamed":
          seen_fs[fs_id] = rec
  ```
  But `get_fsx_optimization_analysis` at `services/efs_fsx.py:406-417` emits FileCache recs with key `FileCacheId`, not `FileSystemId`. All FileCache recs read `fs_id = "Unknown"` and dedupe down to a single entry — only **one** FileCache surfaces in the HTML regardless of how many exist
- **Why it matters**: silent data loss in the report for accounts with multiple FileCaches
- **Recommended fix**: change line 49 to `fs_id = rec.get("FileSystemId") or rec.get("FileCacheId") or "Unknown"` — same `or`-chain the generic per-rec renderer uses

### L3-003 `optimization_descriptions` declared but never read by Phase A renderer  [MEDIUM]
- **Check**: L3.2.4 — declared descriptions should map to actual UI surface area
- **Evidence**: `services/adapters/file_systems.py:92` — `optimization_descriptions=get_file_system_optimization_descriptions()` returns 8 entries (`efs_lifecycle_policies`, `efs_unused_systems`, `efs_one_zone_migration`, `fsx_storage_optimization`, `fsx_capacity_rightsizing`, `fsx_ontap_features`, `fsx_lustre_optimization`, `file_cache_optimization`). The Phase A renderer at `reporter_phase_a.py:25-104` doesn't reference any of them — recommendation strings are hardcoded at lines 87-93
- **Why it matters**: the descriptions ship in the JSON payload (so a downstream consumer can read them) but the HTML report ignores them. Confusing both for maintainers and for anyone trying to keep the description copy in sync
- **Recommended fix**: either (a) have the Phase A renderer consume `optimization_descriptions` to derive the recommendation text, or (b) delete the descriptions from the adapter

### L3-004 No `priority`/`severity` on any rec; size-only sort  [LOW]
- **Check**: L3.2.3
- **Evidence**: zero `priority`, `severity`, or `Priority` fields set in `services/efs_fsx.py`. Phase A's `_priority_class` at `reporter_phase_a.py:14` returns "" for every rec → no CSS distinction between a 10 TB file system and a 100 MB one
- **Recommended fix**: derive `priority="high"` for `SizeGB > 1024`, `"medium"` for `100 < SizeGB <= 1024`, `"low"` otherwise

---

## Cross-layer red flags

**None auto-FAIL** under §4:
- Read-only (no writes)
- No `ClientRegistry` bypass
- No `ALL_MODULES` mutation
- No service-key collision
- Resources tagged with own-account IDs

However, **L2-001 (double-multiply)** + **L2-002 (arbitrary 40%)** + **L2-004 (8 categories with placeholder savings)** + **L2-007 (invented 80/20 split)** stack: a region-multiplier user in eu-west-1 sees a savings figure that is `~1.17× × 0.40 × ((cost-with-no-IA-policy understated by 64%) + placeholder-zero contributions)` — a number that is neither auditable nor reproducible against AWS billing. The summary table in `docs/audits/SUMMARY.md` lists this adapter as WARN; this audit demotes it to **FAIL**.

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/file_systems.py services/efs_fsx.py
All checks passed!

$ python3 -m mypy services/adapters/file_systems.py services/efs_fsx.py
(no errors specific to file_systems.py / efs_fsx.py)

# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m = next(x for x in ALL_MODULES if x.key == 'file_systems'); \
    print('key:', m.key); print('aliases:', m.cli_aliases); \
    print('clients:', m.required_clients()); print('cw:', getattr(m, 'requires_cloudwatch', False))"
key: file_systems
aliases: ('efs', 'file_systems')
clients: ('efs', 'fsx')
cw: False

# 3. FSx paginator availability (live boto3 introspection)
$ python3 -c "import boto3; c = boto3.client('fsx', region_name='us-east-1'); \
    print('describe_file_systems:', c.can_paginate('describe_file_systems')); \
    print('describe_file_caches:', c.can_paginate('describe_file_caches'))"
describe_file_systems: True
describe_file_caches: False

# 4. Offline + 5. Regression / snapshot tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k file_systems -v
============================= 4 passed, 122 deselected in 0.07s ==============================

# 6. PricingEngine has EFS but no FSx method
$ grep -n "get_efs_monthly\|get_fsx" core/pricing_engine.py
445:def get_efs_monthly_price_per_gb(self) -> float:
(no get_fsx_* method exists)
```

Tests pass — fixture confirms shape, not correctness of the dollar math. The double-multiply (L2-001) and 0.40 (L2-002) are masked because the golden was captured against the same flawed math in us-east-1 (where multiplier = 1.0).

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L2-001** — Delete the FSx-path double-multiply at `services/adapters/file_systems.py:75-76`. Single-line fix. Verify by running a scan with `--region eu-west-1` and confirming FSx savings drop by ~7.7%.
2. **[HIGH] L2-002** — Replace `cost * 0.40` with per-opportunity `EFS_SAVINGS_FACTORS` / `FSX_SAVINGS_FACTORS` dicts grounded in AWS pricing pages, applied per-rec inside the shim (S3 adapter pattern at `services/s3.py:104-122`).
3. **[HIGH] L2-003 / L2-007** — Add EFS Archive ($0.008/GB-Mo) and Standard→IA-Elastic-Throughput ($0.016) to `_estimate_efs_cost`. Split current-cost vs projected-cost so the savings delta on a no-policy file system reflects the *actual* opportunity, not the post-migration steady-state.
4. **[HIGH] L2-004** — Quantify the 8 placeholder-savings enhanced checks; emit numeric `EstimatedMonthlySavings` field on each. Or remove the categories per scope rule.
5. **[HIGH] L1-002** — Use `fsx.get_paginator("describe_file_systems").paginate()` in 3 sites (`services/efs_fsx.py:283, 379, 522`). Leave `describe_file_caches` as-is (no paginator exists).
6. **[HIGH] L1-003** — Add `AccessDenied` discrimination to all 7 `except Exception as e: ctx.warn(...)` sites. Route IAM gaps to `ctx.permission_issue`.
7. **[HIGH] L3-001** — Extend `render_file_systems` to route all 10 `CheckCategory` values to dedicated groups; FSx categories must not render under "EFS Optimization".
8. **[HIGH] L3-002** — Fix the FileCache dedup collision: `fs_id = rec.get("FileSystemId") or rec.get("FileCacheId") or "Unknown"`.
9. **[MEDIUM] L2-005** — Make the live-pricing path actually reachable inside `_estimate_efs_cost` rather than as a dead branch after the shim returns.
10. **[MEDIUM] L3-003** — Either consume `optimization_descriptions` in the renderer or delete them from the adapter.
11. **[MEDIUM] L1-004** — Remove the dead `isinstance(..., list)` branches in the adapter; lock shim return shape with a unit test.
12. **[LOW] L1-001** — Delete the module-level `print(...)` in `services/efs_fsx.py:14`; route adapter banner to `logger.debug`.
13. **[LOW] L3-004** — Derive `priority` from size tiers.
14. **[INFO]** — After fixes, re-capture goldens. Current goldens mask the FSx double-multiply because they were generated in us-east-1.
