# WorkSpaces Adapter Cost-Audit Prompt

A deep, WorkSpaces-specific audit brief in the same structure as the Lambda /
RDS / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* WorkSpaces code path so the auditor starts from
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
> Service-specific live-audit findings for `workspaces`:
> - **PROMPT-BODY DRIFT (correct before trusting it):** the adapter has changed since this prompt was generated — it now declares `requires_cloudwatch=True`, `reads_fast_mode=True`, and `required_clients()` returns `("workspaces", "cloudwatch")`, and the shim reads CloudWatch for the billing-mode lever (classify CW failures via `record_aws_error`, gate reads on `ctx.fast_mode`). Treat the body's "no usage telemetry / no cloudwatch / no requires_cloudwatch" statements as STALE.
> - Otherwise run the invariant sweeps in `_LIVE_AUDIT_LESSONS.md` and the known-issue catalogue below (advisory-leak, string↔numeric agreement, flat-global rate scaling, silent-failure classification).

You are auditing the **`workspaces`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the recently-audited
**Lambda** adapter (`services/adapters/lambda_svc.py`) as the worked example for
the metric-gated `$0`-advisory pattern (`mark_zero_savings_advisory`,
`Counted=False`), the arch/constant-aware pricing discipline, the structured
`AuditBasis`, and the test style I expect; treat **file_systems**
(`services/adapters/file_systems.py`) as the model for CloudWatch
**evidence-gating** a saving (the discipline this adapter currently lacks); and
**EC2** (`services/adapters/ec2.py`) as the model for the exact current→target
price delta and the `$0`-placeholder→warning pattern.

### NOTE on structure (WorkSpaces is a config-only adapter with a hardcoded price table)
- The adapter is `services/adapters/workspaces.py` → `WorkspacesModule.scan`. It
  delegates discovery to **`services/workspaces.py` →
  `get_enhanced_workspaces_checks(ctx)`** (a free function, NOT a legacy
  `ServiceModule`), then re-prices everything itself.
- **There is NO usage telemetry.** `required_clients()` returns `("workspaces",)`
  only — no `cloudwatch` client, no `requires_cloudwatch`, no `reads_fast_mode`.
  Every saving is derived from configuration (running mode, state, bundle type)
  with **zero** measured utilization. This is the central scope question for the
  whole audit.
- **Pricing is a hardcoded table, NOT the PricingEngine.** The adapter docstring
  states the generic `get_instance_monthly_price("AmazonWorkSpaces", …)` lookup
  is "structurally dead" and uses `WORKSPACE_BUNDLE_MONTHLY` (a us-east-1 Windows
  + Plus list-price dict in `services/workspaces.py`) instead, region-scaled by
  `ctx.pricing_multiplier`. The module constant
  `_AUTOSTOP_SAVINGS_FACTOR = 0.30` (in `services/adapters/workspaces.py`)
  captures the AlwaysOn→AutoStop delta midpoint.
- The shim returns `{"recommendations": [...], "checks": {...}}` with buckets
  `billing_mode_optimization`, `bundle_rightsizing`, `unused_workspaces`. The
  adapter emits a **single SourceBlock `enhanced_checks`** (the flattened
  `recommendations` list) — NOT per-bucket sources.
- **Counted vs displayed:** for `bundle_rightsizing` the shim sets
  `EstimatedSavingsAmount` (a concrete `current − target` delta) and `scan()`
  uses it directly. For the other recs there is NO `EstimatedSavingsAmount`, so
  `scan()` falls back to `WORKSPACE_BUNDLE_MONTHLY[compute_type] *
  pricing_multiplier * 0.30`. The human `EstimatedSavings` strings
  (`"$50/month potential per workspace"`, `"Full workspace monthly cost"`) are
  **display-only and can desync** from that counted number — check this explicitly.
- WorkSpaces consumes **neither Cost Optimization Hub nor Compute Optimizer**. It
  is not in `core/scan_orchestrator.py` `_HUB_SERVICES` / `type_map`, and pulls no
  CO helper. A "missing CoH/CO source" finding is NOT fair game — drop that axis.
- **Rendering:** `workspaces` is NOT in `_PHASE_A_SERVICES`, has NO
  `("workspaces", …)` entry in `PHASE_B_HANDLERS`, and is NOT in
  `_PHASE_B_SKIP_PER_REC`. Its single `enhanced_checks` source therefore renders
  through the **generic Phase B per-rec fallback** (the per-rec resource id comes
  from the generic extractor that reads `rec.get("WorkspaceId")`, see
  `reporter_phase_b.py` ~line 1852). Confirm this path actually renders the cards.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. The Live-Pricing table row for
    `workspaces.py` lists the method as
    `get_instance_monthly_price("AmazonWorkSpaces", ...)`. **Reconcile against
    reality:** the adapter does NOT call that method at all — it uses the
    hardcoded `WORKSPACE_BUNDLE_MONTHLY` table because (confirmed via the Pricing
    API) `AmazonWorkSpaces` has **no `instanceType` attribute** (its filterable
    dimensions are `bundle`, `runningMode`, `license`, `operatingSystem`,
    `pricingUnit`, …). The doc row is wrong; note it for the Phase-7 doc update.
0b. Confirm module identity in `services/adapters/workspaces.py`:
    `key="workspaces"`, `cli_aliases=("workspaces",)`,
    `display_name="WorkSpaces"`, `required_clients()` → `("workspaces",)`. Note
    the module constant `_AUTOSTOP_SAVINGS_FACTOR = 0.30` and that no
    `requires_cloudwatch` / `reads_fast_mode` is declared.
0c. WorkSpaces has **no AWS advisory source and no usage metric**. Focus on:
    whether config-only savings are defensible at all, the hardcoded-table
    accuracy + region scaling, the `$50` display string vs the `×0.30` counted
    number, the STOPPED/AutoStop "already cheap" coverage trap, and the
    bundle-rightsizing target logic.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/workspaces.py` (`WorkspacesModule.scan`,
   `_AUTOSTOP_SAVINGS_FACTOR`), `services/workspaces.py`
   (`get_enhanced_workspaces_checks`, `_get_bundle_price_by_compute_type`,
   `WORKSPACE_BUNDLE_MONTHLY`, `WORKSPACE_BUNDLE_MAP`, `WORKSPACE_BUNDLE_RANK`,
   `WORKSPACES_OPTIMIZATION_DESCRIPTIONS`), `core/contracts.py`
   (`ServiceFindings`, `SourceBlock`), `services/_base.py`, `core/scan_context.py`
   (`pricing_multiplier`), `core/scan_orchestrator.py`, `core/result_builder.py`,
   and the reporter (`reporter_phase_b.py` generic per-rec path incl. the
   `rec.get("WorkspaceId")` resource-id extractor ~line 1852;
   `html_report_generator.py`).
2. List **every** cost check and for each give: trigger condition, data source
   (`describe_workspaces` config only — there is no metric), the exact
   `EstimatedSavings` string, whether `EstimatedSavingsAmount` is set, and how
   `scan()` actually counts it. Map each to the single `enhanced_checks` source.
   The known inventory to confirm:
   - **`billing_mode_optimization`** — `State=="AVAILABLE"` AND
     `RunningMode=="ALWAYS_ON"`. String: `"$50/month potential per workspace"`
     (hardcoded `$50`, no `EstimatedSavingsAmount`). Counted by `scan()`:
     `WORKSPACE_BUNDLE_MONTHLY[ComputeType] * pricing_multiplier * 0.30`.
   - **`unused_workspaces`** — `State in {STOPPED, ERROR, SUSPENDED}`. String:
     `"Full workspace monthly cost"` (no `EstimatedSavingsAmount`). Counted by
     `scan()`: ALSO `bundle_monthly * pricing_multiplier * 0.30` (NOT full cost).
   - **`bundle_rightsizing`** — `State=="AVAILABLE"`, `WORKSPACE_BUNDLE_RANK`
     gate: `current_rank >= 4` → target `STANDARD` (only if `ctx.pricing_engine`
     truthy); `elif current_rank >= 3` → target `PERFORMANCE`. Sets
     `EstimatedSavingsAmount = max(current_price − target_price, 0)` (concrete
     delta, used directly by `scan()`). String: `"$<savings>/month"`.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate the bundle price table against the live Pricing API
   (`AmazonWorkSpaces`, region scaling). Confirmed facts to re-verify and build on:
   - **`get_instance_monthly_price("AmazonWorkSpaces", …)` is genuinely dead** —
     `AmazonWorkSpaces` has no `instanceType` attribute, and the `bundle`
     dimension commingles base-hardware rows (many `$0.00` for `WorkSpaces Core`
     "Basic utilities" bundles), software-license add-on rows ($15.70 Office
     2021, $48.51 Visual Studio, $21.43 Office Pro), and inconsistent bundle
     names (`Standard Plus`, `Power-0`, `GeneralPurpose.8xlarge Plus`). A naive
     `bundle`-filtered, first-row lookup would be non-deterministic and often
     `$0`. This justifies the hardcoded table — but the table must then be
     manually validated and maintained.
   - **Validate EACH `WORKSPACE_BUNDLE_MONTHLY` constant** (VALUE 25 / STANDARD 35
     / PERFORMANCE 60 / POWER 80 / POWERPRO 124 / GRAPHICS 350 / GRAPHICSPRO 735)
     against the live AlwaysOn, Windows, License-Included monthly bundle price.
     Known drift to confirm and quantify: live `Power` AlwaysOn ≈ `$78/mo`,
     `PowerPro` ≈ `$140/mo` (table 124 → understated ~11%), `GraphicsPro` ≈
     `$999/mo` (table 735 → understated ~26%). Treat each stale constant as a
     finding.
   - **AlwaysOn vs AutoStop is NOT a flat 30%.** AutoStop bills a small fixed
     monthly fee PLUS an hourly rate; the realized saving depends entirely on
     monthly login hours (the break-even is ~80–85 hrs/mo). `0.30` is a fabricated
     midpoint applied to a config dimension with **no usage metric** — the
     canonical "config dimension alone → fabricated $" issue. Pull the AutoStop
     hourly + monthly-fee numbers from the API and show how far `0.30` can be from
     reality for a heavy vs light user.
   - **OS / license correctness.** The table is Windows + License-Included. BYOL
     (`license` BYOL) and Linux/Ubuntu WorkSpaces bill differently; `compute_type`
     alone ignores `operatingSystem`, `license`, and the storage (root/user GB)
     variant. Flag wrong-OS/license pricing where the account has non-Windows or
     BYOL WorkSpaces.
   - **Region scaling.** The constants are us-east-1 list prices scaled by
     `ctx.pricing_multiplier`. Confirm the multiplier is applied exactly once
     (it is, in `_get_bundle_price_by_compute_type` and in the `scan()` fallback —
     check neither path double-applies) AND judge whether WorkSpaces regional
     prices are even a clean linear multiple of the generic multiplier.
4. Confirm each counted finding is defensible from the report alone. A `0.30`
   AutoStop saving with no login data, or a bundle price off by 26%, is a finding.
   Record a structured **AuditBasis** (bundle / region / list-price-source /
   AutoStop-vs-AlwaysOn assumption / formula) on each counted finding, as the
   Lambda/RDS audits did.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** a single `AVAILABLE` WorkSpace can match `State=="AVAILABLE"`
   gates for BOTH `billing_mode_optimization` (if `ALWAYS_ON`) AND
   `bundle_rightsizing` (if over-ranked). Determine whether both recs are emitted
   for the same WorkSpace and whether `scan()` counts both — switching to AutoStop
   AND downsizing the bundle discount the same monthly bill. Decide if they may
   legitimately stack (they partially can, but not additively at face value).
6. The `STOPPED/ERROR/SUSPENDED` and `ALWAYS_ON` gates are mutually exclusive by
   `State`, so confirm no instance lands in `unused_workspaces` AND
   `billing_mode_optimization` simultaneously.
7. **Cross-adapter:** WorkSpaces are not surfaced by any other adapter and no
   `_extract_*` helper in `html_report_generator.py` pulls them into a synthetic
   tab — confirm both, then drop this axis.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Confirm full pagination of `describe_workspaces`. Confirm the `State` gates
   cover the billable states correctly — note that a WorkSpace in `STOPPED` with
   `RunningMode=="AUTO_STOP"` already costs only the small monthly fee, so
   flagging it for "Full workspace monthly cost" (counted at 30% of the AlwaysOn
   bundle) is a false positive on an already-cheap resource (mirror the EKS
   0-node / scaled-to-zero fix). An ALWAYS_ON STOPPED workspace, by contrast,
   still bills near full — distinguish them.
9. **`bundle_rightsizing` coverage holes:** the rank logic only rightsizes
   `current_rank >= 3` (POWER and up); `VALUE`(0)/`STANDARD`(1)/`PERFORMANCE`(2)
   are never rightsized, and the `current_rank >= 4 → STANDARD` branch is gated on
   `ctx.pricing_engine` being truthy (so with no engine, GRAPHICS/GRAPHICSPRO are
   only ever offered `PERFORMANCE`, never `STANDARD`). Confirm each gate is
   intentional and that no valid downgrade is silently skipped. Also note the rec
   asserts "based on utilization profile" while reading **no utilization metric
   at all** — a heuristic assuming a usage target with no evidence.

### Phase 5 — Silent failures (nothing fails quietly)
10. Enumerate every swallow:
    - `services/workspaces.py:158` outer `except Exception as e: ctx.warn(...)` —
      a `describe_workspaces` permission gap is recorded via `ctx.warn` but NOT
      classified. Confirm `AccessDenied`/`UnauthorizedOperation`/`OptInRequired`
      are routed to `ctx.permission_issue` (mirror the EC2/RDS pattern), else the
      whole tab can vanish as a generic warning.
    - Confirm there is no inner bare `except`/`pass` hiding per-WorkSpace errors.
11. A bundle miss (`WORKSPACE_BUNDLE_MONTHLY.get(compute_type.upper(), 0.0) == 0`)
    is skipped for the dollar tally ("skip rather than fabricate $35") but the rec
    is still emitted by the shim — confirm such a rec renders with `$0`/no savings
    and is NOT counted, and decide whether a `$0` rec should be advisory or
    dropped (mirror the Lambda `Counted=False` advisory pattern).
12. **No CloudWatch, no fast-mode.** Because the adapter reads no metric and no
    `cloudwatch` client, there is no fast-mode gating to check — but that is
    itself the finding: every saving is config-derived with no usage evidence.
    Decide whether `billing_mode_optimization` / `unused_workspaces` should become
    CloudWatch-gated (WorkSpaces publishes `AWS/WorkSpaces` `UserConnected` /
    `ConnectionAttempt` / session metrics) or be demoted to `$0` advisory, the way
    file_systems gates its IA-lifecycle saving on real metrics.

### Phase 6 — Reporting (one tab, counted == rendered)
13. **Render wiring:** `workspaces` has no Phase A descriptor, no Phase B handler,
    and is not skip-per-rec, so its single `enhanced_checks` source renders via
    the generic per-rec fallback (resource id from `rec.get("WorkspaceId")`).
    Trace `html_report_generator._get_detailed_recommendations` /
    `_get_affected_resources_list` and confirm the cards actually render (a source
    with no handler in a *skip-per-rec* service would render nothing — verify
    `workspaces` is genuinely on the per-rec path and does NOT silently drop).
14. **Counted == rendered:** confirm `total_recommendations == len(recs)` and
    `total_monthly_savings` equals the sum of the per-rec counted dollars.
    Critically, the rendered card for `billing_mode_optimization` shows
    `"$50/month potential per workspace"` while the headline counts
    `bundle_monthly × multiplier × 0.30` (e.g. STANDARD ≈ `$10.50`) — a direct
    display-vs-counted desync, and the `$50` is fabricated (it appears in no price
    table). `unused_workspaces` shows "Full workspace monthly cost" but counts
    30%. Quantify both desyncs and reconcile the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings`) against the
    per-service WorkSpaces total.
15. Confirm no finding is counted but dropped from the table (or vice-versa)
    across all three `CheckCategory` groups.

### Phase 7 — Tooling & evidence
16. Run a real scan scoped to WorkSpaces:
    `python3 cli.py <region> --scan-only workspaces`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service workspaces`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), display-vs-counted desync, and STOPPED-AutoStop false
    positive. Reconcile the headline against the per-source sum. Caveats:
    WorkSpaces is regional and often absent — try a region that actually has
    directories/WorkSpaces; exercise AVAILABLE+ALWAYS_ON, STOPPED, and an
    over-ranked bundle. Use `.venv/bin/python` (3.14).
17. For any accuracy claim, show the AWS Pricing API value next to the scanner's
    constant — e.g. the live AlwaysOn `PowerPro`/`GraphicsPro` monthly vs the
    table's `124`/`735`, and the AutoStop hourly + monthly-fee numbers vs the flat
    `0.30`. For the `$50` string, show that it appears in no price table.

### Deliverable
- The complete check list (Phase 1.2), with the counted basis and the display
  string marked where they diverge.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add a `tests/test_workspaces_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `WorkspacesModule.scan` with a `SimpleNamespace` ctx + a fake `workspaces`
  client/paginator returning controlled `State` / `RunningMode` /
  `WorkspaceProperties`; assert the `$50`-string-vs-counted reconciliation, the
  AutoStop-factor demotion to advisory or metric-gating, the
  STOPPED-AutoStop false-positive fix, the bundle-table accuracy + single region
  scaling, the bundle-rightsizing coverage/target logic, the
  `permission_issue` classification, and counted == rendered.
- For the AutoStop / unused-state savings that assume a usage profile with no
  metric, either CloudWatch-gate them (`UserConnected` / session metrics) or make
  them `$0` advisory (`Counted=False`) — never fabricate a `$`.
- Record a structured **AuditBasis** (bundle / region / list-price-source /
  AutoStop assumption / formula) on each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for WorkSpaces first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same display-desync / config-only-fabrication / stale-table bug
  in a sibling adapter out of scope, note it as a follow-up (don't fix unprompted).
- Update the `workspaces` row in `services/adapters/CLAUDE.md` (the
  `get_instance_monthly_price` claim is wrong — it uses `WORKSPACE_BUNDLE_MONTHLY`).
- Stage ONLY the files you changed when committing.

### Known-issue catalogue to check against

**Universal (from prior audits — verify each is absent or fix it):**
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

**WorkSpaces-specific (found while grounding this prompt — confirm each):**
- **`$50` display string vs `×0.30` counted number.** `billing_mode_optimization`
  renders `"$50/month potential per workspace"` (a fabricated flat figure in no
  price table) while the headline counts
  `WORKSPACE_BUNDLE_MONTHLY[ComputeType] × pricing_multiplier × 0.30` (e.g.
  STANDARD ≈ `$10.50`). Reconcile the card and the count.
- **AutoStop saving from config alone.** `_AUTOSTOP_SAVINGS_FACTOR = 0.30` is
  applied to every `ALWAYS_ON` WorkSpace with NO login/usage metric; AutoStop is
  hourly + monthly-fee, so the real saving swings widely with hours used. Demote
  to advisory or CloudWatch-gate (`UserConnected`/session metrics).
- **STOPPED-AutoStop false positive.** `unused_workspaces` flags
  `STOPPED/ERROR/SUSPENDED` as "Full workspace monthly cost" and counts 30% of the
  AlwaysOn bundle — but a STOPPED **AutoStop** WorkSpace already bills only the
  small monthly fee. Distinguish AlwaysOn-stopped (bills near full) from
  AutoStop-stopped (already cheap).
- **Stale / wrong bundle table.** `WORKSPACE_BUNDLE_MONTHLY` is a us-east-1
  Windows+Included list table; live `PowerPro` ≈ `$140` (table 124) and
  `GraphicsPro` ≈ `$999` (table 735) — understated ~11% / ~26%. Re-validate every
  constant.
- **OS / license blindness.** The table assumes Windows + License-Included;
  `compute_type` ignores `operatingSystem` / `license` (BYOL, Linux/Ubuntu price
  differently) and the storage (root/user GB) variant.
- **`get_instance_monthly_price("AmazonWorkSpaces", …)` is dead, yet the
  `services/adapters/CLAUDE.md` row still lists it as the pricing method.**
  Reconcile the doc (the API has no `instanceType` attribute; `bundle` rows mix
  `$0` base + software add-ons).
- **`bundle_rightsizing` target logic gaps.** Only `rank >= 3` is rightsized, the
  `rank >= 4 → STANDARD` branch is gated on `ctx.pricing_engine` truthiness, and
  the rec claims a "utilization profile" while reading no metric.

## PROMPT (end)
