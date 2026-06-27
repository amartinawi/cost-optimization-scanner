# Step Functions Adapter Cost-Audit Prompt

A deep, Step-Functions-specific audit brief in the same structure as the Lambda /
RDS / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Step Functions code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`step_functions`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the canonical model for the
`mark_zero_savings_advisory` / `Counted=False` metric-gated pattern and the
region-scaled module constant; the **EC2** adapter (`services/adapters/ec2.py`)
for the `$0`-placeholder→`ctx.warn` pattern and the exact-delta (not
reduction-factor) rule; the **network** adapter (`services/adapters/network.py`)
for the rate-string-rejection boundary; and the recently-audited **Lambda / RDS**
test files (`tests/test_lambda_audit_fixes.py`, `tests/test_rds_audit_fixes.py`)
as the test style I expect.

### NOTE on structure (step_functions: a two-stage adapter whose counted lever is dead)
- This adapter is split across **two files** that BOTH read CloudWatch:
  - `services/step_functions.py` (the legacy shim — `get_enhanced_step_functions_checks`,
    `STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS`) lists state machines, reads
    `ExecutionsStarted` over **7 days**, and emits a `standard_vs_express` rec
    only when `daily_avg > 100`. It sets NO dollar field — only a
    `"DailyExecutions"` string and an `EstimatedSavings` **percentage string**
    `"Up to 90% cost reduction for high-volume workflows"`.
  - `services/adapters/step_functions.py` → `StepFunctionsModule.scan` then
    RE-reads `ExecutionsStarted` over **30 days** and tries to compute a dollar
    figure with `STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025` and
    `AVG_STATES_PER_EXECUTION = 5`.
- There is **no** `*_logic.py`. The adapter emits a **single SourceBlock** named
  **`enhanced_checks`** (Remember for Phase 6).
- Step Functions consumes **neither Cost Optimization Hub nor Compute Optimizer**.
  Not in `core/scan_orchestrator.py`'s `type_map` / `_HUB_SERVICES`; no
  `services.advisor` helper. A "missing CoH/CO source" finding is **NOT** fair
  game — drop those axes.
- **Pricing is a module constant**, not `PricingEngine` (there is no Step
  Functions method in `core/pricing_engine.py`). The constant is
  `STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025` ($/1K Standard state transitions,
  `services/adapters/step_functions.py:42`).
- **CRITICAL up front:** trace the `eligible_for_migration` gate
  (`step_functions.py:75`). The shim never sets `StateCount` or `AvgDurationSec`,
  so the adapter defaults `state_count = AVG_STATES_PER_EXECUTION = 5` and
  `avg_duration_sec = 0`, making `eligible_for_migration = (5 > 25 and 0 < 60)`
  **always False** → the `savings += …` block (line 87-92) **never executes** →
  `total_monthly_savings` is **always 0**. Because `step_functions ∈
  _FLAT_SAVINGS_SERVICES` (`html_report_generator.py:82`), the reporter then
  substitutes a flat **$50 per rec** (`_calculate_service_savings` line 3076).
  That dead-lever → flat-$50 chain is the central finding; keep it in view
  through Phases 2, 5, and 6.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`, find the `step_functions.py` row (under
    "Live Pricing", method "CloudWatch ExecutionsStarted → $0.025/1K"). Reconcile:
    the row implies a working CloudWatch-driven counted saving, but the
    `eligible_for_migration` gate means **nothing is ever counted** — the real
    rendered dollars come from the flat-$50 substitution. Note the drift.
0b. Confirm module identity in `services/adapters/step_functions.py`:
    `key="step_functions"`, `cli_aliases=("step_functions",)`,
    `display_name="Step Functions"`, `reads_fast_mode=True`,
    `requires_cloudwatch=True`, `required_clients()=("states","cloudwatch")`.
    **Flag the client-name mismatch:** `required_clients` declares **`"states"`**
    but the shim calls `ctx.client("stepfunctions")`
    (`services/step_functions.py:37`) and the adapter calls
    `ctx.client("cloudwatch")` (line 55) — boto3's client id for Step Functions
    is **`stepfunctions`**, not `states` (`states` is only the CloudWatch
    namespace `AWS/States`). Verify whether `ctx.client` aliases these or whether
    the declared `"states"` client is simply wrong/unused.
0c. Step Functions has **no AWS advisory source** (no CoH/CO). Focus instead on:
    the dead `eligible_for_migration` lever, the flat-$50 fabrication, the
    fabricated `AVG_STATES_PER_EXECUTION = 5` dimension and `0.60` reduction
    factor, the double CloudWatch read, the fast_mode hole, and the render-string
    vs counted-number desync.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/step_functions.py` (the scan loop,
   constants, the CloudWatch re-read, `eligible_for_migration`),
   `services/step_functions.py` (the shim — `get_enhanced_step_functions_checks`,
   `STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS`), `services/_base.py`,
   `core/contracts.py`, `core/scan_context.py`
   (`ScanContext.client`/`fast_mode`/`pricing_multiplier`/`warn`),
   `core/result_builder.py`, and the reporter:
   `reporter_phase_a.py` (`_extract_step_functions_details` line 219,
   `PHASE_A_DESCRIPTORS["step_functions"]` line 319, `savings_mode="always"`,
   `render_grouped_by_category` line 390), `reporter_phase_b.py`
   (`_PHASE_A_SERVICES` line 2485), `html_report_generator.py`
   (`_FLAT_SAVINGS_SERVICES` line 82, `_calculate_service_savings` line 3076,
   `_get_service_content` line 3111, `_get_executive_summary_content` line 2795).
2. List **every** cost check and for each give: trigger, data source, the exact
   `EstimatedSavings` string, the constant/factor, and counted-vs-advisory. The
   known inventory to confirm:
   - **Shim `standard_vs_express`** (the only populated category): trigger =
     `sm_type == "STANDARD"` AND 7-day CloudWatch `daily_avg > 100`
     (`services/step_functions.py:45,64`). Emits `StateMachineArn`,
     `StateMachineName`, `Type`, `DailyExecutions` (a formatted string),
     `Recommendation`, `EstimatedSavings = "Up to 90% cost reduction…"` (a
     percentage), `CheckCategory = "Step Functions Type Optimization"`. **No
     dollar field, no `StateCount`, no `AvgDurationSec`, no `MonthlyExecutions`.**
   - The shim `checks` dict also declares `excessive_transitions`,
     `polling_workflows`, `nonprod_24x7` — all initialized empty and **never
     populated** (the non-prod 24/7 finding was removed,
     `services/step_functions.py:81-82`). `STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS`
     advertises `standard_vs_express` and `nonprod_24x7`; the latter is orphaned.
   - **Adapter dollarization** (`services/adapters/step_functions.py:45-96`): for
     each rec, if `not ctx.fast_mode`, read `MonthlyExecutions` (absent → 0 →
     re-read `ExecutionsStarted` over 30 days), then
     `monthly_transitions = monthly_executions * AVG_STATES_PER_EXECUTION (5)`,
     and **only if** `eligible_for_migration` (state_count>25 AND duration<60)
     add `(monthly_transitions/1000) * 0.025 * 0.60 * pricing_multiplier`.
     Confirm `eligible_for_migration` is **always False** (see Phase 0c).

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate EACH rate against the live AWS Pricing API. **The service code is
   `AmazonStates`, NOT `AWSStepFunctions`** (the latter returns empty — note this
   for any future live-pricing migration):
   - **Standard state transition**: `usagetype USE1-StateTransition`,
     `group SFN-StateTransitions` = **$0.000025 per transition = $0.025/1K** →
     confirms `STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025` (us-east-1). Note the
     **first 4,000 transitions/month/account are FREE** (free tier) — not
     subtracted anywhere; for low-volume Standard machines the saving is
     overstated.
   - **Express** is a **fundamentally different model**: `$1.00/M requests`
     (`USE1-StepFunctions-Request`) **+ duration** `$0.00001667/GB-second`
     (`USE1-StepFunctions-GB-Second`, first tier). So Standard→Express savings is
     **not** "60% of the Standard transition cost" — it depends on per-execution
     request count, memory, and duration. Validate this and flag the `0.60`
     factor as an un-calibrated reduction factor (the code comment at
     `step_functions.py:80-86` itself admits real savings range from 95%+ to
     NEGATIVE).
   - **`AVG_STATES_PER_EXECUTION = 5`** is a fabricated config dimension with no
     evidence: the real transition count per execution comes from the ASL state
     machine definition (never fetched via `describe_state_machine`). Using a
     flat 5 invents the transition volume → fabricated $.
   - **Region scaling**: the `0.025` constant is us-east-1; the adapter applies
     `ctx.pricing_multiplier` once inside the savings expression
     (`step_functions.py:91`). Confirm it is applied exactly once (not double).
4. Confirm the savings basis is defensible from the report alone. Because the
   counted lever is dead (always 0) and the rendered dollars are the flat-$50
   substitution, **there is currently no defensible per-finding AuditBasis at
   all** — record this. Any fixed lever must carry a structured **AuditBasis**
   (rate `$0.025/1K`, region, metric-window, formula, free-tier subtraction) as
   the Lambda/RDS audits did.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** only `standard_vs_express` is populated and each state
   machine appears once. Low risk, but confirm a machine cannot be appended twice
   across paginator pages.
6. **Cross-source:** none — no CoH/CO. State explicitly and drop the axis.
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*` helper in
   `html_report_generator.py` pulls state machines into a synthetic tab, and that
   Lambda functions invoked BY a state machine are not double-counted (they are
   separate resources under the Lambda adapter — confirm no overlap).
8. **Double CloudWatch read of the SAME metric:** the shim reads
   `ExecutionsStarted` (7-day) AND the adapter reads `ExecutionsStarted` (30-day)
   for the same `StateMachineArn`. Not a double-count of dollars, but a wasteful
   duplicate API call and an inconsistency (7-day `daily_avg` gate vs 30-day Sum
   for dollarization). Flag for consolidation.

### Phase 4 — Coverage (works for ALL resources, not a subset)
9. **STANDARD-only coverage:** only `sm_type == "STANDARD"` machines are examined
   (`services/step_functions.py:45`). EXPRESS machines are skipped entirely — no
   check for over-provisioned/idle Express workflows (which bill on requests +
   GB-seconds). Confirm and decide if intentional.
10. **`daily_avg > 100` gate:** only high-volume Standard machines are flagged.
    Document the cutoff. Confirm full pagination of `list_state_machines` (it uses
    `get_paginator`, good) and that `describe_state_machine`/`type` is read for
    every machine (the `list` response includes `type`, so no extra call — but it
    also means `StateCount`/definition is never fetched, feeding the fabricated
    `AVG_STATES_PER_EXECUTION`).
11. **Idle machines incur no charge:** the adapter comment
    (`step_functions.py:93-96`) correctly notes idle Standard machines cost $0 and
    a prior $150 fallback was removed — confirm no fabricated fallback survives,
    and that the ONLY remaining fabricated number is the reporter's flat-$50.

### Phase 5 — Silent failures (nothing fails quietly)
12. Find every `except: pass`, bare `except`, `logger`-only, and `return`
    fallback:
    - `services/step_functions.py:78` — `except Exception: pass` swallows the
      shim's CloudWatch `get_metric_statistics` failure per machine; a throttled
      metric read silently drops the machine with no `ctx.warn` /
      `ctx.permission_issue`. Classify AccessDenied/Throttling appropriately.
    - `services/adapters/step_functions.py:70` — `except Exception:
      monthly_executions = 0` swallows the adapter's CloudWatch re-read failure.
    - `services/step_functions.py:84-85` — the outer `except Exception as e:` DOES
      call `ctx.warn(...)` (good); confirm it classifies permission errors.
13. **Dead lever → flat-$50 fabrication (CRITICAL to trace):**
    `total_monthly_savings` is always 0 (Phase 0c). Because `step_functions ∈
    _FLAT_SAVINGS_SERVICES`, `_calculate_service_savings`
    (`html_report_generator.py:3076-3094`) substitutes **$50 per rec** with no
    rate/metric/basis, driving the per-tab headline (`_get_service_content` line
    3114-3119) and the executive summary (line 2831, reconciliation footnote line
    2281). Every high-volume Standard machine silently becomes $50. Decide: the
    rec should be **advisory (`Counted=False`)** until a real, calibrated
    Standard→Express delta exists.
14. **fast_mode hole:** `reads_fast_mode=True` is declared, and the adapter's
    dollarization block is gated on `if not ctx.fast_mode`
    (`step_functions.py:47`). BUT the **shim's** CloudWatch read
    (`services/step_functions.py:50-59`) is **NOT** gated on `ctx.fast_mode` — it
    runs unconditionally for every STANDARD machine. So fast_mode does **not**
    skip the expensive per-machine metric read; `reads_fast_mode` is only
    half-honored. Mirror the Lambda fast-mode fix (gate the shim read too).
15. **Percentage / rate strings counted?** Confirm the shim's
    `EstimatedSavings = "Up to 90% cost reduction…"` is never parsed into a
    counted dollar (it is a render-only string; the counted path is the dead
    transition formula). Ensure any future real number does not get double-read
    from the percentage string.

### Phase 6 — Reporting (one tab, counted == rendered)
16. **Single source / Phase A render:** the adapter emits one source
    (`enhanced_checks`); confirm it reaches `render_grouped_by_category` via
    `PHASE_A_DESCRIPTORS["step_functions"]` and no source is silently unrendered.
17. **Render-string vs counted-number desync (verify carefully):**
    `render_grouped_by_category` with `savings_mode="always"`
    (`reporter_phase_a.py:450-451`) prints
    `resources[0]["EstimatedSavings"]` = the literal **"Up to 90% cost reduction
    for high-volume workflows"** percentage, while the headline shows the
    flat-$50 substitution (because counted is 0). So the card text ("Up to 90%"),
    the counted value ($0), and the rendered headline ($50/rec) are **three
    different numbers**. Document this explicitly.
18. **Counted == rendered:** `total_recommendations = len(recs)` but
    `total_monthly_savings = 0` always; the rendered tab total is the flat-$50
    override. Confirm the counted-vs-advisory split is shown
    (`_counted_advisory_counts`) and that the headline is reconciled — currently
    it is NOT (the headline is fabricated). No rec should be counted in the total
    but missing from the table, or vice-versa.

### Phase 7 — Tooling & evidence
19. Run a real scan scoped to Step Functions:
    `python3 cli.py <region> --scan-only step_functions`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service step_functions`.
    Triage every: silent failure, `$0`/flat-$50 finding, and any machine
    appearing more than once. Reconcile the headline against the per-rec sum —
    expect the dead lever (every counted = 0) and the flat-$50 substitution in
    the rendered tab. Caveats: you need a STANDARD machine with >100 exec/day to
    populate any rec; EXPRESS machines won't appear; CloudWatch may be empty
    (exercise the `monthly_executions=0` path). Use `.venv/bin/python` (3.14) —
    system `python3` lacks `datetime.UTC`.
20. For the dead-lever claim, show a scan JSON where a `standard_vs_express` rec
    has counted savings 0 yet the rendered tab / executive summary shows $50. For
    any accuracy claim, show the AWS Pricing API value (Standard $0.025/1K;
    Express $1/M requests + $0.00001667/GB-s) next to the scanner's `0.025 × 0.60`.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked and the
  dead-lever → flat-$50 chain documented.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file
  + line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add `tests/test_step_functions_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: test the
  pure decision logic directly (the `eligible_for_migration` gate, the transition
  formula, free-tier subtraction, the flat-$50 vs advisory decision) and drive
  `StepFunctionsModule.scan` with a `SimpleNamespace` ctx + fake
  `stepfunctions`/`cloudwatch` clients & paginators. Cover every fix: dead-lever
  repair OR demotion to advisory, flat-$50 → advisory, fast_mode gating of the
  shim read, double-read consolidation, required_clients mismatch, render-string
  vs counted-number, counted==rendered.
- The Standard→Express lever is currently undefendable (fabricated states ×
  factor; Express is a different model). Either compute a real delta from the ASL
  state count + Express request/duration model, or keep the rec **$0 advisory**
  (`Counted=False`) — never the flat $50 and never the `0.60` factor on a
  fabricated transition count. Respect `ctx.fast_mode` and never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula /
  free-tier) on each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for step_functions first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same flat-$50 / silent-failure bug in a sibling adapter
  (`opensearch`, `api_gateway` also in `_FLAT_SAVINGS_SERVICES`), note it as a
  follow-up (don't fix unprompted).
- Update the `step_functions.py` row in `services/adapters/CLAUDE.md` to match
  reality (dead lever, flat-$50 rendered path, STANDARD-only coverage).
- Stage ONLY the files you changed when committing.

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

### Step-Functions-specific issues to check (discovered in the code)
- **Dead counted lever — `total_monthly_savings` is ALWAYS $0:** savings are
  added only when `eligible_for_migration = state_count > 25 and avg_duration_sec
  < 60` (`services/adapters/step_functions.py:75`), but the shim never sets
  `StateCount`/`AvgDurationSec`, so the adapter defaults `state_count = 5` and
  `avg_duration_sec = 0` → `5 > 25` is always False → the `savings +=` block
  (line 87-92) never runs. No Standard→Express dollar is ever counted.
- **Flat-$50-per-rec fabrication takes over:** `step_functions ∈
  _FLAT_SAVINGS_SERVICES` (`html_report_generator.py:82`). Because counted is
  always 0, `_calculate_service_savings` (line 3076-3094) assigns **$50 per rec**
  with no basis, driving the per-tab headline and executive summary.
- **Fabricated dimension + reduction factor:** `AVG_STATES_PER_EXECUTION = 5`
  invents the transition volume (real state count comes from the ASL definition,
  never fetched), and `0.60` is an un-calibrated Standard→Express reduction
  factor — but Express is a **different pricing model** ($1/M requests +
  $0.00001667/GB-s, validated), so the factor can be 95%+ or NEGATIVE (the code
  comment admits this).
- **Double CloudWatch read + fast_mode hole:** the shim reads `ExecutionsStarted`
  (7-day, `services/step_functions.py:50`) UNCONDITIONALLY — NOT gated on
  `ctx.fast_mode` — and the adapter re-reads `ExecutionsStarted` (30-day,
  `services/adapters/step_functions.py:58`) gated on `not ctx.fast_mode`. So
  `reads_fast_mode=True` is only half-honored and the same metric is fetched
  twice with inconsistent windows.
- **`required_clients` mismatch:** the adapter declares
  `required_clients()=("states","cloudwatch")` but the shim calls
  `ctx.client("stepfunctions")` — boto3's client id is `stepfunctions`, not
  `states` (which is only the CloudWatch namespace). Verify aliasing or fix the
  declared client.
- **Wrong Pricing API service code in any future migration:** the live pricing
  service code is **`AmazonStates`**, not `AWSStepFunctions` (the latter returns
  empty). Standard `USE1-StateTransition` = $0.000025/transition confirms
  `0.025/1K`; note the **first 4,000 transitions/month/account are free** and are
  not subtracted.
- **STANDARD-only coverage + orphaned descriptions:** only `sm_type ==
  "STANDARD"` is examined (EXPRESS skipped), and the shim's
  `excessive_transitions`/`polling_workflows`/`nonprod_24x7` categories plus the
  `nonprod_24x7` description are declared but never emitted (dead config).
- **Silent per-machine failure:** `except Exception: pass`
  (`services/step_functions.py:78`) and `except Exception: monthly_executions =
  0` (`services/adapters/step_functions.py:70`) swallow CloudWatch failures with
  no `ctx.warn` / `ctx.permission_issue`.

## PROMPT (end)
