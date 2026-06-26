# App Runner Adapter Cost-Audit Prompt

A deep, App Runner-specific audit brief in the same structure as the Network /
Lambda / RDS / Glue audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* App Runner code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`apprunner`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory (App Runner is
priced per **vCPU-hour** and **GB-hour**, `serviceCode=AWSAppRunner`). Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. App
Runner's saving is a **module-constant config heuristic with a fixed reduction
factor and no usage metric**, so the canonical sibling references are the
**Lambda** adapter (`services/adapters/lambda_svc.py`) for the metric-gated
`$0`-advisory discipline, the arch/constant-priced-and-region-scaled-once
pattern, and the test style I expect (`tests/test_lambda_audit_fixes.py`); the
**Glue** adapter (`services/adapters/glue.py`) for the
"fixed-hours-assumption-with-no-usage-evidence" and
"reduction-factor-instead-of-price-delta" findings; and the **EC2** adapter
(`services/adapters/ec2.py`) for the "exact current→target price delta, not a
flat factor" principle.

### NOTE on structure (App Runner is a constant-priced heuristic, NOT CoH/CO — and the shim emits ZERO recs)
- `services/adapters/apprunner.py` → `AppRunnerModule.scan` wraps the shim
  `services/apprunner.py:get_enhanced_apprunner_checks`. The shim is supposed to
  discover services and emit raw recs; the adapter does all pricing.
- **CRITICAL up-front fact to verify first:** the shim's three check buckets
  (`auto_scaling_optimization`, `instance_rightsizing`, `unused_services`) are
  initialized empty and **never populated** — the only finding that used to fire
  (Auto Scaling) was deleted (see the `# App Runner Auto Scaling Optimization
  finding removed` comment, `services/apprunner.py` ~lines 46–49). So
  `get_enhanced_apprunner_checks` returns `{"recommendations": []}` for **every**
  account, which means the adapter's elaborate dual-billing pricing loop
  (`AppRunnerModule.scan` lines ~82–99) is **dead code** and the module always
  emits `total_recommendations=0`, `total_monthly_savings=0.0`. Confirm this is
  the real behavior before judging anything downstream — it reframes most other
  findings as latent (would-fire-if-recs-existed) rather than live.
- Pricing is **module constants** in `services/adapters/apprunner.py`:
  `APP_RUNNER_VCPU_HOURLY = 0.064`, `APP_RUNNER_MEM_GB_HOURLY = 0.007`,
  `DEFAULT_ACTIVE_HOURS_PER_MONTH = 160`, `RIGHTSIZING_SAVINGS_RATE = 0.12`.
  There is NO `PricingEngine` method for App Runner (see
  `services/adapters/CLAUDE.md`: "`$0.064/vCPU/hr + $0.007/GB/hr × 730`").
  Per-rec saving (if any rec existed) =
  `(provisioned_monthly + active_monthly) × RIGHTSIZING_SAVINGS_RATE × multiplier`,
  where `provisioned_monthly = mem_gb × 0.007 × 730` and
  `active_monthly = (vcpus × 0.064 + mem_gb × 0.007) × active_hours`.
- **App Runner has NO Cost Optimization Hub source and NO Compute Optimizer
  source.** There is no `AppRunner*` `currentResourceType` in
  `core/scan_orchestrator.py:_prefetch_advisor_data.type_map`, `apprunner` is not
  in `_HUB_SERVICES`, and there is no `services/advisor.py` App Runner helper.
  Drop the CoH/CO axes — savings are expected to be locally derived.
- **CloudWatch IS read** (unlike Glue): the adapter declares
  `requires_cloudwatch=True` and `reads_fast_mode=True`, and
  `_estimate_active_hours` queries `AWS/AppRunner CpuUtilization`. But that read
  only nudges `active_hours` between `160×0.5` / `160` / `160` — it does **not**
  gate the saving and (see Phase 2/5) is almost certainly a no-op because the
  dimension is malformed.
- **Render path:** `apprunner` is NOT a Phase A service (no
  `PHASE_A_DESCRIPTORS["apprunner"]`), NOT in `_PHASE_A_SERVICES`, and NOT in
  `_PHASE_B_SKIP_PER_REC`. It emits a single `enhanced_checks` SourceBlock with
  **no** registered `PHASE_B_HANDLERS[("apprunner","enhanced_checks")]` entry.
  Because it is **not** in `_PHASE_B_SKIP_PER_REC`,
  `should_fallback_to_per_rec("apprunner")` is **True** — so the generic per-rec
  renderer handles it (a deliberate fallback, not a silent render-desync like the
  network case). Confirm this is intact in Phase 6.

### Phase 0 — Orient (5-minute map before judging)
0a. Confirm identity in `services/adapters/apprunner.py`: `key="apprunner"`,
    `cli_aliases=("apprunner",)`, `display_name="App Runner"`,
    `reads_fast_mode=True`, `requires_cloudwatch=True`,
    `required_clients()=("apprunner","cloudwatch")`.
0b. Read `services/adapters/CLAUDE.md` — App Runner is listed under Live Pricing
    as "`$0.064/vCPU/hr + $0.007/GB/hr × 730`" (module constant). Reconcile: the
    doc says "Live Pricing / Module constants" but there is no `PricingEngine`
    method; the constants are hardcoded in the adapter. Note the discrepancy.
0c. **Establish the empty-recs fact (Phase-0 priority):** read
    `services/apprunner.py:get_enhanced_apprunner_checks` in full and confirm the
    three `checks` lists are never appended to. Everything else in this audit is
    conditional on whether any rec is ever emitted — state that clearly at the top
    of your findings.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/apprunner.py` and `services/apprunner.py` in full;
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`); `core/scan_context.py`
   (`fast_mode`, `pricing_multiplier`, `client`, `warn`);
   `core/result_builder.py`; and the render path
   (`reporter_phase_b.py` dispatch helpers `should_skip_source_loop`,
   `should_use_handler`, `should_fallback_to_per_rec`, and the generic per-rec
   renderer they route to). There is no `PricingEngine` method to read for this
   service.
2. List **every** cost check, and for each give: trigger condition, data source
   (App Runner `list_services` / `describe_service` only, plus a CloudWatch
   `CpuUtilization` read that influences `active_hours`), the savings formula, the
   embedded constants, and whether it parses to a **counted** float or a **$0
   advisory**. The known inventory to confirm:
   - **`auto_scaling_optimization`** — bucket exists, **never populated** (the
     finding was removed; the inner `describe_service` loop computes nothing and
     swallows exceptions with `except Exception: pass`).
   - **`instance_rightsizing`** — bucket exists, **never populated** (no code path
     appends to it).
   - **`unused_services`** — bucket exists, **never populated** (no idle/zero-
     request detection is implemented, even though it would be the most defensible
     App Runner cost lever).
   So the live inventory is **empty**. Document the *intended* pricing math
   (dual-billing model, lines ~82–99) and the `RIGHTSIZING_SAVINGS_RATE = 0.12`
   factor as latent behavior to validate, then flag the absence of any actual
   check as the headline coverage gap.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate each constant against the live AWS Pricing API
   (`serviceCode=AWSAppRunner`, region `us-east-1`):
   - **`APP_RUNNER_VCPU_HOURLY = 0.064`**: confirm the `USE1-AppRunner-vCPU-hours`
     SKU lists **$0.064/vCPU-hour** (verified at audit-authoring time — matches).
   - **`APP_RUNNER_MEM_GB_HOURLY = 0.007`**: confirm both the active
     `USE1-AppRunner-GB-hours` SKU **and** the `USE1-AppRunner-Provisioned-GB-hours`
     SKU list **$0.007/GB-hour** (verified — both match, so reusing the same
     constant for the provisioned and active memory terms is correct).
   - **Dual-billing model correctness**: App Runner bills (a) **provisioned**
     memory at $0.007/GB-hr **24/7 while the service exists** and (b) **active**
     vCPU+memory only during request handling. The adapter models this as
     `provisioned_monthly = mem_gb × 0.007 × 730` (correct — 730 hr/mo, always-on)
     plus `active_monthly = (vcpus×0.064 + mem_gb×0.007) × active_hours`. Confirm
     the active term correctly *adds* memory at the active rate on top of the
     provisioned memory (App Runner does bill active memory separately from
     provisioned). Watch for a **double-count of memory** if active_hours
     approaches 730 — verify against the AWS billing model.
   - **`DEFAULT_ACTIVE_HOURS_PER_MONTH = 160`**: a fabricated "8 hr/day × 20
     workdays" always-runs assumption with **NO usage evidence** (the docstring
     itself admits "an invented average that didn't reflect actual traffic"). This
     is the canonical "fixed hours, no metric" fabrication — flag it. The
     `_estimate_active_hours` CW branch only scales it to `160×0.5` / `160` / `160`
     by CPU tier (5%/20% thresholds, themselves invented), so the floor is 80 hr
     and the value is never traffic-derived.
   - **`RIGHTSIZING_SAVINGS_RATE = 0.12`**: a flat 12% reduction factor applied to
     the *entire* modeled monthly cost, not an exact current→target config delta
     (e.g. 2 vCPU → 1 vCPU). Prefer `(current_config − target_config) × rate` over
     a blanket 12%. Flag the arbitrary factor (cf. Glue's `0.30`, EC2's exact
     delta).
   - **Region scaling**: the constants are us-east-1; `active_monthly` and
     `provisioned_monthly` are each multiplied by `ctx.pricing_multiplier` once
     (constant path — single application, correct). Confirm no double-multiply and
     that a non-us-east-1 scan scales correctly.
4. Confirm the basis is defensible from the report alone: each counted finding
   should record vCPU, memory, active_hours assumed, the two rates, and the 0.12
   factor. Add a structured **AuditBasis** (rate / region / active-hours-window /
   formula) on each counted finding (moot today since no rec fires, but required
   the moment a real check is added).

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** with the current empty inventory there is no stacking. If
   checks are added, ensure a single service can't be counted by both a
   rightsizing check and an unused-service check (an idle service should be
   "delete/pause" = full cost, not also "rightsize" = partial). Define the
   authority order before adding checks.
6. **Cross-source:** none — App Runner has no CoH/CO. Confirm.
7. **Cross-adapter:** App Runner services run on managed compute and may pull
   container images from ECR and route via a load balancer, but the App Runner
   compute dollar is App-Runner-owned. Confirm no other adapter (containers, ECR,
   network) double-counts the same service.

### Phase 4 — Coverage (works for ALL services, not a subset)
8. Confirm pagination: `list_services` returns `ServiceSummaryList` — verify
   whether it paginates (App Runner `ListServices` is paginated via `NextToken`);
   the shim makes a **single** `list_services()` call with **no NextToken loop**,
   so accounts with many services are truncated — flag.
9. Are whole classes skipped?
   - Only `Status == "RUNNING"` services are inspected; **PAUSED** services still
     incur the provisioned ($0.007/GB-hr 24/7) charge yet are excluded — a
     paused-but-provisioned service is the clearest real saving and is invisible.
   - **No idle / zero-request detection** at all (the `unused_services` bucket is
     empty), even though App Runner exposes `RequestCount` / `ActiveInstances` in
     CloudWatch — the most defensible cost lever is unimplemented.
   - Auto-scaling-config and concurrency-limit checks were removed entirely; the
     `describe_service` call result (`instance_config`) is fetched and then
     discarded (`_ = instance_config`).

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only path, and `return []`:
    - **`services/apprunner.py` inner `except Exception: pass`** (~line 50) around
      `describe_service` — a per-service describe failure (e.g. AccessDenied)
      vanishes with no record on `ctx`. Classify
      `AccessDenied`/`UnauthorizedOperation`/`OptInRequired` →
      `ctx.permission_issue`, other → `ctx.warn`.
    - **Outer `except Exception as e:` → `ctx.warn`** (~line 53) wraps the whole
      `list_services` loop — a denied `apprunner:ListServices` produces one generic
      warn and an empty report; classify AccessDenied → `ctx.permission_issue`.
    - **`_estimate_active_hours` `except Exception as e: → ctx.warn`** (adapter
      ~line 70): CloudWatch failure is warned (good) but then silently falls back
      to 160 hr — confirm the fallback is honest and the warn classifies
      AccessDenied as a permission issue, not a generic warn.
11. **CloudWatch dimension bug (verify carefully):** `_estimate_active_hours`
    calls `get_metric_statistics` with
    `Dimensions=[{"Name": "Service", "Value": "*"}]`. `"*"` is **not** a valid
    CloudWatch dimension wildcard — CloudWatch matches dimensions exactly, so this
    query returns **no datapoints for any account**, the `if datapoints:` branch is
    dead, and `active_hours` is **always** the `DEFAULT_ACTIVE_HOURS_PER_MONTH`
    fallback. So the CW read is pure overhead that never influences the number
    (and `requires_cloudwatch`/`reads_fast_mode` are declared for a no-op read).
    Prove this and decide: wire per-service dimensions (`ServiceName`/`ServiceID`)
    and a real `RequestCount`/`ActiveInstances`-derived active-hours, or drop the
    CW read and the `requires_cloudwatch` declaration.
12. **Fast-mode:** `_estimate_active_hours` short-circuits to 160 when
    `ctx.fast_mode` (good) — confirm. But since the CW read is a no-op anyway
    (item 11), fast vs full mode produce identical numbers; note this.
13. Does a pricing miss fall back and still emit a finding? There is no
    `PricingEngine` call to miss (constants are inline), but the `cpu_str`/`mem_str`
    parsing has bare `try/except (ValueError, IndexError)` defaults (`vcpus=1.0`,
    `mem_gb=2.0`) — confirm a malformed `InstanceConfiguration` doesn't silently
    price a service at the 1 vCPU / 2 GB default and count it as real.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Render wiring (verify):** `apprunner` emits `enhanced_checks` with no
    `PHASE_B_HANDLERS` entry, is NOT in `_PHASE_A_SERVICES`, and is NOT in
    `_PHASE_B_SKIP_PER_REC` — so `should_fallback_to_per_rec("apprunner")` is True
    and the generic per-rec renderer renders each rec. Confirm this path actually
    renders the `enhanced_checks` recs (trace `should_skip_source_loop` →
    `should_use_handler` → `should_fallback_to_per_rec`). Because the inventory is
    empty today, the tab renders **nothing** — confirm the executive summary and
    per-service total both show $0 / 0 recs for App Runner without erroring.
15. **Counted == rendered:** `total_monthly_savings = savings` (sum of per-rec
    `monthly_cost × 0.12`) and `total_recommendations = len(recs)`. Confirm the
    per-tab total equals the sum of the counted rendered findings (trivially 0
    today), and that the moment a real check is added, advisory `$0` recs render
    but contribute $0 and counted recs carry an `AuditBasis`.

### Phase 7 — Tooling & evidence
16. Run a real scan scoped to App Runner:
    `.venv/bin/python cli.py <region> --scan-only apprunner`
    then `.venv/bin/python tools/scan_doctor.py <json> --service apprunner`.
    You will almost certainly get **0 recommendations** regardless of the account
    — that itself is the headline finding; capture the empty output as evidence.
    Caveat: use an account that has a RUNNING App Runner service and a PAUSED one
    to demonstrate the PAUSED-service coverage gap and the dead pricing loop. Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC` (the adapter
    imports `from datetime import UTC`).
17. For any accuracy claim, show the AWS Pricing API App Runner vCPU-hour /
    GB-hour / Provisioned-GB-hour value next to the scanner's `0.064` / `0.007`,
    and demonstrate the `"Service": "*"` CloudWatch dimension returns no datapoints.

### Deliverable
- The complete check list (Phase 1.2) with the **empty-inventory** fact called
  out as the headline, plus the dead pricing loop, the no-op CloudWatch read, the
  fabricated 160-hour assumption, and the 0.12 reduction factor.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**.
  End with an ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_apprunner_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the pure pricing logic (vCPU/mem
  parsing, the dual-billing `provisioned + active` formula, region multiplier
  applied once, the 0.12 factor) and drive `AppRunnerModule.scan` with a
  `SimpleNamespace` ctx + a fake `apprunner` client (`list_services` /
  `describe_service`) and a fake `cloudwatch` client. Cover every fix: an actual
  emitted check (so the pricing loop stops being dead code), PAUSED-service
  coverage, the CloudWatch dimension fix (or removal), `list_services`
  pagination, silent-failure classification, fabricated-hours → metric-derived or
  $0 advisory, reduction-factor → config delta, fast-mode skip, counted==rendered.
- For `DEFAULT_ACTIVE_HOURS_PER_MONTH = 160`, replace it with a real
  `RequestCount`/`ActiveInstances` CloudWatch-derived active-hours, or keep the
  rec a `$0` advisory — never fabricate hours. Respect `ctx.fast_mode`.
- Implement at least one defensible counted check (idle/zero-request service
  delete-or-pause, priced at the real provisioned + active cost) so the adapter
  produces a saving; mark metric-gated nudges `Counted=False`.
- Record a structured **AuditBasis** (rate / region / active-hours-window /
  formula) on each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for App Runner first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `apprunner.py` row in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed.

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

#### App Runner-specific items (found in this code)
- **Shim emits ZERO recs**: `get_enhanced_apprunner_checks` initializes three
  empty buckets and never populates any of them (Auto Scaling finding deleted,
  `instance_rightsizing`/`unused_services` never implemented) — the adapter
  always returns `0` recs / `$0`, and the entire dual-billing pricing loop
  (`scan` lines ~82–99) is **dead code**. Headline finding.
- **No-op CloudWatch dimension**: `_estimate_active_hours` queries
  `Dimensions=[{"Name":"Service","Value":"*"}]`; `"*"` is not a valid CloudWatch
  wildcard, so the read returns nothing and `active_hours` is **always** the 160
  fallback — `requires_cloudwatch`/`reads_fast_mode` are declared for a read that
  never affects the number.
- **`DEFAULT_ACTIVE_HOURS_PER_MONTH = 160` is fabricated**: an "8 hr/day × 20
  workdays" always-runs average with no traffic evidence (the docstring admits
  it); the 5%/20% CPU tiers only scale it to 80/160 hr, never to real usage.
- **`RIGHTSIZING_SAVINGS_RATE = 0.12` is an arbitrary reduction factor**, not an
  exact current→target config (vCPU/GB) price delta.
- **Only `Status == "RUNNING"` inspected**: PAUSED services still incur the 24/7
  provisioned $0.007/GB-hr charge but are excluded — the clearest real saving is
  invisible.
- **`list_services` not paginated**: a single `list_services()` call with no
  `NextToken` loop truncates accounts with many services.
- **`describe_service` result discarded**: `instance_config` is fetched and thrown
  away (`_ = instance_config`) under an `except Exception: pass` — the config that
  the (now-dead) pricing loop expects (`InstanceConfiguration.Cpu/Memory`) is never
  attached to any rec.

## PROMPT (end)
