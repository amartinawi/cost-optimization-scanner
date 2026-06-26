# Containers Adapter Cost-Audit Prompt

A deep, containers-specific audit brief in the same structure as the Lambda /
RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* containers code path (ECS Fargate + ECR; EKS
is out of scope, owned by `eks_cost`) so the auditor starts from facts, not a
blind find-replace. Scope is **strictly cost**: every emitted recommendation
must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`containers`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **EC2** adapter
(`services/adapters/ec2.py`) as the canonical model for the cross-source dedup
and the CO `$0`-placeholder→warning pattern; the recently-audited **Lambda**
adapter (`services/adapters/lambda_svc.py`) and **RDS** adapter
(`services/adapters/rds.py`) as the worked examples for CoH consumption via
`ctx.cost_hub_splits`, normalized-id cross-source dedup, metric-gated `$0`
advisory (`Counted=False`), arch-aware constants, and the test style I expect
(`tests/test_lambda_audit_fixes.py`, `tests/test_rds_audit_fixes.py`).

### NOTE on structure (containers is a multi-source ECS+ECR adapter; EKS is NOT here)
- `services/adapters/containers.py` → `ContainersModule.scan` aggregates **three
  sources** into the returned `ServiceFindings.sources`:
  - `enhanced_checks` — local heuristics from
    `services/containers.py:get_enhanced_container_checks` (ECS Fargate
    rightsizing + ECR untagged-layer reclaim).
  - `cost_optimization_hub` — `ctx.cost_hub_splits["containers"]`, populated by
    `core/scan_orchestrator.py` from CoH `currentResourceType` ∈
    {`EcsService`, `EcsTask`, `EcsCluster`} (see `type_map`, ~lines 86–101).
  - `compute_optimizer` — `services/advisor.py:get_ecs_compute_optimizer_recommendations`
    (ECS service rightsizing).
- **EKS is intentionally NOT analyzed here.** `get_enhanced_container_checks`
  emits only ECS + ECR; EKS is owned by the dedicated `eks_cost` adapter. EKS is
  scanned only for the `service_counts` extras (`get_eks_analysis` →
  `container_data["eks"]`), never as a counted/rendered rec. The aliases
  `cli_aliases=("containers","ecs","ecr")` route `--scan-only ecs`/`ecr` here;
  EKS routes to `eks_cost`.
- Savings carried structurally, not as a parse-string: heuristic recs get a
  numeric `EstimatedMonthlySavings` computed by `ContainersModule._quantify_rec`
  (Fargate rightsizing) and `Counted=True/False`. CoH/CO recs are summed from
  their own numeric `estimatedMonthlySavings`. There is **no
  `parse_dollar_savings`** path in this adapter (unlike network/ami).
- Pricing is **live, arch/OS-aware**: `core/pricing_engine.py`
  `get_fargate_vcpu_hourly(architecture, os)`, `get_fargate_gb_hourly(architecture, os)`,
  `get_fargate_windows_os_hourly()`, and `get_ecr_storage_gb_month()`. Each has a
  region-scaled fallback constant (see `FALLBACK_FARGATE_*` / `FALLBACK_ECR_GB_MONTH`,
  ~lines 305–327). The pure Fargate cost math lives in
  `services/containers_logic.py` (dependency-free, unit-testable).
- `SPOT_SAVINGS_FACTOR = 0.70` is **defined at module top of containers.py but
  appears unused** in the scan/quantify path — confirm whether the Fargate→Spot
  lever is dead code or wired somewhere, and treat a dead pricing constant as a
  finding (it implies an unimplemented or silently-dropped Spot recommendation).

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `containers.py` row (Live
    Pricing: "Fargate vCPU/mem hourly rates × 730 — Module constants").
    **Reconcile the doc against reality:** pricing is NOT bare module constants —
    it routes through `PricingEngine.get_fargate_vcpu_hourly` /
    `get_fargate_gb_hourly` / `get_fargate_windows_os_hourly` /
    `get_ecr_storage_gb_month` (live Pricing API, region-correct, with module
    *fallback* constants only on API failure). Note any drift and that the row
    omits the CoH + CO sources this adapter now consumes.
0b. Confirm module identity in `services/adapters/containers.py`:
    `key="containers"`, `cli_aliases=("containers","ecs","ecr")`,
    `display_name="Containers"`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`, `required_clients()` =
    `("ecs","eks","ecr","compute-optimizer","cloudwatch")`. The ECS rightsizing
    path reads CloudWatch (`AWS/ECS` CPU/MemoryUtilization) and ECR walks
    manifests — both are gated on `ctx.fast_mode` in `services/containers.py`;
    confirm the gating actually short-circuits the reads.
0c. CoH/CO **are** fair game here (unlike network): the adapter consumes both. A
    "missing CoH/CO source" finding is valid only if a real `EcsService`/`EcsTask`/
    `EcsCluster` CoH type or an ECS CO recommendation is being dropped. Confirm
    the `type_map` buckets all three ECS CoH types into `containers` and that the
    bucket is actually consumed (it is, line ~75) — i.e. NOT an orphan like
    elasticache/opensearch/redshift/s3.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/containers.py`
   (`ContainersModule.scan`, `_quantify_rec`, `fargate_rates`, `_fmt_fargate_size`,
   `_heuristic_resource_key`, `_co_placeholder`); `services/containers.py`
   (`get_container_services_analysis`, `get_ecs_analysis`, `get_eks_analysis`,
   `get_ecr_analysis`, `get_enhanced_container_checks`,
   `collect_ecs_fargate_rightsizing_recs`, `_ecs_service_rightsizing`,
   `_container_insights_enabled`, `_ecr_repo_reclaimable`,
   `_ecr_batch_get_manifests`, `_ecs_failure`/`_ecr_failure`,
   `estimate_fargate_rightsizing_monthly`); `services/containers_logic.py`
   (`quantify_fargate_rightsizing`, `snap_to_valid_fargate`, `snap_down_fargate`,
   `rightsize_fargate_target`, `fargate_task_hourly`,
   `compute_untagged_reclaimable_bytes`, `parse_manifest_layers`,
   `normalize_resource_name`, `dedupe_by_authority`);
   `services/advisor.py:get_ecs_compute_optimizer_recommendations` +
   `_normalize_ecs_co_rec` + `_compute_optimizer_opt_in_rec`;
   `core/pricing_engine.py` (the four container methods ~lines 909–1014 and
   `FALLBACK_FARGATE_*`/`FALLBACK_ECR_GB_MONTH` ~305–327);
   `core/scan_orchestrator.py` (`_HUB_SERVICES`, `type_map`);
   `core/result_builder.py`; and the reporter
   (`reporter_phase_b.py:_render_containers_enhanced_checks` ~line 942,
   `_containers_resource_table`, `_render_cost_hub_source`,
   `_render_compute_optimizer_source`, the `PHASE_B_HANDLERS` entries
   `("containers","enhanced_checks")` / `("containers","cost_optimization_hub")` /
   `("containers","compute_optimizer")`, `_PHASE_B_SKIP_PER_REC`,
   `SOURCE_TYPE_MAP`; `html_report_generator.py`).
2. List **every** cost check across the three sources, and for each give:
   trigger condition, the data source (CoH / CO / `AWS/ECS` CloudWatch / ECS-ECR
   describe-API / pure config), the savings formula + constant, and whether it is
   **counted** or a **$0 advisory** (`Counted=False`). Map each to its emitting
   SourceBlock. The known inventory to confirm:
   - **enhanced_checks → ECS Fargate rightsizing** (`ECS Rightsizing -
     Metric-Backed`): gated on Container Insights enabled + measured `avg CPU <
     20%` AND `avg mem < 30%` over 7d; quantified by `_quantify_rec` →
     `quantify_fargate_rightsizing` (peak-aware target snapped to a valid Fargate
     combo, priced `(cur_hourly − tgt_hourly) × 730 × task_count`). **Counted**
     when `LaunchType==FARGATE` and saving > 0; an EC2-launch-type task returns 0
     and is **dropped**.
   - **enhanced_checks → ECR untagged-layer reclaim** (`ECR Lifecycle
     Management`): `ReclaimableBytes` (deduplicated layers referenced ONLY by
     untagged images) × `get_ecr_storage_gb_month()`. **Counted** when > 0;
     repos over `ECR_MANIFEST_ANALYSIS_MAX_IMAGES (600)` emit `Advisory=True`,
     `Counted=False`.
   - **cost_optimization_hub**: CoH `EcsService`/`EcsTask`/`EcsCluster` recs,
     summed from `estimatedMonthlySavings`. **Counted**.
   - **compute_optimizer**: ECS service rightsizing, summed from
     `estimatedMonthlySavings` (already filtered to > 0 in the helper).
     **Counted**. The opt-in placeholder (`ResourceId=="compute-optimizer-service"`)
     is `ctx.warn`'d and dropped.
   Confirm what the adapter does NOT count: ECS desired-count over-provisioning,
   EKS node groups, the `get_ecs_analysis`/`get_eks_analysis`/`get_ecr_analysis`
   text "OptimizationOpportunities" (used only for `service_counts`, never
   emitted as recs).

### Phase 2 — Accuracy of every number (validate with MCP)
3. Re-derive each **counted** figure from the live AWS Pricing API. Validate each
   rate separately (AWS Pricing MCP `service_code="AmazonECS"`, region-scoped):
   - **Fargate vCPU/hr** (`usagetype` `*Fargate-vCPU-Hours:perCPU`): us-east-1
     x86 Linux = **$0.04048**, ARM = **$0.03238**, Windows
     (`*Fargate-Windows-vCPU-Hours:perCPU`) = **$0.046552** — these match the
     `FALLBACK_FARGATE_VCPU_HOURLY` constants exactly (verified 2026-06). Confirm
     `get_fargate_vcpu_hourly` maps `architecture` ("arm"/"arm64"/"graviton"→arm)
     and `os` ("win*"→windows) to the right SKU, and that ARM Graviton tasks are
     NOT priced as x86 (arm is ~20% cheaper).
   - **Fargate GB/hr** (`*Fargate-GB-Hours`): us-east-1 x86 Linux = **$0.004445**,
     ARM = **$0.00356**, Windows = **$0.0051117**. Confirm the memory leg uses GB
     (mem_mb/1024), not MB.
   - **Fargate Windows OS license** (`get_fargate_windows_os_hourly`,
     `*Fargate-Windows-OS-*:perCPU` ≈ $0.046/vCPU-hr): confirm it is added as
     `vcpu × win_os` in `fargate_task_hourly` ONLY for Windows tasks, and that it
     is applied to BOTH the current and target legs (so the delta is correct, not
     double-charged).
   - **ECR storage** (`get_ecr_storage_gb_month`, `AmazonECR`,
     `TimedStorage-ByteHrs`): ≈ **$0.10/GB-month**. Confirm `ReclaimableBytes` is
     converted GiB (`/1024**3`) consistently and priced once.
   - **Region scaling**: `PricingEngine` methods are region-correct; the fallback
     constants are us-east-1 × `self._fallback_multiplier`. Confirm `_quantify_rec`
     / `fargate_rates` do NOT additionally multiply by `ctx.pricing_multiplier`
     (that would double-apply region scaling on an already-region-correct engine
     value). CO/CoH savings are region-priced upstream and `_normalize_ecs_co_rec`
     correctly does NOT re-apply `pricing_multiplier` — confirm.
4. Confirm the savings basis is defensible from the report alone: each counted
   Fargate rightsizing rec carries a structured `AuditBasis` (current/target
   size, vcpu_rate, gb_rate, architecture, os, launch_type, formula) and ECR carries
   `{reclaimable_gib, rate, unit, basis, formula}`. Verify the snapped target is a
   *real* valid Fargate combo (`snap_to_valid_fargate` /
   `rightsize_fargate_target`) and that the peak-aware path
   (`PeakCPUPct`/`PeakMemoryPct` + `FARGATE_RIGHTSIZE_HEADROOM=1.2`) never sizes
   below measured peak. Confirm CoH/CO recs also surface a legible basis.

### Phase 3 — Duplication (no dollar counted twice)
5. **Cross-source (the main risk):** the same ECS service can appear in CoH,
   Compute Optimizer, AND the heuristic rightsizing check. Verify
   `dedupe_by_authority` (CoH > CO > heuristic) drops a heuristic rec whose
   `normalize_resource_name` collides with a CoH/CO resource. Scrutinize the key
   extractors: `coh_key` reads `resourceArn`/`ResourceArn`, `co_key` reads
   `resource_id`, `heuristic_key` (`_heuristic_resource_key`) reads
   ServiceName/ClusterName/TaskDefinitionArn/RepositoryName — confirm
   `normalize_resource_name` reduces an ECS service ARN
   (`...:service/cluster/web` → `web`) and the CO `resource_id`
   (`cluster/web` → `web`) to the SAME token so the dedup actually fires. A
   cluster-qualified name collision (two services named `web` in different
   clusters) would over-dedup — note it.
6. **Intra-source stacking:** can one ECS service emit both a metric rightsizing
   rec AND get summed again via CoH/CO without dedup? Can an ECR repo match more
   than once? Confirm `_quantify_rec` is called once per surviving heuristic rec
   and the CoH/CO sums are over the deduped populations.
7. **Cross-adapter:** ECS Fargate rightsizing total is handed to
   `commitment_analysis` via `ctx.fargate_rightsizing_monthly` (and
   `estimate_fargate_rightsizing_monthly` recomputes it when Containers didn't
   run). Confirm this is consumed as a *baseline input* for the Fargate Savings
   Plan model, NOT re-counted as a second savings line. Confirm EKS node groups
   counted by `eks_cost` are never also counted here (they aren't emitted), and
   that EC2-launch-type ECS task instances are owned by the EC2 adapter, not
   double-counted as Fargate here.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Pagination: `list_clusters`, `list_services`, `describe_repositories`,
   `describe_images`, `list_images` are paginated in
   `collect_ecs_fargate_rightsizing_recs` / `_ecr_repo_reclaimable`, but
   `get_ecr_analysis` (service_counts) calls `ecr.describe_repositories()`
   **un-paginated** — confirm whether the count under-reports on accounts with
   >100 repos (cosmetic, but a coverage gap).
9. Hardcoded gates that silently exclude valid resources:
   - ECS rightsizing requires **Container Insights enabled**
     (`_container_insights_enabled`) — services without it produce **no rec at
     all** (not even an advisory). The reporter has an `ECS Container Insights
     Required` group, but the adapter never emits that category → confirm it is a
     dead renderer branch and decide whether a `$0` "enable Container Insights"
     advisory should be emitted.
   - Thresholds `avg CPU < 20%` AND `avg mem < 30%` are hardcoded — a CPU-idle /
     memory-busy task is skipped. Confirm intentional.
   - ECR analysis is skipped entirely in `fast_mode` (advisory warn) and for
     repos with 0 untagged images or > 600 images. Confirm each skip is recorded.
   - EKS is wholly skipped (owned by `eks_cost`) — confirm no EKS rec leaks and
     the reporter's `Unused EKS Clusters` group is dead for this adapter.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return []`/
    `return 0.0`/`return None` fallback. Specifically:
    - `services/advisor.py:get_ecs_compute_optimizer_recommendations` — a CO error
      that is NOT `OptInRequiredException`/`not registered` (e.g. AccessDenied,
      throttling) is `logger.warning`'d and returns `[]` **without** any
      `ctx.warn`/`ctx.permission_issue`. This is the canonical silent-failure
      class — ECS rightsizing from CO vanishes with no record. Classify
      AccessDenied/Unauthorized → `ctx.permission_issue`, throttling/other →
      `ctx.warn`.
    - `services/adapters/containers.py:scan` wraps `get_container_services_analysis`
      and `get_enhanced_container_checks` in try/except → `ctx.warn` (OK, but a
      blanket warn loses the AccessDenied→permission_issue distinction the inner
      `_ecs_failure`/`_ecr_failure` make — confirm the inner classification still
      fires before the outer catch).
    - `services/containers.py` inner `except Exception` sites
      (`_ecs_failure`/`_ecr_failure` callers: list_clusters, list_services,
      describe_services, describe_task_definition, describe_clusters,
      CloudWatch metrics, describe_images, batch_get_image, describe_repositories)
      — confirm each records on `ctx`, and that `get_ecr_analysis` line ~241/242
      (`logger.warning` for image-count failure) is NOT a silent drop that should
      be a `ctx.warn`.
11. Pricing miss → `0.0`: if `fargate_rates` returns None (vcpu/gb rate ≤ 0) the
    rec quantifies to 0 and is **dropped** (good, not counted as $0). Confirm the
    ECR pricing miss path (`get_ecr_storage_gb_month` ≤ 0 → `ctx.warn` + return
    0.0 → dropped) never emits a `$0` counted finding.
12. **CloudWatch gating / fast-mode:** confirm the ECS Container Insights reads
    and ECR manifest walk are both skipped under `ctx.fast_mode` (they are, with a
    `ctx.warn`), and that throttling/permission failures on the CloudWatch reads
    are recorded (they go through `_ecs_failure`). `requires_cloudwatch=True` /
    `reads_fast_mode=True` are declared — confirm they match behavior.
13. Opt-in / `$0` nudges: confirm the CO opt-in placeholder is converted to
    `ctx.warn` and dropped (it is, mirroring EC2/RDS), and that the
    `ECR_MANIFEST_ANALYSIS_MAX_IMAGES` advisory and any future Container-Insights
    nudge land as `Counted=False`, not counted.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Source → handler:** the adapter emits `enhanced_checks`,
    `cost_optimization_hub`, `compute_optimizer`. Confirm all three have
    `PHASE_B_HANDLERS` entries (`_render_containers_enhanced_checks`,
    `_render_cost_hub_source`, `_render_compute_optimizer_source`) and that
    `containers` ∈ `_PHASE_B_SKIP_PER_REC` (no per-rec fallback) — so a source
    with no handler would render nothing. (All three are currently registered;
    verify no fourth source is added silently.)
15. **Category-name binding in `_render_containers_enhanced_checks`:** the
    grouping dict keys (`ECS Container Insights Required`, `ECS Rightsizing -
    Metric-Backed`, `ECS Over-Provisioned Services`, `Unused ECS Clusters`,
    `Unused EKS Clusters`, `ECR Lifecycle Missing`, `Other Optimizations`) must
    match the `CheckCategory` values the adapter actually emits. The adapter emits
    `ECS Rightsizing - Metric-Backed` (matches) and `ECR Lifecycle Management`
    (does NOT match `ECR Lifecycle Missing` — it falls through to the
    `"RepositoryName" in rec` branch). Confirm ECR recs still render and note the
    category-name drift + the dead `Container Insights Required`/`EKS`/`Unused ECS
    Clusters` branches.
16. **Counted == rendered:** `total_recommendations = len(enhanced) +
    len(cost_hub) + len(co)` (includes the `Counted=False` ECR advisory);
    `total_monthly_savings` sums only counted heuristic savings + CoH + CO.
    Confirm the per-tab headline shows the counted/advisory split, the advisory
    ECR rec renders but contributes $0, and the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings` +
    reconciliation footnote) equals the per-service total. Verify no rec is in the
    count but dropped from the table (or vice-versa) across all three sources.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to containers:
    `.venv/bin/python cli.py <region> --scan-only containers`
    then pass the JSON through
    `.venv/bin/python tools/scan_doctor.py <json> --service containers`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and resource appearing in >1 source. Reconcile the
    headline against the per-source sum. Caveats: most accounts need Container
    Insights enabled + a genuinely over-provisioned Fargate service to exercise
    the rightsizing path — try `--fast` to confirm the CW/manifest skip warnings,
    and a CO-enabled account to exercise the dedup. Use `.venv/bin/python` (3.14)
    — system `python3` lacks `datetime.UTC`.
18. For any duplication claim, prove it: show the same ECS service (normalized
    name) in two of {CoH, CO, heuristic}. For any accuracy claim, show the AWS
    Pricing API value (Fargate vCPU/GB/Windows, ECR storage) next to the
    scanner's constant and the engine result.

### Deliverable
- The complete check list (Phase 1.2), per source, with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add a `tests/test_containers_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: test the
  pure helpers directly (`quantify_fargate_rightsizing` boundaries,
  `snap_to_valid_fargate`/`snap_down_fargate`, `rightsize_fargate_target` peak
  headroom, `compute_untagged_reclaimable_bytes` layer dedup,
  `normalize_resource_name`, `dedupe_by_authority` authority order) and drive
  `ContainersModule.scan` with a `SimpleNamespace` ctx + monkeypatched
  `get_container_services_analysis`/`get_enhanced_container_checks`/CO helper +
  fake `cost_hub_splits`. Cover every fix: CO silent-failure classification,
  no-double-region-scale, ARM/Windows pricing, cross-source dedup,
  fast-mode skip, render wiring (all three sources), counted==rendered,
  advisory `$0` gating, SPOT_SAVINGS_FACTOR wiring/removal.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding (already present for Fargate + ECR — extend to any new
  counted line).
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for containers first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-failure / pricing / dedup bug in a sibling adapter
  out of scope (e.g. the EC2 CO helper), note it as a follow-up (don't fix
  unprompted).
- Update the `containers.py` row in `services/adapters/CLAUDE.md` to match reality
  (live Fargate/ECR Pricing-API methods + the CoH/CO sources, not "module constants").
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against
First, the UNIVERSAL catalogue (every adapter), then containers-specific items.

#### Universal (from prior audits)
- Usage savings computed from a config dimension alone (memory/size/capacity/RCU-WCU/DPU)
  with NO usage metric → fabricated $.
- Wrong architecture/edition/OS/license/node-type pricing (arm64 as x86; BYOL as
  license-included; Windows as Linux; SQL/Oracle edition default; reserved as on-demand).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`) instead of pinned filters.
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`, OR
  `pricing_multiplier` double-applied on an already-region-correct engine/CO path.
- Per-unit RATE string ($/GB, $/hour, $/request, $/1K) counted as a monthly total —
  must be rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier / free resource (Lambda free tier; Gateway VPC endpoints; free per-ENI IP;
  free backup allotment) recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority dedup
  CoH > CO > heuristic, by NORMALIZED resource id (strip ARN; mind version/alias/cluster-vs-instance).
- Two heuristic checks stacking on the same resource (rightsize + migrate discount the
  same bill), or SUBSET redundancy (one population ⊆ another) — fix by removal not dedup.
- Reduction factor instead of exact price delta (`price × factor` vs `current − target`);
  validated factors off 2-3×.
- $0 "enable X"/opt-in placeholder (CO `ResourceId=compute-optimizer-service`) counted
  as a recommendation instead of converted to `ctx.warn` and dropped.
- Metric-gated $0 nudge rendered as a COUNTED opportunity instead of advisory (`Counted=False`).
- Cost Hub: (a) a `currentResourceType` with no `type_map` bucket → dropped (warns only
  on a full scan); (b) a bucket populated but consumed by NO adapter → dropped with NO
  warning (dead-renderer tell; known orphans: elasticache / opensearch / redshift / s3).
- A source the adapter emits with no `PHASE_B_HANDLERS` entry in a
  `_PHASE_B_SKIP_PER_REC` service → renders nothing, silently.
- Render-time substring/category/Optimized/RI filter desyncing the headline from the
  visible cards (filter at the SOURCE, not at render).
- Coverage gated to a hardcoded family/type/size/state allowlist, only-running/
  only-provisioned, or a scaled-to-zero/idle resource flagged for savings.
- CloudWatch / Cost Explorer / CO / CoH permission or throttling failure logged via
  `logger` only, not recorded via `ctx.warn` / `ctx.permission_issue`
  (AccessDenied/Unauthorized/OptInRequired → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (and `reads_fast_mode` not declared);
  agent-metric dimension mismatch (CWAgent mem/disk under more dimensions than InstanceId
  → `get_metric_statistics` by InstanceId alone silently returns nothing).
- Heuristic that assumes a usage target ("shrink to 20GB") with no usage evidence.
- Cross-adapter overlap (same volume/IP/snapshot/ASG/instance/cluster in two tabs) —
  single responsibility; add to the dedup `covered` set.
- Spot/discounted resources priced at on-demand; Spot recommended without an explicit
  interruptible-workload signal.
- RI / SP buy recommendation overlapping a rightsizing lever — keep RI/SP advisory,
  rightsize first.
- Each counted finding must carry a structured AuditBasis (rate/region/metric-window/
  formula) so the number is defensible from the report alone; counted == rendered.

#### Containers-specific (found from the code)
- **CO helper silent failure** (`get_ecs_compute_optimizer_recommendations`):
  a non-opt-in CO error (AccessDenied/throttling) is `logger.warning`'d and
  returns `[]` with NO `ctx.warn`/`ctx.permission_issue` — ECS CO rightsizing
  silently vanishes.
- **`SPOT_SAVINGS_FACTOR = 0.70` is defined but unused** in containers.py — a
  dead pricing constant implying an unimplemented/dropped Fargate→Spot lever
  (the `get_ecs_analysis` text even advertises "Save 70%"). Either wire a
  defensible Spot delta (`describe_spot_price_history`-equivalent / published
  Fargate Spot rate, interruptible-workload-gated) or remove it.
- **Reporter category drift / dead branches**
  (`_render_containers_enhanced_checks`): the adapter emits `ECR Lifecycle
  Management` but the group key is `ECR Lifecycle Missing`; and the
  `ECS Container Insights Required` / `Unused ECS Clusters` / `Unused EKS
  Clusters` groups are never populated by this adapter (EKS is out of scope) →
  dead renderer branches and a category-name mismatch.
- **Container-Insights-gated coverage hole**: ECS Fargate rightsizing emits
  *nothing* (not even a `$0` advisory) when Container Insights is disabled, so the
  scan silently under-reports on the common case; consider a `Counted=False`
  "enable Container Insights for rightsizing" advisory.
- **Cluster-name collision in dedup**: `normalize_resource_name` reduces an ECS
  service to its last path segment, so two services with the same name in
  different clusters normalize to the same token — `dedupe_by_authority` could
  over-drop a legitimate heuristic rec, or under-dedup if CoH uses a different ARN
  shape than CO's `resource_id`.
- **Un-paginated `get_ecr_analysis` describe_repositories** (service_counts only)
  under-reports `ecr_repositories` on accounts with >100 repos (cosmetic count,
  not savings, but a coverage inconsistency vs the paginated reclaim path).
- **Windows OS license leg**: confirm `get_fargate_windows_os_hourly` is applied
  to both current and target legs (so the delta is correct) and only for Windows
  tasks — a one-sided application would mis-state the Windows Fargate saving.
- **`commitment_analysis` coupling**: `ctx.fargate_rightsizing_monthly` /
  `estimate_fargate_rightsizing_monthly` must feed the Fargate Savings Plan
  baseline as an input, never as a second counted savings line (cross-adapter
  double-count risk).

## PROMPT (end)
