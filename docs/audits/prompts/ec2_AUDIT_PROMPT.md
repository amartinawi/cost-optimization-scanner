# EC2 Adapter Cost-Audit Prompt

A deep, EC2-specific audit brief in the same structure as the Network / Lambda /
RDS audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* EC2 code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `ec2`:
> - AUDIT-METHOD: EC2 CoH recs render as a few action-GROUPED cards ('MigrateToGraviton (5 resources)') with every instance listed inside — a low card count vs many recs is NOT a render desync; their savings are in camelCase `estimatedMonthlySavings` (empty PascalCase string).
> - Consider attaching a structured `AuditBasis` to CoH recs so the largest chunk of the headline is defensible from the report alone.
> - DEDUP/COH: EC2 consumes both CoH (`ctx.cost_hub_splits["ec2"]`) and CO inline; audit the CoH > CO > heuristic dedup at normalized instance-id granularity (A1/A2) and confirm no additional EC2-related CoH `currentResourceType` (e.g. `Ec2AutoScalingGroup`) is orphaned without a consuming bucket (E2).

You are auditing the **`ec2`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. EC2 is
the **canonical reference adapter** for this codebase's cross-source dedup,
ASG-member dedup, and `$0`-placeholder→warning patterns — so audit it against
itself rigorously, and treat the **RDS** (`services/adapters/rds.py` +
`services/rds_logic.py`) and **Lambda** (`services/adapters/lambda_svc.py`)
adapters as the worked examples for the `AuditBasis` / advisory-`$0` discipline
and the test style I expect (`tests/test_ec2_audit_fixes.py`,
`tests/test_rds_audit_fixes.py`, `tests/test_lambda_audit_fixes.py`).

### NOTE on structure (EC2 is a 5-source multi-source adapter)
- `services/adapters/ec2.py` → `EC2Module.scan` aggregates **five SourceBlocks**:
  `cost_optimization_hub`, `compute_optimizer`, `asg_compute_optimizer`,
  `enhanced_checks`, `advanced_ec2_checks` (+ `extras.instance_count`).
- The heuristic logic lives in the legacy shim `services/ec2.py`
  (`get_enhanced_ec2_checks`, `get_advanced_ec2_checks`, `get_auto_scaling_checks`,
  `get_ec2_instance_count`, and the pure helpers `_compute_ec2_savings`,
  `_classify_utilization`, `_one_size_down`, `_instance_pricing_os`,
  `_instance_license_model`). **DO NOT modify the shim's behaviour casually** —
  but it is not off-limits to audit; it is where every heuristic check is.
- **Cost Optimization Hub**: consumed via `ctx.cost_hub_splits["ec2"]` (bucketed
  by `core/scan_orchestrator.py._prefetch_advisor_data`, `type_map["Ec2Instance"]
  = "ec2"`). The adapter re-applies the reporter's render filter in
  `_coh_is_renderable` (drops `ebsVolume` recs, `PurchaseReservedInstances`, and
  `resourceId == "N/A"` recs) so counted == rendered.
- **Compute Optimizer**: pulled inline via
  `services.advisor.get_ec2_compute_optimizer_recommendations` and
  `get_asg_compute_optimizer_recommendations`. A `$0` opt-in placeholder
  (`ResourceId == "compute-optimizer-service"`) is converted to `ctx.warn` and
  dropped — the canonical pattern. Savings via `compute_optimizer_savings`
  (nested `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings`).
- `get_auto_scaling_checks` ALSO lives in `services/ec2.py` but is consumed by
  the **network** adapter, NOT this one — see Phase 3 (cross-adapter overlap).
- EC2 is Phase B and in `_PHASE_B_SKIP_PER_REC` (`reporter_phase_b.py`); every
  one of its five sources has a registered `PHASE_B_HANDLERS` entry.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and read the `ec2.py` row (OS/license-aware
    `get_ec2_hourly_price`; exact current→target delta for prev-gen/rightsizing/
    burstable; `EC2_SAVINGS_FACTORS` for the factor checks; spot via
    `describe_spot_price_history`; cross-source + ASG-member dedup; every finding
    records `OS` + `PricingBasis`). Reconcile the doc against the code.
0b. Confirm identity in `services/adapters/ec2.py`: `key="ec2"`,
    `cli_aliases=("ec2",)`, `display_name="EC2"`, `reads_fast_mode=True`,
    `requires_cloudwatch=True`, `required_clients()=("ec2","compute-optimizer","autoscaling")`.
0c. EC2 HAS both AWS advisory sources (CoH + CO). The dedup chain (CoH > CO >
    heuristic, plus ASG-member exclusion) is the crown jewel — most of the risk
    is there and in the heuristic pricing factors.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/ec2.py` in full and the shim `services/ec2.py` in full;
   `services/_savings.py` (`parse_dollar_savings`, `compute_optimizer_savings`,
   `mark_zero_savings_advisory`); `services/advisor.py`
   (`get_ec2_compute_optimizer_recommendations`,
   `get_asg_compute_optimizer_recommendations`, the `compute-optimizer-service`
   placeholder); `core/pricing_engine.py` (`get_ec2_hourly_price`,
   `get_ec2_instance_monthly_price`); `core/scan_orchestrator.py`
   (`_HUB_SERVICES`, `type_map`); `core/result_builder.py`; and the reporter
   (`reporter_phase_b.py`: `_render_ec2_cost_hub` + its `_filter_ec2_recommendations`,
   `_render_ec2_compute_optimizer`, `_render_ec2_enhanced_checks`,
   `_render_ec2_advanced_checks`, `_render_compute_optimizer_source`).
2. List **every** cost check, by source, with: trigger, data source
   (CoH / CO / CloudWatch / describe-API / pricing), the exact savings formula,
   the constant/factor, and counted-vs-advisory status. The known inventory to
   confirm — enhanced checks (`get_enhanced_ec2_checks`): **idle** (factor 1.0 =
   full monthly cost, terminate), **rightsizing** (exact delta to `_one_size_down`
   target), **previous_generation** (exact delta via `_PREVIOUS_GEN_TARGETS`
   migration map), **dedicated_hosts** (factor 0.30), **burstable_credits** (exact
   delta to one-size-down on t2/t3/t4g credit exhaustion). Advanced checks
   (`get_advanced_ec2_checks`): **cron** (factor 0.85), **batch** (0.75, mutually
   exclusive with cron), **underutilized_instance_store** (0.15), **nonprod_scheduling**
   (0.64 off-fraction), **spot_migration** (live on-demand − Spot, tag-gated).
   Plus CoH (flat `estimatedMonthlySavings`), CO + ASG CO (`compute_optimizer_savings`
   / flat).

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive from the live AWS Pricing API
   (`AmazonEC2`, filter on `instanceType`, `operatingSystem`, `tenancy=Shared`,
   `preInstalledSw=NA`, `capacitystatus=Used`, `licenseModel`) and confirm:
   - **OS correctness**: `_instance_pricing_os` maps `PlatformDetails`
     (Linux/UNIX, RHEL, SUSE, Windows, Windows BYOL) to the pricing
     `operatingSystem`. Confirm Windows is NOT priced as Linux, and confirm the
     Linux-fallback in `_ec2_hourly` (when the OS-specific SKU misses) does not
     silently understate a Windows saving.
   - **License model**: `_instance_license_model` maps "…BYOL" →
     `"Bring your own license"`. Confirm a Windows-BYOL instance is priced at the
     BYOL `licenseModel` SKU, not license-included (overstated).
   - **Exact delta vs factor**: prev-gen / rightsizing / burstable use
     `(current_hourly − target_hourly) × 730` with `target_hourly` from a real
     type (`_PREVIOUS_GEN_TARGETS` map / `_one_size_down`); confirm the delta is
     positive and the target SKU exists (a non-existent rung prices to 0 and
     emits nothing). Validate two `_PREVIOUS_GEN_TARGETS` mappings against the
     API (e.g. `m4.→m6i.`, `c4.→c6i.`) — confirm the target is current-gen,
     same-arch (x86→x86, not x86→Graviton), and actually cheaper.
   - **Active-commitment coverage (SP/RI) — C6**: CoH/CO `estimatedMonthlySavings`
     is on-demand ("before discounts") basis (`estimatedMonthlyCost` == on-demand
     monthly). If the account holds Savings Plans / Reserved Instances, a
     rightsizing/Graviton rec on a covered instance is NOT realizable: an
     **EC2-Instance SP is family-locked**, so a Graviton migration (m4→r6g)
     strands the family commitment to its end date (net zero or cost-NEGATIVE).
     Confirm `ScanContext.commitment_coverage` is prefetched and that
     `covers_ec2(family)` instances are demoted to advisory (`Counted=False`).
     Validate live: `aws savingsplans describe-savings-plans --states active`
     (families + region), `aws ce get-savings-plans-utilization` (headroom). A
     counted rec whose current family has an active EC2-Instance SP, or whose
     target leaves that family, is a finding. *Real: alyasra — $1,057→$13.87/mo.*
   - **Factor checks** (idle 1.0, dedicated 0.30, cron 0.85, batch 0.75,
     instance-store 0.15, nonprod 0.64): these are reduction factors, not exact
     deltas. Confirm each factor is defensible and labelled; the cron/batch/
     serverless-migration factors in particular are heuristic — confirm they are
     not overstated and that `PricingBasis` makes the basis legible.
   - **Spot**: `_spot_hourly` calls `describe_spot_price_history` with
     `MaxResults=1` — confirm this is the on-demand−Spot delta, that the product
     description matches the OS (`_PRICING_OS_TO_SPOT_PRODUCT`), and flag the
     non-deterministic single-datapoint read (latest AZ only).
   - **Region**: `get_ec2_hourly_price` is region-correct; confirm no
     `ctx.pricing_multiplier` double-multiply on the engine path, and that the
     `_compute_ec2_savings` returns `(0.0,"")` when `pricing_engine` is absent
     (no fallback dollars fabricated).
4. Confirm every counted finding records `OS` + `PricingBasis` so the number is
   defensible from the report alone. Where `PricingBasis` is missing or a factor
   is unlabelled, record it. Add a structured `AuditBasis` (rate/region/metric-
   window/formula) where one is absent, mirroring RDS/Lambda.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** the dedup in `EC2Module.scan` keeps at most ONE heuristic
   finding per instance id (highest-savings across enhanced+advanced via
   `best_by_instance`). Confirm: (a) an instance matching idle + prev-gen + cron
   never stacks; (b) anonymous recs keyed `_anon_{id(rec)}` (no `InstanceId`)
   are not silently dropped or mis-deduped; (c) cron/batch are mutually exclusive
   (`is_batch = not is_cron and …`).
6. **Cross-source:** authority order is CoH > CO > heuristic. `covered` starts
   as CoH instance ids (`_coh_instance_id`), CO recs are dropped if covered
   (`_co_instance_id` strips the `instanceArn`), then **ASG member instance ids**
   (`_asg_member_instance_ids`) are added to `covered` so no managed instance is
   rightsized individually. Prove the id-normalization is correct (CoH
   `resourceId` vs CO `instanceArn` suffix) and that a single instance surfaced
   by all three sources is counted once.
7. **Cross-adapter (the big ones):**
   - **EC2 ↔ Network ASG**: `get_auto_scaling_checks` (in `services/ec2.py`) is
     consumed by the **network** adapter and surfaces `oversized_instances` with
     a **factor** (`"Rightsizing Opportunities"` 0.40, no target delta). Confirm
     the SAME ASG member instance is not counted both in the Network ASG block
     and in the EC2 heuristics — and note that `_asg_member_instance_ids`
     silently returns `set()` on any error (see Phase 5), which would *defeat*
     the dedup and let ASG members double-count.
   - **EC2 ↔ EBS**: confirm `get_advanced_ec2_checks` does NOT emit any
     root-volume resize (the docstring says volume sizing is ceded to EBS) — an
     EC2-side volume rec would double-count the EBS tab.
   - **EC2 ↔ EKS**: EKS node-group instances are EC2; the EKS adapter emits them
     advisory (`Counted=False`) and the EC2 heuristics skip `_is_eks_managed_instance`
     tagged instances. Confirm both halves hold (EKS-tagged instances excluded
     from enhanced AND advanced checks).

### Phase 4 — Coverage (works for ALL instances, not a subset)
8. Confirm full pagination of `describe_instances` (enhanced, advanced, count,
   ASG-member). Confirm checks are not gated to a hardcoded family allowlist:
   `_PREVIOUS_GEN_TARGETS` is now multi-family (not t2-only); `_INSTANCE_STORE_FAMILIES`
   is a family-token match (not substring). Validate the instance-store family
   set against current AWS families and flag omissions.
9. Are whole classes skipped intentionally? Spot instances (`_is_spot_instance`),
   EKS-managed instances (`_is_eks_managed_instance`), stopped instances (idle
   finding removed — $0 compute, EBS owns the disk). Confirm each skip is
   documented and that `running`-only gating in advanced checks doesn't drop a
   legitimately-priced opportunity.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return []`/
    `return set()`/`return 0.0` path:
    - **`_asg_member_instance_ids` (`services/adapters/ec2.py`)** returns `set()`
      on ANY exception (missing client / `AccessDenied`) with no `ctx.warn` /
      `ctx.permission_issue` — a denied `describe_auto_scaling_groups` silently
      disables ASG-member dedup, allowing double-counting. Classify
      `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`.
    - **Burstable credit lookup** (`get_enhanced_ec2_checks`, ~line 803-814) on a
      non-permission error logs via `logger.debug` only — inconsistent with the
      CPU lookup which records `ctx.warn`. A throttled `CPUCreditBalance` read
      drops the burstable finding with no trace.
    - `_network_bytes_per_hour`, `_memory_used_percent`, `_discover_mem_dimension_sets`,
      `_spot_hourly` all `return None` on error silently (acceptable as *suppress-only*
      precision signals — confirm they never *invent* a finding, only refine one).
    - `get_auto_scaling_checks` / `get_advanced_ec2_checks` outer `except` →
      generic `ctx.warn` (not permission-classified).
11. Does a pricing miss fall back to `$0` and still emit a finding?
    `_compute_ec2_savings` returns `(0.0,"")` and every caller guards
    `if savings > 0` — confirm no check appends a `$0` rec. Confirm the CO opt-in
    placeholder is `ctx.warn`'d and dropped, not counted.
12. **CloudWatch gating / fast-mode:** `reads_fast_mode=True` and `requires_cloudwatch=True`
    are declared. Confirm `get_enhanced_ec2_checks` skips ALL CloudWatch reads
    when `fast_mode` (cloudwatch client set to `None`), that the structural
    checks (prev-gen, dedicated) still run, and that the **CWAgent memory
    dimension discovery** (`_discover_mem_dimension_sets` via `list_metrics`)
    correctly handles agent metrics published under more dimensions than
    `InstanceId` (the canonical agent-metric dimension-mismatch trap).

### Phase 6 — Reporting (one tab, counted == rendered)
13. **Counted == rendered:** `_coh_is_renderable` in the adapter mirrors
    `_filter_ec2_recommendations` in the reporter (drops EBS-volume / RI-purchase
    / N/A recs). Confirm the predicates are byte-for-byte equivalent — any drift
    means the headline counts dollars the EC2 tab does not show. Verify each of
    the five sources renders under the single EC2 tab via its `PHASE_B_HANDLERS`
    entry, and that `total_monthly_savings` equals the sum of the rendered
    counted findings.
14. Reconcile the executive-summary headline (`_get_executive_summary_content`
    + `_calculate_service_savings` + reconciliation footnote) against the EC2
    per-service total. Confirm `total_recommendations` counts exactly the five
    source blocks' lengths.

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to EC2:
    `.venv/bin/python cli.py <region> --scan-only ec2`
    then `.venv/bin/python tools/scan_doctor.py <json> --service ec2`.
    Triage every: silent failure, `$0`/missing-savings finding, and instance
    appearing in >1 source. Reconcile the headline against the per-source sum.
    Caveats: try a region with running instances; exercise the CO-not-enabled
    path (expect a `ctx.warn`, not a `$0` rec); exercise an account with an ASG
    to confirm member dedup. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC`.
16. For any duplication claim, prove it: show the same instance id in two
    sources (e.g. a CoH `resourceId` == a CO `instanceArn` suffix). For any
    accuracy claim, show the AWS Pricing API value next to the scanner's
    `PricingBasis`.

### Deliverable
- The complete check list (Phase 1.2), per source, counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**.
  End with an ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Extend `tests/test_ec2_audit_fixes.py` (mirror `tests/test_rds_audit_fixes.py`
  / `tests/test_lambda_audit_fixes.py`): test the pure helpers directly
  (`_compute_ec2_savings` delta vs factor modes, `_classify_utilization`,
  `_one_size_down`, `_instance_pricing_os`, `_instance_license_model`,
  `_coh_instance_id`/`_co_instance_id` normalization, `best_by_instance` dedup),
  and drive `EC2Module.scan` with a `SimpleNamespace` ctx + monkeypatched
  sources + fake boto3 clients/paginators. Cover every fix: ASG-member
  silent-failure classification, burstable-lookup warning, OS/BYOL pricing,
  cross-source dedup, fast-mode skip, counted==rendered.
- Record a structured `AuditBasis` (rate/region/metric-window/formula) on each
  counted finding alongside the existing `PricingBasis`.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Refresh reporter snapshots (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change
  is intentional, and say so.
- Update the `ec2.py` row in `services/adapters/CLAUDE.md` if behaviour changes.
- Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings computed from a config dimension alone (size/type) with no usage
  metric → fabricated $.
- Wrong architecture/edition/OS/license pricing (arm64 as x86; BYOL as
  license-included; Windows as Linux; SQL/Oracle edition default).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`) instead of
  pinned filters.
- Region: hardcoded constant/fallback not region-scaled, OR `pricing_multiplier`
  double-applied on an already-region-correct engine path.
- Per-unit RATE string ($/GB, $/hour, $/request) counted as a monthly total —
  must be rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier / free resource recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic, by NORMALIZED resource id (strip ARN).
- Two heuristic checks stacking on one resource, or SUBSET redundancy — fix by
  removal not dedup.
- Reduction factor instead of exact price delta (price×factor vs current−target);
  factors off 2-3×.
- `$0` "enable X"/opt-in placeholder (CO `ResourceId=compute-optimizer-service`)
  counted instead of converted to `ctx.warn` and dropped.
- Metric-gated `$0` nudge rendered as COUNTED instead of advisory (`Counted=False`).
- Cost Hub: `currentResourceType` with no `type_map` bucket → dropped (warns only
  on full scan); bucket populated but consumed by no adapter → dropped silently.
- A source with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC` service →
  renders nothing, silently.
- Render-time substring/category/RI filter desyncing the headline from cards
  (filter at SOURCE — `_coh_is_renderable` vs `_filter_ec2_recommendations`).
- Coverage gated to a hardcoded family/type/state allowlist, only-running, or a
  scaled-to-zero resource flagged for savings.
- CloudWatch/CE/CO/CoH permission or throttling failure logged via `logger` only,
  not recorded via `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized/
  OptInRequired → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode`; agent-metric dimension mismatch
  (CWAgent mem under more dimensions than InstanceId → empty result).
- Spot/discounted priced at on-demand; Spot recommended without an explicit
  interruptible signal.
- Each counted finding must carry a structured `AuditBasis`; counted == rendered.

#### EC2-specific items (found in this code)
- **`_asg_member_instance_ids` swallows `AccessDenied` and returns `set()`** —
  silently disables ASG-member dedup, letting managed instances double-count in
  the EC2 heuristics and the Network ASG block. Classify the permission error.
- **Burstable `CPUCreditBalance` failure logs `logger.debug` only** (not
  `ctx.warn`), unlike the sibling CPU lookup — a throttled read drops the
  burstable saving with no audit trail.
- **`get_auto_scaling_checks` oversized-instance saving uses the 0.40
  `Rightsizing Opportunities` factor with NO target delta** (and is rendered in
  the *Network* tab, not EC2) — a factor estimate where every other EC2 path
  switched to exact deltas; cross-adapter ownership of ASG sizing is ambiguous.
- **Spot via `describe_spot_price_history(MaxResults=1)`** — a single
  latest-AZ datapoint; non-deterministic and not a window average.
- **`_PREVIOUS_GEN_TARGETS` arch safety** — every mapping must stay x86→x86
  (e.g. `m4.→m6i.`, never `m4.→m6g.`) so the delta isn't a Graviton apples-to-
  oranges comparison; validate the map.
- **Cron/batch/instance-store name-keyword heuristics** (`"cron"`, `"batch"`,
  `"job"`, `"worker"`) assume a workload from the Name tag with no usage
  evidence — confirm the factors (0.85/0.75/0.15) are defensible and that
  `"web"` exclusion is the only guard against false positives.
- **Idle factor 1.0 = full monthly cost** assumes termination; confirm the
  recommendation text ("terminate or downsize") matches counting the *full* cost.

## PROMPT (end)
