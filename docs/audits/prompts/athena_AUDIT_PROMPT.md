# Athena Adapter Cost-Audit Prompt

A deep, Athena-specific audit brief in the same structure as the Network / Lambda /
RDS / AMI audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Athena code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`athena`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory (Athena is
priced at **$5.00 per TB of data scanned**, `serviceCode=AmazonAthena`). Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. Athena's
saving is **entirely CloudWatch-evidence-gated** (ProcessedBytes → $/TB), so the
canonical sibling references are the **S3** adapter
(`services/adapters/s3.py`) for the CloudWatch-evidence-gating + `$0`-advisory
discipline (no metric → no counted saving), and the **Lambda** adapter
(`services/adapters/lambda_svc.py`) for the rate-string / advisory-`$0` discipline
and the test style I expect (`tests/test_lambda_audit_fixes.py`).

### NOTE on structure (Athena is CloudWatch-metric-priced, NOT CoH/CO)
- `services/adapters/athena.py` → `AthenaModule.scan` is a thin wrapper over the
  shim `services/athena.py:get_enhanced_athena_checks`. The shim discovers
  workgroups; the adapter does all pricing.
- Pricing is a **module constant**, `ATHENA_PRICE_PER_TB = 5.0`, multiplied by the
  CloudWatch `AWS/Athena ProcessedBytes` Sum over a 30-day window converted to TB,
  by `ctx.pricing_multiplier`, and by a hardcoded **`0.75`** compression factor.
  There is NO `PricingEngine` method for Athena — the $5/TB is a literal in the
  adapter (see `services/adapters/CLAUDE.md`: "CloudWatch ProcessedBytes → $5/TB").
- The adapter emits a **single SourceBlock** named `enhanced_checks`.
- **Athena has NO Cost Optimization Hub source and NO Compute Optimizer source.**
  There is no Athena `currentResourceType` in
  `scan_orchestrator._prefetch_advisor_data.type_map`, Athena is not in
  `_HUB_SERVICES`, and no `services/advisor.py` helper pulls Athena CO findings.
  So "missing CoH/CO source" is NOT fair game — savings are expected to be locally
  derived from the CloudWatch ProcessedBytes metric. Drop the CoH/CO axes.
- The adapter **declares** `requires_cloudwatch=True` and `reads_fast_mode=True`,
  and `required_clients()=("athena","cloudwatch")`. Confirm both flags are honored
  (the CW read IS gated on `ctx.fast_mode`; under fast mode every rec is emitted at
  `$0` + `PricingWarning`).
- **Render path:** `athena` is NOT in `PHASE_A_DESCRIPTORS`, NOT in
  `PHASE_B_HANDLERS`, and NOT in `_PHASE_B_SKIP_PER_REC` (`reporter_phase_b.py`).
  Therefore `should_fallback_to_per_rec("athena")` is True and `enhanced_checks`
  renders via the **generic per-record renderer**. Confirm that renderer reads the
  Athena rec shape (it sets `EstimatedMonthlySavings` float + `PricingWarning`, not
  an `EstimatedSavings` string).

### Phase 0 — Orient (5-minute map before judging)
0a. Confirm identity in `services/adapters/athena.py`: `key="athena"`,
    `cli_aliases=("athena",)`, `display_name="Athena"`, `reads_fast_mode=True`,
    `requires_cloudwatch=True`, `required_clients()=("athena","cloudwatch")`.
0b. Read `services/athena.py:get_enhanced_athena_checks` carefully. **The critical
    fact:** both historical checks ("Workgroup Optimization" scan-limit and "Athena
    Query Results" S3-lifecycle) were **removed** — the `checks` dict
    (`{"workgroup_optimization": [], "query_results": []}`) is now populated by
    NOTHING, so `recommendations` is **always empty**. Confirm the adapter
    therefore currently emits **zero findings** for any account. The whole
    ProcessedBytes pricing loop in the adapter runs over an empty list. Decide
    whether this is intended (dead adapter) or a regression that dropped the only
    quantifiable check, and whether the workgroup→ProcessedBytes pricing should be
    wired to actually produce recs.
0c. Read `services/adapters/CLAUDE.md` — Athena is listed under Live Pricing as
    "CloudWatch ProcessedBytes → $5/TB" (CloudWatch + constant). Reconcile the doc
    against the empty-checks reality from 0b.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/athena.py` and `services/athena.py` in full; the CW
   read in `AthenaModule.scan` (the `get_metric_statistics` call: Namespace
   `AWS/Athena`, MetricName `ProcessedBytes`, Dimension `WorkGroup`, Period
   `2592000`, Statistic `Sum`, 30-day window); `core/contracts.py`;
   `core/scan_context.py` (`pricing_multiplier`, `fast_mode`, `client`);
   `core/result_builder.py`; and the generic per-rec renderer in
   `reporter_phase_b.py` / `html_report_generator.py`.
2. List **every** cost check the adapter *would* emit if the shim produced recs,
   and for each give: trigger condition, data source (workgroup discovery via
   `athena.list_work_groups` / `get_work_group`; CloudWatch ProcessedBytes), the
   savings formula (`monthly_tb × $5/TB × pricing_multiplier × 0.75`), the embedded
   constants ($5/TB, 0.75 compression factor), and whether the result is **counted**
   (a real $ from a positive ProcessedBytes) or a **$0 advisory** (CW empty / fast
   mode → `EstimatedMonthlySavings=0.0` + `PricingWarning`). Note that the rec
   carries `ProcessedBytesTB` and `WorkGroup` fields that the shim never sets today.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate each constant against the live AWS Pricing API:
   - **$5/TB scanned**: confirm `serviceCode=AmazonAthena` lists the per-TB
     data-scanned rate at **$5.00/TB us-east-1** (DataScannedInTB usagetype).
     Confirm whether other Athena pricing modes exist (provisioned capacity DPU
     pricing, Athena for Spark, federated query) that this flat $5/TB ignores.
   - **The `0.75` compression factor**: the comment claims "70-91% compression for
     columnar formats (Parquet/ORC)" → 75% savings. Confirm this is a *defensible
     labelled* heuristic and not a fabricated number. It assumes the workgroup is
     NOT already using Parquet/ORC/partitioning — if it is, the 75% is pure fiction.
     There is no detection of current data format. Flag the unconditional factor.
   - **Region scaling**: `$5/TB` is us-east-1 and is multiplied by
     `ctx.pricing_multiplier` (constant path — correct, single application). Confirm
     no double-multiply (there is no PricingEngine path here).
   - **Unit conversion**: `total_bytes / (1024**4)` converts bytes→TiB, but AWS
     bills per **TB (10^12), not TiB (2^40)** — confirm whether the binary divisor
     understates TB count by ~10% (and thus the saving). This is a real units bug
     class.
4. Confirm the basis is defensible from the report alone: each counted finding
   should record the ProcessedBytes window, the TB scanned, the $5/TB rate, and the
   0.75 factor. Add a structured `AuditBasis` (rate / region / metric-window /
   formula) on each counted finding, mirroring the Lambda/RDS audits.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** the adapter loops per rec and sums `rec_savings`. If two recs
   ever carried the same `WorkGroup`, the same ProcessedBytes would be counted
   twice — confirm workgroup uniqueness if the shim is re-wired to emit recs.
6. **Cross-source:** none — Athena has no CoH/CO. Confirm.
7. **Cross-adapter:** Athena query results land in S3; confirm the Athena saving
   (data *scanned*) does not overlap the S3 adapter's *storage* saving on the query
   results bucket (different cost dimension — scan vs storage — so no overlap, but
   confirm S3 doesn't also flag the same bytes).

### Phase 4 — Coverage (works for ALL workgroups, not a subset)
8. Confirm `athena.list_work_groups` is **paginated** (it is NOT today — a single
   `list_work_groups()` call with no `NextToken` loop; accounts with >50 workgroups
   silently drop the rest). Flag the missing pagination.
9. Are whole classes skipped? The default `primary` workgroup; workgroups with
   per-query data-scan limits already set; workgroups on **provisioned capacity**
   (priced per-DPU-hour, NOT per-TB — the $5/TB model is wrong for them). Confirm
   each skip/assumption is intentional and documented.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only path, and `return []`
    fallback:
    - **Shim outer `except` (`get_enhanced_athena_checks`)** records `ctx.warn(...)`
      — good, but the inner per-workgroup `except Exception: continue` swallows a
      denied/throttled `get_work_group` with NO record. Classify
      `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`, other →
      `ctx.warn`.
    - **Adapter CW `except Exception as e:` (`AthenaModule.scan`)** logs
      `logger.warning("[athena] CloudWatch ProcessedBytes metric check failed")`
      ONLY and sets `monthly_tb = 0` — a CloudWatch permission/throttle failure is
      silently converted to a `$0` finding with no `ctx.warn`/`ctx.permission_issue`.
      This is the canonical prior-audit silent-failure class. Fix it.
11. Does a pricing miss fall back to `0.0` and still emit a finding? When
    ProcessedBytes is empty OR under fast mode the rec is set to
    `EstimatedMonthlySavings=0.0` + `PricingWarning`. Confirm these are surfaced as
    **advisory** (not counted) — but note the adapter does NOT set `Counted=False`
    on these recs; the $0 simply doesn't add to the float total. Confirm the
    reporter/exec-summary doesn't count a $0 rec as a quantified saving, and that
    the rec count vs counted-savings split is honest.
12. **CloudWatch gating / fast-mode:** confirm the CW read is gated on
    `ctx.fast_mode` (it is — the `else` branch emits $0 + "re-run without --fast").
    Confirm a fast-mode scan produces only advisory $0 recs and never a fabricated
    saving.

### Phase 6 — Reporting (one tab, counted == rendered)
13. **Generic per-rec render (verify carefully):** with `athena` absent from
    `PHASE_A_DESCRIPTORS`, `PHASE_B_HANDLERS`, and `_PHASE_B_SKIP_PER_REC`,
    `enhanced_checks` renders via the generic per-record renderer. Trace
    `html_report_generator._get_detailed_recommendations` / the per-rec fallback and
    confirm it reads the Athena rec fields. Since the shim emits **no recs today**,
    confirm the Athena tab renders empty/clean (no blank cards, no crash).
14. **Counted == rendered:** `total_monthly_savings = savings` (sum of the float
    `EstimatedMonthlySavings`); `total_recommendations = len(recs)`. Confirm the
    per-tab total equals the sum of the counted rendered findings and that advisory
    `$0` recs are rendered but contribute $0. Reconcile the Athena per-service total
    against the executive-summary headline.

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to Athena:
    `.venv/bin/python cli.py <region> --scan-only athena`
    then `.venv/bin/python tools/scan_doctor.py <json> --service athena`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and resource appearing in >1 source. Confirm the empty
    output from 0b reproduces. Caveats: use an account with active Athena workgroups
    and recent query history (so ProcessedBytes is non-zero); also exercise the
    fast-mode (`--fast`) advisory path. Use `.venv/bin/python` (3.14) — system
    `python3` lacks `datetime.UTC` (the adapter imports `timezone` and calls
    `datetime.now(timezone.utc)`).
16. For any accuracy claim, show the AWS Pricing API Athena $5/TB value next to the
    scanner's constant, and show the ProcessedBytes datapoints behind any counted TB
    figure.

### Deliverable
- The complete check list (Phase 1.2), with the **empty-shim** finding called out
  first (the adapter currently emits nothing).
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**.
  End with an ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_athena_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the pure pricing logic
  (bytes→TB conversion, $5/TB × multiplier × 0.75, empty-ProcessedBytes → $0
  advisory, fast-mode → $0 advisory) and drive `AthenaModule.scan` with a
  `SimpleNamespace` ctx + a fake `cloudwatch` client returning ProcessedBytes
  datapoints (and an empty/raising one). Cover every fix: CW silent-failure
  classification, TiB-vs-TB units, workgroup pagination, advisory `$0` gating,
  re-wiring the shim to emit recs (if adopted), generic render wiring,
  counted==rendered.
- Record a structured `AuditBasis` (rate / region / metric-window / formula) on each
  counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for Athena first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `athena.py` row in `services/adapters/CLAUDE.md` to match reality.
- If you find the same silent-failure / pricing / units bug in a sibling
  CloudWatch-priced adapter out of scope (`step_functions.py`), note it as a
  follow-up — don't fix unprompted.
- Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (DPU/capacity/SPICE GB) with NO usage
  metric → fabricated $.
- Wrong tier/edition/model pricing.
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded constant not region-scaled via `pricing_multiplier`, OR
  double-applied on an already-region-correct engine path.
- Per-unit RATE string ($/TB, $/DPU-hr) counted as a monthly total — rejected by
  `parse_dollar_savings` → $0 advisory.
- Fixed hours assumption (×160, ×730) applied as if always-on with no evidence.
- Free-tier recommended for a saving it cannot realize.
- Same resource counted twice; authority dedup CoH > CO > heuristic by NORMALIZED id.
- Two heuristic checks stacking, or SUBSET redundancy — fix by removal.
- Reduction factor instead of exact price delta.
- $0 "enable X"/opt-in placeholder counted instead of converted to `ctx.warn` and
  dropped.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (`Counted=False`).
- Cost Hub: `currentResourceType` with no bucket → dropped; bucket consumed by NO
  adapter → dropped silently (verify Athena isn't wired — it isn't).
- A source emitted with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC` service →
  renders nothing silently (Athena uses the generic per-rec fallback — confirm it
  renders).
- Render-time category filter desyncing headline from cards (filter at SOURCE).
- Coverage gated to a hardcoded type/tier allowlist.
- CloudWatch/Cost Explorer permission/throttle failure logged via logger only, not
  `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic assuming usage with no evidence.
- Cross-adapter overlap.
- Each counted finding must carry a structured `AuditBasis` (rate/region/metric-
  window/formula); counted == rendered.

#### Athena-specific items (found in this code)
- **Shim emits ZERO recommendations** (`get_enhanced_athena_checks` returns empty
  `checks`/`recommendations`): both prior checks were removed and never replaced, so
  the entire ProcessedBytes pricing loop runs over an empty list — the adapter
  currently produces no Athena findings at all. Highest-priority reconcile.
- **Unconditional `0.75` compression factor**: assumes the workgroup is NOT already
  using Parquet/ORC/partitioning, with no detection of current data format — the
  75% saving is fabricated for any already-optimized workgroup.
- **TiB-vs-TB units**: `total_bytes / (1024**4)` divides by 2^40 (TiB) while AWS
  bills per TB (10^12) — understates the TB count and the saving by ~10%.
- **CloudWatch failure → silent `$0`**: the adapter's `except Exception` logs via
  `logger.warning` only and sets `monthly_tb = 0`, converting a CW
  permission/throttle error into a $0 finding with no `ctx.warn`/`permission_issue`.
- **`list_work_groups` not paginated**: a single call with no `NextToken` loop drops
  workgroups past the first page.
- **Provisioned-capacity workgroups mispriced**: the flat $5/TB data-scanned model is
  wrong for workgroups on Athena provisioned capacity (per-DPU-hour) — no detection.
- **Doc/code mismatch**: `services/adapters/CLAUDE.md` advertises a live $5/TB
  CloudWatch-priced check, but the shim produces nothing — the documented saving is
  unreachable today.

## PROMPT (end)
