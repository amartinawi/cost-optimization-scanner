# Glue Adapter Cost-Audit Prompt

A deep, Glue-specific audit brief in the same structure as the Network / Lambda /
RDS / AMI audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Glue code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`glue`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory (Glue ETL is
priced per **DPU-hour**, `serviceCode=AWSGlue`). Use the codebase/search tools
(CodeGraph if present) to trace actual code paths. Glue's saving is a **pure
config heuristic priced from a module constant with no usage metric**, so the
canonical sibling references are the **Lambda** adapter
(`services/adapters/lambda_svc.py`) for the metric-gated `$0`-advisory discipline
and the `parse_dollar_savings` rate-string rejection, and the **EC2** adapter
(`services/adapters/ec2.py`) for the "exact price delta, not a reduction factor"
principle and the test style I expect (`tests/test_lambda_audit_fixes.py`).

### NOTE on structure (Glue is a constant-priced config heuristic, NOT CoH/CO)
- `services/adapters/glue.py` → `GlueModule.scan` wraps the shim
  `services/glue.py:get_enhanced_glue_checks`. The shim discovers jobs / dev
  endpoints / crawlers and emits raw recs; the adapter does all pricing.
- Pricing is **module constants** in `GlueModule.scan`:
  `GLUE_DPU_HOURLY = 0.44` ($/DPU-hr us-east-1), `GLUE_RIGHTSIZE_FACTOR = 0.30`,
  and **`ASSUMED_MONTHLY_DPU_HOURS = 160`** (8 hr/day × 20 workdays — a fixed
  always-runs assumption with NO usage evidence). There is NO `PricingEngine`
  method for Glue (see `services/adapters/CLAUDE.md`: "$0.44/DPU/hour × 160").
  Saving = `0.44 × dpu_count × 160 × pricing_multiplier × 0.30`.
- The adapter emits **per-check-type SourceBlocks** from the shim's `checks` dict:
  `job_rightsizing`, `dev_endpoints`, `crawler_optimization` (the last is always
  empty — its check was removed).
- **Glue has NO Cost Optimization Hub source and NO Compute Optimizer source.**
  No Glue `currentResourceType` in
  `scan_orchestrator._prefetch_advisor_data.type_map`, Glue is not in
  `_HUB_SERVICES`, no `services/advisor.py` Glue helper. Drop the CoH/CO axes —
  savings are expected to be locally derived. Note the adapter does **NOT** declare
  `requires_cloudwatch` / `reads_fast_mode` and makes **no CloudWatch reads at all**
  — so there is no usage signal behind the saving (this is the central finding to
  scrutinize).
- **Render path:** `glue` IS a Phase A service (`_PHASE_A_SERVICES` in
  `reporter_phase_b.py`, with a `PHASE_A_DESCRIPTORS["glue"]` entry,
  `_extract_glue_details`, `savings_mode="conditional"`, `close_div_location="inner"`).
  Phase A renders the **`EstimatedSavings` STRING** off the rec — NOT the float
  `EstimatedMonthlySavings` the adapter computes. This string/float split is a
  render-desync hot spot (see Phase 6).

### Phase 0 — Orient (5-minute map before judging)
0a. Confirm identity in `services/adapters/glue.py`: `key="glue"`,
    `cli_aliases=("glue",)`, `display_name="Glue"`, NO `reads_fast_mode`/
    `requires_cloudwatch`, `required_clients()=("glue",)`.
0b. Read `services/adapters/CLAUDE.md` — Glue is listed under Live Pricing as
    "$0.44/DPU/hour × 160" (module constant). Confirm the `160` is the fixed
    `ASSUMED_MONTHLY_DPU_HOURS` and reconcile.
0c. **Trace the render carefully:** `glue` is in `_PHASE_A_SERVICES` and
    `should_skip_section_header("glue")` is True (it is in the
    `{"lightsail","dms","glue","redshift"}` set). Phase A's `savings_mode`
    `conditional` prints `resources[0].get("EstimatedSavings")` (the string) only
    when present. The adapter overwrites `EstimatedMonthlySavings` (float) but
    leaves the shim's `EstimatedSavings` string untouched. Confirm exactly which
    value reaches the card vs the counted total.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/glue.py` and `services/glue.py` in full
   (`get_enhanced_glue_checks`: `get_jobs` paginator, `get_dev_endpoints`,
   `get_crawlers` paginator); the pricing loop in `GlueModule.scan` (constants
   `GLUE_DPU_HOURLY`, `GLUE_RIGHTSIZE_FACTOR`, `ASSUMED_MONTHLY_DPU_HOURS`, the
   `MaxCapacity`/`NumberOfWorkers` resolution and `None`/non-numeric → $0 +
   `PricingWarning` paths); `core/contracts.py`; `core/scan_context.py`;
   `core/result_builder.py`; and the Phase A render path
   (`reporter_phase_a.py:_extract_glue_details` ~line 199, `PHASE_A_DESCRIPTORS["glue"]`
   ~line 301, the `savings_mode` branch ~line 450).
2. List **every** cost check, and for each give: trigger condition, data source
   (Glue describe-API only — no CloudWatch), the savings formula, the embedded
   constants, and whether it parses to a **counted** float or a **$0 advisory**.
   The known inventory to confirm:
   - **`job_rightsizing`** — fires when `MaxCapacity > 10` OR `NumberOfWorkers > 10`.
     Static `EstimatedSavings="20-40% with auto-scaling"`. Adapter recomputes a
     float from `0.44 × dpu × 160 × mult × 0.30`.
   - **`dev_endpoints`** — fires when `Status == "READY"`. Static
     `EstimatedSavings="$316/month per endpoint"`. **But** the adapter prices via
     `MaxCapacity`/`NumberOfWorkers`, which dev-endpoint recs do NOT carry → the
     adapter sets `EstimatedMonthlySavings=0.0` + `PricingWarning`. So the "$316/mo"
     dev-endpoint saving is dropped to $0 by the adapter even though the rendered
     card shows the $316 string (see Phase 6).
   - **`crawler_optimization`** — removed; always empty.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate each constant against the live AWS Pricing API:
   - **$0.44/DPU-hr**: confirm `serviceCode=AWSGlue` lists the **G.1X / standard
     ETL** DPU-hour at $0.44 us-east-1. Confirm whether the job's actual
     `WorkerType` (G.1X / G.2X / G.025X Python-shell / G.4X / G.8X) and **Flex**
     execution class change the rate — a G.2X is 2 DPU/worker, G.025X is 0.0625 DPU,
     and Flex is **~$0.29/DPU-hr** (≈34% cheaper). The flat $0.44 ignores all of
     this. Also confirm the saving uses `dpu_count = MaxCapacity or NumberOfWorkers`
     — but `NumberOfWorkers` is a *worker* count, NOT a DPU count for G.2X/G.4X/G.8X
     (×2/×4/×8 DPU per worker). Treating workers as DPUs understates G.2X+ by 2-8×.
   - **`ASSUMED_MONTHLY_DPU_HOURS = 160`**: this is a fixed always-runs assumption
     with NO usage evidence. The real figure is in `glue.get_job_runs` (per-run
     `ExecutionTime` × `MaxCapacity`/`DPUSeconds`), which the adapter never reads.
     A job that runs once a week for 10 minutes is priced as if it ran 160 DPU-hours.
     This is the canonical "fixed hours, no evidence" fabrication — flag it.
   - **`GLUE_RIGHTSIZE_FACTOR = 0.30`**: a reduction factor, not an exact price
     delta. Prefer `(current_DPU − target_DPU) × rate × hours` against a defensible
     target (e.g. auto-scaling floor) over a flat 30%. Flag the arbitrary factor.
   - **Region scaling**: `0.44` is us-east-1, multiplied by `ctx.pricing_multiplier`
     (constant path — single application, correct). Confirm no double-multiply.
   - **Dev-endpoint pricing**: a Glue dev endpoint bills DPUs continuously while
     READY (default 5 DPU × $0.44/hr × 730 ≈ $1,606/mo, NOT the static "$316/mo").
     Confirm the real dev-endpoint cost and that the adapter currently emits $0 for
     it (a missed real saving).
4. Confirm the basis is defensible from the report alone: each counted finding
   should record DPU count, hours assumed, rate, and factor. Add a structured
   `AuditBasis` (rate / region / DPU / hours / formula) on each counted finding.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** can one job match multiple checks? `job_rightsizing` and
   `dev_endpoints` are disjoint resource types, so no stacking — confirm. Confirm
   the `> 10` gate doesn't double-emit a job that has both `MaxCapacity > 10` AND
   `NumberOfWorkers > 10` (it appends once per job — verify).
6. **Cross-source:** none — Glue has no CoH/CO. Confirm.
7. **Cross-adapter:** Glue jobs can write to S3 and run on EC2-backed DPUs, but the
   DPU saving is Glue-owned. Confirm no other adapter (S3, EC2) double-counts the
   same Glue compute.

### Phase 4 — Coverage (works for ALL job types, not a subset)
8. Confirm pagination: `get_jobs` and `get_crawlers` ARE paginated;
   **`get_dev_endpoints` is NOT** (single call, no `NextToken` loop) — flag.
9. Are whole classes skipped?
   - The `MaxCapacity > 10 OR NumberOfWorkers > 10` gate **excludes every job with
     ≤10 DPU/workers** — a fleet of small over-provisioned jobs is invisible.
     Confirm this threshold is intentional.
   - **Streaming** jobs (always-on, the biggest spend) vs **batch** vs **Python-shell
     (G.025X)** vs **Ray** vs **Flex** — the flat $0.44 × 160 model is wrong for all
     of them. Confirm coverage/assumption per job type.
   - Jobs configured with `MaxCapacity` (legacy) vs `NumberOfWorkers` + `WorkerType`
     (current) — confirm both are handled and not conflated (DPU vs worker count).

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only path, and `return []`:
    - **Shim outer `except Exception as e:` (`get_enhanced_glue_checks`)** records
      `ctx.warn(...)` — good, but it wraps ALL of `get_jobs`/`get_dev_endpoints`/
      `get_crawlers`, so one denied API kills the others silently under a single
      generic warn. Classify `AccessDenied`/`UnauthorizedOperation` →
      `ctx.permission_issue`, other → `ctx.warn`, and consider per-API isolation.
11. Does a pricing miss fall back to `0.0` and still emit a finding? When
    `dpu_count is None` (e.g. every dev-endpoint rec, and any job missing both
    capacity fields) or non-numeric, the adapter sets `EstimatedMonthlySavings=0.0`
    + `PricingWarning` and `continue`s. Confirm these recs are surfaced as
    **advisory** (the $0 doesn't add to the total) — but note the adapter does NOT
    set `Counted=False`; confirm the reporter/exec-summary treats the $0 rec
    correctly and the count vs counted-savings split is honest.
12. **No CloudWatch / no fast-mode:** the adapter reads no metrics, so there is no
    fast-mode concern — but also no usage signal. Confirm whether a CloudWatch
    `glue.driver.aggregate.elapsedTime` / `get_job_runs` read should gate the
    saving (and if added, declare `requires_cloudwatch`/`reads_fast_mode`).

### Phase 6 — Reporting (one tab, counted == rendered)
13. **String-vs-float render desync (the important one):** Phase A renders
    `resources[0].get("EstimatedSavings")` — the **static string** ("20-40% with
    auto-scaling" / "$316/month per endpoint"). The adapter's counted total sums the
    **float** `EstimatedMonthlySavings` it computed from DPU pricing. So the card
    can display "$316/month per endpoint" while the tab total counts $0 for that
    endpoint, and display "20-40% with auto-scaling" while the total counts a precise
    DPU-derived dollar figure. Prove the desync and decide the single source of
    truth (render the computed float; drop or convert the static string).
14. **Counted == rendered:** `total_monthly_savings = savings` (sum of float
    `EstimatedMonthlySavings`); `total_recommendations = len(recs)`. Confirm the
    per-tab total equals the sum of the counted rendered findings, that advisory $0
    recs (dev endpoints, capacity-less jobs) render but contribute $0, and reconcile
    the Glue per-service total against the executive-summary headline.

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to Glue:
    `.venv/bin/python cli.py <region> --scan-only glue`
    then `.venv/bin/python tools/scan_doctor.py <json> --service glue`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage — esp. the dev-endpoint $316-string-but-$0-counted case),
    and the string-vs-float desync. Caveats: use an account with Glue jobs >10
    DPU/workers AND a READY dev endpoint (exercise both branches and the $0 path).
    Use `.venv/bin/python` (3.14).
16. For any accuracy claim, show the AWS Pricing API Glue DPU-hour value (G.1X,
    G.2X, Flex) next to the scanner's `0.44`, and show that `NumberOfWorkers` ≠ DPU
    for G.2X+.

### Deliverable
- The complete check list (Phase 1.2), with the dev-endpoint $0-vs-$316 mismatch and
  the worker-vs-DPU conflation called out.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**.
  End with an ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_glue_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the pure pricing logic
  (`MaxCapacity`/`NumberOfWorkers` resolution, None/non-numeric → $0 advisory,
  worker→DPU multiplier per WorkerType, the 0.44 × dpu × hours × 0.30 formula) and
  drive `GlueModule.scan` with a `SimpleNamespace` ctx + a fake `glue` client with
  paginators for `get_jobs`/`get_crawlers` and `get_dev_endpoints`. Cover every fix:
  worker-vs-DPU, Flex/streaming rate, dev-endpoint real pricing, hours-from-job-runs
  (if adopted), reduction-factor→delta, string/float render reconciliation, advisory
  $0 gating, silent-failure classification, dev-endpoint pagination.
- For the `ASSUMED_MONTHLY_DPU_HOURS = 160` fabrication, replace it with a real
  `get_job_runs` aggregate or keep the rec a $0 advisory — never fabricate hours.
- Record a structured `AuditBasis` (rate / region / DPU / hours / formula) on each
  counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for Glue first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `glue.py` row in `services/adapters/CLAUDE.md` to match reality.
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
  adapter → dropped silently (verify Glue isn't wired — it isn't).
- A source emitted with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC` service →
  renders nothing silently (Glue is Phase A — confirm Phase A renders all its
  sources).
- Render-time category filter desyncing headline from cards (filter at SOURCE).
- Coverage gated to a hardcoded type/tier allowlist.
- CloudWatch/Cost Explorer permission/throttle failure logged via logger only, not
  `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic assuming usage with no evidence.
- Cross-adapter overlap.
- Each counted finding must carry a structured `AuditBasis` (rate/region/metric-
  window/formula); counted == rendered.

#### Glue-specific items (found in this code)
- **`ASSUMED_MONTHLY_DPU_HOURS = 160` is fabricated**: a fixed 160 DPU-hours/mo with
  no `get_job_runs` evidence — a weekly 10-minute job is priced as if it ran 160
  DPU-hours. The single biggest accuracy risk.
- **`NumberOfWorkers` treated as DPU count**: for G.2X/G.4X/G.8X a worker is 2/4/8
  DPUs, so `dpu_count = NumberOfWorkers` understates cost 2-8× (and the saving).
- **Flat $0.44 ignores WorkerType and Flex**: G.025X (Python-shell, 0.0625 DPU) and
  Flex (~$0.29/DPU-hr) are mispriced; streaming jobs (always-on) are not modeled.
- **Dev-endpoint $316-string vs $0-counted**: the rec advertises "$316/month per
  endpoint" but the adapter prices via `MaxCapacity`/`NumberOfWorkers` (absent on
  dev-endpoint recs) → counts $0; the real READY dev-endpoint cost (≈$1,606/mo at
  5 DPU) is missed entirely.
- **String-vs-float render desync**: Phase A renders the static `EstimatedSavings`
  string while the total sums the computed float `EstimatedMonthlySavings` — the
  card and the headline can disagree.
- **`GLUE_RIGHTSIZE_FACTOR = 0.30` is an arbitrary reduction factor**, not an exact
  current→target DPU delta.
- **`> 10` DPU/worker gate excludes all small jobs**: a fleet of over-provisioned
  ≤10-DPU jobs is invisible.
- **`get_dev_endpoints` not paginated**.

## PROMPT (end)
