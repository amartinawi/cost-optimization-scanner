# SageMaker Adapter Cost-Audit Prompt

A deep, SageMaker-specific audit brief in the same structure as the Network /
Lambda / RDS audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* SageMaker code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`sagemaker`** adapter (`SageMakerModule`,
`services/adapters/sagemaker.py`) of this AWS cost-optimization scanner. Scope is
strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. Treat
the **RDS** (`services/adapters/rds.py`) and **EC2** (`services/adapters/ec2.py`)
adapters as the canonical model for instance-monthly pricing patterns (SKU
pinning, region-correct PricingEngine values used without re-multiplying), and
the recently-audited **Lambda** adapter (`services/adapters/lambda_svc.py`) as
the worked example for the `mark_zero_savings_advisory` / `Counted=False`
pattern, the `AuditBasis` record, and the test style I expect
(`tests/test_lambda_audit_fixes.py`).

### NOTE on structure (SageMaker is single-file, instance-priced, NO advisory gating)
- The whole adapter is `services/adapters/sagemaker.py` (`SageMakerModule`) —
  there is **no `services/sagemaker.py` shim and no `sagemaker_logic.py`**; every
  check is an inline module-level `_check_*` function. Treat each as a mini-check.
- Module identity: `key="sagemaker"`, `cli_aliases=("sagemaker",)`,
  `display_name="SageMaker"`, `required_clients()=("sagemaker","cloudwatch")`,
  `requires_cloudwatch=True`, `reads_fast_mode=True`,
  `grouping=GroupingSpec(by="check_category")`, plus `stat_cards`.
- It emits **four SourceBlocks**: `idle_endpoints`, `idle_notebooks`,
  `spot_training`, `multi_model_consolidation`. Recs use a **lowercase
  snake_case schema** (`endpoint_name`/`notebook_name`/`job_name`,
  `instance_type`, `check_category`, `current_value`, `recommended_value`,
  `monthly_savings`, `reason`) — DIFFERENT from the EC2/EBS PascalCase rec shape.
  There is **no `EstimatedSavings` string** and **no `Counted` flag** anywhere —
  remember this for Phase 5/6.
- **Pricing is instance-monthly only**: every saving derives from
  `_get_instance_monthly(ctx, instance_type)` →
  `ctx.pricing_engine.get_sagemaker_instance_monthly(instance_type)`
  (`core/pricing_engine.py` ~line 890). That method queries the
  **`AmazonSageMaker`** service code (so the SageMaker-over-EC2 managed-ML premium
  is already baked in), and on API miss falls back to
  `get_ec2_hourly_price(instance_type) × SAGEMAKER_OVER_EC2 × 730`, else `0.0`.
  The adapter comment is explicit that `ctx.pricing_multiplier` must **NOT** be
  re-applied (the values are region-correct); confirm no check multiplies twice.
- **No Cost Optimization Hub, no Compute Optimizer.** SageMaker is **not** in
  `scan_orchestrator._prefetch_advisor_data`'s `type_map` for any instance
  bucket. The ONE SageMaker-related CoH type, `SageMakerSavingsPlans`, is mapped
  to **`commitment_analysis`** (orchestrator line ~112), NOT to this adapter — so
  a "missing CoH source" finding is NOT fair game here, and SageMaker Savings
  Plans recommendations are the commitment tab's concern, not this adapter's.
- **No `mark_zero_savings_advisory` call.** `total_monthly_savings =
  sum(r["monthly_savings"] for r in all_recs)` with **no `Counted` filter** — so
  any rec whose `monthly_savings` resolved to `0.0` (pricing miss, unknown
  instance type) is still counted in both the rec count and the dollar total.
  This is the headline risk class for this adapter.
- **Phase A vs B:** `sagemaker` is **neither** in `PHASE_A_DESCRIPTORS` **nor** in
  `_PHASE_B_SKIP_PER_REC` **nor** in `PHASE_B_HANDLERS`. It therefore renders via
  the generic fallback `render_generic_per_rec → _render_generic_other_rec`
  (`reporter_phase_b.py`). There is **no synthetic tab** (no `_extract_*` helper
  pulls SageMaker resources). `html_report_generator.py` (~line 163) only defines
  the `sagemaker` stat-card descriptor (`active_endpoint_count`,
  `idle_endpoint_count`, `running_notebook_count`).

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. SageMaker is **not** listed in the Live
    Pricing / Parse-rate / Flat-rate tables — confirm whether it should be (it
    uses live `get_sagemaker_instance_monthly`) and add the missing row as part
    of the deliverable. Reconcile the doc against reality.
0b. Confirm module identity in `services/adapters/sagemaker.py` (key, aliases,
    display_name, `required_clients`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`). Confirm the four source names match the four
    `_check_*` functions and the four `optimization_descriptions` keys.
0c. SageMaker has **no AWS advisory source** (no CoH/CO consumed by this adapter).
    Savings are expected to be locally derived from instance pricing. Focus on:
    the missing `$0`-advisory gate, the no-usage-evidence notebook check, the
    one-time-vs-monthly spot saving, the idle/consolidation double-count, pricing
    SKU correctness, and the generic-renderer key-schema mismatch.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/sagemaker.py` end to end: the constants
   (`SPOT_SAVINGS_RATE=0.70`, `HOURS_PER_MONTH=730`, `IDLE_ENDPOINT_DAYS=7`,
   `MIN_TRAINING_SECONDS=3600`, `CONSOLIDATION_SAVINGS_RATE=0.30`,
   `_CW_PERIOD_1D=86400`); helpers `_get_instance_monthly`,
   `_get_cloudwatch_invocations_sum`, `_list_endpoints`, `_describe_endpoint`,
   `_describe_endpoint_config`; the four checks `_check_idle_endpoints`,
   `_check_idle_notebooks`, `_check_spot_training`,
   `_check_multi_model_consolidation`; and `SageMakerModule.scan`. Then read
   `core/pricing_engine.py:get_sagemaker_instance_monthly` (+ `_fetch_sagemaker_instance_price`,
   `SAGEMAKER_OVER_EC2`), `core/contracts.py`, `core/scan_orchestrator.py`
   (confirm no SageMaker instance bucket; `SageMakerSavingsPlans→commitment_analysis`),
   `core/result_builder.py`, `services/_savings.py`
   (`mark_zero_savings_advisory`, `parse_dollar_savings` — note neither is
   currently imported by this adapter), and the reporter generic path
   (`reporter_phase_b.py:render_generic_per_rec`, `_render_generic_other_rec`,
   `_PHASE_B_SKIP_PER_REC`, `PHASE_B_HANDLERS`; `html_report_generator.py`
   `_get_detailed_recommendations` ~line 3315, the `sagemaker` descriptor ~163).
2. List **every** cost check, and for each give: trigger condition, the data
   source (SageMaker describe/list API, CloudWatch metric, or pure config
   heuristic), the savings formula and constant, and whether the resulting
   `monthly_savings` is a real account-specific saving or could resolve to `$0`
   yet still be counted. The known inventory to confirm:
   - **`idle_endpoints`** — `list_endpoints`, status `InService`; under
     `fast_mode` the check `continue`s after counting active (no findings);
     CloudWatch `AWS/SageMaker Invocations` Sum over `IDLE_ENDPOINT_DAYS=7`,
     `Period=86400`. Skips if `invocations is None or > 0` (so missing metric →
     no finding). Saving = **full** `instance_monthly` of variant[0].
   - **`idle_notebooks`** — `list_notebook_instances(StatusEquals="InService")`,
     paginated. **No usage metric, no fast_mode guard.** Flags EVERY running
     notebook and recommends stopping it for the **full** `instance_monthly`.
   - **`spot_training`** — `list_training_jobs(StatusEquals="Completed",
     MaxResults=100)`; per job with `TrainingTimeInSeconds >= 3600` and
     `EnableManagedSpotTraining == False`; saving =
     `(instance_monthly/730) × training_hours × 0.70` (a per-historical-run
     figure) with a `< 0.50` floor; skip if `instance_type=="unknown"` or
     `instance_monthly <= 0`.
   - **`multi_model_consolidation`** — groups `InService` endpoints by variant[0]
     `instance_type`; for groups of size `> 1`, saving =
     `(len-1) × instance_monthly × 0.30`.
   Map each check to its emitting SourceBlock and note which keys the rec carries.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** savings figure, re-derive it from the live AWS Pricing
   API and confirm it matches. Validate each pricing path separately:
   - **SageMaker instance rates** — confirm `get_sagemaker_instance_monthly`
     returns the right per-instance-type, per-region rate against the
     `AmazonSageMaker` service code. Validate that **notebook**, **endpoint
     (real-time/hosting)**, and **training** instances of the same `ml.*` type
     are priced from the **correct SageMaker usage-type family** — AWS prices
     `ml.m5.xlarge` differently for `Hosting` vs `Notebook` vs `Training` vs
     `Processing`. The adapter uses ONE method for all three; confirm whether
     that conflates distinct SKUs (e.g. a notebook priced at the hosting rate).
     Check `_fetch_sagemaker_instance_price` for a deterministic filter (single
     SKU, not `MaxResults=1` over several usage types).
   - **Fallback path** — `get_ec2_hourly_price × SAGEMAKER_OVER_EC2 × 730`.
     Confirm `SAGEMAKER_OVER_EC2` is a defensible premium and that the fallback
     EC2 price is region-correct; an `ml.*` type has no EC2 equivalent, so
     confirm the fallback strips the `ml.` prefix or otherwise resolves a real
     EC2 type (else it silently returns 0.0 → a $0 counted finding).
   - **Region scaling** — PricingEngine is region-correct; confirm **no** check
     re-applies `ctx.pricing_multiplier` (the `_ = pricing_multiplier` lines are
     intentional no-ops; verify none was missed and none double-applies).
   - **Spot saving** — `SPOT_SAVINGS_RATE=0.70` is the saving fraction on a
     **single completed run**, labelled `monthly_savings`. Validate the Managed
     Spot Training discount (typically up to ~70%, but variable) and, more
     importantly, flag that a one-time retrospective run cost is **not** a
     recurring monthly saving — the report headline sums it as monthly. Decide
     whether it should be advisory or annualised/labelled.
   - **Consolidation factor** — `CONSOLIDATION_SAVINGS_RATE=0.30` and the
     `(len-1)×rate` shape assume every duplicate endpoint can fold onto a
     multi-model endpoint with zero capacity added. Confirm the factor is
     calibrated/labelled, not arbitrary, and that it isn't a reduction factor
     standing in for a real price delta.
4. Confirm the savings basis is defensible from the report alone: record a
   structured **AuditBasis** (rate / region / metric-window / formula) on each
   counted finding, as the Lambda/RDS audits did. An idle-endpoint saving driven
   by the 7-day Invocations window, or a spot saving driven by one run's
   `TrainingTimeInSeconds`, must make that window/measure legible.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter (the big one):** an `InService` endpoint with zero
   invocations can be counted by **`idle_endpoints`** (full `instance_monthly`)
   AND, if another endpoint shares its `instance_type`, by
   **`multi_model_consolidation`** (`×0.30`) — the same instance's dollars
   stacked across two sources. Prove whether a single endpoint id appears in both
   blocks and decide the single owner (idle deletion supersedes consolidation).
   Also check `idle_endpoints` vs the `active_endpoint_count` stat (an idle
   endpoint is still counted active).
6. **Cross-source:** there is no CoH/CO for this adapter, so no CoH-vs-heuristic
   overlap — confirm that and drop the axis. But confirm the **commitment_analysis**
   tab (which DOES receive `SageMakerSavingsPlans`) cannot double-count the same
   endpoint dollars this adapter already claims (SP is a billing-model lever over
   the same compute the idle/consolidation checks want to delete).
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*` helper in
   `html_report_generator.py` pulls SageMaker resources into a synthetic tab, and
   that a SageMaker endpoint/notebook instance is not also surfaced by EC2.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Are checks gated to states/shapes that silently exclude valid resources?
   Confirm full pagination of `list_endpoints` (the `_list_endpoints` fallback to
   a single `list_endpoints()` call drops pages on paginator failure — flag),
   `list_notebook_instances`, and `list_training_jobs`. Note
   `list_training_jobs` uses `MaxResults=100` with NextToken — confirm it truly
   paginates and isn't capped.
9. Are whole classes skipped? The adapter only covers **real-time endpoints,
   notebook instances, and training jobs**. Confirm the intentional gaps and
   whether any is a cost blind spot: **Studio apps / KernelGateway apps** (idle
   JupyterLab/KernelGateway instances bill hourly), **Serverless Inference**
   endpoints, **Asynchronous Inference**, **multi-variant endpoints** (only
   `ProductionVariants[0]` is priced — a 2nd variant's instances are ignored),
   `InstanceCount > 1` on a variant (saving prices ONE instance, not the fleet),
   **Processing / Transform / AutoML jobs**, and **Feature Store / pipelines**.
   For multi-variant and `InstanceCount`, the under-pricing is a *correctness*
   bug, not just coverage — call it out.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass` / `except: continue` / `return None` /
    `return [], 0` fallback. The adapter is dense with them:
    `_get_instance_monthly`, `_get_cloudwatch_invocations_sum`,
    `_list_endpoints` (double swallow), `_describe_endpoint`,
    `_describe_endpoint_config`, and the per-item `except: continue` in all four
    checks. **None record anything on `ctx`.** A `list_endpoints` /
    `list_notebook_instances` / CloudWatch `AccessDenied` vanishes with no
    `ctx.warn` / `ctx.permission_issue`. Classify:
    `AccessDenied`/`UnauthorizedOperation`/`OptInRequired` → `ctx.permission_issue`,
    other → `ctx.warn`. This is the canonical prior-audit silent-failure class.
11. **The $0-counted bug:** because there is **no `mark_zero_savings_advisory`**,
    every check appends a rec even when `_get_instance_monthly` returns `0.0`
    (pricing miss, `instance_type=="unknown"` for an idle endpoint). Confirm:
    `_check_idle_endpoints` and `_check_idle_notebooks` append with
    `monthly_savings=round(0.0,2)` and that $0 is summed into the headline and the
    rec count. (`_check_spot_training` and `_check_multi_model_consolidation`
    correctly `continue` on `instance_monthly <= 0` — note the inconsistency.)
    Every $0 finding must be advisory (`Counted=False`) or skipped.
12. **CloudWatch gating / fast-mode:** `_check_idle_endpoints` honours
    `fast_mode` (skips the CW read). But `_check_idle_notebooks`,
    `_check_spot_training`, and `_check_multi_model_consolidation` make
    describe/list calls **regardless of `fast_mode`** — confirm whether the
    `reads_fast_mode=True` contract is actually honoured across all four checks,
    and whether CW throttling/permission failures are recorded (currently they
    are swallowed to `None` and read as "no invocations → skip", which is benign
    for false positives but hides permission gaps).
13. **No-evidence heuristic:** `_check_idle_notebooks` recommends stopping EVERY
    running notebook for its full monthly cost with **zero idle signal** — no
    `LastModifiedTime`, no CloudWatch, no connection metric. This fabricates a
    saving on actively-used notebooks. Decide: gate on a real idle signal
    (CloudWatch `Invocations`-equivalent is N/A for notebooks; consider
    `describe_notebook_instance` `LastModifiedTime` / a CW agent metric), or make
    it a `$0` advisory.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Generic-renderer key mismatch (verify carefully):** SageMaker is NOT in
    `PHASE_A_DESCRIPTORS` and NOT in `_PHASE_B_SKIP_PER_REC`, so it renders via
    `render_generic_per_rec → _render_generic_other_rec`. That renderer prints
    `rec["EstimatedSavings"]` as the styled `.savings` line — but SageMaker recs
    carry **`monthly_savings`** (a float), **not** `EstimatedSavings`. Trace what
    actually renders: the dollar appears only as a generic
    `<p>Monthly Savings: …</p>` from the key-loop, with no `.savings` styling and
    no `$`/`/mo` formatting. Confirm the card's visible saving matches the
    JSON-counted number (it should be the same float, but unstyled) — and whether
    the resource title resolves (the resolver checks `EndpointName` /
    `NotebookInstanceName`, but recs use lowercase `endpoint_name` /
    `notebook_name`, so the title may fall through to "Resource"). This is a
    real render-fidelity finding.
15. **Counted == rendered:** `total_recommendations = len(all_recs)` and
    `total_monthly_savings = sum(monthly_savings)` with no advisory split. Confirm
    the per-tab headline equals the sum of the rendered cards, and reconcile the
    executive-summary headline (`_get_executive_summary_content` +
    `_calculate_service_savings`) against the SageMaker per-service total. If $0
    findings are present (Phase 5.11), the count is inflated relative to real
    dollars — quantify the gap.
16. Confirm no finding is counted but dropped from the table (or vice-versa)
    across all four sources and all `check_category` groups.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to SageMaker:
    `python3 cli.py <region> --scan-only sagemaker`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service sagemaker`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and resource appearing in >1 source. Reconcile the
    headline against the per-source sum. Caveats: most accounts have no
    `InService` endpoints — exercise the notebook and (historical) training-job
    branches, and a region with at least one running notebook to prove the
    no-evidence finding. Try a second region if the first is sparse. Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
18. For any duplication claim, prove it: show the same endpoint id in both
    `idle_endpoints` and `multi_model_consolidation`. For any accuracy claim,
    show the AWS Pricing API value (SageMaker `Hosting` vs `Notebook` vs
    `Training` rate for the instance type) next to the scanner's
    `get_sagemaker_instance_monthly` output.

### Deliverable
- The complete check list (Phase 1.2), per source, with counted-vs-should-be-
  advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add a `tests/test_sagemaker_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the pure helpers directly
  (`_get_instance_monthly` zero-path, the idle/spot/consolidation savings
  formulas, the `$0`→advisory gate, the idle/consolidation dedup) and drive
  `SageMakerModule.scan` with a `SimpleNamespace` ctx + fake boto3
  clients/paginators for the list/describe/CloudWatch paths. Cover every fix:
  silent-failure classification, `$0`-advisory gating, no-evidence notebook
  gating, spot one-time-vs-monthly handling, idle/consolidation dedup,
  multi-variant/`InstanceCount` pricing, fast-mode skip on all four checks,
  generic-renderer key fidelity, counted==rendered.
- Introduce `mark_zero_savings_advisory(all_recs, lambda r: r["monthly_savings"])`
  before computing the total, and sum only `Counted=True` (mirror Lambda /
  network_cost).
- For any heuristic that assumes usage with no evidence (idle notebooks),
  replace it with a real idle signal or keep it a $0 advisory — respect
  `ctx.fast_mode` and never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for sagemaker first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-failure / $0-counted / no-evidence bug in a sibling
  adapter out of scope, note it as a follow-up (don't fix unprompted).
- Add the missing `sagemaker` row to `services/adapters/CLAUDE.md` Live Pricing
  table (method `get_sagemaker_instance_monthly`, source AWS Pricing API).
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (an `InService` endpoint /
  running notebook) with **no usage metric** → fabricated $ (the
  `idle_notebooks` check flags every running notebook with zero idle evidence).
- Wrong instance-type SKU: a notebook/training instance priced at the **hosting**
  rate (one `get_sagemaker_instance_monthly` for three distinct SageMaker
  usage-type families); reserved/Savings-Plan-covered capacity priced as
  on-demand.
- Non-deterministic pricing filter (multiple SageMaker usage-type SKUs,
  `MaxResults=1`).
- Region: hardcoded constant/fallback not region-scaled, OR `pricing_multiplier`
  double-applied on an already-region-correct PricingEngine path.
- Per-run training saving (`SPOT_SAVINGS_RATE × one run cost`) summed as a
  **recurring monthly** total in the headline.
- Free-tier / opt-in saving recommended for a saving it cannot realize
  (Managed Spot "for similar future jobs" is speculative, not an existing cost).
- Same endpoint counted twice (`idle_endpoints` full cost + `multi_model_consolidation`
  ×0.30); authority should be idle-delete > consolidation by normalized id.
- Two heuristic checks stacking, or SUBSET redundancy (idle endpoints ⊆ the
  consolidation grouping population) — fix by removal, not double-count.
- Reduction factor (`CONSOLIDATION_SAVINGS_RATE=0.30`) instead of an exact price
  delta to a concrete consolidated target.
- `$0` "delete endpoint" / "stop notebook" placeholder **counted** (no
  `mark_zero_savings_advisory`) when `_get_instance_monthly` returns 0.0 —
  must convert to `Counted=False` advisory and drop from the total.
- Metric-gated `$0` nudge rendered as COUNTED instead of advisory.
- Cost Hub: `SageMakerSavingsPlans` routes to `commitment_analysis`, NOT this
  adapter — do not flag it as a missing source here; do confirm no double-count
  of the same endpoint compute between the SP tab and idle/consolidation.
- A source emitted with no PHASE_B handler: `sagemaker` falls through to the
  generic per-rec renderer, which reads `EstimatedSavings`/PascalCase keys the
  recs don't have → savings shown unstyled and the resource title may render as
  "Resource". Verify the visible card.
- CloudWatch/SageMaker `AccessDenied`/throttle failure swallowed to `None`/`[]`
  and read as "no usage", logged via nothing at all (not even `logger`) — never
  recorded on `ctx`.
- CloudWatch / list / describe reads not gated on `ctx.fast_mode` in three of the
  four checks despite `reads_fast_mode=True`.
- Coverage gated to real-time endpoints + notebooks + training only: Studio /
  KernelGateway apps, Serverless/Async Inference, 2nd+ `ProductionVariants`, and
  `InstanceCount>1` fleets are unpriced (the last two understate real cost).
- Each counted finding must carry a structured `AuditBasis` (rate / region /
  metric-window / formula); counted == rendered.

## PROMPT (end)
