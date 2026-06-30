# OpenSearch Adapter Cost-Audit Prompt

A deep, OpenSearch-specific audit brief in the same structure as the Lambda /
RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* OpenSearch code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `opensearch`:
> - CoH consumer — verify the bucket is consumed and dedup is CoH > heuristic by normalized id; non-priceable levers (e.g. the r5.medium.search $0 pricing fallback) must be `$0` `Counted=False` advisories (they are flagged via the rec's `EstimatedSavings` string, NOT via `scan_warnings`).
> - B1 advisory-leak: the adapter has four distinct demotion paths (CoH supersession, best-lever per-domain dedup, idle-vs-storage exclusion, non-priceable fallback); verify every `Counted=False` rec carries `EstimatedMonthlySavings == 0.0` after the advisory-zeroing loop before the SourceBlock is built.

You are auditing the **`opensearch`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **RDS** adapter
(`services/adapters/rds.py`, `services/rds_logic.py`) as the canonical model for
**Cost Optimization Hub consumption** (`ctx.cost_hub_splits[...]`), cross-source
dedup, and RI demotion-to-advisory; the **EC2** adapter (`services/adapters/ec2.py`)
as the model for the `$0`-placeholder→warning pattern and exact price-delta
rightsizing; the recently-audited **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the worked example for **metric-gated `$0`
advisory** (`Counted=False`) findings, the arch-aware module constant, and the
test style I expect; and the **ElastiCache** adapter (`services/adapters/elasticache.py`)
as the structural sibling that uses the same `PricingEngine.get_instance_monthly_price`
generic-instance path.

### NOTE on structure (OpenSearch is a keyword-rate adapter with a hidden orphan bucket)

- The adapter is **thin**: `services/adapters/opensearch.py` →
  `OpensearchModule.scan` delegates collection to
  `services/opensearch.py:get_enhanced_opensearch_checks(ctx)` (the helper shim),
  then re-prices each rec in the adapter via a **`CheckCategory`-keyed discount
  table** and dedupes instance-axis levers per domain.
- The shim emits recs carrying a human **`EstimatedSavings` string**
  (`"30-60% vs On-Demand…"`, `"Estimated: 20-40%…"`, `"20% storage cost"`,
  `"100% of domain cost"`, `"30-50%"`). The adapter then overwrites the dollar
  value on a NEW key, **`EstimatedMonthlySavings`** (a float), and sums only
  `Counted=True` recs into `total_monthly_savings`. The two fields can disagree —
  the renderer shows the **string**, the headline sums the **float** (see Phase 6).
- Pricing is a hybrid: **storage** recs are priced from a module constant
  `GP3_PRICE_PER_GB_MONTH = 0.11` × `STORAGE_SAVINGS_FACTOR = 0.20` ×
  `ctx.pricing_multiplier`; **instance** recs are priced live via
  `ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)` ×
  `instance_count` × a **per-category rate** (`rate_by_category`). There is NO
  fallback constant for the instance path (if `pricing_engine is None`, value
  stays `0.0`).
- The adapter emits a **single SourceBlock named `enhanced_checks`** (not the
  five per-category buckets the shim builds). Its registered renderer is
  `("opensearch","enhanced_checks") → _render_opensearch_enhanced_checks`
  (`reporter_phase_b.py` ~line 1092). OpenSearch is a Phase B service (NOT in
  `_PHASE_A_SERVICES`).
- **CRITICAL pre-flight — the orphaned CoH bucket.** OpenSearch IS wired into
  Cost Optimization Hub: `core/scan_orchestrator.py` lists `"opensearch"` in
  `_HUB_SERVICES` (~line 58) and maps `"OpenSearchDomain" → "opensearch"` in
  `type_map` (~line 93), so `ctx.cost_hub_splits["opensearch"]` IS populated on
  any scan that selects opensearch. **But `OpensearchModule.scan` never reads
  `ctx.cost_hub_splits` at all.** That bucket is a populated-but-consumed-by-no-
  adapter **silent orphan drop** — the exact dead-renderer class flagged in prior
  audits (known orphans: elasticache / opensearch / redshift / s3). Confirm this
  with your own read and treat it as a primary finding, not a maybe.

### Phase 0 — Orient (5-minute map before judging)

0a. Open `services/adapters/CLAUDE.md` and find the `opensearch.py` row (listed
    under **Parse-rate (5 adapters)** as "Keyword-based"). **Reconcile the doc
    against reality:** the adapter is only partly keyword-based — instance recs
    are priced live via `get_instance_monthly_price("AmazonES", …)`, storage via a
    module constant. Note the row says nothing about the CoH bucket; flag the doc
    gap.
0b. Confirm module identity in `services/adapters/opensearch.py`:
    `key="opensearch"`, `cli_aliases=("opensearch",)`,
    `display_name="OpenSearch"`, `required_clients()` returns `("opensearch",)`.
    Note the shim ALSO uses a `cloudwatch` client (`AWS/ES` `CPUUtilization`) and
    `ctx.account_id` (as the `ClientId` dimension) but the adapter declares
    **neither** `requires_cloudwatch` **nor** `reads_fast_mode` — flag if the
    CloudWatch read is not `fast_mode`-gated (it is not; see Phase 5).
0c. Decide what advisory sources are fair game. OpenSearch **is** covered by Cost
    Optimization Hub (`OpenSearchDomain` rightsizing; `OpenSearchReservedInstances`
    / `EsReservedInstances` route to `commitment_analysis`). Compute Optimizer does
    NOT cover OpenSearch (no `get_opensearch_compute_optimizer_recommendations`
    helper exists). So "missing CO source" is NOT fair game, but "**CoH bucket
    populated and dropped**" very much is (Phase 0 pre-flight, Phase 5.12).

### Phase 1 — Understand the code (read before judging)

1. Read the full path: `services/adapters/opensearch.py`,
   `services/opensearch.py` (the shim + `OPENSEARCH_OPTIMIZATION_DESCRIPTIONS` +
   `get_enhanced_opensearch_checks` + `LOW_CPU_THRESHOLD=20`),
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`),
   `core/pricing_engine.py` (`get_instance_monthly_price` ~line 737 and its
   `_fetch_generic_instance_price` helper — does it pin `usagetype`/`operation`,
   or `MaxResults=1` over multiple SKUs?), `core/scan_context.py`,
   `core/scan_orchestrator.py` (`_prefetch_advisor_data`, `_HUB_SERVICES`,
   `type_map`), `core/result_builder.py`, and the reporter
   (`reporter_phase_b.py:_render_opensearch_enhanced_checks` ~line 1092 +
   `PHASE_B_HANDLERS` ~line 2472; `html_report_generator.py` dispatch).
2. List **every** cost check and for each give: trigger, data source (describe-API
   vs CloudWatch vs pure config), the shim `EstimatedSavings` string, the
   adapter's re-priced `EstimatedMonthlySavings` formula + constant/rate, and
   whether the final rec is **counted** or **advisory** (`Counted`). The known
   inventory to confirm (shim `CheckCategory` → adapter handling):
   - **Reserved Instances Opportunity** (`instance_count >= 2`): string
     `"30-60% vs On-Demand…"`. Adapter sets `Counted=False` (commitment lever →
     advisory) because `"Reserved" in category`. Priced $0.
   - **Graviton Migration** (`instance_type` not in `m7g/r7g/m6g/r6g/c7g/c6g/t4g`):
     string `"Estimated: 20-40%…"`. Adapter rate `0.25` × live monthly ×
     `instance_count`. Counted (if it wins the per-domain dedup).
   - **Storage Optimization** (`storage_type == "gp2"`): string `"20% storage
     cost"`. Adapter: `EBSVolumeSize × 0.11 × pricing_multiplier × 0.20`. Counted.
   - **Idle Domain** (`avg_cpu < 5`): string `"100% of domain cost"`. **Adapter
     rate = `rate_by_category.get("Idle Domain", 0.0)` = 0.0 → priced `$0` →
     advisory/dropped.** This is the biggest real saving (delete the domain) and
     it is silently zeroed — confirm and quantify (Phase 2/5).
   - **Underutilized Domain** (`5 <= avg_cpu < 20`): string `"30-50%"`. Adapter
     rate `0.30` × live monthly × `instance_count`. Counted; deduped against
     Graviton per domain (alternatives on the same nodes — `best_instance` keeps
     the max).
   - **Old versions** check is intentionally removed (no cost delta) — confirm it
     emits nothing.
   Map every rec to the single `enhanced_checks` SourceBlock.

### Phase 2 — Accuracy of every number (validate with MCP)

3. Re-derive each **counted** figure from the live AWS Pricing API. Record a
   structured **AuditBasis** (rate / region / metric-window / formula) per counted
   finding, as the Lambda/RDS audits did.
   - **Instance rate (`AmazonES`).** Confirm `get_instance_monthly_price("AmazonES",
     instance_type)` resolves the real SKU. OpenSearch instance types carry a
     **`.search` suffix** (e.g. `r6g.large.search`) and the Pricing API
     `instanceType` matches that exact string (validated: `r6g.large.search` =
     **$0.167/hr** in us-east-1, `productFamily="Amazon OpenSearch Service
     Instance"`, `operation="ESDomain"`, `usagetype="ESInstance:r6g.large"`).
     Check `_fetch_generic_instance_price` pins a deterministic filter (operation
     `ESDomain`, not a UltraWarm/cross-cluster SKU) and does not `MaxResults=1`
     over multiple operations — non-determinism here silently mis-prices.
   - **Graviton 0.25 / Underutilized 0.30 reduction factors.** These are flat
     `price × factor`, NOT the exact `current − target` delta the EC2 audit
     mandates. A Graviton "25%" with no target instance, and a downsize "30%" with
     no target size, are undefended reduction factors — validate against a real
     r-family x86→Graviton delta and a one-size-down delta, or label them as
     coarse advisory. (Reference: EC2 m4→m6i factor was 2.8× too high.)
   - **Storage constant `GP3_PRICE_PER_GB_MONTH = 0.11`.** The live OpenSearch
     gp3 rate is **$0.122/GB-month** (`AmazonES`, `usagetype=ES:GP3-Storage`,
     us-east-1) — the constant is **understated ~10%**. Worse, the check is a
     **gp2→gp3 migration** (`storage_type=="gp2"`), so the real saving is the
     gp2→gp3 *delta*: gp2 = **$0.135/GB-mo** (`ES:GP2-Storage`), gp3 = $0.122 →
     delta **≈ $0.013/GB-mo ≈ 9.6% of gp2**. The adapter instead bills
     `0.11 × 0.20 = $0.022/GB-mo` (≈18% of the constant), which **overstates** the
     real migration delta ~1.7×. Also note `STORAGE_SAVINGS_FACTOR`'s docstring
     claims it is a "cold-tier / UltraWarm" midpoint, but it is applied to a
     gp2→gp3 rec — UltraWarm/Managed storage is **$0.024/GB-mo** (an ~80% cut vs
     gp3), so neither the factor nor its rationale matches the trigger. Flag the
     constant value, the factor, and the description/trigger mismatch.
   - **Region scaling.** Instance recs use `get_instance_monthly_price` (already
     region-correct) and are NOT multiplied by `pricing_multiplier` — confirm no
     double-apply. Storage recs use the us-east-1 constant `0.11 ×
     pricing_multiplier` — confirm the multiplier is the only region scaling and
     that it is applied exactly once.
   - **`instance_count` multiply.** Instance recs multiply by `instance_count`
     (data-node count). Confirm dedicated-master / UltraWarm / warm nodes are not
     double-counted or omitted from the priced node count (the shim only reads
     `ClusterConfig.InstanceType` / `InstanceCount` — the data tier only).
4. Confirm each counted number is defensible from the report alone. A Graviton or
   downsize saving emitted as a flat `monthly × factor` with no target instance
   and no per-node breakdown is a finding — prefer a concrete target + delta or a
   `$0` advisory.

### Phase 3 — Duplication (no dollar counted twice)

5. **Intra-adapter / intra-domain.** The adapter already dedupes Graviton vs
   Underutilized per domain via `best_instance` (keeps the higher-$ instance-axis
   lever) and keeps storage as a separate axis. Verify: (a) a single domain can
   still stack **Storage + (Graviton|Underutilized)** — is that legitimate
   (different cost components) or double-dipping? (b) **Idle Domain** (full delete)
   and Graviton/Underutilized are mutually exclusive levers on the same nodes —
   because Idle is priced `$0` it never enters `best_ids`, so a genuinely idle
   domain could be counted for a *downsize* instead of the larger *delete* saving.
   Confirm the dedup picks the right lever once Idle is priced correctly.
6. **Cross-source (the orphan).** Confirm whether the SAME domain can be surfaced
   by both the heuristic checks here AND Cost Optimization Hub
   (`OpenSearchDomain`). Today CoH is dropped (never read), so there is no *double*
   count — but the fix (consume `cost_hub_splits["opensearch"]`) MUST add
   authority dedup **CoH > heuristic** by normalized domain id (strip ARN), mirror
   `services/rds_logic.py`. Decide whether CoH rightsizing should supersede the
   heuristic Graviton/downsize recs for the same domain.
7. **Cross-adapter.** OpenSearch domains are not surfaced by any other tab; confirm
   no `_extract_*` helper in `html_report_generator.py` pulls OpenSearch EBS
   volumes into the EBS/Snapshots synthetic tabs (OpenSearch-managed EBS is not a
   standalone `ec2:Volume`, so it should not — verify).

### Phase 4 — Coverage (works for ALL domains, not a subset)

8. Pagination & client coverage. `opensearch.list_domain_names()` is a single
   call (no paginator) — confirm it returns ALL domains in the region (it has no
   pagination token, so this is acceptable, but note legacy `es` (Elasticsearch)
   domains: are they returned by the `opensearch` client, or do they need the
   `es` client?). `describe_domain` is per-domain — confirm no silent skip on a
   describe failure beyond the logged inner `except` (Phase 5).
9. Whole-class skips. Confirm intentional/documented handling of: **Serverless
   collections** (OpenSearch Serverless — OCU-priced, not instance-priced; the
   describe path returns no `ClusterConfig.InstanceType` → priced `$0`);
   **UltraWarm / cold storage tiers** (never priced — a cold-tier downsize saving
   is missed); **multi-AZ / dedicated-master** node costs (omitted from the priced
   node count); domains with `InstanceCount` defaulting to 0 (no `ClusterConfig`)
   — mirror the EKS 0-node / scaled-to-zero fix: a 0-node domain should not be
   flagged for an instance saving.

### Phase 5 — Silent failures (nothing fails quietly)

10. Enumerate every `except`/`logger`-only/`return`-fallback in
    `services/opensearch.py`:
    - Outer `except Exception as e: ctx.warn(...)` around the whole scan — OK
      (recorded), but confirm `AccessDenied`/`UnauthorizedOperation`/
      `OptInRequired` are classified to `ctx.permission_issue`, not a generic
      `ctx.warn` (mirror the canonical silent-failure class).
    - Inner per-domain `except Exception as e: logger.warning(...)` (~line 170) —
      a `describe_domain` permission/throttle failure makes the domain **vanish
      from the report with NO `ctx.warn`** (logger only). Classify and record it.
    - CloudWatch `except Exception as e: logger.warning(...); continue` (~line 166)
      — a `GetMetricStatistics` AccessDenied/throttle silently drops the idle/
      underutilized checks for that domain, logger-only. Record on `ctx`.
11. Pricing-miss → `$0`. `get_instance_monthly_price` returns `0.0` on a Pricing
    API miss (no fallback constant on the instance path). A counted instance rec
    that prices to `0.0` would be dropped by the `keep = … and EstimatedMonthlySavings
    > 0` guard — confirm a real saving is never silently zeroed by a transient
    pricing miss (it would be, with no warning). Also confirm the **Idle Domain
    `$0` defect** (Phase 1/2): `rate_by_category` has no `"Idle Domain"` key, so the
    full-delete saving is computed as `monthly × count × 0.0 = $0`, demoted to
    advisory, and dropped from the count — a counted-savings leak (the inverse of
    the usual over-count bug). This is a confirmed bug, not a tradeoff.
12. **Cost Optimization Hub orphan (primary).** Confirm `ctx.cost_hub_splits["opensearch"]`
    is populated (Phase 0) and that NO code in the adapter consumes it →
    `OpenSearchDomain` CoH recs are dropped with no warning. On a full-region
    scan, the orchestrator's "no service bucket" warning will NOT fire (the bucket
    exists; it is the *consumer* that is missing), so the drop is invisible.
    Decide the fix: consume the bucket in `scan()` (mirror RDS), dedup
    CoH > heuristic, demote `OpenSearchReservedInstances`-style commitment recs to
    advisory.
13. Opt-in / metric-gated `$0` nudges. The **Reserved Instances Opportunity** rec
    is correctly demoted (`Counted=False`) — confirm it is RENDERED (visible) but
    excluded from counts. The **Idle/Underutilized** recs depend on a CloudWatch
    metric; if `Datapoints` is empty the checks never fire (no $0 leak there), but
    confirm the CloudWatch read is skipped under `ctx.fast_mode` (it is NOT today)
    and that `reads_fast_mode` is declared — mirror the Lambda fast-mode fix.

### Phase 6 — Reporting (one tab, counted == rendered)

14. Single source / registered handler. The adapter emits one source
    `enhanced_checks`; `PHASE_B_HANDLERS[("opensearch","enhanced_checks")]` is
    registered → no silent unrendered source. Confirm.
15. **Counted == rendered (the desync).** `_render_opensearch_enhanced_checks`
    groups recs by `CheckCategory` and prints **`domains[0].get("EstimatedSavings")`
    — the raw human string** (e.g. `"100% of domain cost"`, `"30-50%"`), NOT the
    adapter's per-rec `EstimatedMonthlySavings` float. So the visible card shows a
    **percentage**, while the headline sums **dollars** — a guaranteed desync.
    Worse, the renderer ignores `Counted` entirely: advisory `$0` recs
    (Reserved, Idle) render identically to counted recs, and it only shows ONE
    string per category even when per-domain dollars differ. Verify the per-tab
    total equals the sum of `Counted=True` `EstimatedMonthlySavings`, and that the
    cards either show the per-domain dollar or are clearly marked advisory.
    Reconcile the executive-summary headline (`_get_executive_summary_content` +
    `_calculate_service_savings` + reconciliation footnote) against the per-service
    total.
16. No counted-but-undisplayed (or displayed-but-uncounted) findings across all
    `CheckCategory` groups. The Idle-Domain `$0` case is displayed-but-uncounted
    today; the percentage strings are displayed-but-not-the-counted-number.

### Phase 7 — Tooling & evidence

17. Run a real scan scoped to opensearch:
    `python3 cli.py <region> --scan-only opensearch`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service opensearch`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from the Idle-Domain leak), and resource appearing in >1 source.
    Reconcile the headline against the per-category sum. Caveats: idle/underutilized
    require ≥1 CloudWatch datapoint over 14d (exercise the empty-metric path); try
    a second region if the first has no domains; create/inspect a gp2 domain to
    exercise the storage path. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC` (the shim imports `from datetime import UTC`).
18. Prove each claim. For the orphan: do a **full** scan, dump
    `ctx.cost_hub_splits["opensearch"]`, and show `OpenSearchDomain` recs present
    in the bucket yet absent from the rendered tab. For accuracy: show the AWS
    Pricing API value (`ES:GP3-Storage` $0.122, `ES:GP2-Storage` $0.135,
    `ES:Managed-Storage` $0.024, an `AmazonES` instance hourly) next to the
    scanner's constant/rate. For the Idle-Domain `$0` defect: show a domain with
    `avg_cpu < 5` rendered with `EstimatedMonthlySavings == 0` and excluded from
    the count.

### Deliverable

- The complete check list (Phase 1.2), per `CheckCategory`, with counted-vs-advisory
  marked and the priced formula.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** (orphan CoH drop;
  Idle-Domain `$0`; render shows percentage not dollar; gp3 constant + factor
  mis-set) from **known limitations / tradeoffs** (Serverless/UltraWarm not
  priced; coarse reduction factors). End with a short, ID'd fix plan
  (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)

- Add `tests/test_opensearch_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `OpensearchModule.scan` with a `SimpleNamespace` ctx + a fake `opensearch`
  client (`list_domain_names` / `describe_domain`) + a fake `cloudwatch`
  paginator-free `get_metric_statistics`, and a fake `pricing_engine`. Cover every
  fix: Idle-Domain now counted at the right $ (full domain cost), CoH bucket
  consumed + CoH>heuristic dedup by normalized domain id, gp3 constant/factor
  corrected to the real gp2→gp3 delta, instance-rate region/`instance_count`
  correctness, silent-failure classification (`AccessDenied`→`permission_issue`),
  fast-mode skip of the CloudWatch read, and render wiring (counted == rendered:
  card shows the per-domain dollar or an advisory tag, not a bare percentage).
- For any reduction factor (Graviton 0.25, downsize 0.30) replace with a concrete
  target + exact `current − target` delta, or keep it a `$0` advisory — never
  fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for opensearch first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same orphan-bucket / `$0`-leak / render-desync bug in a sibling
  adapter out of scope (elasticache / redshift / s3 are the known CoH orphans),
  note it as a follow-up (don't fix unprompted).
- Update the `opensearch.py` row in `services/adapters/CLAUDE.md` to match reality
  (hybrid keyword + live-instance pricing; CoH-consuming once fixed).
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)

<!-- UNIVERSAL — embed verbatim in every prompt -->
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

<!-- OPENSEARCH-SPECIFIC — discovered from the code -->
- **Orphaned CoH bucket (CRITICAL):** `_HUB_SERVICES` + `type_map` populate
  `ctx.cost_hub_splits["opensearch"]` from `OpenSearchDomain` recs, but
  `OpensearchModule.scan` never reads it → AWS-authoritative rightsizing dollars
  silently dropped, with NO "no service bucket" warning (the bucket exists; the
  consumer is missing). Fix by consuming the bucket (mirror RDS) with CoH>heuristic
  dedup.
- **Idle-Domain priced at `$0` (CRITICAL):** `rate_by_category` has keys only for
  `"Graviton Migration"` and `"Underutilized Domain"`; `"Idle Domain"` falls to the
  `0.0` default → `monthly × count × 0.0 = $0` → demoted to advisory and dropped.
  The largest real saving (delete the domain = 100% of cost) is never counted.
- **Render shows percentage, not the priced dollar (HIGH):**
  `_render_opensearch_enhanced_checks` prints `domains[0]["EstimatedSavings"]`
  (the shim's `"30-50%"`/`"100% of domain cost"` string) while the headline sums
  `EstimatedMonthlySavings` (float) — guaranteed counted≠rendered desync; renderer
  also ignores `Counted` (advisory recs look identical to counted ones).
- **gp3 storage constant + savings factor wrong (HIGH):**
  `GP3_PRICE_PER_GB_MONTH = 0.11` vs live `ES:GP3-Storage` **$0.122/GB-mo**
  (understated ~10%); the check is gp2→gp3, whose real delta is
  `$0.135 − $0.122 ≈ $0.013/GB-mo (~9.6%)`, but the adapter bills `0.11 × 0.20 =
  $0.022/GB-mo`, overstating ~1.7×; `STORAGE_SAVINGS_FACTOR`'s "UltraWarm" docstring
  (Managed storage is $0.024/GB-mo, ~80% cut) doesn't match the gp2→gp3 trigger.
- **Flat reduction factors with no target (MEDIUM):** Graviton `0.25` and downsize
  `0.30` are `monthly × factor`, not `current − target` deltas; no target instance/
  size recorded → undefended numbers (EC2 audit precedent: factors off 2-3×).
- **CloudWatch read not fast-mode-gated, failures logger-only (MEDIUM):** the
  `AWS/ES CPUUtilization` read runs even under `ctx.fast_mode`; adapter declares
  neither `requires_cloudwatch` nor `reads_fast_mode`; the inner CloudWatch/describe
  `except` paths use `logger.warning` (+`continue`) with no `ctx.warn`/
  `ctx.permission_issue`, so a permission/throttle gap silently removes domains.
- **Serverless / UltraWarm / dedicated-master not priced (MEDIUM):** the shim reads
  only `ClusterConfig.InstanceType`/`InstanceCount` (data tier); OpenSearch
  Serverless (OCU-priced) yields empty `InstanceType` → priced `$0`; UltraWarm/cold
  (`ES:Managed-Storage` $0.024/GB-mo) and dedicated-master nodes are never costed,
  and a 0-node/empty-ClusterConfig domain is not guarded (mirror the EKS 0-node fix).

## PROMPT (end)
