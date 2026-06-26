# API Gateway Adapter Cost-Audit Prompt

A deep, API-Gateway-specific audit brief in the same structure as the Lambda /
RDS / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* API Gateway code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`api_gateway`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the canonical model for the
`mark_zero_savings_advisory` / `Counted=False` metric-gated pattern and the
arch/region-scaled module constant; the **EC2** adapter
(`services/adapters/ec2.py`) for the `$0`-placeholder→`ctx.warn` pattern; the
**network** adapter (`services/adapters/network.py`) for the parse-rate / keyword
boundary and the rate-string-rejection pattern; and the recently-audited
**Lambda / RDS** test files (`tests/test_lambda_audit_fixes.py`,
`tests/test_rds_audit_fixes.py`) as the test style I expect.

### NOTE on structure (api_gateway is a thin keyword/CloudWatch adapter)
- The adapter is a **thin wrapper**: `services/adapters/api_gateway.py` →
  `ApiGatewayModule.scan` simply calls
  `get_enhanced_api_gateway_checks(ctx)` from the **legacy shim**
  `services/api_gateway.py` and sums `EstimatedMonthlySavings` over the returned
  recs, multiplied once by `ctx.pricing_multiplier`. There is exactly **one**
  helper file (`services/api_gateway.py`); there is **no** `*_logic.py`.
- The adapter emits a **single SourceBlock** named **`enhanced_checks`** — this
  is the only source. (Remember for Phase 6.)
- API Gateway consumes **neither Cost Optimization Hub nor Compute Optimizer**.
  It is not in `core/scan_orchestrator.py`'s `type_map` / `_HUB_SERVICES`, and
  pulls no `services.advisor` CO helper. A "missing CoH/CO source" finding is
  **NOT** fair game here — savings are expected to be locally derived. Drop those
  axes entirely.
- **Pricing is module constants, not `PricingEngine`.** `core/pricing_engine.py`
  has **no** API-Gateway method. The shim hardcodes `REST_PER_M = 3.50`,
  `HTTP_PER_M = 1.00`, `SAVINGS_PER_M = REST_PER_M - HTTP_PER_M = 2.50`
  (`services/api_gateway.py:14-16`), all us-east-1. Per-rec counted savings =
  `(monthly_requests / 1_000_000) * SAVINGS_PER_M` (shim line 91-93), region-
  scaled once by the adapter (`api_gateway.py:43`).
- **This service is rendered in Phase A**, not Phase B. `api_gateway` is in
  `_PHASE_A_SERVICES` (`reporter_phase_b.py:2485`) and has a
  `PHASE_A_DESCRIPTORS["api_gateway"]` entry (`reporter_phase_a.py:310`,
  `savings_mode="always"`). **Critically**, `api_gateway` is ALSO in
  `_FLAT_SAVINGS_SERVICES = {"opensearch","api_gateway","step_functions"}`
  (`html_report_generator.py:82`) — meaning when the adapter's
  `total_monthly_savings` is 0, `_calculate_service_savings` substitutes a flat
  **$50 per recommendation** (line 3076-3094). That substitution path is the
  single biggest accuracy risk in this adapter — keep it front of mind through
  Phases 2, 5, and 6.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`, find the `api_gateway.py` row (under
    "Parse-rate (5 adapters)", method "Keyword-based"). **Reconcile the doc
    against reality:** the adapter is NOT really "parse a dollar amount from
    text" — it computes a CloudWatch-`Count`-driven request delta and ALSO is
    wired to the `_FLAT_SAVINGS_SERVICES` flat-$50 path. Note any drift between
    the doc's "keyword-based" label and the actual two-number behaviour
    (real `EstimatedMonthlySavings` vs flat-$50 override).
0b. Confirm module identity in `services/adapters/api_gateway.py`:
    `key="api_gateway"`, `cli_aliases=("api_gateway",)`,
    `display_name="API Gateway"`, `reads_fast_mode=True`,
    `requires_cloudwatch=True`, `required_clients()=("apigateway","cloudwatch")`.
    Confirm the shim actually honors `ctx.fast_mode` (it does, at
    `services/api_gateway.py:71`) and that the CloudWatch read is inside that
    guard.
0c. API Gateway has **no AWS advisory source** (no CoH/CO). Focus instead on:
    pricing-tier accuracy, the flat-$50 fabrication path, REST-only coverage
    (HTTP/WebSocket invisibility), the CloudWatch `ApiName`-dimension fragility,
    and the render-string-vs-counted-number desync.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/api_gateway.py`,
   `services/api_gateway.py` (the shim — `get_enhanced_api_gateway_checks`,
   `API_GATEWAY_OPTIMIZATION_DESCRIPTIONS`, `REST_PER_M`/`HTTP_PER_M`/
   `SAVINGS_PER_M`), `services/_base.py` (`BaseServiceModule`),
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`), `core/scan_context.py`
   (`ScanContext.client`, `fast_mode`, `pricing_multiplier`, `warn`),
   `core/result_builder.py`, and the reporter:
   `reporter_phase_a.py` (`_extract_api_gateway_details` line 213,
   `PHASE_A_DESCRIPTORS["api_gateway"]` line 310, `render_grouped_by_category`
   line 390), `reporter_phase_b.py` (`_PHASE_A_SERVICES` line 2485),
   `html_report_generator.py` (`_FLAT_SAVINGS_SERVICES` line 82,
   `_calculate_service_savings` line 3076, `_get_service_content` line 3111,
   `_get_executive_summary_content` line 2795).
2. List **every** cost check and for each give: trigger condition, data source
   (apigateway describe-API, CloudWatch metric, or pure config), the exact
   `EstimatedSavings` string template, the constant/rate it embeds, whether it
   parses to a **counted** dollar (`EstimatedMonthlySavings`) or a string, and
   the emitting SourceBlock. The known inventory to confirm:
   - **`rest_vs_http`** (the ONLY emitted category): trigger = REST API with
     `resource_count <= 10` (`services/api_gateway.py:69`). Source = `get_resources`
     count + CloudWatch `Count` (`AWS/ApiGateway`, dimension `ApiName`,
     30-day Sum, `Period=2592000`). `EstimatedSavings` = literal string
     `"10-30% cost reduction for simple APIs"`; `EstimatedMonthlySavings` =
     `(monthly_requests/1e6) * SAVINGS_PER_M` if `monthly_requests > 0` else
     `0.0`. Counted via the adapter sum.
   - Confirm that `unused_stages`, `caching_opportunities`,
     `throttling_optimization`, `request_validation` exist as keys in
     `API_GATEWAY_OPTIMIZATION_DESCRIPTIONS` (`services/api_gateway.py:18-44`)
     but are **never populated** by `get_enhanced_api_gateway_checks` — the
     `checks` dict initializes them empty and nothing appends. Note the caching
     FINDING was already removed (`services/api_gateway.py:111-115`) but the
     **caching DESCRIPTION survives**, advertising "Enable API Gateway Caching …
     reduce costs" — which is the wrong cost direction (a dedicated cache ADDS
     $0.02–$3.80/hr, see Phase 2).

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate EACH rate against the live AWS Pricing API (service code
   **`AmazonApiGateway`**, region us-east-1):
   - **REST**: `usagetype USE1-ApiGatewayRequest` ("API calls received") =
     **$3.50/M for the first 333M req/mo**, then $2.80/$2.38/$1.51 in higher
     tiers. Confirm `REST_PER_M = 3.50` is the **first-tier** rate only.
   - **HTTP**: `usagetype USE1-ApiGatewayHttpRequest` ("HTTP API Requests") =
     **$1.00/M for the first 300M req/mo**, then $0.90. Confirm `HTTP_PER_M = 1.00`.
   - **Therefore the flat `SAVINGS_PER_M = 2.50/M` is correct ONLY in the first
     tier.** For a high-volume API (>333M req/mo) the true REST→HTTP delta is
     smaller (e.g. $2.80−$0.90 = $1.90, $2.38−$0.90 = $1.48), so the flat
     $2.50/M **overstates** savings for high-traffic APIs. Flag as a finding
     (tiered-pricing flat delta) and decide whether to apply the tiered schedule
     or label the number an upper bound.
   - **Region scaling**: the constants are us-east-1; the adapter multiplies the
     summed savings by `ctx.pricing_multiplier` once (`api_gateway.py:43`).
     Confirm it is applied **exactly once** (not double) and that REST/HTTP per-M
     rates barely vary across regions (so the multiplier is an approximation).
   - **Caching**: confirm against the API that "API Gateway Dedicated Cache" is
     a **cost ADDER** ($0.020/hr for 0.5GB up to $3.80/hr for 237GB,
     `productFamily "Amazon API Gateway Cache"`). The surviving
     `caching_opportunities` description claims caching "reduce[s] costs" — net
     savings depend on un-measured backend pricing. Flag the description as a
     non-cost / misleading nudge (the FINDING was already removed; the
     description should follow).
4. Confirm the savings basis is defensible from the report alone. The counted
   number depends entirely on the CloudWatch `Count` metric — record a structured
   **AuditBasis** (rate `$2.50/M`, region, 30-day `Count` window, formula) on
   each counted finding, as the Lambda/RDS audits did. A `rest_vs_http` rec with
   `monthly_requests == 0` (fast_mode, missing metric) emits
   `EstimatedMonthlySavings = 0.0` — trace what the report then shows for it
   (see Phase 5/6: the flat-$50 path).

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** only one category (`rest_vs_http`) is ever populated, and
   each REST API appears at most once (one pass over `get_rest_apis`). Confirm no
   API can be appended twice (e.g. across paginator pages with duplicate ids).
   Low risk — but confirm rather than assume.
6. **Cross-source:** none — no CoH/CO. State this explicitly and drop the axis.
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*` helper in
   `html_report_generator.py` pulls API Gateway resources into a synthetic tab,
   and that API Gateway request volume is not also counted by another adapter
   (Lambda behind the API, etc. are separate resources — confirm no overlap).

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. **REST-only coverage (the big one):** `get_enhanced_api_gateway_checks`
   paginates only `apigateway.get_paginator("get_rest_apis")` — i.e. **REST
   (v1) APIs only**. **HTTP APIs and WebSocket APIs live under the separate
   `apigatewayv2` client (`get_apis`) and are NEVER scanned.** The adapter
   docstring claims it scans "REST/HTTP APIs" — it does not. Whole API classes
   are invisible:
   - HTTP APIs ($1/M) — already cheap, but unused/over-provisioned ones still
     have cost.
   - WebSocket APIs — billed **$1/M messages + $0.25/M connection-minutes**
     (validate via the Pricing API, `productFamily "WebSocket"`,
     `USE1-ApiGatewayMessage` / `USE1-ApiGatewayMinute`); idle WebSocket APIs are
     a real cost with no check.
   Decide whether to add `apigatewayv2` coverage and whether `required_clients`
   must include `apigatewayv2`.
9. **`resource_count <= 10` gate:** only REST APIs with ≤10 resources are
   eligible for the migration rec (`services/api_gateway.py:69`). A "simple"
   high-cost REST API with 11+ resources is silently excluded. Confirm whether
   the ≤10 threshold is a defensible proxy for "HTTP-feature-compatible" or an
   arbitrary cutoff. Also confirm full pagination of both `get_rest_apis` and
   `get_resources` (the latter is a single non-paginated call at line 66 — a REST
   API with >limit resources may under-count `resource_count` and wrongly pass
   the ≤10 gate).
10. **CloudWatch `Count` availability:** the `Count` metric requires
    **Detailed CloudWatch Metrics** enabled on the stage (extra cost, off by
    default). Without it, `monthly_requests = 0` → `EstimatedMonthlySavings = 0`
    → the flat-$50 path. Confirm and document; this is a coverage-vs-fabrication
    interaction.

### Phase 5 — Silent failures (nothing fails quietly)
11. Find every `except: pass`, bare `except`, `logger`-only, and `return`
    fallback:
    - `services/api_gateway.py:108` — `except Exception: pass` wraps the
      per-API `get_resources` + CloudWatch block. A throttle/permission failure
      on `get_resources` silently drops that API with **no `ctx.warn` /
      `ctx.permission_issue`**. Classify: `AccessDenied`/`UnauthorizedOperation`
      → `ctx.permission_issue`, other → `ctx.warn`.
    - `services/api_gateway.py:88` — `except Exception: monthly_requests = 0.0`
      swallows the CloudWatch `get_metric_statistics` failure; a throttled metric
      read becomes "$0 → flat-$50", not a recorded warning.
    - `services/api_gateway.py:117-118` — the outer `except Exception as e:` DOES
      call `ctx.warn(...)` (good); confirm it classifies permission errors.
12. **Pricing/metric miss → $0 → flat-$50 fabrication (CRITICAL to trace):**
    when `total_monthly_savings == 0` (every rec had `monthly_requests == 0`,
    e.g. fast_mode or no Detailed Metrics), `_calculate_service_savings`
    (`html_report_generator.py:3076`) sees `api_gateway ∈ _FLAT_SAVINGS_SERVICES`
    and adds **$50 per recommendation** (line 3093-3094) — a number with **no
    rate, no metric, no basis**. This drives BOTH the per-tab headline
    (`_get_service_content` line 3114-3119) and the executive summary
    (`_get_executive_summary_content` line 2831, reconciliation footnote line
    2281). This is the canonical "metric-gated $0 nudge counted as a fabricated
    $" failure. Decide: such recs should be **advisory (`Counted=False`)**,
    rendered but excluded from counts — NOT silently worth $50 each.
13. **fast_mode:** confirm the CloudWatch read is fully gated on `ctx.fast_mode`
    (`services/api_gateway.py:71`) — it is. But note: under fast_mode every rec
    has `monthly_requests = 0` → flat-$50 takes over, so fast_mode *inflates*
    the headline rather than zeroing it. That interaction is the finding.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Single source / Phase A render:** the adapter emits one source
    (`enhanced_checks`). Confirm it reaches `render_grouped_by_category` via the
    `PHASE_A_DESCRIPTORS["api_gateway"]` path and that no source is silently
    unrendered. (Phase A reads `sources[*].recommendations` directly,
    `reporter_phase_a.py:411-416`.)
15. **Render-string vs counted-number desync (verify carefully):**
    `render_grouped_by_category` with `savings_mode="always"`
    (`reporter_phase_a.py:450-451`) prints
    `resources[0]["EstimatedSavings"]` = the literal **"10-30% cost reduction for
    simple APIs"** string per category — a **percentage**, NOT the counted
    dollar. Meanwhile the tab headline shows either `total_monthly_savings` (the
    real request-delta) or the flat-$50 override. So the card text, the counted
    request-delta, and the flat-$50 headline can be **three different numbers**
    for the same rec. Document this desync explicitly.
16. **Counted == rendered:** `total_recommendations = len(recs)` but
    `total_monthly_savings` sums only the request-delta. Reconcile: does the
    per-tab total equal the sum of the rendered findings' real savings, or the
    flat-$50 substitution? Confirm `_counted_advisory_counts` / the
    counted-vs-advisory split is shown, and that no rec is counted in the total
    but missing from the table (or vice-versa).

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to API Gateway:
    `python3 cli.py <region> --scan-only api_gateway`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service api_gateway`.
    Triage every: silent failure, `$0`/flat-$50 finding (separate a genuine
    request-delta saving from the $50 fabrication), and any API appearing more
    than once. Reconcile the headline against the per-rec sum. Caveats: most
    accounts lack Detailed CloudWatch Metrics (exercise the `monthly_requests=0`
    → flat-$50 path); try a region with HTTP/WebSocket APIs to prove they are
    invisible; use `.venv/bin/python` (3.14) — system `python3` lacks
    `datetime.UTC`.
18. For any accuracy claim, show the AWS Pricing API value (REST $3.50/M, HTTP
    $1.00/M, the higher tiers, WebSocket $1/M + $0.25/M-min, cache $/hr) next to
    the scanner's constant. For the flat-$50 claim, show a scan JSON where a rec
    with `EstimatedMonthlySavings: 0.0` becomes $50 in the rendered tab /
    executive summary.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked and the
  flat-$50 interaction noted.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file
  + line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add `tests/test_api_gateway_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: test the
  pure helpers directly (the request-delta formula, the tier schedule if added,
  the flat-$50 vs advisory decision) and drive `ApiGatewayModule.scan` with a
  `SimpleNamespace` ctx + fake `apigateway`/`cloudwatch` clients & paginators.
  Cover every fix: flat-$50 → advisory, REST-only coverage (or documented skip),
  tiered-pricing delta, silent-failure classification, fast_mode interaction,
  render-string vs counted-number, counted==rendered.
- For the flat-$50 fabrication: either remove `api_gateway` from
  `_FLAT_SAVINGS_SERVICES` and make `monthly_requests==0` recs **$0 advisory**
  (`Counted=False`), or replace $50 with a defensible derived number — never a
  bare constant per rec.
- Record a structured **AuditBasis** (rate / region / metric-window / formula)
  on each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for api_gateway first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same flat-$50 / silent-failure bug in a sibling adapter
  (`opensearch`, `step_functions` also in `_FLAT_SAVINGS_SERVICES`), note it as a
  follow-up (don't fix unprompted).
- Update the `api_gateway.py` row in `services/adapters/CLAUDE.md` to match
  reality (two-number behaviour, REST-only coverage, flat-$50 path).
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

### API-Gateway-specific issues to check (discovered in the code)
- **Flat-$50-per-rec fabrication:** `api_gateway ∈ _FLAT_SAVINGS_SERVICES`
  (`html_report_generator.py:82`). When `total_monthly_savings == 0`
  (fast_mode, REST API with no Detailed CloudWatch Metrics, or a throttled
  `Count` read), `_calculate_service_savings` (line 3076-3094) assigns **$50 per
  recommendation** with no rate/metric/basis, driving the per-tab headline and
  the executive summary. Must become $0 advisory (`Counted=False`), not $50.
- **REST-only coverage despite "REST/HTTP" docstring:** the shim paginates only
  `get_rest_apis` (REST v1). **HTTP APIs and WebSocket APIs (`apigatewayv2`)
  are never scanned** — whole API classes invisible; WebSocket ($1/M messages +
  $0.25/M connection-minutes) has no check at all.
- **Render-string vs counted-number desync:** Phase A renders the literal
  `EstimatedSavings = "10-30% cost reduction for simple APIs"` percentage
  (`reporter_phase_a.py:451`), while the headline shows the request-delta or the
  flat-$50 override — up to three different numbers for one rec.
- **Tiered-pricing flat delta:** `SAVINGS_PER_M = 2.50/M` uses only the
  first-tier REST ($3.50/M ≤333M) and HTTP ($1.00/M ≤300M) rates; for >333M
  req/mo the true delta is smaller ($1.90/$1.48), so the flat $2.50/M overstates
  high-volume savings (validated against the Pricing API).
- **CloudWatch `ApiName`-dimension fragility:** `Count` is queried by
  `Dimensions=[{"Name":"ApiName",...}]` only — requires Detailed CloudWatch
  Metrics (extra cost, off by default), `ApiName` is non-unique across
  stages/duplicate-named APIs, and there is no `Stage`/`Method` dimension →
  silently empty → $0 → flat-$50.
- **Non-paginated `get_resources` + `≤10` gate:** `get_resources` is a single
  call (`services/api_gateway.py:66`) that can under-count resources on a large
  REST API, wrongly passing the `resource_count <= 10` migration gate; and the
  ≤10 cutoff is an unvalidated proxy for HTTP feature compatibility.
- **Orphaned / wrong-direction descriptions:** `unused_stages`,
  `caching_opportunities`, `throttling_optimization`, `request_validation` exist
  in `API_GATEWAY_OPTIMIZATION_DESCRIPTIONS` but are never emitted; the surviving
  `caching_opportunities` description claims caching "reduce[s] costs" when a
  dedicated cache is a $0.02–$3.80/hr cost ADDER (the caching FINDING was already
  removed; the description should follow).
- **Silent per-API failure:** `except Exception: pass`
  (`services/api_gateway.py:108`) drops an API on a `get_resources`/CloudWatch
  error with no `ctx.warn` / `ctx.permission_issue`; the inner CloudWatch
  `except` (line 88) maps a throttled read to `$0` → flat-$50 rather than a
  recorded warning.

## PROMPT (end)
