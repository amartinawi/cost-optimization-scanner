# Lightsail Adapter Cost-Audit Prompt

A deep, Lightsail-specific audit brief in the same structure as the Lambda /
RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Lightsail code path so the auditor starts from
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
> Service-specific live-audit findings for `lightsail`:
> - Two adapter-specific gaps apply (see below); also run the invariant sweeps in `_LIVE_AUDIT_LESSONS.md` and the known-issue catalogue (advisory-leak, string↔numeric agreement, flat-global rate scaling, dedup granularity, silent-failure classification).
> - E1 (silent failure): `get_enhanced_lightsail_checks` wraps all Lightsail API calls in a single `except Exception: ctx.warn()`, routing `AccessDenied`/throttle to a warning instead of `record_aws_error`; permission gaps appear as false "no resources" results.
> - C1 (flat-global rate): `LIGHTSAIL_UNUSED_STATIC_IP_HOURLY` ($0.005/hr, sourced from `USE1-UnusedStaticIP`) is multiplied by `pricing_multiplier`; verify whether Lightsail static-IP pricing is flat-global (the shim comment notes "Matches the AWS public-IPv4 charge" — same rate as EIP) — if so, the multiplier fabricates a region-specific rate for a flat charge.

You are auditing the **`lightsail`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **EC2** adapter
(`services/adapters/ec2.py`) as the canonical model for the `$0`-placeholder
pattern and exact current→target rightsizing deltas; the **Network** adapter
(`services/adapters/network.py` + `services/_savings.py`) as the model for a
us-east-1 fallback constant region-scaled via `pricing_multiplier` and for the
rate-string boundary; and the recently-audited **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the worked example for metric-gated `$0`
advisory (`Counted=False`) and the test style I expect.

### NOTE on structure (Lightsail is a Phase A, bundle-priced, no-advisory adapter)

- `services/adapters/lightsail.py` → `LightsailModule.scan` delegates collection
  to `services/lightsail.py:get_enhanced_lightsail_checks(ctx)` (the shim), then
  computes a single account total from a **hardcoded bundle-price dict**.
- Pricing is **NOT** live. The adapter comment states the previous
  `get_instance_monthly_price("AmazonLightsail", …)` path was **structurally dead
  (returned 0 every time)** because the Lightsail Pricing API keys on
  `bundle`/`bundleGroup`, not `instanceType`. The authoritative source in this
  codebase is now `services/lightsail.py:get_lightsail_bundle_cost(bundle_id)`,
  backed by the module dict **`_BUNDLE_COSTS`** with `_DEFAULT_BUNDLE_COST = 20.00`.
- **Savings accounting is unusual.** `scan()` sums
  `get_lightsail_bundle_cost(BundleId) × ctx.pricing_multiplier` for **every rec
  that carries a `BundleId`** — which is BOTH `idle_instances` AND
  `oversized_instances`. Recs without a `BundleId` (`unused_static_ips`)
  contribute **$0** to `total_monthly_savings`. Meanwhile the shim writes a human
  `EstimatedSavings` **string** on each rec (`"$X/month"`, `"$X/month potential"`,
  `"$3.65/month"`) that the renderer displays. The summed number and the displayed
  strings can disagree (see Phase 6).
- The adapter emits **per-check-type SourceBlocks** keyed by the shim's `checks`
  dict: `idle_instances`, `oversized_instances`, `unused_static_ips`,
  `load_balancer_optimization`, `database_optimization`. The last two keys exist
  but are **never populated** (no code scans Lightsail databases or load
  balancers) — they render as empty (Phase 4).
- **Lightsail is a Phase A service.** It is in `_PHASE_A_SERVICES` with a
  `PHASE_A_DESCRIPTORS["lightsail"]` entry (`extract_detail=_extract_lightsail_details`,
  `savings_mode="conditional"`, `close_div_location="inner"`,
  `fallback_category="Lightsail Optimization"`) and in `PHASE_A_INNER_CLOSE_GROUP`.
  There is NO `PHASE_B_HANDLERS` entry for lightsail. Remember this for Phase 6.
- Lightsail consumes **neither Cost Optimization Hub nor Compute Optimizer**
  (`"lightsail"` is not in `scan_orchestrator._HUB_SERVICES`; no
  `get_lightsail_compute_optimizer_recommendations` helper exists). All savings
  are local heuristics. A "missing CoH/CO source" finding is NOT fair game.

### Phase 0 — Orient (5-minute map before judging)

0a. Open `services/adapters/CLAUDE.md` and find the `lightsail.py` row. It is
    listed under **Live Pricing** as `get_instance_monthly_price("AmazonLightsail",
    ...)` / "AWS Pricing API". **This is stale/wrong:** the adapter explicitly
    abandoned that path as dead and prices from the hardcoded `_BUNDLE_COSTS` dict.
    Note the doc-vs-reality mismatch and plan to fix the row.
0b. Confirm module identity in `services/adapters/lightsail.py`: `key="lightsail"`,
    `cli_aliases=("lightsail",)`, `display_name="Lightsail"`, `required_clients()`
    returns `("lightsail",)`. The shim reads no CloudWatch — confirm
    `requires_cloudwatch`/`reads_fast_mode` are correctly absent (there is no
    metric read to gate; the oversized check is config-only, which is itself a
    finding — Phase 2).
0c. Lightsail has **no AWS advisory source**. Focus on bundle-price accuracy
    (the hardcoded dict), the OS/generation correctness of the bundle map, the
    counted-vs-displayed mismatch, coverage of Lightsail resource classes
    (databases, load balancers, disks), and the Phase A render wiring.

### Phase 1 — Understand the code (read before judging)

1. Read the full path: `services/adapters/lightsail.py`,
   `services/lightsail.py` (the shim + `LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS` +
   `_BUNDLE_COSTS` + `_DEFAULT_BUNDLE_COST` + `get_lightsail_bundle_cost` +
   `get_enhanced_lightsail_checks`), `core/contracts.py`
   (`ServiceFindings`, `SourceBlock`), `core/scan_context.py`
   (`pricing_multiplier`), `core/result_builder.py`, and the reporter
   (`reporter_phase_a.py:_extract_lightsail_details` ~line 177 +
   `PHASE_A_DESCRIPTORS["lightsail"]` ~line 283 + the Phase A render path
   `render_grouped_by_category`; `html_report_generator.py` dispatch). Confirm
   `core/pricing_engine.py:get_instance_monthly_price` exists but is unused by
   this adapter.
2. List **every** cost check and for each give: trigger, data source (describe-API
   vs pure config), the shim `EstimatedSavings` string + embedded constant, the
   adapter's contribution to `total_monthly_savings`, and whether it is effectively
   counted or display-only. The known inventory to confirm:
   - **idle_instances** (`state == "stopped"`): string
     `"${bundle_cost:.2f}/month"`; contributes **full** `get_lightsail_bundle_cost(
     BundleId) × multiplier` to the total. (Note: a *stopped* Lightsail instance is
     still billed, so deleting it is a real saving — legitimate.)
   - **oversized_instances** (`state == "running"` AND bundle id contains
     `"large"` or `"xlarge"`): string `"${bundle_cost × 0.3:.2f}/month potential"`
     — but the adapter sums the **full** bundle cost (it keys on `BundleId`
     presence, not the 0.3 factor) → **counted ≠ displayed** (Phase 6). Pure size
     heuristic, **no utilization metric** (universal known-issue: config-dimension
     savings with no usage signal).
   - **unused_static_ips** (`not attachedTo`): string `"$3.65/month"`; **no
     `BundleId`** → contributes **$0** to the counted total but displays $3.65
     (Phase 6). A static IP is only billed when unattached, so the saving is real
     but uncounted.
   - **load_balancer_optimization / database_optimization**: keys present, never
     populated → empty SourceBlocks (Phase 4 coverage gap).

### Phase 2 — Accuracy of every number (validate with MCP)

3. Re-derive each counted figure from the live AWS Pricing API (`AmazonLightsail`).
   Record a structured **AuditBasis** (rate / region / formula) per counted finding.
   - **`_BUNDLE_COSTS` are partly fabricated.** Validate each against the live
     `AmazonLightsail` `Lightsail Instance` hourly rate × the monthly cap (~744 h).
     Findings to confirm: `nano_2_0 = 3.50` matches the published $3.50 (IPv4); but
     the mid tiers look like a **synthetic ×2 geometric series** (`3.43→6.86→13.72→
     27.45→54.90`) rather than AWS list prices — e.g. the 4GB Linux bundle (the
     `medium_2_0` plan) is **$0.03225/hr ≈ $24/mo cap**, not the dict's **$27.45**
     (overstated ~14%); the 32GB Linux bundle is **$0.22043/hr ≈ $164/mo** vs the
     dict's `2xlarge_2_0 = 160.00`. Validate `micro/small/large` the same way and
     replace fabricated values with the real per-bundle monthly price.
   - **OS is ignored — Windows priced as Linux.** Lightsail Windows bundles cost
     roughly **2×** Linux (e.g. 4GB Windows `$0.05913/hr` vs Linux `$0.03225/hr`;
     16GB ComputeOptimized Windows `$0.53763/hr` vs Linux `$0.10753/hr`). The shim
     reads only `bundleId` and never `operatingSystem`/the `_win_` suffix, so a
     Windows instance is priced at the Linux dict value → understated. Confirm and
     map the OS.
   - **Generation / Windows bundle ids fall through to `$20`.** `get_instances`
     returns bundle ids like `medium_3_0` (gen-3), `medium_win_2_0` (Windows), and
     ComputeOptimized / MemoryOptimized variants — **none** are in `_BUNDLE_COSTS`
     (which only has `*_2_0` Linux standard), so they hit
     `_DEFAULT_BUNDLE_COST = 20.00`, a **fabricated flat $20** unrelated to the real
     bundle. The shim also defaults a missing `bundleId` to `"medium_2_0"`. Both
     are find­ings: a fabricated/placeholder price counted as a real saving.
   - **`pricing_multiplier` applied to a flat list price.** The adapter multiplies
     the (us-east-1-anchored) bundle price by `ctx.pricing_multiplier`, an
     EC2-derived region factor. Lightsail publishes its own per-region bundle
     prices that do NOT track the EC2 multiplier — confirm whether scaling a
     Lightsail list price by an EC2 factor is correct or introduces region error;
     prefer the real per-region Lightsail price (or document the approximation).
   - **`unused_static_ips` $3.65/month.** Validate: `AmazonLightsail`
     `Lightsail Networking` `USE1-UnusedStaticIP` is **$0.005/hr (first hour free)
     ≈ $3.65/mo** — the hardcoded string is **correct** for us-east-1, but it is a
     bare string never region-scaled and (per Phase 1) never added to the counted
     total. Confirm both.
   - **`oversized` 0.3 factor.** It is a `bundle_cost × 0.3` reduction factor with
     no target bundle and no utilization evidence — undefended (EC2 audit precedent:
     reduction factors off 2-3×). Prefer the exact `current_bundle − target_bundle`
     delta or keep it a `$0` advisory.
4. Confirm each counted number is defensible from the report alone. A bundle-cost
   "saving" with no `operatingSystem`/generation recorded, or an oversized saving
   with no utilization window, is a finding.

### Phase 3 — Duplication (no dollar counted twice)

5. **Intra-adapter.** A given instance is either `stopped` (idle) or `running`
   (oversized) — confirm the two checks are mutually exclusive on `state` and
   cannot both fire for the same instance (they branch on `instance_state`, so they
   should not, but verify the branches are exclusive and not `elif`-less duplicates).
   Confirm a static IP attached to a *stopped* instance flagged as idle is not also
   double-counted.
6. **Cross-source.** None — no CoH/CO. State this explicitly and drop the axis.
7. **Cross-adapter.** Lightsail is a self-contained walled garden (its instances,
   IPs, DBs, LBs are NOT EC2/EIP/RDS/ELB resources and do not appear in those
   adapters' describe calls) — confirm no `_extract_*` helper in
   `html_report_generator.py` pulls Lightsail resources into a synthetic tab and
   that the EC2/Network adapters do not see Lightsail static IPs.

### Phase 4 — Coverage (works for ALL Lightsail resources, not a subset)

8. Pagination. `get_instances` IS paginated (good). **`get_static_ips()` is a
   single un-paginated call** — confirm it returns all static IPs or add the
   paginator (a large account silently truncates).
9. Whole classes skipped. The shim scans only **instances** and **static IPs**.
   Lightsail bills several resource classes the adapter never touches, each a real
   cost-saving surface:
   - **Databases** (`get_relational_databases`) — billed hourly (e.g. 2GB HA
     `$0.0806/hr`, 8GB standard `$0.1546/hr`); `database_optimization` key is
     declared but never filled. Idle/stopped DBs and oversized DBs are missed.
   - **Load balancers** (`get_load_balancers`) — ~$18/mo each;
     `load_balancer_optimization` key declared but never filled; LBs with 0
     attached instances are missed.
   - **Block-storage disks** (`get_disks`) — `$0.10/GB-mo` attached; unattached
     disks are billed and never flagged.
   - **Manual snapshots** (`get_instance_snapshots` / `get_disk_snapshots`) —
     `$0.05/GB-mo`; never flagged.
   - **Container services / CDN distributions / buckets** — billed, never scanned.
   Confirm each omission is intentional/documented or a coverage finding. Also
   confirm a *running* non-large instance with low utilization is simply not
   evaluated (the oversized check is gated to `large`/`xlarge` name substrings —
   a hardcoded size allowlist, mirroring the EC2 prev-gen `t2`-only finding).

### Phase 5 — Silent failures (nothing fails quietly)

10. Enumerate every `except`/fallback in `services/lightsail.py`:
    - Outer `except Exception as e: ctx.warn(...)` around the whole scan — OK
      (recorded), but confirm `AccessDenied`/`UnauthorizedOperation` are classified
      to `ctx.permission_issue`, not a generic `ctx.warn`. A single describe
      failure (`get_instances`/`get_static_ips`) aborts the WHOLE scan via the
      single try block — so a static-IP permission gap also loses the instance
      findings. Flag the coarse-grained try as a robustness finding.
    - `get_lightsail_bundle_cost` silently returns `_DEFAULT_BUNDLE_COST` ($20) for
      any unknown bundle id — a silent fabricated price (Phase 2). It should warn
      or skip, not invent.
11. Pricing-miss → fabricated `$`. There is no Pricing API call to miss, but the
    `$20` default and the `"medium_2_0"` bundle default both fabricate a number
    that is then counted — treat as the equivalent of a `$0`/placeholder leak: a
    counted saving with no real basis.
12. CoH dropped types — N/A (Lightsail not in `type_map`/`_HUB_SERVICES`). State so.
13. Nudge-as-counted. The **oversized** rec is a "verify utilization before
    downsizing" nudge (its own `Note` says so) yet contributes the **full** bundle
    cost to the counted total. Per the Lambda precedent it should be **advisory
    (`Counted=False`/`$0`)** unless backed by a real metric, or priced as the exact
    downsize delta. Confirm and decide.

### Phase 6 — Reporting (Phase A tab, counted == displayed)

14. Render wiring. Lightsail renders via the **Phase A** path
    (`PHASE_A_DESCRIPTORS["lightsail"]`, `savings_mode="conditional"`,
    `close_div_location="inner"`), NOT `PHASE_B_HANDLERS`. Confirm
    `html_report_generator` dispatches lightsail to the Phase A renderer and that
    every populated source (`idle_instances`, `oversized_instances`,
    `unused_static_ips`) renders under the single Lightsail tab. Confirm the two
    never-populated sources (`load_balancer_optimization`, `database_optimization`)
    render nothing (empty), not a broken/blank card.
15. **Counted == displayed (the desyncs).** Three mismatches to verify:
    - `oversized`: displayed `"${cost × 0.3}/month potential"` but **full** cost is
      summed into `total_monthly_savings` → tab shows 30% while the headline counts
      100%.
    - `unused_static_ips`: displayed `"$3.65/month"` but **$0** is summed → headline
      omits a saving the card advertises.
    - `savings_mode="conditional"`: confirm what the conditional Phase A renderer
      shows for a rec whose displayed string doesn't parse to the counted number.
    Verify the per-tab total equals the sum of what is actually counted, and
    reconcile the executive-summary headline (`_get_executive_summary_content` +
    `_calculate_service_savings` + reconciliation footnote) against the per-service
    total. Decide the single source of truth: either write the per-rec dollar onto
    each rec (like the OpenSearch/EC2 adapters do) and have the renderer show THAT,
    or make the counted total equal the sum of the displayed strings.
16. No counted-but-undisplayed or displayed-but-uncounted findings (the static-IP
    and oversized cases are the displayed-but-mis-counted offenders today).

### Phase 7 — Tooling & evidence

17. Run a real scan scoped to lightsail:
    `python3 cli.py <region> --scan-only lightsail`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service lightsail`.
    Triage every: silent failure, `$0`/missing-savings finding (the static-IP
    uncounted case), fabricated `$20`/`medium_2_0` default, and resource appearing
    in >1 source. Reconcile the headline against the per-source sum. Caveats:
    Lightsail is region-scoped — try a region that actually has instances; create
    or inspect a Windows and a gen-3 (`*_3_0`) bundle to exercise the OS/generation
    miss and the `$20` default; a stopped instance exercises the idle path, an
    unattached static IP the $0-counted path. Use `.venv/bin/python` (3.14).
18. Prove each claim. For accuracy: show the AWS Pricing API value (the
    `AmazonLightsail` `Lightsail Instance` hourly × 744 for the bundle, the
    `USE1-UnusedStaticIP` $0.005/hr, a Windows-vs-Linux hourly pair) next to the
    dict constant. For the desync: show a running large instance whose card reads
    "× 0.3 … potential" while the JSON `total_monthly_savings` includes its full
    bundle cost; show an unattached static IP card reading "$3.65/month" while it
    adds $0 to the total.

### Deliverable

- The complete check list (Phase 1.2), per source, with counted-vs-displayed marked
  and the embedded constant.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** (fabricated mid-tier bundle
  prices; Windows priced as Linux; `$20`/`medium_2_0` defaults; oversized counts
  100% while displaying 30%; static IP displayed-but-uncounted) from **known
  limitations / tradeoffs** (DB/LB/disk/snapshot coverage; flat-price region
  approximation). End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)

- Add `tests/test_lightsail_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: test the
  pure helper `get_lightsail_bundle_cost` (known bundle, unknown bundle, Windows
  bundle) and drive `LightsailModule.scan` with a `SimpleNamespace` ctx + a fake
  `lightsail` client (paginated `get_instances`, `get_static_ips`, and — once
  coverage is added — `get_relational_databases`/`get_load_balancers`/`get_disks`).
  Cover every fix: corrected/real bundle prices, OS-aware pricing, removal of the
  `$20`/`medium_2_0` fabricated defaults (warn or skip instead), oversized as the
  exact downsize delta or `$0` advisory, static-IP saving counted, static-IP
  pagination, and counted == displayed.
- For any heuristic that assumes a size/utilization with no metric (oversized),
  replace with the exact bundle delta or keep it a `$0` advisory — never fabricate
  a `$`.
- Record a structured **AuditBasis** (rate / region / formula) on each counted
  finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for lightsail first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same fabricated-price / displayed≠counted bug in a sibling
  flat-rate adapter out of scope (batch / dynamodb / containers / glue), note it as
  a follow-up (don't fix unprompted).
- Update the `lightsail.py` row in `services/adapters/CLAUDE.md` to match reality
  (bundle-dict pricing, NOT `get_instance_monthly_price`).
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

<!-- LIGHTSAIL-SPECIFIC — discovered from the code -->
- **Fabricated mid-tier bundle prices (HIGH):** `_BUNDLE_COSTS` mid tiers
  (`micro 6.86 / small 13.72 / medium 27.45 / large 54.90`) are a synthetic ×2
  geometric series, not AWS list prices — e.g. the 4GB Linux bundle is
  `$0.03225/hr ≈ $24/mo cap` vs the dict's `$27.45` (~14% high); validate every
  tier against `AmazonLightsail` hourly × 744 and replace.
- **Windows priced as Linux (HIGH):** the shim reads only `bundleId`, never
  `operatingSystem`/the `_win_` suffix; Windows bundles cost ~2× Linux (4GB Windows
  `$0.05913/hr` vs Linux `$0.03225/hr`) → Windows instances understated.
- **`$20` / `medium_2_0` fabricated defaults (HIGH):** unknown bundle ids
  (gen-3 `*_3_0`, Windows `*_win_2_0`, Compute/Memory-optimized) fall to
  `_DEFAULT_BUNDLE_COST = 20.00`, and a missing `bundleId` defaults to
  `"medium_2_0"` — a fabricated price counted as a real saving; should warn/skip.
- **Counted ≠ displayed (HIGH):** `oversized` displays `"× 0.3 … potential"` but the
  adapter sums the **full** bundle cost; `unused_static_ips` displays `"$3.65/month"`
  but contributes **$0** (no `BundleId`) — the tab headline and the visible cards
  disagree in both directions.
- **Oversized is a metric-less size nudge (MEDIUM):** gated only on the bundle id
  containing `large`/`xlarge` (a hardcoded size allowlist) with no utilization
  signal, yet counted at full cost; should be the exact downsize delta or `$0`
  advisory (mirror Lambda metric-gating).
- **Whole resource classes uncovered (MEDIUM):** Lightsail **databases**
  (`get_relational_databases`, e.g. 2GB HA `$0.0806/hr`), **load balancers**
  (`get_load_balancers`, ~$18/mo), **disks** (`get_disks`, `$0.10/GB-mo`), and
  **snapshots** are never scanned — `database_optimization`/`load_balancer_optimization`
  source keys are declared but always empty.
- **`get_static_ips()` un-paginated + flat-price region scaling (LOW):** static IPs
  are fetched in a single call (truncates large accounts); the us-east-1 bundle
  prices are scaled by the EC2-derived `pricing_multiplier`, which does not track
  Lightsail's own per-region list prices.

## PROMPT (end)
