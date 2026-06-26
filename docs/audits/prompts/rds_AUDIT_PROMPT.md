# RDS Adapter Cost-Audit Prompt

A deep, RDS-specific audit brief in the same structure as the Network / Lambda /
EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* RDS code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving. RDS is
the most mature multi-source adapter in the scanner — it is the *canonical
worked example* the aurora / dynamodb / redshift prompts reference for
cross-source dedup, RI demotion, and Cost-Hub consumption — so this audit is
mostly a regression check that the established invariants still hold.

---

## PROMPT (copy from here)

You are auditing the **`rds`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. RDS is itself the canonical
model the other database adapters copy, so hold it to the highest bar:
treat **`services/rds_logic.py`** as the reference for cross-source de-dup and
RI/backup demotion, and `tests/test_rds_audit_fixes.py` as the worked example of
the test style I expect.

### NOTE on structure (RDS is a multi-source aggregator)
- The adapter `services/adapters/rds.py` → `RdsModule.scan` fuses **three**
  savings sources and de-dups across them:
  - **Compute Optimizer** (inline, pulled via
    `services.rds.get_rds_compute_optimizer_recommendations` →
    `services/advisor.py`) — rightsizing/idle, source block `compute_optimizer`.
  - **Cost Optimization Hub** (consumed from `ctx.cost_hub_splits["rds"]`, the
    bucket the orchestrator fills from `RdsDbInstance` **and** `RdsDbCluster`
    `currentResourceType`s) — source block `cost_optimization_hub`.
  - **Enhanced heuristics** (`services.rds.get_enhanced_rds_checks`) — Multi-AZ
    disable, non-prod scheduling, old snapshots, RI opportunities, backup
    retention; source block `enhanced_checks`.
- All cross-source arithmetic lives in **`services/rds_logic.py:resolve_rds_findings`**:
  authority **Cost Hub > Compute Optimizer > heuristic**, keyed by
  `normalize_rds_arn` (strips ARN to bare DB name; snapshots keep a
  `snapshot:`/`cluster-snapshot:` prefix so they never collide with instances).
  Among surviving CO + heuristic findings, only the **single highest-savings**
  finding per DB is kept (they are alternative remediations, not redundant
  detections). Snapshots are independent keys, always counted.
- **Advisory (rendered but NOT summed)**: `ADVISORY_CATEGORIES =
  {"Reserved Instance Opportunities", "Backup Retention Optimization"}`. RI is
  the `commitment_analysis` tab's domain; backup retention cannot be quantified
  at scan time (free allotment = 100% of provisioned). Snapshot savings are an
  **upper bound** capped at the actual billed backup from Cost Explorer
  (`get_rds_backup_actuals` → `reconcile_snapshot_savings`).
- Pricing is **live** via `core/pricing_engine.py`:
  `get_rds_instance_monthly_price(engine, class, *, multi_az, license_model,
  aurora_io_optimized)` (pins `databaseEdition` + `licenseModel` +
  `deploymentOption` + Aurora `storage` SKU), `get_rds_monthly_storage_price_per_gb`,
  `get_rds_backup_storage_price_per_gb(engine)`. No hardcoded $ constants in the
  adapter; the shim embeds rates only through these methods.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`, find the `rds.py` row, and reconcile it
    against reality: it claims Multi-AZ-disable & non-prod scheduling are
    CloudWatch-gated (`DatabaseConnections`), no gp2→gp3 check, backup retention
    advisory, Aurora-aware snapshot/storage pricing, and an **intentional
    coverage-gap list** (Aurora Serverless v2 ACU, Aurora cluster rightsizing,
    read replicas, stopped instances, Extended Support). Confirm each claim in
    code; flag any drift.
0b. Confirm module identity in `services/adapters/rds.py`: `key="rds"`,
    `cli_aliases=("rds",)`, `display_name="RDS"`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`, `required_clients()=("rds","compute-optimizer",
    "cloudwatch","ce")`.
0c. RDS has **all three** advisory sources (CoH + CO + heuristic) plus Cost
    Explorer (`ce`). So "missing source" is fair game only for genuinely absent
    coverage; focus instead on the dedup authority order, RI/backup demotion,
    snapshot reconciliation, pricing-SKU accuracy, and the Aurora cross-adapter
    overlap (Phase 3.6).

### Phase 1 — Understand the code (read before judging)
1. Read the full RDS path: `services/adapters/rds.py`, `services/rds_logic.py`
   (`normalize_rds_arn`, `partition_enhanced`, `reconcile_snapshot_savings`,
   `resolve_rds_findings`, `ADVISORY_CATEGORIES`), `services/rds.py`
   (`get_enhanced_rds_checks`, `get_rds_compute_optimizer_recommendations`,
   `get_rds_instance_count`, `_snapshot_savings_text`,
   `RDS_OPTIMIZATION_DESCRIPTIONS`), `services/advisor.py`
   (`get_rds_compute_optimizer_recommendations`, `get_rds_backup_actuals`),
   `services/_savings.py` (`compute_optimizer_savings`, `parse_dollar_savings`),
   `core/pricing_engine.py` (RDS methods ~559–700, `_normalize_rds_license_model`),
   `core/scan_orchestrator.py` (`type_map`: `RdsDbInstance`/`RdsDbCluster`→`rds`),
   `core/result_builder.py`, and the reporters
   (`reporter_phase_b.py:_render_rds_compute_optimizer` ~440,
   `_render_rds_enhanced_checks` ~565, `_render_rds_ri_scenarios_table` ~506,
   `_render_generic_rds_rec` ~1526; `rds` is in `_PHASE_B_SKIP_PER_REC`).
2. List **every** cost check, and for each give: trigger condition, data source
   (CoH / CO / CloudWatch `DatabaseConnections` / describe-API / pricing API),
   the savings formula, the constant/factor, whether it is **counted or
   advisory**, and the source block it lands in. Confirm the known inventory:
   - Compute Optimizer rightsizing (counted, `compute_optimizer`).
   - Cost Hub rightsizing/idle/storage for instances **and clusters** (counted,
     `cost_optimization_hub`; RI/SP/`N/A` filtered by `_coh_is_renderable`).
   - Multi-AZ Optimization (counted, CW-gated on `DatabaseConnections`).
   - Non-Production Scheduling (counted, CW-gated; SQL Server now included,
     Aurora excluded).
   - Old RDS Snapshots / Old Aurora Cluster Snapshots (counted upper-bound,
     Aurora priced at the Aurora backup rate; 0-size → advisory, no `$0.00`).
   - Reserved Instance Opportunities (**advisory** — commitment_analysis).
   - Backup Retention Optimization (**advisory** — no fabricated $).

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive it from the live AWS Pricing API and
   confirm the SKU filters are correct and **deterministic** (pinned filters, not
   `MaxResults=1` over multiple matching rows):
   - **Instance price** `get_rds_instance_monthly_price`: confirm
     `databaseEngine`, `databaseEdition` (SQL Server Standard/Enterprise/Web,
     Oracle SE2/EE), `licenseModel` (License included vs **Bring your own
     license** — Oracle-EE defaults to BYOL+Enterprise), and `deploymentOption`
     (`Single-AZ` / `Multi-AZ` / `Multi-AZ (SQL Server Mirror)`). Verify Multi-AZ
     is **not** priced as Single-AZ. Verify Aurora `storage` SKU pins
     `Aurora IO Optimization Mode` vs `EBS Only` from the cluster `StorageType`
     (`aurora-iopt1`).
   - **Storage** `get_rds_monthly_storage_price_per_gb`: `gp3` →
     `General Purpose-GP3`, `gp2` → `General Purpose`. Confirm RDS gp2 and gp3
     base $/GB are equal (the documented reason there is **no** gp2→gp3 check —
     verify the assumption still holds in the API).
   - **Backup** `get_rds_backup_storage_price_per_gb(engine)`: Aurora
     (`Aurora MySQL`/`Aurora PostgreSQL`) ≈ **$0.021/GB-mo** vs standard
     (`MySQL`) ≈ **$0.095/GB-mo**, `productFamily=Storage Snapshot`. Confirm the
     snapshot upper-bound formula `size_gb × rate` and that a 0-size snapshot
     becomes an advisory string with **no** `$0.00`.
   - **Region scaling**: every PricingEngine method is region-correct — confirm
     the adapter does **NOT** re-multiply by `ctx.pricing_multiplier` on those
     paths (double-region bug). The multiplier should appear only on a fallback
     constant path, of which RDS has essentially none.
4. Record a structured **AuditBasis** (rate / region / engine / metric-window /
   formula) on each counted finding and confirm the existing ones
   (`test_multiaz_finding_carries_audit_basis`, snapshot `reconciliation_*`
   keys) are present and accurate. The number must be defensible from the report
   alone.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** confirm `resolve_rds_findings` keeps only the single
   highest-savings finding per normalized DB key across CO + heuristic, and that
   the Multi-AZ-disable and scheduling levers for the same DB cannot both be
   summed. Confirm snapshots use independent keys (so a snapshot and its parent
   DB don't suppress each other) — and that two snapshot checks can't both fire
   on one snapshot id.
6. **Cross-source:** confirm CoH suppresses that DB's CO and heuristic findings
   entirely (authority by `coh_keys`), keyed by `normalize_rds_arn` so a CoH bare
   `resourceId` and a CO/heuristic ARN converge. Prove RI/SP purchase recs are
   filtered out by `_coh_is_renderable` (they belong to `commitment_analysis`)
   and the heuristic RI category is advisory — an RI saving must never stack with
   a rightsizing/Multi-AZ saving on the same DB.
7. **Cross-adapter (the Aurora overlap — the big one):** the orchestrator buckets
   **`RdsDbCluster` → `rds`**, so Aurora clusters' Cost-Hub recs render in the
   **RDS** tab, while the separate **`aurora`** adapter
   (`services/adapters/aurora.py`) independently surfaces Aurora ACU/IO/instance
   findings. Determine whether the SAME Aurora cluster or its provisioned member
   instance can be counted in BOTH the RDS tab and the Aurora tab (Aurora
   provisioned-instance rightsizing/Graviton in `aurora.py` vs Cost-Hub /
   `get_enhanced_rds_checks` aurora handling here). Decide the single owner and
   confirm `core/result_builder.py` does not blindly sum across tabs.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Confirm full pagination of `describe_db_instances`, `describe_db_snapshots`,
   `describe_db_clusters`, `describe_db_cluster_snapshots`. Confirm the
   CloudWatch gate (`DatabaseConnections`) covers the engines it should and that
   `get_rds_instance_count` classifies `aurora-mysql`/`aurora-postgresql` as
   Aurora, not MySQL/Postgres.
9. Re-examine the **intentional coverage gaps** (Aurora Serverless v2 ACU, Aurora
   cluster rightsizing, read replicas, **stopped** instances, Extended Support).
   For each, confirm it is genuinely owned elsewhere (Serverless v2 / ACU by the
   `aurora` adapter) or is a real gap to log as a follow-up — and that a stopped
   instance is not silently priced as running, nor a scaled-down resource
   mis-handled. Reserved-capacity is a commitment lever; rightsizing comes first.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except`, `logger`-only, and `return []`/`0.0` path. The adapter
    wraps each source in `try/except → ctx.warn`; confirm the inner advisor
    (`get_rds_compute_optimizer_recommendations`) classifies `OptInRequired` →
    synthetic placeholder (converted to `ctx.warn` and dropped — see
    `compute-optimizer-service` guard), `AccessDenied`/`Unauthorized` →
    `ctx.permission_issue` (action `compute-optimizer:GetRDSDatabaseRecommendations`),
    other → `ctx.warn`. Confirm `get_rds_backup_actuals` records
    `ce:GetCostAndUsage` permission issues and that a CE gap (0/missing) leaves
    snapshot upper bounds **untouched** rather than zeroing real savings.
11. Confirm the CloudWatch `DatabaseConnections` reads in `get_enhanced_rds_checks`
    record `AccessDenied` → `ctx.permission_issue` (`cloudwatch:GetMetricStatistics`),
    skip+warn on empty datapoints ("no DatabaseConnections data"), and are
    skipped under `fast_mode` with a warning. A pricing miss returning `0.0` must
    drop the finding, never emit a `$0` counted card.
12. Confirm no opt-in/"enable Compute Optimizer" `$0` placeholder inflates the
    count (it is converted to a warning), and that backup-retention / 0-size
    snapshot advisories parse to `$0` and are excluded from the total but still
    rendered.

### Phase 6 — Reporting (one tab, counted == rendered)
13. RDS is in `_PHASE_B_SKIP_PER_REC` with three registered handlers:
    `("rds","compute_optimizer")`, `("rds","enhanced_checks")`,
    `("rds","cost_optimization_hub")`. Confirm every emitted source has a
    handler (no silent unrendered source) and that the RI scenarios table and
    snapshot/reconciliation caveats render
    (`_render_rds_ri_scenarios_table`, "advisory — not included in the tab
    total", "upper bound", "reconciled to actual billed backup",
    "size not reported (still billable)").
14. **Counted == rendered:** `resolve_rds_findings` returns
    `total_recommendations` and `total_monthly_savings` such that the rendered
    cards, the count, and the total agree (advisory recs rendered but $0). Verify
    the per-tab total equals the sum of COUNTED rendered findings, and reconcile
    the executive-summary headline + `_calculate_service_savings` +
    reconciliation footnote against the per-service total. Confirm no finding is
    counted but dropped from the table (or vice-versa).

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to RDS: `python3 cli.py <region> --scan-only rds`,
    then `python3 tools/scan_doctor.py <json> --service rds`. Triage every silent
    failure, `$0`/missing-savings finding (separate genuine advisory from
    leakage), and resource in >1 source/tab (especially an Aurora cluster in both
    the RDS and Aurora tabs). Reconcile the headline against the per-source sum.
    Caveats: try a region with SQL Server / Oracle BYOL and Aurora I/O-Optimized
    clusters to exercise the edition/license/storage SKUs; a region with no
    Compute Optimizer opt-in to exercise the placeholder→warning path; old
    snapshots to exercise the upper-bound + CE reconciliation path. Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
16. For any duplication claim, prove it (same normalized DB id in two sources, or
    the same Aurora cluster in two tabs). For any accuracy claim, show the AWS
    Pricing API value (engine/edition/license/deploymentOption SKU) next to the
    scanner's number.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked and the
  source block named.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs** (the documented coverage gaps are tradeoffs). End with an ID'd fix
  plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Extend `tests/test_rds_audit_fixes.py` (mirrors `tests/test_lambda_audit_fixes.py`):
  unit-test the pure helpers directly (`normalize_rds_arn`, `partition_enhanced`,
  `reconcile_snapshot_savings`, `resolve_rds_findings`, `_snapshot_savings_text`)
  and drive `RdsModule.scan` with a `SimpleNamespace` ctx + monkeypatched source
  helpers + fake boto3/pricing/CloudWatch/CE clients. Cover every fix.
- Record a structured **AuditBasis** on each counted finding; keep counted ==
  rendered.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the RDS golden fixture first; refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) only when a rendering change is intentional, and say so.
- If you find the same bug class in `aurora`/`dynamodb`/`redshift`, note it as a
  follow-up (don't fix unprompted). Update the `rds.py` row in
  `services/adapters/CLAUDE.md`. Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings computed from a config dimension alone (capacity/size) with NO
  usage metric → fabricated $.
- Wrong engine/edition/OS/license pricing (BYOL priced as license-included;
  SQL/Oracle edition default; Multi-AZ priced as single-AZ; Aurora I/O-Optimized
  vs Standard).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`) instead of
  pinned filters.
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`,
  OR `pricing_multiplier` double-applied on an already-region-correct engine path.
- Per-unit RATE string ($/GB, $/hour) counted as a monthly total — must be
  rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier / free allotment (backup ≤100% of provisioned) recommended for a
  saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic, by NORMALIZED resource id (strip ARN; mind cluster
  vs instance vs snapshot namespace).
- Two heuristic checks stacking on the same resource, or SUBSET redundancy — fix
  by removal not dedup.
- Reduction factor instead of exact price delta (price×factor vs current−target).
- $0 "enable X"/opt-in placeholder (CO `ResourceId=compute-optimizer-service`)
  counted instead of converted to `ctx.warn` and dropped.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (Counted=False).
- Cost Hub: (a) a `currentResourceType` with no `type_map` bucket → dropped
  (warns only on full scan); (b) a bucket populated but consumed by NO adapter →
  dropped with NO warning.
- A source the adapter emits with no PHASE_B handler in a
  `_PHASE_B_SKIP_PER_REC` service → renders nothing, silently.
- Render-time filter desyncing the headline from visible cards (filter at SOURCE).
- Coverage gated to a hardcoded family/type/state allowlist, only-running, or a
  scaled-to-zero/stopped resource mishandled.
- CloudWatch/Cost Explorer/CO/CoH permission or throttling failure logged via
  logger only, not recorded via `ctx.warn`/`ctx.permission_issue`
  (AccessDenied/Unauthorized/OptInRequired → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic that assumes a usage target with no usage evidence.
- Cross-adapter overlap (same cluster/instance/snapshot in two tabs) — single
  responsibility; add to the dedup covered set.
- Reserved Instance / Savings Plan buy recommendation overlapping a rightsizing
  lever — keep RI/SP advisory, rightsize first.
- Each counted finding must carry a structured AuditBasis (rate/region/engine/
  metric-window/formula); counted == rendered.

**RDS-specific items discovered in the code (verify each):**
- **Aurora cluster cross-tab overlap (HIGH):** `RdsDbCluster` is bucketed to the
  **rds** Cost-Hub split (`scan_orchestrator.type_map`), so Aurora cluster
  findings surface in the RDS tab while the `aurora` adapter also reports Aurora.
  Prove/disprove a provisioned Aurora instance or cluster counted in both tabs;
  there is no cross-adapter `covered` set spanning rds↔aurora.
- **RI demotion correctness:** confirm the heuristic `Reserved Instance
  Opportunities` category and CoH `PurchaseReservedInstances`/`PurchaseSavingsPlans`
  are BOTH excluded from the RDS total (advisory) — `ADVISORY_CATEGORIES` +
  `_coh_is_renderable`. A regression here double-counts commitment savings
  against rightsizing.
- **Snapshot upper-bound vs actual:** snapshot savings use provisioned
  `AllocatedStorage` (an over-estimate of actual backup bytes). Validate the
  Cost-Explorer cap (`reconcile_snapshot_savings`) is per-engine-pool
  (`standard`/`aurora`), that a 0/missing CE actual is treated as "no data"
  (never zeroes savings), and that a 0-size snapshot is advisory (no `$0.00`).
- **License-model threading:** the instance's `LicenseModel` must reach
  `get_rds_instance_monthly_price`; an Oracle-EE / SQL Server default must not
  silently fall back to a "No license required" row. Validate against the live
  Oracle/SQL Server SKUs (BYOL has no license-included row for Oracle-EE).
- **Scheduling engine scope:** Non-Production Scheduling now covers SQL Server
  but excludes Aurora — confirm Aurora is excluded because the `aurora` adapter
  owns it, not by accident, and that the idle gate (`DatabaseConnections`) is
  real evidence, not a name-based `dev`/`test` guess alone.
- **gp2==gp3 assumption:** the deliberate absence of a gp2→gp3 RDS storage check
  rests on equal base $/GB — re-validate against the live Pricing API; if AWS has
  diverged, the missing check is now a real gap.

## PROMPT (end)
