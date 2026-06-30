# Batch Adapter Cost-Audit Prompt

A deep, AWS Batch-specific audit brief in the same structure as the Network /
Lambda / RDS / Glue audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Batch code path so the auditor starts from
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
> Service-specific live-audit findings for `batch`:
> - This service is (largely) ADVISORY-ONLY — verify it still renders a TAB despite `$0` counted savings (the tab gate keys off RENDERED cards, counted + advisory, not the counted-only headline count), and confirm no `Counted=False` rec carries a non-zero `EstimatedMonthlySavings` (advisory-leak).

You are auditing the **`batch`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. **AWS Batch has
no direct compute charge of its own** — there is **no `AWSBatch` service code in
the Pricing API** (confirmed at audit-authoring time); a Batch job is billed as
the **underlying EC2 / Fargate** compute it launches. So every Batch saving must
re-derive from EC2 (`serviceCode=AmazonEC2`) or Fargate
(`serviceCode=AmazonECS`/Fargate) pricing, and the adapter does exactly this via
`ctx.pricing_engine.get_ec2_hourly_price(...)`. Use the codebase/search tools
(CodeGraph if present) to trace actual code paths. The canonical sibling
references are the **EC2** adapter (`services/adapters/ec2.py`) for Spot /
Graviton pricing done as an **exact on-demand→target delta** (not a flat factor)
and the live-price-is-region-correct-don't-re-multiply rule, and the **Lambda**
adapter (`services/adapters/lambda_svc.py`) for the metric-gated `$0`-advisory
discipline and the test style I expect (`tests/test_lambda_audit_fixes.py`).

### NOTE on structure (Batch is a FLAT-RATE category→factor adapter over EC2 pricing, NOT CoH/CO)
- `services/adapters/batch.py` → `BatchModule.scan` wraps the shim
  `services/batch_svc.py:get_enhanced_batch_checks`. The shim discovers compute
  environments (CEs) and job definitions and emits raw recs; the adapter prices
  them.
- **Pricing model is a category-string → savings-factor map** in
  `BatchModule.scan`:
  `"Fargate Spot" → 0.70`, `"Graviton" → 0.20`, `"Spot" → 0.70`, else `0.30`.
  The dollar is computed **only** when the rec carries a non-empty
  `InstanceTypes`: `hourly = get_ec2_hourly_price(instance_types[0])`,
  `monthly = hourly × 730`, `savings += monthly × rate`. If `InstanceTypes` is
  empty (or `pricing_engine is None`), the rec is **skipped** (no fabricated
  fallback — the old `BATCH_COMPUTE_FALLBACK_MONTHLY = 150.0` constant is retained
  in the module but deliberately **unused**; confirm it is dead). The
  `ctx.pricing_multiplier` is intentionally NOT re-applied because
  `get_ec2_hourly_price` is already region-correct (comment "L2.3.1").
- **CRITICAL pricing-vs-rec mismatch to verify first:** only recs that carry an
  `InstanceTypes` key get a counted dollar. Inspect `services/batch_svc.py`: the
  **Graviton Migration** rec (lines ~70–78) is the **only** rec with
  `InstanceTypes`. The **Fargate Spot** rec (~43–53), the **EC2 Spot** rec
  (~56–66), and the **Job Rightsizing** rec (~90–99) carry **no** `InstanceTypes`
  key, so they are all **skipped by the pricing loop and count $0** despite their
  category-rate (0.70) implying a large saving. Confirm: in practice only Graviton
  produces a counted number; everything else is effectively a `$0` advisory by
  accident, not by design. This is the headline finding.
- **Batch has NO Cost Optimization Hub source and NO Compute Optimizer source.**
  No `Batch*` `currentResourceType` in
  `core/scan_orchestrator.py:_prefetch_advisor_data.type_map`, `batch` is not in
  `_HUB_SERVICES`, no `services/advisor.py` Batch helper. (Underlying EC2 *is*
  CoH/CO-covered, but those recs land in the EC2 tab, not here — see Phase 3.)
  Drop the CoH/CO axes for Batch itself.
- **No CloudWatch:** the adapter declares neither `requires_cloudwatch` nor
  `reads_fast_mode` and reads no metrics — so there is **no usage signal** behind
  the savings (a CE that ran 2 hours last month is priced as if its instance ran
  730 hr). Central accuracy concern.
- **Render path:** `batch` is NOT a Phase A service (no
  `PHASE_A_DESCRIPTORS["batch"]`), NOT in `_PHASE_A_SERVICES`, and NOT in
  `_PHASE_B_SKIP_PER_REC`. It emits a single `enhanced_checks` SourceBlock with
  **no** registered `PHASE_B_HANDLERS[("batch","enhanced_checks")]` entry. Because
  it is not in `_PHASE_B_SKIP_PER_REC`, `should_fallback_to_per_rec("batch")` is
  **True** — the generic per-rec renderer handles it (a deliberate fallback, not a
  silent render-desync). Confirm in Phase 6.

### Phase 0 — Orient (5-minute map before judging)
0a. Confirm identity in `services/adapters/batch.py`: `key="batch"`,
    `cli_aliases=("batch",)`, `display_name="Batch"`, NO `reads_fast_mode`/
    `requires_cloudwatch`, `required_clients()=("batch",)`.
0b. Read `services/adapters/CLAUDE.md` — Batch is the **single** entry under the
    "Flat-rate (1 adapter)" section: "`batch.py` | Fixed per-rec estimate". Note
    the doc still says "Fixed per-rec estimate" but the code now prices via
    `get_ec2_hourly_price` and only the unused `BATCH_COMPUTE_FALLBACK_MONTHLY`
    constant is "flat" — reconcile the doc against reality.
0c. **Confirm Batch has no native price:** verify via the Pricing MCP that there
    is no `AWSBatch` service code, so the only legitimate cost basis is the
    underlying EC2/Fargate compute. This frames every accuracy check.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/batch.py` and `services/batch_svc.py` in full
   (`get_enhanced_batch_checks`: the `describe_compute_environments` paginator,
   the `is_fargate` detection, the allocation-strategy and Graviton branches, and
   the `describe_job_definitions` paginator); the adapter's pricing loop
   (category → rate map, `get_ec2_hourly_price`, `× 730`); `core/pricing_engine.py`
   `get_ec2_hourly_price` (region-correct on-demand $/hr, `os="Linux"`,
   `license_model="No License required"`, `quiet` fallback semantics);
   `core/contracts.py`; `core/scan_context.py`; `core/result_builder.py`; and the
   render dispatch helpers (`should_skip_source_loop`, `should_use_handler`,
   `should_fallback_to_per_rec`) and the generic per-rec renderer.
2. List **every** cost check, and for each give: trigger condition, data source
   (Batch `describe_compute_environments` / `describe_job_definitions` only — no
   CloudWatch, no Spot price history), the savings formula, the embedded factor,
   and whether it parses to a **counted** float or a **$0 advisory** (i.e. whether
   it carries `InstanceTypes`). The known inventory to confirm:
   - **Batch Fargate Spot Optimization** (`compute_environments`) — fires when a
     CE is detected as Fargate AND `allocationStrategy != SPOT_CAPACITY_OPTIMIZED`.
     `EstimatedSavings="70% with Fargate Spot"`, category rate `0.70`. **No
     `InstanceTypes`** → adapter skips → **$0 counted**.
   - **Batch Spot Optimization** (`compute_environments`) — fires when a non-Fargate
     CE has `allocationStrategy != SPOT_CAPACITY_OPTIMIZED`.
     `EstimatedSavings="60-90% with Spot instances"`, category rate `0.70`. **No
     `InstanceTypes`** → skipped → **$0 counted**.
   - **Batch Graviton Migration** (`compute_environments`) — fires when a CE's
     `instanceTypes` contains no `6g`/`7g` family AND `instanceTypes` is non-empty.
     Carries `InstanceTypes` → priced: `get_ec2_hourly_price(instance_types[0]) ×
     730 × 0.20`. **The only counted check.**
   - **Batch Job Rightsizing** (`job_definitions`) — fires when
     `containerProperties.vcpus > 8` OR `memory > 16384`.
     `EstimatedSavings="Rightsize based on actual usage"`, no category match →
     would use the `else 0.30` rate, but **no `InstanceTypes`** → skipped → **$0
     counted**.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate the Graviton (only counted) figure and scrutinize the factors:
   - **`get_ec2_hourly_price(instance_types[0])`**: confirm it returns the
     region-correct on-demand Linux rate for the CE's first instance type
     (`serviceCode=AmazonEC2`, pinned `os/licenseModel`). Confirm `× 730` and that
     `ctx.pricing_multiplier` is **not** re-applied (it would double-count region —
     the comment claims L2.3.1 compliance; verify).
   - **Graviton rate `0.20` is a flat reduction factor, NOT the x86→arm price
     delta.** The defensible number is `(price(x86_type) − price(equivalent_arm_type))
     × 730 × running_fraction`, e.g. `m5.xlarge → m6g.xlarge`. A flat 20% of the
     **x86 on-demand of `instance_types[0]`** is a proxy; validate it against the
     real per-family delta (often 10–20%, and the arm equivalent must exist).
   - **`instance_types[0]` is arbitrary**: a CE lists multiple instance types (or
     families like `optimal`, `m5`, `c5`); pricing only the first, at full
     on-demand, ignores the mix and the actual chosen type. Flag.
   - **`× 730` assumes the instance runs 24/7** — Batch is inherently **bursty**
     (CEs scale from `minvCpus` 0 up to `maxvCpus` and back). With no CloudWatch /
     job-runtime evidence, pricing a CE's instance as always-on is the canonical
     "fixed hours, no usage metric" fabrication. Prefer a `minvCpus`-floor or a
     CloudWatch/`ListJobs` runtime-derived hour count, or make it a `$0` advisory.
   - **Spot factors `0.70` (Fargate Spot, EC2 Spot)**: even though these recs
     currently count $0 (no `InstanceTypes`), if wired they must use the real
     on-demand−Spot delta (`describe_spot_price_history`, as EC2 does), not a flat
     70%. Note for the fix plan.
   - **Region scaling**: live path is region-correct (single application, no
     multiplier) — confirm. The unused `BATCH_COMPUTE_FALLBACK_MONTHLY = 150.0` is
     us-east-1 flat; confirm it is genuinely never reached.
4. Confirm the basis is defensible from the report alone: each counted finding
   should record the instance type, the rate source, hours assumed, and the
   factor. Add a structured **AuditBasis** (rate / region / hours-window /
   formula) on each counted finding.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** a single non-Fargate CE can emit **both** a Spot rec AND a
   Graviton rec (the Spot branch and the Graviton branch are not mutually
   exclusive). Today only Graviton counts a dollar, so there is no live double-
   count — but if Spot is wired (fix plan), the **same CE's instances** would be
   discounted by 70% (Spot) AND 20% (Graviton) on the same bill. Define the
   authority order (Spot first, then Graviton on the post-Spot price, or pick one)
   before wiring Spot. Flag the latent stacking.
6. **Cross-source:** none for Batch itself (no CoH/CO). Confirm.
7. **Cross-adapter (the important one):** Batch EC2 CEs launch **EC2 instances**
   that the **EC2 adapter** independently surfaces (Compute Optimizer rightsizing,
   Spot, idle) and adds to its dedup `covered` set. Determine whether the **same
   underlying instance** can be counted in BOTH the EC2 tab (as an EC2 instance)
   and the Batch tab (as a CE Graviton/Spot saving). Batch-managed instances are
   typically transient and tagged `AWSBatchServiceTag` — decide the correct single
   owner (Batch CE config is a Batch lever; per-instance rightsizing is an EC2
   lever) and confirm `core/result_builder.py` doesn't blindly sum across tabs.
   Similarly, Fargate Batch CEs overlap the **containers** adapter's Fargate
   pricing — confirm no overlap.

### Phase 4 — Coverage (works for ALL CEs/jobs, not a subset)
8. Confirm pagination: `describe_compute_environments` and
   `describe_job_definitions` both use paginators — confirm both iterate all pages
   (`describe_job_definitions` is paginated with `status="ACTIVE"`; INACTIVE job
   defs are excluded — confirm intentional).
9. Are whole classes skipped or misdetected?
   - **`is_fargate` detection is likely broken:** the code tests
     `ce_type == "MANAGED" and any(t.upper() == "FARGATE" for t in instance_types)`.
     Real Batch CE `type` values are `"MANAGED"`/`"UNMANAGED"`, and the
     Fargate-ness lives in `computeResources.type` ∈
     `{"EC2","SPOT","FARGATE","FARGATE_SPOT"}` — Fargate CEs have **no
     `instanceTypes`**. So `any(... == "FARGATE" ...)` over `instanceTypes` is
     ~always False, and **every Fargate CE is misclassified into the EC2 branch**
     (and then skipped for lack of `instanceTypes`). Verify against the
     `describe_compute_environments` response shape and flag — the Fargate Spot
     check effectively never fires.
   - **Only `state == "ENABLED"` CEs** are inspected — confirm DISABLED CEs with
     lingering instances aren't a missed cost (usually fine).
   - **`allocationStrategy` default `"BEST_FIT"`**: Fargate CEs have no
     `allocationStrategy`, so the `!= SPOT_CAPACITY_OPTIMIZED` gate always passes
     for them — confirm the EC2-only concept isn't applied to Fargate.
   - **Job rightsizing uses legacy `containerProperties.vcpus`/`memory`** — Fargate
     and ECS-style job defs use `resourceRequirements` (VCPU/MEMORY entries) and
     `nodeProperties` (multi-node), which carry no top-level `vcpus`/`memory`, so
     those job defs are **invisible** to the `>8 vCPU / >16384 MB` gate. Flag.
   - The Graviton `has_graviton` test (`"6g" in inst or "7g" in inst`) substring-
     matches instance type strings — confirm it doesn't false-negative on families
     like `m6gd`/`c7gn` (it matches) or false-positive on a hypothetical `…6g…`
     substring elsewhere.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only path, and `return []`:
    - **`services/batch_svc.py` inner `except Exception: pass`** (~line 100) around
      the `describe_job_definitions` paginator — a denied/throttled job-def API
      kills job rightsizing silently. Classify
      `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`, other →
      `ctx.warn`.
    - **Outer `except Exception as e: → ctx.warn`** (~line 102) wraps the whole CE
      loop — a denied `batch:DescribeComputeEnvironments` produces one generic warn
      and an empty report; classify AccessDenied → `ctx.permission_issue`.
11. Does a pricing miss fall back and still emit a finding? `get_ec2_hourly_price`
    returns `0.0` on a Pricing API miss (non-`quiet` path logs a fallback warn) —
    confirm a `0.0` hourly yields `savings += 0`, i.e. a Graviton rec with an
    unpriceable `instance_types[0]` silently counts $0 but **still renders** as a
    recommendation. Confirm that is acceptable (advisory) and not a phantom counted
    finding. Confirm `pricing_engine is None` (no engine) skips pricing for all
    recs (every rec → $0) without crashing.
12. **No CloudWatch / no fast-mode:** the adapter reads no metrics, so there is no
    fast-mode concern — but also no usage signal behind `× 730`. Confirm whether a
    CloudWatch / `ListJobs` runtime read should gate the saving (and if added,
    declare `requires_cloudwatch`/`reads_fast_mode`).
13. Are the category-string nudges that lack `InstanceTypes` (Fargate Spot, EC2
    Spot, Job Rightsizing) surfaced honestly as **advisory** (rendered but $0), or
    do they masquerade as savings? They render via the per-rec fallback with their
    `EstimatedSavings` string ("70% with Fargate Spot") while counting $0 — a
    string-vs-counted desync (the card says 70%, the total adds nothing). Decide:
    mark them `Counted=False` advisory or wire real pricing.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Render wiring (verify):** `batch` emits `enhanced_checks` with no
    `PHASE_B_HANDLERS` entry, is NOT in `_PHASE_A_SERVICES`, and is NOT in
    `_PHASE_B_SKIP_PER_REC` — so `should_fallback_to_per_rec("batch")` is True and
    the generic per-rec renderer renders each rec (trace `should_skip_source_loop`
    → `should_use_handler` → `should_fallback_to_per_rec`). Confirm the
    `enhanced_checks` recs actually render and that the Spot/Fargate/Job recs
    appear as cards even though they count $0.
15. **String-vs-counted desync:** the per-rec renderer shows the rec's
    `EstimatedSavings` string ("70% with Fargate Spot", "60-90% with Spot
    instances", "Rightsize based on actual usage", "20-40% cost reduction") while
    the tab total sums only the adapter's computed float (Graviton only). Prove the
    card can advertise "70%" while contributing $0 to the headline, and decide the
    single source of truth.
16. **Counted == rendered:** `total_monthly_savings = savings`,
    `total_recommendations = len(recs)`. Confirm the per-tab total equals the sum
    of the counted rendered findings (today: Graviton only), that advisory $0 recs
    render but contribute $0, and reconcile the Batch per-service total against the
    executive-summary headline.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to Batch:
    `.venv/bin/python cli.py <region> --scan-only batch`
    then `.venv/bin/python tools/scan_doctor.py <json> --service batch`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage — esp. the Fargate Spot / EC2 Spot / Job Rightsizing
    recs that count $0), the `is_fargate` misclassification, and any underlying
    instance also appearing in the EC2 tab. Caveats: use an account with at least
    one EC2 CE (non-`SPOT_CAPACITY_OPTIMIZED`, non-Graviton instance types) to fire
    the Graviton counted path, and a Fargate CE to exercise the (broken) Fargate
    branch. Use `.venv/bin/python` (3.14).
18. For any accuracy claim, show the AWS Pricing API EC2 on-demand value for the
    CE's `instance_types[0]` and the equivalent Graviton family next to the
    scanner's `hourly × 730 × 0.20`, demonstrating the flat-20%-vs-real-delta gap.
    For any duplication claim, show the same Batch-launched EC2 instance in both
    the EC2 tab and the Batch tab.

### Deliverable
- The complete check list (Phase 1.2), with the **only-Graviton-counts** fact and
  the `is_fargate` misclassification called out.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**.
  End with an ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_batch_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the category→rate map, the
  `InstanceTypes`-present-vs-absent pricing branch (the core bug), the
  `get_ec2_hourly_price × 730 × rate` formula, `pricing_engine is None` → all $0,
  and the `is_fargate` detection; drive `BatchModule.scan` with a
  `SimpleNamespace` ctx + a fake `batch` client with paginators for
  `describe_compute_environments` and `describe_job_definitions`, and a fake
  `pricing_engine`. Cover every fix: Fargate detection via
  `computeResources.type`, Spot/Fargate-Spot real delta pricing, Graviton x86→arm
  delta, bursty-hours (not flat 730), Spot+Graviton stacking dedup, EC2 cross-
  adapter dedup, `resourceRequirements` job-def coverage, advisory $0 gating
  (`Counted=False`), silent-failure classification, string-vs-counted
  reconciliation.
- For the `× 730` always-on assumption, replace it with a real
  CloudWatch / `ListJobs` runtime-derived hour count or keep the rec a `$0`
  advisory — never fabricate hours. Respect `ctx.fast_mode` if CloudWatch is added.
- Either remove the dead `BATCH_COMPUTE_FALLBACK_MONTHLY = 150.0` constant or wire
  a real fallback; do not reintroduce the flat $150 (it was over/under-stated
  1.5–36× by instance type).
- Record a structured **AuditBasis** (rate / region / hours-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for Batch first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `batch.py` row in `services/adapters/CLAUDE.md` to match reality (it
  now prices via `get_ec2_hourly_price`, not a flat per-rec estimate).
- Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
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

#### Batch-specific items (found in this code)
- **Only Graviton recs count a dollar**: the pricing loop requires a non-empty
  `InstanceTypes`, which **only** the Graviton Migration rec carries. Fargate
  Spot, EC2 Spot, and Job Rightsizing recs are emitted but **skipped** by the
  pricing loop → counted `$0` despite advertising 60–90% in their
  `EstimatedSavings` string. Headline finding (string-vs-counted desync).
- **`is_fargate` detection is broken**: `ce_type == "MANAGED" and any(t.upper()
  == "FARGATE" for t in instance_types)` — Fargate-ness lives in
  `computeResources.type` (`FARGATE`/`FARGATE_SPOT`), and Fargate CEs have no
  `instanceTypes`, so the Fargate branch ~never fires and every Fargate CE is
  misclassified as EC2 (then skipped).
- **`× 730` assumes 24/7**: Batch CEs are bursty (scale from `minvCpus` 0); pricing
  `instance_types[0]` as always-on with no CloudWatch/job-runtime evidence
  fabricates hours.
- **Graviton `0.20` is a flat factor, not the x86→arm price delta**; and only
  `instance_types[0]` is priced, ignoring the CE's instance-type mix.
- **Cross-adapter overlap with EC2**: Batch-launched EC2 instances may also be
  surfaced by the EC2 adapter (Compute Optimizer / Spot) — risk of the same
  instance counted in two tabs.
- **Latent Spot+Graviton stacking**: a single non-Fargate CE emits both a Spot rec
  and a Graviton rec; if Spot is wired, the same bill is discounted 70% and 20%.
- **Job rightsizing misses `resourceRequirements`**: the `>8 vCPU / >16384 MB` gate
  reads legacy `containerProperties.vcpus`/`memory`; Fargate/ECS job defs using
  `resourceRequirements`/`nodeProperties` are invisible.
- **Dead `BATCH_COMPUTE_FALLBACK_MONTHLY = 150.0`**: the module constant is
  retained but never referenced (the `else` no-`InstanceTypes` path skips rather
  than falling back) — confirm and remove or wire.

## PROMPT (end)
