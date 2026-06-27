# Aurora Adapter Cost-Audit Prompt

A deep, Aurora-specific audit brief in the same structure as the Network / RDS /
Lambda audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Aurora code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`aurora`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the recently-audited
**RDS** adapter (`services/adapters/rds.py`, `services/rds_logic.py`,
`tests/test_rds_audit_fixes.py`) as the canonical sibling: it is the worked
example for cross-source dedup, Cost-Hub consumption, RI demotion, and the
`AuditBasis` / advisory-`$0` patterns. Aurora is closely entangled with RDS
(see Phase 3), so RDS is the reference you reconcile against throughout.

### NOTE on structure (Aurora is a self-contained CloudWatch adapter)
- The adapter `services/adapters/aurora.py` → `AuroraModule.scan` analyzes
  Aurora DB clusters and their provisioned member instances directly. It emits
  **three** source blocks: `serverless_v2`, `io_tier_analysis`,
  `instance_optimization`.
- Helpers: pure class-sizing math in **`services/aurora_logic.py`**
  (`parse_instance_class`, `is_graviton_family`, `graviton_equivalent`,
  `rightsize_target_size`, `_SIZE_VCPU`, `_GRAVITON_FAMILY`, `RIGHTSIZE_HEADROOM`).
  There is **NO** `services/aurora.py` legacy shim — the adapter and
  `aurora_logic.py` are the whole path.
- Pricing: `_get_acu_hourly(ctx)` calls **`PricingEngine.get_aurora_acu_hourly()`**
  (region-correct) with a module fallback `ACU_HOURLY_FALLBACK = 0.06` ×
  `ctx.pricing_multiplier`; instance pricing reuses
  **`PricingEngine.get_rds_instance_monthly_price(engine, class)`** (the same
  method RDS uses). I/O-tier math uses two **module constants**:
  `IO_COST_PER_MILLION = 0.20` and `IO_OPTIMIZED_PREMIUM_PER_GB = 0.025`;
  `HOURS_PER_MONTH = 730`.
- Aurora consumes **neither Cost Optimization Hub nor Compute Optimizer directly**
  — but beware: the orchestrator buckets **`RdsDbCluster` → `rds`** (NOT
  `aurora`), so Aurora clusters' CoH recs flow into the **RDS** tab. Aurora has
  **no** `cost_hub_splits["aurora"]` bucket. All Aurora savings here are local
  heuristics priced via `PricingEngine`.
- Savings are carried as a numeric **`monthly_savings`** float per rec (and an
  `EstimatedSavings` string **only** on `instance_optimization` recs). The total
  is `sum(r["monthly_savings"])` — there is no `parse_dollar_savings` /
  `mark_zero_savings_advisory` boundary here, so a fabricated number is summed
  directly. Remember this for Phases 2 and 6.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. Aurora is **not** in the Live-Pricing
    table (it is implicit). Note that the `rds.py` row explicitly lists "Aurora
    Serverless v2 (ACU), Aurora cluster rightsizing, read replicas, stopped
    instances" as **RDS coverage gaps owned elsewhere** — "elsewhere" is this
    adapter. Confirm the division of labor and flag any double coverage.
0b. Confirm module identity in `services/adapters/aurora.py`: `key="aurora"`,
    `cli_aliases=("aurora",)`, `display_name="Aurora"`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`, `required_clients()=("rds","cloudwatch")`. Note the
    `stat_cards` (cluster_count / serverless_cluster_count / monthly_savings) and
    `grouping = GroupingSpec(by="check_category", label_path="check_type")`.
0c. Aurora has **no AWS advisory source** (no CoH/CO bucket of its own). So a
    "missing CoH/CO source" finding is NOT fair game — savings are expected to be
    locally derived. Focus on pricing accuracy, metric semantics, the
    fabricated-vs-evidenced boundary, the RDS cross-adapter overlap, and render
    wiring (Phase 6 — Aurora's three sources have **no** PHASE_B handler).

### Phase 1 — Understand the code (read before judging)
1. Read the full Aurora path: `services/adapters/aurora.py` (the module +
   `_get_acu_hourly`, `_describe_aurora_clusters`, `_get_cloudwatch_avg`,
   `_get_cloudwatch_sum`, `_get_cloudwatch_avg_max`, `_check_provisioned_instances`,
   `_check_serverless_v2`, `_check_io_tier`), `services/aurora_logic.py`,
   `core/pricing_engine.py` (`get_aurora_acu_hourly` ~876, `get_rds_instance_monthly_price`
   ~579, `FALLBACK_AURORA_ACU_HOURLY`), `core/contracts.py` (`GroupingSpec`,
   `StatCardSpec`, `SourceBlock`), `core/scan_orchestrator.py` (confirm there is
   NO `aurora` entry in `type_map`/`_HUB_SERVICES`; `RdsDbCluster`→`rds`),
   `core/result_builder.py`, and the reporter dispatch
   (`html_report_generator.py:_get_detailed_recommendations` ~3299–3362,
   `reporter_phase_b.py:render_generic_per_rec` ~1345 →
   `_render_generic_other_rec` ~1812). Note `aurora` is in the
   `html_report_generator` stat-card descriptor (~149) but **not** in
   `PHASE_B_HANDLERS`, **not** in `_PHASE_B_SKIP_PER_REC`, and **not** in
   `_PHASE_A_SERVICES` — so it renders via `render_generic_per_rec("aurora", …)`.
2. List **every** cost check across the three checks and for each give: trigger,
   data source (CloudWatch metric vs describe-API vs pricing), the exact savings
   formula + constant, whether it carries a real usage metric or is config-only,
   and the source block. The known inventory to confirm:
   - **Serverless v2 ACU waste** (`_check_serverless_v2`): `wasted_acu = max_acu ×
     (1 − avg_util/100)`; `monthly_savings = wasted_acu × acu_hourly × 730`,
     gated on `ServerlessV2CapacityUtilization` (avg over 14×1-day datapoints);
     emitted only when `> $1`.
   - **I/O tier** (`_check_io_tier`): `standard_io_cost = (monthly_io/1e6) × 0.20`,
     `optimized_premium = storage_gb × 0.025`, `savings = standard − premium`,
     gated on `VolumeReadIOPs + VolumeWriteIOPs` (14-day **Sum**); emitted only
     when `> $10`; multiplied by `pricing_multiplier`.
   - **Provisioned instance rightsizing** (`_check_provisioned_instances`):
     peak-aware downsize via `rightsize_target_size(vcpu, peak_cpu)` gated on
     `CPUUtilization` (avg<50 AND target smaller); savings = exact
     `cur_price − cand_price` delta.
   - **Provisioned instance Graviton** (`_check_provisioned_instances`):
     x86→ARM class change on the (possibly rightsized) class; savings = exact
     `base_price − grav_price` delta. Note rightsizing + Graviton **compound** on
     the same instance by design.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate each rate/constant against the live Pricing API:
   - **ACU**: confirm `get_aurora_acu_hourly` returns the per-ACU $/hr for
     Aurora Serverless v2 (≈ **$0.12/ACU-hr** us-east-1 for standard; **higher**
     for I/O-Optimized clusters) and that the `ACU_HOURLY_FALLBACK = 0.06`
     constant is correct — **0.06 looks ~2× low vs the published ~$0.12/ACU-hr**;
     verify and flag. Confirm `_check_serverless_v2` does **NOT** re-multiply by
     `pricing_multiplier` (helper already region-corrects), and that the fallback
     path applies the multiplier exactly once.
   - **I/O tier constants**: validate `IO_COST_PER_MILLION = 0.20` against Aurora
     Standard's **$0.20 per 1M I/O** and `IO_OPTIMIZED_PREMIUM_PER_GB = 0.025`
     against the I/O-Optimized **storage premium** (Aurora I/O-Optimized storage
     is ~ +$0.025/GB-mo over Standard storage AND charges no per-request I/O).
     Both are us-east-1 module constants embedded in the formula — confirm
     `pricing_multiplier` is applied (it is applied to `savings` in
     `_check_io_tier`, but is the **premium** side region-scaled correctly?).
   - **Instance price**: `get_rds_instance_monthly_price(engine, class)` is called
     **without** `multi_az` / `license_model` / `aurora_io_optimized` — confirm
     Aurora reader/writer instances are priced correctly (Aurora has no Multi-AZ
     deploymentOption per se, but I/O-Optimized clusters price instances higher).
     Flag that the I/O-Optimized storage mode is **not** threaded into instance
     pricing here even though RDS's own path pins it.
4. **Metric-semantics scrutiny (the core Aurora risk):**
   - `_check_io_tier` derives `monthly_io` from `VolumeReadIOPs`/`VolumeWriteIOPs`
     summed over 14 one-day datapoints, then `/14 × 30`. `VolumeReadIOPs` is an
     **average IOPS over the period**, not a count — confirm whether
     `Statistics=["Sum"]` with `Period=86400` actually yields total operations or
     a meaningless sum-of-averages. This determines whether the I/O saving is
     real or fabricated.
   - `_check_serverless_v2` treats `(1 − avg_util)` of `max_acu` as recoverable —
     but ACU billing is driven by **actual** ACU-seconds consumed, not by
     `max_acu`. Lowering `MaxCapacity` only saves money if the workload was
     *hitting* the ceiling; averaging utilization understates peaks. Confirm the
     saving is not double-counting headroom you never paid for, and that the
     recommended new Max ACU floor (`max(min_acu, max_acu×util×1.2)`) is sound.
   - Record a structured **AuditBasis** (rate / region / metric-window / formula)
     on each counted finding, as RDS/Lambda do — **none of the three Aurora
     checks currently emit an AuditBasis**; that is itself a finding.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** rightsizing + Graviton compound on one instance by design
   (Graviton prices the delta on the already-rightsized class) — confirm the
   math truly composes (no overlap) and cannot exceed the instance's full cost.
   Confirm serverless/IO/instance checks target disjoint cluster populations
   (a Serverless v2 cluster has no provisioned member to rightsize).
6. **Cross-adapter (the RDS overlap — the big one):** Aurora provisioned member
   instances are `describe_db_instances` rows the **RDS** adapter
   (`get_enhanced_rds_checks`, Cost Hub `RdsDbInstance`/`RdsDbCluster`→`rds`) may
   also surface. Determine whether the SAME Aurora instance/cluster can be counted
   in BOTH the Aurora tab (`instance_optimization`) and the RDS tab (Cost Hub /
   heuristic / Compute Optimizer). There is **no shared `covered` set** between
   the adapters. Decide the single owner (Aurora-specific levers here; generic
   rightsizing maybe RDS) and confirm `core/result_builder.py` does not blindly
   sum across tabs.
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*` helper in
   `html_report_generator.py` pulls Aurora clusters into a synthetic tab, and
   that an Aurora cluster's snapshots aren't double-counted by the RDS snapshot
   checks AND any Aurora clone/snapshot logic.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Confirm `_describe_aurora_clusters` paginates `describe_db_clusters` filtered
   to `AURORA_ENGINES = ("aurora","aurora-mysql","aurora-postgresql")` and that
   the instance loop paginates `describe_db_instances`. Confirm the
   `DBInstanceStatus == "available"` gate doesn't silently drop a billable
   stopped/`incompatible` instance that still incurs storage cost.
9. Re-examine coverage vs the module docstring's promises. The docstring lists
   **Clone/snapshot sprawl, Global DB replica lag, Backtrack window cost** — but
   the code only implements Serverless-v2 ACU, I/O tier, and provisioned-instance
   rightsizing/Graviton. Confirm the unimplemented checks are intentional gaps
   (and that `global_count` is only a stat, not a savings claim). Confirm an
   Aurora **Serverless v2** cluster with min==max (no headroom) and a
   **scaled-to-zero / auto-paused** cluster are not flagged for phantom savings.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except`, `logger`-only, and `return []`/`None` path:
    - `_describe_aurora_clusters` swallows BOTH the paginator and the fallback
      `describe_db_clusters` exceptions with a bare `except … pass` — a
      permission/throttle gap makes Aurora vanish from the report with **no**
      `ctx.warn`/`ctx.permission_issue`. Classify `AccessDenied`/`Unauthorized`
      → `ctx.permission_issue`, other → `ctx.warn` (mirror the RDS advisor).
    - `_get_cloudwatch_avg`, `_get_cloudwatch_sum`, `_get_cloudwatch_avg_max` each
      `except: pass → return None`; a CloudWatch `AccessDenied`/throttle is
      indistinguishable from "no data" and is never recorded on `ctx`. Confirm
      and classify.
    - The `scan` cluster loop wraps each cluster in `except → logger.warning`
      only (no `ctx.warn`).
    - `_check_provisioned_instances` records `describe_db_instances` failure via
      `ctx.warn` (good) but the per-instance pricing `except → cur_price = 0.0`
      silently skips — confirm a pricing miss never emits a `$0` counted card.
11. Confirm fast-mode handling: `_check_io_tier` returns `[]` immediately in
    `fast_mode` (silent — no warn); `_check_serverless_v2` skips the metric and
    returns `[]` when `avg_util is None`; `_check_provisioned_instances` warns
    once ("fast mode: skipping Aurora instance rightsizing metric reads") but
    still emits Graviton (metric-free). Verify each fast-mode skip is correct and
    that the IO-tier silent skip should also warn.
12. Confirm no metric-gated `$0` nudge is summed: every check emits only when
    `monthly_savings` clears its threshold (`>1`, `>10`, or a positive delta), so
    there should be no `$0` rec — verify there is genuinely no path that appends a
    rec with `monthly_savings <= 0`.

### Phase 6 — Reporting (one tab, counted == rendered)
13. **Source-name vs handler mismatch (CRITICAL — verify carefully):** Aurora
    emits `serverless_v2`, `io_tier_analysis`, `instance_optimization`, but
    **none** of these are in `PHASE_B_HANDLERS`. Because `aurora` is NOT in
    `_PHASE_B_SKIP_PER_REC` and NOT in `_PHASE_A_SERVICES`,
    `should_fallback_to_per_rec("aurora")` is True, so all three sources render
    via `render_generic_per_rec("aurora", …)` → the `else` branch
    `_render_generic_other_rec`. Trace what that renderer shows for each rec
    shape:
    - `instance_optimization` recs DO carry `CheckCategory`, `Recommendation`,
      and an `EstimatedSavings` **string** → they render with a savings line.
    - `serverless_v2` and `io_tier_analysis` recs have **NO** `CheckCategory`,
      **NO** `Recommendation`, and **NO** `EstimatedSavings` string — only
      `check_type`, `current_value`, `recommended_value`, `monthly_savings`,
      `reason`. `_render_generic_other_rec` will dump each field as a generic
      `<p>` and show no styled "Estimated Savings" line, so the **counted dollars
      do not appear as a savings figure on the card**. This is a render-desync:
      the tab headline includes serverless/IO savings the visible cards don't
      legibly present. Confirm and decide the fix (give serverless/IO recs an
      `EstimatedSavings` string + `CheckCategory`, or register a dedicated
      handler).
14. **GroupingSpec mismatch:** `grouping = GroupingSpec(by="check_category",
    label_path="check_type")` — but serverless/IO recs have no `check_category`
    key (they use `CheckCategory` only on instance recs, and `check_type`
    everywhere). Confirm the grouping does not silently bucket everything under a
    blank/Other category.
15. **Counted == rendered:** `total_recommendations = len(all_recs)` and
    `total_monthly_savings = sum(monthly_savings)`. Confirm the per-tab total
    equals the sum of the COUNTED rendered findings and reconcile the
    executive-summary headline (`_calculate_service_savings`) against it. Verify
    no rec is counted but invisible (or rendered but uncounted) across all three
    sources.

### Phase 7 — Tooling & evidence
16. Run a real scan scoped to Aurora: `python3 cli.py <region> --scan-only aurora`,
    then `python3 tools/scan_doctor.py <json> --service aurora`. Triage every
    silent failure, `$0`/missing-savings finding, and resource appearing in >1
    source/tab (the canonical case: an Aurora instance in both the Aurora and RDS
    tabs). Reconcile the headline against the per-source sum. Caveats: you need an
    account with a **Serverless v2** cluster (exercise ACU), a **provisioned**
    Aurora cluster with x86 members (exercise rightsizing/Graviton), and a
    cluster with I/O history (exercise the IO-tier path); CloudWatch metrics may
    be empty (exercise the None/skip paths). Use `.venv/bin/python` (3.14) —
    system `python3` lacks `datetime.UTC`.
17. For any duplication claim, prove it (same Aurora instance id in the Aurora
    `instance_optimization` source and an RDS source/tab). For any accuracy
    claim, show the AWS Pricing API ACU $/hr, the Aurora Standard $/1M-I/O, and
    the I/O-Optimized storage premium next to the scanner's constants.

### Deliverable
- The complete check list (Phase 1.2), per source block, with the metric/formula
  and "config-only vs metric-evidenced" marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with an ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add `tests/test_aurora_audit_fixes.py` mirroring `tests/test_rds_audit_fixes.py`
  / `tests/test_lambda_audit_fixes.py`: unit-test the pure helpers directly
  (`parse_instance_class`, `is_graviton_family`, `graviton_equivalent`,
  `rightsize_target_size`) and drive `AuroraModule.scan` with a `SimpleNamespace`
  ctx + fake boto3 RDS/CloudWatch clients + fake `PricingEngine` (Multi-AZ-style
  deterministic prices) + fake paginators. Cover every fix: silent-failure
  classification (`_describe_aurora_clusters`, CloudWatch helpers), ACU-rate
  validation, IO-metric semantics, RDS cross-adapter dedup, render wiring,
  AuditBasis on every counted rec, counted == rendered.
- For any check that assumes throughput/utilization with weak metric evidence
  (IO-tier sum-of-IOPS, Serverless ACU headroom), replace it with a sound
  CloudWatch read or demote it to a $0 advisory — respect `ctx.fast_mode` and
  never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the Aurora golden fixture first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same bug class in `rds`/`dynamodb`/`redshift`, note it as a
  follow-up (don't fix unprompted). Add an `aurora` row to the Live-Pricing table
  in `services/adapters/CLAUDE.md`. Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings computed from a config dimension alone (capacity/size/ACU) with
  NO usage metric → fabricated $.
- Wrong engine/edition/OS/license pricing (Multi-AZ priced as single-AZ; Aurora
  I/O-Optimized vs Standard storage mode not threaded into instance pricing).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`) instead of
  pinned filters.
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`,
  OR `pricing_multiplier` double-applied on an already-region-correct engine path.
- Per-unit RATE string ($/GB, $/hour, $/ACU) counted as a monthly total.
- Free-tier / free allotment recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic, by NORMALIZED resource id (strip ARN; cluster vs
  instance).
- Two heuristic checks stacking on the same resource, or SUBSET redundancy.
- Reduction factor instead of exact price delta (price×factor vs current−target).
- $0 "enable X"/opt-in placeholder counted instead of converted to `ctx.warn`.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (Counted=False).
- Cost Hub: (a) `currentResourceType` with no `type_map` bucket → dropped;
  (b) a bucket populated but consumed by NO adapter → dropped with NO warning.
- A source the adapter emits with no PHASE_B handler in a `_PHASE_B_SKIP_PER_REC`
  service → renders nothing, silently.
- Render-time substring/category/Optimized filter desyncing headline from cards.
- Coverage gated to a hardcoded family/type/size/state allowlist, only-available,
  or a scaled-to-zero/idle/auto-paused resource mishandled.
- CloudWatch/Cost Explorer permission or throttling failure logged via logger
  only, not recorded via `ctx.warn`/`ctx.permission_issue` (AccessDenied/
  Unauthorized/OptInRequired → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic that assumes a usage target with no usage evidence.
- Cross-adapter overlap (same cluster/instance in two tabs) — single
  responsibility; add to the dedup covered set.
- Reserved Instance / Savings Plan buy recommendation overlapping a rightsizing
  lever — keep RI/SP advisory, rightsize first.
- Each counted finding must carry a structured AuditBasis; counted == rendered.

**Aurora-specific items discovered in the code (verify each):**
- **Render desync (CRITICAL candidate):** `serverless_v2` and `io_tier_analysis`
  recs carry no `EstimatedSavings` string and no `CheckCategory`/`Recommendation`,
  yet contribute to `total_monthly_savings`; `_render_generic_other_rec` shows
  their `monthly_savings` only as an unstyled generic `<p>`. The headline dollars
  are not legibly attributed on the cards. (Phase 6.13.)
- **ACU fallback looks ~2× low:** `ACU_HOURLY_FALLBACK = 0.06` vs published
  ~$0.12/ACU-hr Aurora Serverless v2. If the live API is unavailable, the
  fallback understates ACU savings by ~half — validate and correct.
- **I/O metric semantics (HIGH):** `_check_io_tier` sums `VolumeReadIOPs`/
  `VolumeWriteIOPs` (an averaged IOPS rate) with `Statistics=["Sum"]` over daily
  periods to get "monthly_io" operations. Verify this yields true operation
  counts, not a meaningless sum-of-averages — the entire IO-tier saving depends
  on it.
- **Serverless ACU headroom assumption:** savings assume `(1−avg_util)×max_acu`
  is recoverable, but you are billed on consumed ACU-seconds, not `max_acu`.
  Lowering `MaxCapacity` saves only when the workload hit the ceiling — confirm
  the model isn't crediting headroom that was never billed.
- **RDS cross-adapter overlap (HIGH):** Aurora provisioned instances/clusters can
  appear in the RDS tab too (`RdsDbCluster`/`RdsDbInstance` → `rds` Cost-Hub
  bucket; `get_enhanced_rds_checks` covers aurora engines). No shared `covered`
  set. Prove/disprove double counting between the Aurora and RDS tabs.
- **No AuditBasis anywhere:** unlike RDS/Lambda, none of the three Aurora checks
  records a structured AuditBasis (rate/region/metric-window/formula). Counted
  numbers are not defensible from the report alone.
- **`_describe_aurora_clusters` total silence:** double `except … pass` hides any
  permission/throttle failure with no `ctx.warn` — Aurora silently disappears.
- **Docstring overpromise:** the module docstring advertises clone/snapshot
  sprawl, Global DB replica lag, and Backtrack window checks that are not
  implemented — confirm these are intentional gaps, not silently broken.

## PROMPT (end)
