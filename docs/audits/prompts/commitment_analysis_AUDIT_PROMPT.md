# Commitment Analysis Adapter Cost-Audit Prompt

A deep, commitment-specific audit brief in the same structure as the Network /
Lambda / RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* `commitment_analysis` code path so the
auditor starts from facts, not a blind find-replace. Scope is **strictly cost**:
every emitted recommendation must produce a concrete, account-specific dollar
saving — and the single most important rule for this adapter is that **"buy"
recommendations (RI / Savings Plans purchases) are inherently ADVISORY, not
realized savings, and must never be summed into the headline alongside the
rightsizing levers they overlap.**

---

## PROMPT (copy from here)

You are auditing the **`commitment_analysis`** adapter of this AWS
cost-optimization scanner (Savings Plans + Reserved Instance utilization,
coverage, expiry, and purchase recommendations, plus a Fargate-isolated Compute
SP view). Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **RDS** adapter
(`services/adapters/rds.py`, `services/rds_logic.py`) as the canonical model for
**RI demotion to advisory** (`ADVISORY_CATEGORIES`; the reporter comment at
`reporter_phase_b.py` ~line 575 that "RI is the commitment_analysis tab's"
lever) and for **CoH consumption** via `ctx.cost_hub_splits`. Treat the
**Lambda** adapter as the model for the `Counted=False` advisory pattern and the
structured `AuditBasis` I expect on every counted finding.

### THE KEY RULE for this adapter (read first, hold throughout)

A commitment **"buy" recommendation** (purchase an RI or a Savings Plan, or close
a coverage gap by buying one) is a **mutually-exclusive alternative** to the
per-service **rightsizing / Graviton / idle-delete** levers that act on the SAME
resources. Buying a 3-year db.r5.8xlarge RI and *downsizing* that db.r5.8xlarge
are two different decisions on one bill — summing both double-counts. AWS's RI/SP
purchase savings are also **projected** (what you *would* save if you commit
capital up-front), not money currently being wasted. Therefore:

- Purchase / coverage-gap / Fargate-SP recs must stay **`Counted=False`**
  (advisory): rendered prominently, **excluded** from `total_monthly_savings`.
- Only genuine waste on **EXISTING** commitments — **under-utilization** of an
  already-purchased SP/RI, and **expiry** alerts — is real money already being
  spent, and may be **counted** (utilization) or $0-advisory (expiry).

Phase 3 is where you prove the adapter actually enforces this. The adapter's
`scan()` docstring/comment block (lines ~128–135) claims it does — verify it
against the code and against the per-service tabs.

### NOTE on structure (commitment_analysis is a composite multi-source adapter)

- The adapter is `services/adapters/commitment_analysis.py` →
  `CommitmentAnalysisModule.scan`. It is driven by **Cost Explorer** (`ce`) +
  **SavingsPlans** (`savingsplans`) clients — `required_clients()` returns
  `("ce", "savingsplans")`; `requires_cloudwatch = False`.
- It emits **eight SourceBlocks**: `cost_optimization_hub`, `sp_utilization`,
  `sp_coverage_gaps`, `ri_utilization`, `ri_coverage_gaps`,
  `expiring_commitments`, `purchase_recommendations`, `fargate_savings_plan`.
- It **consumes Cost Optimization Hub**: `core/scan_orchestrator.py`
  `_prefetch_advisor_data` lists `commitment_analysis` in `_HUB_SERVICES`, and
  its `type_map` routes every reservation/SP resourceType
  (`EC2ReservedInstances`, `RdsReservedInstances`, `ElastiCacheReservedInstances`,
  `OpenSearchReservedInstances`, `RedshiftReservedInstances`, `EsReservedInstances`,
  `ComputeSavingsPlans`, `EC2InstanceSavingsPlans`, `SageMakerSavingsPlans`) →
  `"commitment_analysis"`. The adapter reads
  `ctx.cost_hub_splits.get("commitment_analysis", [])`. It pulls **no** Compute
  Optimizer helper (CO does not produce RI/SP recs).
- Its own purchase recs come from **Cost Explorer** across a `(term, payment)`
  matrix: SP via `get_savings_plans_purchase_recommendation` (2 terms × 3
  payments × `COMPUTE_SP`), RI via `get_reservation_purchase_recommendation`
  (2 × 3 × the single `_RI_SERVICES` entry, EC2 only).
- The **Fargate Savings Plan** view (`_check_fargate_savings_plan` +
  `services/commitment_logic.py` `fargate_sp_analysis` / `fargate_sp_cell`,
  `DEFAULT_COVERAGE_RATIO = 0.70`) isolates Fargate's SP-eligible on-demand
  spend, reconciles it against the Containers-tab rightsizing hand-off
  (`ctx.fargate_rightsizing_monthly` or a local ECS-only estimate), and prices the
  2×3 matrix from **live `describe_savings_plans_offering_rates`**. It is purely
  advisory (`monthly_savings = 0.0`, `Counted=False`).
- Constants to note: `AVG_SP_DISCOUNT_RATE = 0.30`, `UTILIZATION_THRESHOLD =
  0.95`, `COVERAGE_GAP_THRESHOLD = 0.80`. `stat_cards` expose the four
  utilization/coverage rates; `grouping = GroupingSpec(by="check_category")`.

### Phase 0 — Orient (5-minute map before judging)

0a. Open `services/adapters/CLAUDE.md`. Note commitment_analysis is NOT in the
    per-adapter pricing tables (it has no PricingEngine method and no module
    `$/unit` constant — its dollars come from CE / CoH / live SavingsPlans rates).
    Confirm the top-of-file note that CoH recs flow via
    `ctx.cost_hub_splits[<service_key>]` and that reservation/SP types bucket to
    `commitment_analysis`. Reconcile any drift.
0b. Confirm module identity: `key="commitment_analysis"`,
    `cli_aliases=("commitment_analysis", "commitments", "savings_plans", "ri")`,
    `display_name="Commitment Analysis"`, `required_clients()=("ce","savingsplans")`,
    `requires_cloudwatch=False`. Note the CE API cost disclosed in the docstring
    (~$0.07/scan base + the 12-call purchase matrix + Fargate calls).
0c. CoH/CO fairness: CoH **is** fair game (the adapter consumes it). Compute
    Optimizer is **not** (CO produces no RI/SP recs) — do not flag a "missing CO
    source". CloudWatch is not used. Focus on the buy-vs-rightsize advisory
    boundary (Phase 3), the counted==rendered reconciliation (Phase 6), the
    inconsistent utilization-waste bases, and coverage breadth.

### Phase 1 — Understand the code (read before judging)

1. Read the whole path: `services/adapters/commitment_analysis.py` (every
   `_check_*` / `_fetch_*` method), `services/commitment_logic.py`
   (`fargate_sp_cell`, `fargate_sp_analysis`, `DEFAULT_COVERAGE_RATIO`,
   `HOURS_PER_MONTH=730`), `core/contracts.py` (`ServiceFindings`, `SourceBlock`,
   `StatCardSpec`, `GroupingSpec`), `core/scan_context.py` (`warn`,
   `permission_issue`, `cost_hub_splits`, `fargate_rightsizing_monthly`),
   `core/scan_orchestrator.py` (`_prefetch_advisor_data` `_HUB_SERVICES` +
   `type_map`), `core/result_builder.py`, the CoH fetch
   (`services.advisor` / `get_detailed_cost_hub_recommendations`), and the
   reporter: `reporter_phase_b.py` — `PHASE_B_HANDLERS` registers only
   `("commitment_analysis","cost_optimization_hub") → _render_cost_hub_source`
   and `("commitment_analysis","fargate_savings_plan") → _render_fargate_savings_plan`;
   commitment_analysis is **NOT** in `_PHASE_B_SKIP_PER_REC`, so its other six
   sources fall through `should_fallback_to_per_rec` → `render_generic_per_rec` →
   `_render_generic_other_rec`. Also confirm it is **not** in
   `reporter_phase_a.PHASE_A_DESCRIPTORS` despite carrying a `GroupingSpec`, and
   trace `html_report_generator.py` `_get_detailed_recommendations` +
   `_extract`/`multi_source_cards` (the `commitment_analysis` descriptor at
   `html_report_generator.py` ~178 drives the four rate stat-cards).
2. List **every** check and for each give: trigger, data source, exact savings
   formula + constant, and **counted vs advisory** status. Confirm this inventory:
   - **`sp_utilization`** (`_check_sp_utilization`): per-SP `UtilizationPercentage
     < 0.95` → `waste = TotalHourlyCommitment × (1 − rate) × 730`. **COUNTED**
     (`Counted` defaults True). Genuine waste on an existing SP.
   - **`sp_coverage_gaps`** (`_check_sp_coverage`): per-service coverage `< 0.80`
     → `potential = OnDemandCost × (1 − rate) × AVG_SP_DISCOUNT_RATE(0.30)`. Set
     **`Counted=False`** in `scan()` (advisory). A flat-30%-discount heuristic.
   - **`ri_utilization`** (`_check_ri_utilization`): per-SUBSCRIPTION_ID
     `UtilizationPercentage < 0.95` → `waste = TotalAmortizedCost × (1 − rate)`.
     **COUNTED**. Note the basis differs from SP (no `× 730`).
   - **`ri_coverage_gaps`** (`_check_ri_coverage`): **always empty** — the method
     deliberately takes only the overall rate (no groupBy) for the stat card and
     returns `recs = []`. Confirm.
   - **`expiring_commitments`** (`_check_expiring`): SP `EndDateTime` within 90
     days → severity HIGH/MED/LOW, `monthly_savings = 0.0`. **$0** (counted but
     zero). **SP only** — no RI expiry.
   - **`purchase_recommendations`** (`_fetch_sp_recommendations` +
     `_fetch_ri_recommendations`): CE matrix; each rec is built with
     **`Counted=False`** ("mutually-exclusive alternative") and carries
     `term`/`payment_option`/`upfront_cost`. Advisory.
   - **`cost_optimization_hub`**: `ctx.cost_hub_splits["commitment_analysis"]`
     (CoH RI/SP recs, CoH schema with `estimatedMonthlySavings`/`actionType`);
     set **`Counted=False`** in `scan()`. Advisory.
   - **`fargate_savings_plan`**: 2×3 matrix from live SP offering rates,
     reconciled against rightsizing; `monthly_savings = 0.0`, **`Counted=False`**.
     Advisory.
3. Pin down the headline math in `scan()`: `all_recs = sp_util + sp_cov + ri_util
   + ri_cov + expiry + purchase`; `total_savings = Σ monthly_savings for r in
   all_recs if r.get("Counted", True)`; `total_recommendations = len(all_recs) +
   len(cost_hub_recs)`. **Note `fargate_sp_recs` are in their SourceBlock but NOT
   in `all_recs` and NOT in `total_recommendations`** — carry this to Phase 6.

### Phase 2 — Accuracy of every number (validate with MCP)

4. **Counted figures (the only two that hit the headline):**
   - `sp_utilization` waste = `TotalHourlyCommitment × (1 − rate) × 730`. Confirm
     `TotalHourlyCommitment` is the amortized hourly commitment (so × 730 ≈ a
     month of the unused portion). Record an **AuditBasis** (rate / window /
     formula) — there is none today.
   - `ri_utilization` waste = `TotalAmortizedCost × (1 − rate)`. Confirm whether
     `TotalAmortizedCost` is already a 30-day (monthly) figure for the `tp`
     window; if so the missing `× 730` is correct and the SP/RI bases are
     consistent *in result* though inconsistent *in form*. Flag the
     form-inconsistency and pin the window basis on each.
5. **`AVG_SP_DISCOUNT_RATE = 0.30`** is a flat reduction factor applied in
   `sp_coverage_gaps` instead of an exact SP-rate delta (universal "reduction
   factor" issue). It is advisory, so the impact is presentation not headline —
   but validate that a 30% Compute-SP discount is a defensible *typical* figure
   against the live SavingsPlans offering rates, and label it as an estimate.
6. **Purchase recs come straight from Cost Explorer** (`EstimatedMonthlySavingsAmount`
   for SP; `Σ EstimatedMonthlySavings` for RI) — these are AWS-computed and
   region-correct; confirm the adapter does **not** multiply by
   `pricing_multiplier` (it must not — double-apply risk). Same for CoH recs
   (flat float at top level) and Fargate rates (live, region-filtered via
   `filters=[{"name":"region","values":[ctx.region]}]`).
7. **Fargate SP math** (`commitment_logic.fargate_sp_cell`): verify
   `discount = (eligible_od − sp_cost_full) / eligible_od`,
   `rightsized_od = max(0, eligible_od − rightsizing_monthly)`,
   `ceiling_saving = (eligible_od − sp_cost_full) × scale`,
   `recommended_saving = ceiling × coverage_ratio`. Confirm the rates feeding it
   are the correct Fargate Compute-SP `usageType` rows from
   `describe_savings_plans_offering_rates` (`products=["Fargate"]`,
   `serviceCodes=["AmazonECS"]`) and that `_SP_DURATION_TO_TERM`
   (`31536000→1yr`, `94608000→3yr`) maps the offering durations correctly.
   Cross-check one cell's discount against the live rate via MCP.

### Phase 3 — Duplication (THE big one — buy vs rightsize, no dollar counted twice)

8. **Prove the advisory boundary holds.** In `scan()`, confirm `sp_cov_recs +
   ri_cov_recs + cost_hub_recs` are forced `Counted=False`, and that
   `purchase_recs` and the Fargate cells are built `Counted=False` at source.
   Confirm `total_savings` therefore includes ONLY `sp_utilization` +
   `ri_utilization` (expiry is $0). If any buy/coverage/Fargate rec carries a
   positive `monthly_savings` AND `Counted=True`, that is a CRITICAL
   double-count.
9. **Cross-tab overlap with rightsizing levers (the core risk):** the SAME
   resource can appear as (a) a rightsizing/Graviton/idle recommendation in its
   own service tab (EC2 / RDS / ElastiCache / Redshift / OpenSearch / containers),
   AND (b) a CoH `*ReservedInstances` / `*SavingsPlans` buy rec routed here, AND
   (c) a CE purchase rec, AND (for Fargate) a Containers rightsizing dollar. The
   adapter keeps (b)/(c)/Fargate advisory — confirm. Then confirm the
   **per-service tabs** do not *also* count an RI as realized: check
   `services/rds_logic.py` `ADVISORY_CATEGORIES` and the
   `reporter_phase_b.py:_render_rds_enhanced_checks` ~line 575 comment that RDS RI
   is demoted because "RI is the commitment_analysis tab's lever." Verify the same
   RI is not advised in BOTH the RDS tab and here in a way that reads as two
   opportunities.
10. **Fargate double-advisory:** the Fargate SP view overlaps BOTH the account-level
    Compute-SP purchase rec (already advisory here) AND the Containers-tab
    rightsizing total (subtracted via the hand-off). Confirm the rightsizing
    hand-off (`ctx.fargate_rightsizing_monthly`, else
    `services.containers.estimate_fargate_rightsizing_monthly`) is applied BEFORE
    the commitment so the SP is sized against the reduced baseline (no
    double-count), and that the Fargate cells stay $0/`Counted=False`.
11. **Intra-adapter expiry overlap:** `_check_expiring` re-calls
    `get_savings_plans_utilization_details` (same API as `_check_sp_utilization`)
    — confirm an expiring under-utilized SP appears in both
    `sp_utilization` (counted waste) and `expiring_commitments` ($0 alert) without
    double-counting dollars (expiry is $0, so safe, but confirm the operator isn't
    shown the same SP twice as if two distinct opportunities).

### Phase 4 — Coverage (works for ALL commitments, not a subset)

12. Confirm pagination via `NextToken` loops on every CE call that supports it
    (`get_savings_plans_utilization_details`, `get_savings_plans_coverage`,
    `get_reservation_utilization`).
13. Whole classes skipped / narrow matrices — confirm and document each:
    - **`ri_coverage_gaps` always empty** (no groupBy) → no per-service RI coverage
      opportunity is ever surfaced.
    - **RI expiry never detected** — `_check_expiring` reads SP details only.
    - **Purchase matrix is narrow:** SP only `_SP_TYPES=("COMPUTE_SP",)` (no
      `EC2_INSTANCE_SP`, no SageMaker SP); RI only `_RI_SERVICES` = EC2 (`"Amazon
      Elastic Compute Cloud - Compute"`). RDS / ElastiCache / Redshift / OpenSearch
      RI purchase recs arrive ONLY via CoH, never via CE here.
    - **Fargate legs filter** (`_fargate_legs`) restricts to SERVICE = `"Amazon
      Elastic Container Service"` and `usageType` containing `Fargate` +
      (`vCPU-Hours` or `GB-Hours`), excluding `Ephemeral`. Confirm **EKS Fargate**
      (which can bill under the EKS service dimension) is therefore missed, and
      whether that is intentional.
    - **Utilization/coverage thresholds** (`0.95` / `0.80`) are global constants —
      confirm they are not silently excluding marginal-but-real waste.

### Phase 5 — Silent failures (nothing fails quietly)

14. `_route_ce_error` is the canonical good pattern: it classifies
    `AccessDenied`/`AccessDeniedException`/`UnauthorizedOperation` →
    `ctx.permission_issue(..., "commitment_analysis")`, else `ctx.warn`. Confirm
    EVERY CE call routes its exception through it (it does for util/coverage/
    expiry/purchase/cost-and-usage). Flag any path that doesn't.
15. **Bare swallow:** `_account_coverage_ratio` has `except Exception: return
    DEFAULT_COVERAGE_RATIO` — a denied/throttled
    `get_savings_plans_purchase_recommendation` silently falls back to 0.70 with
    NO `ctx.warn`. Classify it (mirror `_route_ce_error`).
16. `ce` is None → `_empty_findings()` with no `ctx.warn`/`permission_issue` —
    confirm whether a missing CE client (region/permission) should be surfaced.
17. `_fargate_sp_rates` inner `except` → `ctx.warn` (ok); the
    `estimate_fargate_rightsizing_monthly` import/calc and the missing-SP-client
    paths → `ctx.warn` (ok). Confirm none of the Fargate paths emit a counted
    dollar on partial failure.
18. Confirm no purchase/coverage path emits a **$0 counted** finding: purchase
    recs `continue` when `monthly_savings <= 0` (SP also requires `hourly_commit
    <= 0`); verify a zero-savings scenario is dropped, not rendered as a $0
    opportunity.

### Phase 6 — Reporting (counted == rendered)

19. **Handler coverage:** only `cost_optimization_hub` and `fargate_savings_plan`
    have dedicated handlers. Because commitment_analysis is NOT in
    `_PHASE_B_SKIP_PER_REC`, the other six sources render via
    `render_generic_per_rec` → `_render_generic_other_rec`. Confirm each of
    `sp_utilization` / `sp_coverage_gaps` / `ri_utilization` / `ri_coverage_gaps`
    (empty) / `expiring_commitments` / `purchase_recommendations` actually renders
    (the generic renderer dumps every scalar field + `reason`). This is the
    OPPOSITE failure mode from a `_PHASE_B_SKIP_PER_REC` service — here the
    fallback SAVES the un-handled sources; confirm it does and that nothing
    renders blank.
20. **Advisory dollars look counted (HIGH candidate):** `_render_generic_other_rec`
    prints `monthly_savings` for `sp_coverage_gaps` and `purchase_recommendations`
    as a plain field even though they are `Counted=False`. A positive
    `monthly_savings` shown with no "advisory / not added to total" label reads as
    a realized saving. The dedicated `_render_fargate_savings_plan` DOES label its
    table "Advisory — … not added to the tab total"; confirm the generic-rendered
    advisory sources get an equivalent visible disclaimer, or flag the gap.
21. **Count desync (MEDIUM):** `total_recommendations = len(all_recs) +
    len(cost_hub_recs)` **omits `fargate_savings_plan` recs** even though they
    render. Confirm the per-tab "N items" / headline count and reconcile against
    the rendered card count. Also confirm `total_monthly_savings` (sum of
    `Counted=True` `monthly_savings`) equals the sum of the COUNTED rendered
    findings (sp_util + ri_util only), and reconcile the executive-summary
    headline (`_get_executive_summary_content` + `_calculate_service_savings` +
    any reconciliation footnote) against the per-service total.
22. Confirm the four stat-cards (`sp_utilization_rate`, `sp_coverage_rate`,
    `ri_utilization_rate`, `ri_coverage_rate` from `extras`) match the rates the
    `_check_*` methods actually computed, and that an all-empty scan renders the
    `_empty_findings` shape without crashing the descriptor.

### Phase 7 — Tooling & evidence

23. Run a real scan scoped to commitment_analysis:
    `python3 cli.py <region> --scan-only commitment_analysis`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service commitment_analysis`.
    Triage every: silent failure, `$0`/advisory finding (separate genuine advisory
    from leakage), and any resource that appears BOTH here (as a buy/coverage rec)
    AND in another service tab (as a rightsizing rec). Reconcile the headline
    against the per-source sum and prove only `sp_utilization` + `ri_utilization`
    are in the counted total. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC`. Caveats: accounts with no SP/RI return empty; a focused
    `--scan-only commitment_analysis` still triggers the CoH prefetch (the bucket
    gate is `bucket in selected`) — confirm CoH RI/SP recs land. The purchase
    matrix is 12 CE calls (~$0.12) plus Fargate calls — note the API cost.
24. For the duplication claim, prove it: show one resource (e.g. an EC2 instance
    family or an RDS instance) as a rightsizing rec in its own tab AND as a CoH/CE
    RI buy rec here, and show that only the rightsizing side is counted. For the
    accuracy claim, show the live SavingsPlans offering rate next to one Fargate
    cell's computed discount, and the CE `EstimatedMonthlySavings` next to a
    rendered purchase rec.

### Deliverable

- The complete check list (Phase 1.2), per source, with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing/CE value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs** (the narrow purchase matrix, empty RI coverage, and SP-only expiry
  may be deliberate). End with a short, ID'd fix plan (C1/H1/M1…) so a subset can
  be approved.

### Implementation (only after I approve)

- Add `tests/test_commitment_analysis_audit_fixes.py` mirroring
  `tests/test_rds_audit_fixes.py` / `tests/test_lambda_audit_fixes.py`: test the
  pure Fargate helpers directly (`fargate_sp_cell` / `fargate_sp_analysis`
  discount + coverage + rightsizing scaling), and drive
  `CommitmentAnalysisModule.scan` with a `SimpleNamespace` ctx + a fake `ce`
  client (stubbed util/coverage/purchase/cost-and-usage responses) + a fake
  `savingsplans` client + a populated `ctx.cost_hub_splits["commitment_analysis"]`.
  Cover: the advisory boundary (every buy/coverage/Fargate rec `Counted=False`,
  counted total = sp_util + ri_util only); `_account_coverage_ratio` error
  classification; the `total_recommendations` count includes (or intentionally
  excludes, documented) Fargate recs; advisory rendering carries a "not counted"
  disclaimer; `_route_ce_error` permission classification.
- Record a structured **AuditBasis** (rate / window / formula) on each COUNTED
  finding (sp_utilization, ri_utilization) so the number is defensible from the
  report alone, and make the SP/RI waste bases explicitly consistent (both
  monthly).
- Give the generic-rendered advisory sources (`sp_coverage_gaps`,
  `purchase_recommendations`) the same visible "Advisory — not added to the tab
  total" disclaimer the Fargate table already shows; or move them behind dedicated
  handlers.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for commitment_analysis first. Refresh reporter
  snapshots (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and
  say so.
- If you find the same advisory-leak / silent-swallow bug in a sibling adapter
  (e.g. an RI counted as realized in another tab) out of scope, note it as a
  follow-up (don't fix unprompted).
- Update the commitment_analysis entry/cross-references in
  `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (universal — found in prior audits)

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

### Service-specific issues to check against (found grounding this adapter)

- **Buy-vs-rightsize double-count is the headline risk (CRITICAL if it ever
  leaks):** RI/SP purchase recs, CoH RI/SP recs, `sp_coverage_gaps`, and the
  Fargate SP cells are mutually-exclusive alternatives to per-service rightsizing
  levers on the SAME resources, and AWS purchase savings are *projected* not
  realized. They MUST stay `Counted=False`. Verify the `scan()` advisory-flagging
  (lines ~136–137) plus the source-built `Counted=False` on purchase/Fargate recs,
  and that only `sp_utilization` + `ri_utilization` reach `total_monthly_savings`.
- **Cross-tab RI overlap with RDS (and friends):** confirm the same RI is not
  presented as a distinct opportunity in BOTH the RDS tab (where `rds_logic`
  demotes RI to advisory) and here — reconcile the two so the operator sees one
  advisory, not two.
- **Advisory dollars rendered without a "not counted" label (HIGH):**
  `sp_coverage_gaps` (positive `monthly_savings` via `AVG_SP_DISCOUNT_RATE`) and
  `purchase_recommendations` render through `_render_generic_other_rec`, which
  prints `monthly_savings` as a plain field with no disclaimer — unlike
  `_render_fargate_savings_plan`, which labels its table advisory.
- **Count desync (MEDIUM):** `total_recommendations = len(all_recs) +
  len(cost_hub_recs)` omits `fargate_savings_plan` recs that nonetheless render —
  counted-items ≠ rendered-items.
- **Inconsistent utilization-waste bases + no AuditBasis (MEDIUM):**
  `sp_utilization` uses `hourly × (1−rate) × 730`, `ri_utilization` uses
  `TotalAmortizedCost × (1−rate)` (no `×730`); both are counted with no structured
  rate/window/formula basis.
- **`_account_coverage_ratio` silent swallow (MEDIUM):** bare `except Exception:
  return DEFAULT_COVERAGE_RATIO` hides an `AccessDenied`/throttle on the
  account-wide SP recommendation with no `ctx.warn`.
- **Coverage breadth gaps (LOW–MEDIUM, possibly intentional):**
  `ri_coverage_gaps` always empty; RI expiry never detected (SP-only);
  purchase matrix is SP=`COMPUTE_SP`-only and RI=EC2-only; Fargate legs are
  ECS-service-only (EKS Fargate missed).
- **`AVG_SP_DISCOUNT_RATE = 0.30` flat reduction factor (LOW):** used for the
  `sp_coverage_gaps` advisory potential instead of an exact SP-rate delta; advisory
  only, but should be validated and labelled as an estimate.

## PROMPT (end)
