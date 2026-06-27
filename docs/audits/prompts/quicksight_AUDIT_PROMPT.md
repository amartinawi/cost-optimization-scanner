# QuickSight Adapter Cost-Audit Prompt

A deep, QuickSight-specific audit brief in the same structure as the Lambda /
RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* QuickSight code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`quicksight`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, units,
editions, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the recently-audited
**Lambda** adapter (`services/adapters/lambda_svc.py`) as the worked example for
the **metric-gated `$0` advisory** pattern (`mark_zero_savings_advisory`,
`Counted=False`), the **region-scaled module constant**, and the structured
**AuditBasis** + test style I expect; treat **`services/adapters/file_systems.py`**
as the model for **evidence-gating a saving on a real utilization metric** before
counting it; and treat the **EC2** adapter (`services/adapters/ec2.py`) as the
canonical model for the `$0`-placeholder→advisory discipline.

### NOTE on structure (QuickSight is NOT shaped like EC2/RDS/Lambda)
- The adapter is a **thin two-layer split**: `services/adapters/quicksight.py`
  (`QuicksightModule`, ~69 lines) holds the **pricing + counting** logic, and it
  delegates *detection* to a free function `get_enhanced_quicksight_checks(ctx)`
  in the legacy helper **`services/quicksight.py`** (~99 lines). There IS a
  `services/quicksight.py` (unlike network) — read both; the describe-API calls
  and the SPICE-underutilization heuristic live in the helper, the dollar math
  lives in the adapter.
- The adapter emits a **single SourceBlock** named **`enhanced_checks`** (line
  ~60). It is NOT registered in `PHASE_B_HANDLERS`, NOT in `_PHASE_A_SERVICES`,
  and NOT in `_PHASE_B_SKIP_PER_REC` — so it renders through the **generic
  per-rec fallback** `render_generic_per_rec("quicksight", …)` →
  `_render_generic_other_rec` in `reporter_phase_b.py`. There is no
  `"quicksight"` entry in `html_report_generator.py`'s descriptor table (only
  `bedrock`, `eks_cost`, etc.) and the module declares **no `stat_cards` / no
  `grouping` / no `extras`**. Remember this for Phase 6.
- QuickSight consumes **neither Cost Optimization Hub nor Compute Optimizer**.
  It is NOT in `core/scan_orchestrator.py`'s `_HUB_SERVICES` set or its
  `type_map`, and pulls no CO helper. So a "missing CoH/CO source" finding is
  **NOT fair game** here — savings are expected to be locally derived from the
  QuickSight describe APIs. There is also **no CloudWatch read** — SPICE
  utilization comes from `describe_spice_capacity` (`UsedCapacityInBytes` /
  `TotalCapacityInBytes`), so `requires_cloudwatch` / `reads_fast_mode` are
  correctly NOT declared. Focus on **pricing/edition accuracy**, the
  **string-vs-counted desync**, the **free-allotment / removable-floor** question,
  and **render wiring**.
- Savings are carried **two ways on the same rec**: a human **string**
  `EstimatedSavings = "~$X/month"` written by the helper (`services/quicksight.py`
  ~line 77-81, hardcoded `* 0.38`, NO region multiplier) AND a numeric
  `EstimatedMonthlySavings` written by the adapter (`UnusedSpiceCapacityGB *
  0.38 * ctx.pricing_multiplier`, lines 42-50). The counted total
  (`total_monthly_savings`) sums the **numeric** path; the renderer **shows the
  string**. These two can disagree (see Phase 2/6).

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `quicksight.py` row (listed
    under Live Pricing as "SPICE tier pricing ($0.25–$0.38/GB) | Module
    constants"). **Reconcile the doc against reality:** the adapter hardcodes a
    **single** `SPICE_PRICE_PER_GB = 0.38` (line 42) and a code comment claims
    "$0.38/GB-mo for BOTH Standard and Enterprise editions." The Pricing API
    disagrees — confirm the real per-edition split (Phase 2) and decide whether
    the doc's "$0.25–$0.38" range or the code's flat $0.38 is correct.
0b. Confirm module identity in `services/adapters/quicksight.py`: `key="quicksight"`,
    `cli_aliases=("quicksight",)`, `display_name="QuickSight"`,
    `required_clients()` returns `("quicksight",)`. Note the helper also needs
    `ctx.account_id` (passed as `AwsAccountId=` to every describe call) and that
    QuickSight is a **regional** service identity-wise but billed per-account —
    confirm the scan region matches the edition/region the user actually pays in.
0c. QuickSight has **no AWS advisory source** (no CoH / CO) and **no CloudWatch**.
    Drop those axes. The whole cost signal is: account subscribed → users exist →
    SPICE capacity > 50% idle → recommend reducing. Everything hinges on the SPICE
    rate, the edition, and whether "unused = total − used" is actually *removable*.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/quicksight.py` (the `scan` method and
   its pricing loop) and `services/quicksight.py`
   (`get_enhanced_quicksight_checks`, `QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS`);
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`); `services/_base.py`
   (`BaseServiceModule` defaults the adapter inherits); `core/scan_context.py`
   (`ctx.client`, `ctx.account_id`, `ctx.pricing_multiplier`, `ctx.warn`);
   `core/result_builder.py` (how `total_monthly_savings` is serialized and
   summed); and the reporter path `reporter_phase_b.py`
   (`render_generic_per_rec`, `_render_generic_other_rec`,
   `should_fallback_to_per_rec`, `should_use_handler`) plus
   `html_report_generator._get_detailed_recommendations` (lines ~3300-3362).
2. Enumerate **every** cost check (there is effectively ONE active check) and for
   each give: trigger condition, data source (describe-API field or heuristic),
   the exact `EstimatedSavings` string template, the constant it embeds, and
   whether it parses/sums to a **counted** dollar or a **$0 advisory**. The known
   inventory to confirm in `services/quicksight.py`:
   - **`spice_optimization`** (the only emitter): trigger =
     `total_capacity > 0 and used_capacity < total_capacity * 0.5` (≥50% idle).
     Source = `describe_spice_capacity().SpiceCapacityConfiguration`
     (`UsedCapacityInBytes` / `TotalCapacityInBytes`, both ÷ 1024³ to GB).
     `UnusedSpiceCapacityGB = total − used`. String =
     `"~${(total−used) * 0.38:.0f}/month"`. Counted via the adapter's numeric
     `EstimatedMonthlySavings`.
   - **`user_optimization`** and **`capacity_optimization`** buckets are
     **declared but never populated** (lines 27-31) — confirm they are dead and
     that the `Edition`/per-user (author/reader) cost lever is entirely absent
     (a coverage gap, Phase 4).
   - The gate `AccountSubscriptionStatus != "ACCOUNT_CREATED"` early-returns; the
     per-namespace `list_users` loop only counts users to confirm `total_users > 0`.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Re-derive the SPICE figure from the live AWS Pricing API (`AmazonQuickSight`)
   and confirm it matches. The validated facts (confirm them yourself, do not
   take them on faith):
   - **SPICE is edition-priced.** `USE1-QS-Enterprise-SPICE` = **$0.38/GB-Mo**
     (Enterprise); `USE1-QS-Provisioned-SPICE` = **$0.25/GB-Mo** (Standard
     edition). The adapter's flat `SPICE_PRICE_PER_GB = 0.38` and its comment
     ("$0.38 for BOTH Standard and Enterprise") are therefore **wrong for
     Standard-edition accounts** — a Standard account is over-charged by **52%**
     ($0.38 vs $0.25). The real edition comes from
     `describe_account_subscription().AccountInfo.Edition`
     (STANDARD / ENTERPRISE / ENTERPRISE_AND_Q), which the helper reads the
     *status* of but never the *edition*. Note also the helper mislabels
     `Edition` using `capacity_config.get("PurchaseMode", "ENTERPRISE")` — that is
     the SPICE **purchase mode**, not the account edition. Pin pricing to the real
     edition via a pinned-filter Pricing lookup (`usagetype` ANY_OF the two SKUs)
     rather than a single hardcoded constant.
   - **Region scaling on the counted path** is applied
     (`SPICE_PRICE_PER_GB * ctx.pricing_multiplier`, line 45) — confirm that is
     correct and NOT double-applied (there is no PricingEngine call here, so a
     single multiply against the us-east-1 constant is the right pattern; contrast
     EBS/RDS which must NOT multiply an already-region-correct engine price).
   - **The display string is NOT region-scaled.** `services/quicksight.py` line
     ~80 computes the string with a bare `* 0.38` and no multiplier, so in any
     non-us-east-1 region the **shown** `~$X/month` disagrees with the **counted**
     `EstimatedMonthlySavings`. Flag this desync (Phase 6 too).
4. **Is "unused = total − used" actually removable?** SPICE is reduced in whole
   GB but, more importantly, Enterprise edition includes a **free SPICE allotment
   per paid author** (historically 10 GB/author) and a per-reader allotment; you
   cannot save below `used` and you cannot save the included-free GB. Charging
   $0.38 × (total − used) treats the **entire** headroom as recoverable cash,
   ignoring (a) the free included allotment and (b) that capacity can only shrink
   to ≥ `used`. Validate the included-allotment figure via AWS Knowledge MCP and
   decide whether the saving should be `max(0, (total − used − included_free)) ×
   rate`. Record a structured **AuditBasis** (rate / region / edition /
   used-vs-total GB / formula) on the counted finding so the number is defensible
   from the report alone, as the Lambda/RDS audits did.
5. The 50%-idle trigger is a heuristic threshold, not a price — confirm it is
   labelled as such and that the *saving* (not just the flag) is grounded in the
   measured `UnusedSpiceCapacityGB`, not in the threshold.

### Phase 3 — Duplication (no dollar counted twice)
6. **Intra-adapter:** only one bucket (`spice_optimization`) is ever populated, so
   there is no intra-domain stacking today — confirm the dead `user_optimization`
   / `capacity_optimization` buckets cannot silently start double-counting the
   same account-level SPICE capacity if later filled. SPICE capacity is an
   **account-level** singleton (one `describe_spice_capacity` per account/region),
   so verify the loop cannot emit more than one SPICE rec per account (it appends
   once — confirm).
7. **Cross-adapter:** QuickSight SPICE is unique to this adapter; no other adapter
   prices it, and no `_extract_*` helper in `html_report_generator.py` pulls
   QuickSight resources into a synthetic tab. Confirm both.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Pagination/coverage: `list_namespaces` and `list_users` are paginated
   (confirm), but `describe_spice_capacity` is account-level — confirm it is
   region-correct and that a multi-namespace account is fully summed. Confirm the
   `AccountSubscriptionStatus` gate does not skip valid `ENTERPRISE_AND_Q` or
   `CREATED` variants.
9. **Whole-class skips (the real coverage gap):** the adapter prices **only**
   idle SPICE capacity. It never surfaces the **per-user author/reader** cost
   lever — unused/over-provisioned **Authors** (~$18-24/mo Enterprise),
   never-logged-in **Readers** ($3.00/mo cap Enterprise, validated via
   `USE1-Reader-Enterprise-Month`), Standard-edition authors ($9-12/mo), or
   capacity-pricing Readers. A QuickSight account with idle authors but
   well-utilized SPICE produces **zero** recommendations. Decide whether that is
   an intentional limitation or a gap to fill (with real `last-login` /
   activity evidence, not a config-only assumption). Also confirm the 50% trigger
   does not exclude a 60%-idle account that is still wasting real money.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return []`
    fallback in `services/quicksight.py`:
    - The inner SPICE block (`describe_spice_capacity`) is wrapped in
      `except Exception: pass` (~line 85-86) — a permission gap
      (`quicksight:DescribeSpiceCapacity`) or throttle makes the whole SPICE
      finding **vanish silently** with no `ctx.warn` / `ctx.permission_issue`.
      Classify: `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`,
      other → `ctx.warn`.
    - The per-namespace `list_users` loop swallows errors with
      `except Exception: continue` (~line 52-53) — a namespace that fails to list
      users silently under-counts `total_users`, which can flip `total_users > 0`
      to false and suppress the SPICE check entirely. Record it on `ctx`.
    - The outer `except Exception as e` (~line 88-93) already routes the
      "account does not exist" case to `logger.info` and everything else to
      `ctx.warn` — confirm that the `ResourceNotFoundException` string-match is
      robust and that real permission errors are not misrouted to the silent
      "not enabled" branch.
11. Does a pricing/edition miss still emit a counted finding? If `describe_account_
    subscription` cannot return the edition, the adapter falls back to the flat
    $0.38 and **still counts** the saving — for a Standard account that is a 52%
    over-count. A finding whose rate cannot be confirmed for the actual edition
    should be advisory (`Counted=False`) or edition-resolved, not silently
    counted at the Enterprise rate. The adapter's `else` branch (lines 51-58)
    already sets `EstimatedMonthlySavings = 0.0` + a `PricingWarning` when
    `UnusedSpiceCapacityGB` is absent — confirm that path is reached and that such
    a rec is shown but not counted (today it has no `Counted=False`; verify the
    counted total excludes it because the numeric is 0.0).

### Phase 6 — Reporting (one tab, counted == rendered)
12. **Source-name vs handler (verify carefully):** the adapter emits one source
    `enhanced_checks`. There is **no** `("quicksight","enhanced_checks")` entry in
    `PHASE_B_HANDLERS`, and `quicksight` is **not** in `_PHASE_B_SKIP_PER_REC`, so
    `should_fallback_to_per_rec("quicksight")` is **True** and rendering goes
    through `render_generic_per_rec("quicksight", recs, "enhanced_checks")` →
    `_render_generic_other_rec`. Trace
    `html_report_generator._get_detailed_recommendations` and confirm the recs
    actually reach that renderer (contrast network, which IS in
    `_PHASE_B_SKIP_PER_REC` and so renders nothing without a handler). Confirm a
    QuickSight rec is visible in the tab.
13. **String-vs-counted desync (the QuickSight-specific render bug):**
    `_render_generic_other_rec` prints `EstimatedSavings` (the helper's
    non-region-scaled `~$X/month` string), while the headline counts the adapter's
    region-scaled numeric `EstimatedMonthlySavings`. In any non-us-east-1 region
    the card text and the counted number disagree. Also note the renderer dumps
    every other rec key (`UserCount`, `UsedCapacityGB`, `UtilizationPercent`,
    `UnusedSpiceCapacityGB`, `Edition`, `PricingWarning`) as `<p>` lines — confirm
    none leak a misleading value and that the `Edition` shown (from `PurchaseMode`)
    is not presented as the billing edition.
14. **Counted == rendered:** `total_recommendations = len(recs)` but
    `total_monthly_savings` sums only the numeric path. Reconcile the per-tab
    headline against the sum of the counted rendered findings, and reconcile the
    executive-summary headline (`_get_executive_summary_content` +
    `_calculate_service_savings`) against the per-service total. Confirm a rec
    with `EstimatedMonthlySavings = 0.0` + `PricingWarning` is shown but
    contributes $0, and that it is not silently dropped from the table (or
    counted in the total).

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to QuickSight:
    `python3 cli.py <region> --scan-only quicksight`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service quicksight`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and any edition mismatch. Caveats: QuickSight must be
    **enabled** in the account/region or you get the "account does not exist"
    skip — try the region where QuickSight actually lives; exercise BOTH a
    Standard-edition and an Enterprise-edition account if you can, to prove the
    $0.25-vs-$0.38 finding; a fully-utilized SPICE account (<50% idle) produces no
    rec (exercise the no-finding path). Use `.venv/bin/python` (3.14) — system
    `python3` lacks `datetime.UTC`.
16. For the accuracy claim, show the AWS Pricing API value
    (`USE1-QS-Enterprise-SPICE` $0.38 vs `USE1-QS-Provisioned-SPICE` $0.25)
    next to the scanner's constant and next to the account's real edition. For the
    string-vs-counted desync, show a non-us-east-1 run where the card string and
    the counted number differ.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked and the
  dead `user_optimization` / `capacity_optimization` buckets called out.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add a `tests/test_quicksight_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `QuicksightModule.scan` and `get_enhanced_quicksight_checks` with a
  `SimpleNamespace` ctx + a fake `quicksight` client (stubbed
  `describe_account_subscription`, `list_namespaces`, `list_users`,
  `describe_spice_capacity` paginators) and assert: edition-correct SPICE rate
  (Standard $0.25 vs Enterprise $0.38), region scaling on BOTH the counted number
  and the display string, free-allotment flooring, silent-failure classification
  (DescribeSpiceCapacity AccessDenied → `permission_issue`), and counted ==
  rendered. Cover the no-finding (<50% idle) and `$0`-advisory (missing
  `UnusedSpiceCapacityGB`) paths.
- Resolve the real account **edition** from `describe_account_subscription` and
  pin the SPICE rate to it (or to a Pricing API lookup); stop hardcoding $0.38.
- Make the helper's `EstimatedSavings` string and the adapter's numeric agree
  (single region-scaled source of truth), and record a structured **AuditBasis**
  on the counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for quicksight first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same edition-blind pricing / silent-failure bug in a sibling
  adapter out of scope, note it as a follow-up (don't fix unprompted).
- Update the `quicksight.py` row in `services/adapters/CLAUDE.md` to match reality
  (edition-split SPICE, single `enhanced_checks` source, generic per-rec render).
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

### Known issue catalogue — QuickSight-specific (found while grounding this prompt)
- **Edition-blind SPICE rate.** `SPICE_PRICE_PER_GB = 0.38` is hardcoded for ALL
  accounts and the code comment claims it applies to both editions, but the
  Pricing API shows Enterprise SPICE = **$0.38/GB-mo** (`USE1-QS-Enterprise-SPICE`)
  while Standard SPICE = **$0.25/GB-mo** (`USE1-QS-Provisioned-SPICE`) — a flat
  $0.38 over-charges every Standard-edition account by **52%**. Resolve the real
  edition from `describe_account_subscription().Edition`.
- **`PurchaseMode` mislabelled as `Edition`.** The helper sets the rec's `Edition`
  field from `capacity_config.get("PurchaseMode", "ENTERPRISE")` — that is the
  SPICE purchase mode, not the billing edition, and it defaults to ENTERPRISE,
  reinforcing the over-charge.
- **Display string ≠ counted number.** `services/quicksight.py` builds
  `EstimatedSavings = "~$…/month"` with a bare `* 0.38` (no `pricing_multiplier`),
  while the adapter counts `UnusedSpiceCapacityGB * 0.38 * ctx.pricing_multiplier`
  — the rendered card text and the headline diverge in any non-us-east-1 region.
- **Free included SPICE allotment ignored.** Saving = $0.38 × (total − used)
  counts the entire headroom as recoverable, ignoring the per-author included-free
  SPICE GB and the fact that capacity cannot shrink below `used` — over-states the
  realizable saving.
- **Whole per-user cost lever missing.** Idle/over-provisioned **Authors**
  (~$18-24/mo Enterprise) and never-logged-in **Readers** ($3.00/mo cap,
  `USE1-Reader-Enterprise-Month`) are never surfaced; the `user_optimization` /
  `capacity_optimization` buckets are declared but dead. An account with idle
  authors but healthy SPICE yields zero recommendations.
- **`describe_spice_capacity` failure is swallowed silently** (`except: pass`),
  and a per-namespace `list_users` failure silently under-counts users and can
  suppress the entire SPICE check — neither is recorded on `ctx`.

## PROMPT (end)
