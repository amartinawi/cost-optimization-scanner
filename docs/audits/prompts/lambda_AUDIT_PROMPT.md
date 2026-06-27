# Lambda Adapter Cost-Audit Prompt

A deep, Lambda-specific audit brief in the same structure as the Network /
RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

Lambda was **recently audited** (fixes C1–C8, see
`tests/test_lambda_audit_fixes.py` and `tests/test_lambda_dedupe_and_eks_zero.py`).
This brief is pre-grounded in the *fixed* code path so a re-auditor starts from
facts and focuses on the **remaining gaps**, not a blind re-discovery of already
closed issues. Scope is **strictly cost**: every emitted recommendation must
produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`lambda`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **EC2** adapter
(`services/adapters/ec2.py`) as the canonical model for cross-source dedup and
the `$0`-placeholder→warning pattern. Lambda is itself the canonical reference
the other audits cite for three patterns — re-verify each is still intact here:
(1) the **metric-gated `$0` advisory** (`mark_zero_savings_advisory`,
`Counted=False`), (2) the **CoH > CO > enhanced** authority dedup keyed on a
**normalized** function name (qualified ARNs stripped), and (3) the
**architecture-aware module constant** (x86 vs arm64 Provisioned Concurrency
rate, region-scaled exactly once).

### NOTE on structure (Lambda has a naming quirk and renders via Phase A)
- The adapter file is `services/adapters/lambda_svc.py` — the `_svc` suffix
  exists because `lambda` is a Python reserved word (you cannot name a module
  `lambda.py` and `import services.adapters.lambda`). The legacy shim is
  `services/lambda_svc.py` (function `get_enhanced_lambda_checks`). Despite the
  file name, the module `key`, `cli_aliases`, and `display_name` are all plain
  **`lambda`** / `("lambda",)` / `"Lambda"`.
- The adapter is a **multi-source aggregator** of THREE sources, all rendered
  under one `lambda` tab:
  - **Cost Optimization Hub** — consumed via `ctx.cost_hub_splits["lambda"]`
    (the orchestrator buckets `currentResourceType == "LambdaFunction"` →
    `lambda` in `core/scan_orchestrator.py` `type_map`, line ~88). Already
    region-priced upstream.
  - **Compute Optimizer** — pulled inline via
    `services.advisor.get_lambda_compute_optimizer_recommendations` (memorySize
    rightsizing). Already region-priced inside `_normalize_lambda_co_rec`
    (`services/advisor.py` ~375) — do NOT re-multiply by `pricing_multiplier`.
  - **enhanced_checks** — local heuristics from the shim
    (`get_enhanced_lambda_checks`): Excessive Memory, Provisioned Concurrency,
    ARM Migration. (Low-invocation / VPC / reserved-concurrency checks were
    deliberately removed — Lambda has no idle cost, so "delete low-usage
    function" saves ~$0; reserved concurrency itself is free.)
- **Pricing is a module constant, not a PricingEngine method.** Provisioned
  Concurrency bills 24/7 for allocated GB-seconds at a dedicated PC rate,
  architecture-specific:
  `_LAMBDA_PC_PRICE_PER_GB_SEC = 0.0000041667` (x86, SKU BMKCD2ZCEYKTYYCB),
  `_LAMBDA_PC_PRICE_PER_GB_SEC_ARM = 0.0000033334` (arm64, SKU MV7PTBS82AVCMXJJ),
  with `_HOURS_PER_MONTH = 730` and `_SECONDS_PER_HOUR = 3600`. The PC saving is
  `mem_gb × pc_rate × 730 × 3600 × pc_count × (1 − max_util) × pricing_multiplier`
  — region-scaled exactly **once** at the emit site. Excessive-Memory and
  ARM-Migration savings are NOT priced — they are forced to `$0` advisory
  because the shim collects no per-invocation Duration / GB-seconds.
- **Lambda renders via Phase A, NOT Phase B.** `lambda` is in
  `reporter_phase_b._PHASE_A_SERVICES` AND in
  `reporter_phase_a.PHASE_A_DESCRIPTORS` (`savings_mode: "always"`). In
  `html_report_generator._get_detailed_recommendations` the dispatch
  short-circuits to `render_grouped_by_category` (line ~3312) BEFORE the Phase B
  source-loop ever runs. Consequently the registered Phase B handler
  `("lambda", "compute_optimizer") → _render_compute_optimizer_source`
  (`reporter_phase_b.py` ~2441) is **dead code** — it is never reached. Confirm
  this and decide whether the dead binding should be removed or whether CO recs
  are losing the unified CO renderer's typesetting by going through Phase A.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `lambda_svc.py` row (under
    Live Pricing). Reconcile the doc against reality: it claims PC is priced
    from a module constant, arch-aware, CloudWatch-gated on
    `ProvisionedConcurrencyUtilization`, region-scaled once, with a structured
    `AuditBasis`. Confirm each clause against the code; note any drift.
0b. Confirm module identity in `services/adapters/lambda_svc.py`:
    `key="lambda"`, `cli_aliases=("lambda",)`, `display_name="Lambda"`,
    `required_clients() == ("lambda", "compute-optimizer", "cloudwatch")`,
    `requires_cloudwatch=True`, `reads_fast_mode=True`. Verify the flags are
    honest: the shim DOES read CloudWatch (`Invocations` for ARM,
    `ProvisionedConcurrencyUtilization` for PC) and DOES short-circuit those
    reads under `ctx.fast_mode`.
0c. Read the two existing audit test files
    (`tests/test_lambda_audit_fixes.py`, `tests/test_lambda_dedupe_and_eks_zero.py`)
    so you do not re-flag a closed issue. Your job is to find what those tests
    do NOT cover.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the Lambda path: `services/adapters/lambda_svc.py`
   (adapter + `_normalize_lambda_fn_name`), `services/lambda_svc.py` (shim:
   `get_enhanced_lambda_checks`, `_read_pc_max_utilization`, the constants
   `EXCESSIVE_MEMORY_THRESHOLD=3008`, `ARM_MIN_WEEKLY_INVOCATIONS=10`,
   `ARM_SUPPORTED_RUNTIMES`, `PC_UTIL_LOOKBACK_DAYS=14`), the helpers it imports
   (`services/_savings.py` — `mark_zero_savings_advisory`, `parse_dollar_savings`,
   `compute_optimizer_savings`), `services/advisor.py`
   (`get_lambda_compute_optimizer_recommendations`, `_normalize_lambda_co_rec`,
   `_compute_optimizer_opt_in_rec`), `core/contracts.py`,
   `core/scan_orchestrator.py` (the `type_map` / `_HUB_SERVICES`),
   `core/result_builder.py`, and the reporter
   (`reporter_phase_a.py` `PHASE_A_DESCRIPTORS["lambda"]` +
   `_extract_lambda_details`, `html_report_generator.py` dispatch ~3312).
2. List **every** cost check, and for each give: trigger condition, data source
   (CoH / CO / CloudWatch metric / config heuristic), the exact savings formula
   or `EstimatedSavings` string, the constant it embeds, and whether it parses
   to a **counted** dollar or a **`$0` advisory**. The known inventory to
   confirm:
   - **Excessive Memory** — `MemorySize >= 3008 MB`; emits string
     `"30-50% with rightsizing"`; the adapter forces `EstimatedMonthlySavings=0`
     + `PricingWarning` (no Duration metric) → **$0 advisory**.
   - **Provisioned Concurrency** — function has a PC config; saving = unused
     fraction `(1 − max_util)` of the 24/7 GB-second allocation at the
     arch-specific PC rate, region-scaled once; **counted** when
     `ProvisionedConcurrencyUtilization` has datapoints, else **$0 advisory**.
   - **ARM Migration** — `x86_64` runtime in `ARM_SUPPORTED_RUNTIMES` with
     `>10` weekly invocations; emits `"20% cost reduction"`; adapter forces
     `$0` → **$0 advisory**.
   - **Cost Optimization Hub** recs (counted, region-priced upstream).
   - **Compute Optimizer** memorySize rightsizing (counted, region-priced).

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive it from the live AWS Pricing API.
   - **Provisioned Concurrency rates:** validate `0.0000041667/GB-s` (x86) and
     `0.0000033334/GB-s` (arm64) against `AWSLambda` Pricing API for the scan
     region. Confirm the PC duration SKU (NOT the on-demand x86 duration
     `0.0000166667/GB-s` nor on-demand arm `0.0000133334/GB-s`). Confirm arm64
     PC is ~20% cheaper and that the architecture selects the rate.
   - **Free tier:** confirm the saving does NOT include the always-free 1M
     requests + 400k GB-s — PC is dedicated allocation that bills regardless of
     the free tier, so the PC math is fair, but verify Excessive-Memory / ARM
     never quietly recommend a saving against free-tier-covered usage.
   - **Region scaling:** the PC constant is us-east-1 and is scaled by
     `ctx.pricing_multiplier` exactly once at the emit site
     (line ~178). Confirm CoH and CO savings are NOT re-multiplied
     (`_normalize_lambda_co_rec` deliberately ignores the multiplier; the
     adapter comment at line ~213 says CO is already region-priced).
   - **Metric window:** PC utilization is the **max** over
     `PC_UTIL_LOOKBACK_DAYS=14` days, daily granularity. Confirm `1 − max_util`
     (peak, not average) is used so you never deprovision below observed peak.
4. Confirm the savings basis is defensible from the report alone. Each counted
   PC finding carries a structured `AuditBasis` (rate / architecture /
   region_multiplier / metric / max_utilization / provisioned_concurrency /
   memory_gb / formula). Confirm Excessive-Memory and ARM advisories carry a
   `PricingWarning` explaining WHY they are $0 (no Duration/GB-seconds).

### Phase 3 — Duplication (no dollar counted twice)
5. **Cross-source authority dedup (the Lambda signature pattern):** confirm a
   function appearing in BOTH Cost Hub and Compute Optimizer, or in CoH/CO and
   enhanced_checks, is counted **once**. The adapter normalizes every id via
   `_normalize_lambda_fn_name` (strips a full ARN, a qualified
   `:version`/`:alias` suffix, and a bare `name:alias`) so a CoH
   `...:function:NAME:PROD` matches CO's bare `NAME`. Authority order is
   **CoH > CO > enhanced** (`seen_functions` seeded from CoH, then CO, then
   enhanced filtered). Prove it: feed a qualified CoH ARN + a bare-name CO rec
   and confirm only the CoH dollar is counted (mirrors
   `test_qualified_coh_arn_dedupes_against_co`).
6. **Intra-adapter stacking:** can one function match Excessive Memory AND ARM
   Migration AND Provisioned Concurrency and stack? (Both Memory and ARM are
   already $0 advisory, so they cannot inflate the counted total — confirm this
   still holds. PC is the only counted enhanced lever; confirm it is not also
   surfaced by CO for the same function as a memory-rightsizing dollar.)
7. **Cross-adapter / lever separation:** Lambda memory-rightsizing overlaps the
   **Compute Savings Plan** lever in the `commitment_analysis` adapter. Confirm
   the Lambda tab counts a per-function rightsizing delta while commitment counts
   a discount on the (rightsized) baseline, and that `core/result_builder.py`
   does not blind-sum the two into the headline. Confirm no `_extract_*` helper
   in `html_report_generator.py` pulls Lambda functions into a synthetic tab.

### Phase 4 — Coverage (works for ALL functions, not a subset)
8. Confirm full pagination of `list_functions` (the shim uses a paginator) and
   that `list_provisioned_concurrency_configs` is read per function. Confirm a
   PC-config read failure does NOT skip the ARM check (it is caught and treated
   as "no PC" — see `test_pc_config_error_does_not_skip_arm_check`).
9. Are whole classes silently excluded?
   - **Zip vs Image package type** — does any check assume Zip-only?
   - **x86 vs arm64** — Excessive-Memory/ARM only fire for x86; PC prices both.
   - **`$LATEST` vs published versions / aliases** — PC is configured per
     version/alias, but `ProvisionedConcurrencyUtilization` is read dimensioned
     by `FunctionName` only. A PC config on an alias/version may return **no
     datapoints** for the bare `FunctionName` dimension → the saving silently
     degrades to a `$0` advisory even though the allocation is real. Flag this
     as a coverage gap (the metric may need the `Resource`/alias dimension).
   - **ARM allowlist** — `ARM_SUPPORTED_RUNTIMES` is a hardcoded runtime list.
     Confirm it is current (e.g. `python3.13`, `nodejs22.x`, container images)
     and that an unlisted-but-ARM-capable runtime is not permanently skipped.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except`, `logger`-only, and `return []` fallback.
    - The shim records CloudWatch failures via `_note_cw_failure`
      (AccessDenied/Unauthorized → `ctx.permission_issue`
      `action="cloudwatch:GetMetricStatistics"`, other → `ctx.warn`) and config
      failures via `_note_config_failure`, each **once** per scan. Confirm none
      swallow silently.
    - `get_lambda_compute_optimizer_recommendations` (`services/advisor.py`)
      classifies `OptInRequiredException` → opt-in placeholder, `AccessDenied`
      → `ctx.permission_issue`, other → `ctx.warn`. Confirm the
      `logger.warning(...)` is ALWAYS paired with a `ctx` record (it is) — a
      logger-only path would be a finding.
11. Does a pricing/metric miss emit a `$0` **counted** finding? It must be
    advisory or dropped. Verify `mark_zero_savings_advisory` flags every
    Excessive-Memory / ARM / no-metric-PC rec `Counted=False`.
12. **CO opt-in placeholder:** confirm the `$0`
    `ResourceId="compute-optimizer-service"` rec is converted to `ctx.warn` and
    **dropped** from `co_recs` (adapter line ~96), never counted (mirrors
    `test_co_opt_in_placeholder_becomes_warning`).
13. **Cost Hub bucket health:** confirm `LambdaFunction` maps to the `lambda`
    bucket in `scan_orchestrator` `type_map` and that the bucket IS consumed
    (it is — `ctx.cost_hub_splits.get("lambda", [])`). A `currentResourceType`
    with no bucket warns only on a full scan; a bucket with no consumer drops
    silently. Confirm neither applies to Lambda.
14. **fast_mode:** confirm the shim skips BOTH CloudWatch reads under
    `ctx.fast_mode`, warns once, and that PC then degrades to `$0` advisory
    (no `MaxUtilization`) rather than fabricating a flat haircut (mirrors
    `test_fast_mode_skips_cloudwatch_and_warns_once`).

### Phase 6 — Reporting (one tab, counted == rendered)
15. **Phase A render path (verify carefully):** because `lambda` is in
    `PHASE_A_DESCRIPTORS`, all three sources render through
    `render_grouped_by_category` with `savings_mode="always"`, and the Phase B
    `("lambda","compute_optimizer")` handler is never reached. Confirm: (a) CoH,
    CO, and enhanced recs all surface in the single Lambda tab via
    `_extract_lambda_details`; (b) advisory ($0) Memory/ARM/PC recs are rendered
    but excluded from the counted total; (c) the dead Phase B binding is either
    removed or documented as intentional legacy.
16. **Counted == rendered:** `total_recommendations = len(cost_hub) + len(co) +
    len(enhanced)` (includes advisory) but `total_monthly_savings = hub_savings
    + formula_savings + co_savings` sums only counted PC + CoH + CO. Confirm the
    per-tab headline shows the counted/advisory split and reconciles against the
    executive-summary headline (`_calculate_service_savings`).

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to Lambda:
    `python3 cli.py <region> --scan-only lambda`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service lambda`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and function appearing in >1 source. Caveats: try a
    second region if sparse; exercise an account WITH provisioned concurrency
    (the only counted enhanced lever); exercise CO opted-in vs not. Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
18. For any duplication claim, prove it: show the same normalized function name
    in two sources. For any accuracy claim, show the AWS Pricing API PC rate
    (x86 / arm64) next to the module constant.

### Deliverable
- The complete check list (Phase 1.2), counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**
  (e.g. Memory/ARM permanently $0 advisory is a deliberate honesty choice, not a
  bug). End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Extend `tests/test_lambda_audit_fixes.py` (mirror its SimpleNamespace-ctx +
  fake-boto3 style) to cover any new fix: the alias/version PC-metric dimension
  gap, the ARM runtime allowlist, the dead Phase B handler, the
  Lambda↔Compute-Savings-Plan lever separation.
- Any GB-second saving must be backed by a real Duration / GB-seconds metric or
  stay a `$0` advisory — respect `ctx.fast_mode` and never fabricate a `$`.
- Record a structured **AuditBasis** (rate / arch / metric-window / formula) on
  each counted finding; counted == rendered.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Refresh reporter snapshots (`SNAPSHOT_UPDATE=1`) ONLY for an intentional
  rendering change, and say so.
- Update the `lambda_svc.py` row in `services/adapters/CLAUDE.md` to match
  reality. Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- GB-second/usage savings from a config dimension alone (memory/vCPU) with NO
  Duration/usage metric → fabricated $.
- Wrong architecture (arm64 priced as x86) / wrong OS (Windows Fargate) pricing.
- Non-deterministic pricing filter (multiple SKUs, MaxResults=1).
- Region: hardcoded constant/fallback not region-scaled via pricing_multiplier,
  OR double-applied on an already-region-correct engine/CO path.
- Per-unit RATE string counted as a monthly total — rejected by
  parse_dollar_savings → $0 advisory.
- Free-tier (Lambda 1M req + 400k GB-s) recommended for a saving it cannot
  realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic by NORMALIZED id (strip ARN, mind version/alias).
- Two heuristic checks stacking on one resource (Excessive Memory + ARM), or
  SUBSET redundancy — fix by removal.
- Reduction factor instead of exact price delta.
- $0 "enable Compute Optimizer" opt-in placeholder
  (ResourceId=compute-optimizer-service) counted instead of converted to
  ctx.warn and dropped.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (Counted=False).
- Cost Hub: (a) currentResourceType with no type_map bucket → dropped (warns
  only on full scan); (b) bucket populated but consumed by NO adapter → dropped
  silently.
- A source emitted with no PHASE_B handler / Phase A short-circuiting a
  registered Phase B handler → renders nothing or via wrong path.
- Render-time Optimized/category filter desyncing headline from cards (filter at
  SOURCE).
- Coverage gated to a hardcoded runtime/arch/package-type allowlist, or
  $LATEST-only.
- CloudWatch/CO/CoH permission/throttle failure logged via logger only, not
  ctx.warn/ctx.permission_issue (AccessDenied/Unauthorized/OptInRequired →
  permission_issue).
- CloudWatch reads not gated on ctx.fast_mode (reads_fast_mode not declared).
- Heuristic assuming usage with no evidence.
- Cross-adapter overlap (Lambda rightsizing vs Compute Savings Plan lever; ECS
  in Containers vs commitment).
- Each counted finding must carry a structured AuditBasis
  (rate/arch/metric-window/formula); counted == rendered.

#### Lambda-specific items (verify each)
- **PC metric dimension vs alias/version:** `_read_pc_max_utilization` reads
  `ProvisionedConcurrencyUtilization` dimensioned by `FunctionName` only. PC is
  configured per **version/alias**; the bare-name dimension may return no
  datapoints, silently degrading a real, billing 24/7 allocation to a `$0`
  advisory. Confirm whether the alias/version `Resource` dimension is needed.
- **ARM runtime allowlist drift:** `ARM_SUPPORTED_RUNTIMES` is a hardcoded
  tuple. A newer ARM-capable runtime (e.g. `python3.13`, `nodejs22.x`) or a
  container-image function is permanently skipped. (ARM is $0 advisory anyway,
  so impact is "missed nudge," not "wrong $" — classify accordingly.)
- **Excessive-Memory / ARM are structurally $0:** the shim never reads
  `Duration`, only `Invocations`, so GB-seconds cannot be derived. Decide
  whether to wire `Duration` (turning these into counted savings) or keep them
  honest advisories — do NOT let either become a counted config-only number.
- **Dead Phase B handler:** `("lambda","compute_optimizer")` in
  `PHASE_B_HANDLERS` is unreachable because `lambda` renders via Phase A. Remove
  it or document it as in-flight-JSON legacy.
- **CO `resource_name` vs enhanced `FunctionName` keying:** the dedup keys CoH on
  `resourceArn`/`resourceId`/`FunctionName`, CO on `resource_name`/`FunctionName`,
  enhanced on `FunctionName`. Confirm `_normalize_lambda_co_rec` always populates
  `resource_name` (it derives `fn_name` from `functionArn`) so the CO leg of the
  dedup never misses.
- **`MemorySize` default in the PC formula:** `rec.get("MemorySize", 256)` — if a
  PC rec ever lacks `MemorySize`, the saving is computed at 256 MB. Confirm the
  shim always attaches the real `MemorySize` to PC recs (it does, from the
  function config) so the default never silently understates.

## PROMPT (end)
