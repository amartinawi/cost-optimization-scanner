# S3 Adapter Cost-Audit Prompt

A deep, S3-specific audit brief in the same structure as the Network / Lambda /
RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* S3 code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving. S3 is
the canonical **evidence-gated CloudWatch** adapter — the worked example of how
a storage-class saving must be proven by request metrics before it is counted.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `s3`:
> - `_is_static_website_bucket` must forward `ctx` from BOTH call sites (the `get_enhanced_s3_checks` closure was missed on the first pass) to classify an AccessDenied on `GetBucketWebsite`; keep the `NoSuchWebsiteConfiguration` 'not a website' answer silent.
> - S3 is evidence-gated (not advisory-only by design): accounts where no bucket proves cold produce all-advisory cards (138 `$0` observed in one account) — verify the tab still renders, since the tab gate keys off RENDERED cards (counted + advisory), not the counted-only headline count.

You are auditing the **`s3`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat **S3 itself** as the
canonical evidence-gated-CloudWatch model: a storage-class saving is credited
only when CloudWatch S3 *request metrics* prove the data is cold, otherwise it
is a `$0` advisory. The prior S3 audit is written up in
`docs/audits/S3_AUDIT_FINDINGS.md` (findings S3-A … S3-I, status IMPLEMENTED) —
read it first; your job is to confirm those fixes still hold and find anything
new. Treat the recently-audited **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the worked example for the
`mark_zero_savings_advisory` pattern, rate-string rejection, and the test style
I expect.

### NOTE on structure (S3 is NOT shaped like a single-source adapter)
- The adapter `services/adapters/s3.py` (`S3Module.scan`) is a **two-source**
  module aggregating two helpers in `services/s3.py`:
  - `get_s3_bucket_analysis` → emits the **`s3_bucket_analysis`** SourceBlock —
    the **dedicated dollar source**. Each bucket's `SavingsDelta` is the real
    Standard→Standard-IA rate delta on its measured Standard bytes, credited
    ONLY when request metrics show zero GETs over the lookback window.
  - `get_enhanced_s3_checks` → emits the **`enhanced_checks`** SourceBlock —
    config-pattern flags (multipart, versioning, replication, server-access
    logs, empty buckets, static-website). These are **visibility-only**: every
    record carries an informational `$0.00/month - <reason>` string.
- The adapter **dedups overlapping categories**: enhanced-checks records in
  `_DEDICATED_CATEGORIES` (`Storage Class Optimization`, `Static Website
  Optimization`) are filtered OUT of the counted `other_recs` so the dollars are
  counted once by `s3_bucket_analysis` (audit L2-S3-002 / L3-S3-002).
- S3 **does** appear in Cost Optimization Hub bucketing: `core/scan_orchestrator.py`
  `_prefetch_advisor_data` lists `s3` in `_HUB_SERVICES` and maps
  `"S3Bucket": "s3"` in `type_map`, so `ctx.cost_hub_splits["s3"]` is POPULATED.
  **BUT neither `services/adapters/s3.py` nor `services/s3.py` reads
  `ctx.cost_hub_splits["s3"]`** — confirm this is a live **orphaned-bucket**
  (CoH recs fetched and silently dropped). This is a known orphan class; verify
  whether S3 CoH recommendations exist for the test account and, if so, whether
  they should be consumed/deduped or the bucket removed.
- S3 has **no Compute Optimizer** coverage (CO does not cover S3) — a "missing
  CO source" finding is not fair game.
- Pricing is **region-correct and home-region-correct**: `_s3_price_per_gb` uses
  `ctx.pricing_engine.for_region(bucket_region)` so a global bucket is priced at
  its OWN home region, not the scan region (audit S3-I). Fallback path uses
  `S3_STORAGE_COSTS` × `S3_REGIONAL_MULTIPLIERS[region][class]`.
- Savings are carried as a human **string** in `EstimatedSavings`
  (`$X.XX/month`) and parsed by `services/_savings.py:parse_dollar_savings`,
  which counts only a bare/`/month` dollar figure and **rejects per-unit rate
  strings** (`$0.01/GB`) and percentages (`30-50%`). The adapter counts buckets
  via `SavingsDelta` directly, and enhanced checks via `parse_dollar_savings`.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `s3.py` row (Live Pricing,
    `get_s3_monthly_price_per_gb()` via `PricingEngine.for_region(bucket_region)`).
    **Reconcile the doc against reality:** confirm `core/pricing_engine.py`
    exposes `get_s3_monthly_price_per_gb` (~line 665) and `for_region` (~line 379),
    and that `_SC_MAP` translates our class keys (`GLACIER_FLEXIBLE_RETRIEVAL` →
    `GLACIER`, `GLACIER_INSTANT_RETRIEVAL` → `GLACIER_IR`) to the engine's keys.
0b. Confirm module identity in `services/adapters/s3.py`: `key="s3"`,
    `cli_aliases=("s3",)`, `display_name="S3"`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`, `required_clients()` → `("s3", "cloudwatch")`.
0c. Note the count-hygiene contract: `total_recommendations` =
    `savings_bearing_buckets + savings_bearing_enhanced` (only `$>0` records);
    `advisory_count` is carried in `extras` for visibility but NOT in the
    headline (audit S3-C). Every $0 record stays in its SourceBlock for the
    report but must not inflate the count.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the S3 path: `services/adapters/s3.py`,
   `services/s3.py` (the two helpers + `_s3_price_per_gb`, `_cost_from_class_sizes`,
   `_assess_bucket_coldness`, `_classify_opportunities`, `_calculate_s3_storage_cost`,
   `_is_static_website_bucket`, `_route_bucket_error`, `_is_access_denied`,
   `_is_endpoint_unreachable`, `_bucket_cloudwatch_client`, `_mark_region_dead`);
   `services/_savings.py` (`parse_dollar_savings`); `core/contracts.py`;
   `core/pricing_engine.py` (S3 methods, `for_region`); `core/scan_orchestrator.py`
   (`_prefetch_advisor_data`, `_HUB_SERVICES`, `type_map`, the orphan warning);
   `core/result_builder.py`; and the reporter
   (`reporter_phase_b.py:_render_s3_bucket_analysis` ~line 693,
   `_render_s3_enhanced_checks` ~line 810, `render_s3_top_tables` ~line 1879,
   `_render_generic_s3_rec` ~line 1660, `PHASE_B_HANDLERS` ~line 2429;
   `html_report_generator.py` dispatch ~line 3297, `render_s3_top_tables` append
   ~line 3365).
2. List **every** cost check across both sources, and for each give: trigger
   condition, the data source (S3 describe-API, CloudWatch `BucketSizeBytes` /
   `GetRequests`, or pure config heuristic), the exact `EstimatedSavings` string
   template, the constant/rate it embeds, and whether that string parses to a
   **counted** dollar or a **$0 advisory**. The known inventory to confirm:
   - `s3_bucket_analysis`: per-bucket evidence-gated Standard→Standard-IA delta
     (counted only on `coldness=="cold"`); static-website ($0 advisory); gap +
     no-evidence ($0 advisory); fully-optimized ($0).
   - `enhanced_checks`: multipart uploads, lifecycle-missing (Static Website
     Optimization / Storage Class Optimization), versioning growth, cross-region
     replication, server-access logs, empty/unused buckets, static-website — all
     `$0.00/month - <reason>` informational.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** savings figure, re-derive it from the live AWS Pricing
   API and confirm it matches:
   - **Service code `AmazonS3`**, attribute `storageClass` / `volumeType`,
     `location` = bucket home region. Validate the `STANDARD` ($0.023/GB-mo
     us-east-1) and `STANDARD_IA` ($0.0125/GB-mo) rates, and that the counted
     delta is `STANDARD − STANDARD_IA` on the **measured Standard bytes only**
     (`bucket_info["StandardGB"]`), NOT on total bytes (audit S3-A). Confirm a
     bucket already in Glacier/Deep-Archive is priced at its OWN class rate via
     `_cost_from_class_sizes`, not Standard.
   - **Home region, not scan region** (audit S3-I): confirm `for_region(bucket_region)`
     is used for both cost and the savings delta. A non-us-east-1 bucket scanned
     from us-east-1 must price at the bucket's region.
   - **Fallback path**: when `pricing_engine is None`, `_s3_price_per_gb` falls
     back to `S3_STORAGE_COSTS[class] × S3_REGIONAL_MULTIPLIERS[region][class]`.
     Confirm the multiplier dict is region-scaled (NOT us-east-1 flat) and that
     `pricing_multiplier` is NOT *also* applied (it is deliberately `del`'d in
     `get_s3_bucket_analysis` — the regional-multiplier dict is the region scale,
     so double-application would be a bug). Spot-check 3 multipliers against the
     live region/us-east-1 ratio.
   - **Deterministic filter**: confirm `get_s3_monthly_price_per_gb` pins a single
     SKU per storage class (no `MaxResults=1` over multiple matching SKUs).
4. Confirm the savings basis is defensible from the report alone: each counted
   bucket finding carries `PricingBasis` (`{StandardGB} GB in S3 Standard ×
   ${delta}/GB Standard→Standard-IA delta; 0 GET requests over {COLD_LOOKBACK_DAYS}d`).
   Record/confirm a structured **AuditBasis** (rate / region / metric-window /
   formula) on each counted finding, as the Lambda/RDS audits did. A storage
   saving emitted with no `AccessSignal=="cold"` evidence is a finding.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter / per-class dedup:** confirm the enhanced-checks
   `_DEDICATED_CATEGORIES` filter (`Storage Class Optimization`, `Static Website
   Optimization`) actually removes those from the counted `other_recs` so the
   same bucket's lifecycle/static-website opportunity is not counted by BOTH
   `s3_bucket_analysis` AND `enhanced_checks`. Prove the filter set matches the
   category strings emitted by `get_enhanced_s3_checks` exactly (a renamed
   category would silently re-introduce double-counting).
6. **Cross-source (Cost Hub):** `ctx.cost_hub_splits["s3"]` is populated but
   unconsumed. If you make the adapter consume it, dedup by NORMALIZED bucket id
   with authority **CoH > heuristic** (a bucket surfaced by CoH must not also be
   counted by `s3_bucket_analysis`). Confirm `core/result_builder.py` does not
   blindly sum across sources.
7. **Cross-adapter:** confirm no `_extract_*` helper in `html_report_generator.py`
   pulls S3 buckets into a synthetic tab, and that a static-website bucket's
   "CloudFront CDN" advisory is not double-counted by the CloudFront adapter.

### Phase 4 — Coverage (works for ALL buckets, not a subset)
8. Confirm `list_buckets` enumerates ALL buckets (no pagination needed — S3
   returns all in one call) and that per-bucket location resolution handles
   `LocationConstraint is None` → `us-east-1`. Confirm the **full-mode** path
   reads ALL six `_CW_STORAGE_TYPE_TO_CLASS` storage types per bucket, and that
   the cost sums each class at its own rate.
9. Are whole classes skipped? Confirm: Intelligent-Tiering buckets are detected
   (`HasIntelligentTiering`); versioning/replication/logging checks fire on the
   relevant config; the `_GAP_OPPORTUNITY_CLASSES` allowlist
   (`both_missing` / `lifecycle_missing` / `intelligent_tiering`) is the gate for
   an evidence-gated saving — confirm a bucket WITH a lifecycle policy but still
   holding cold Standard bytes is intentionally excluded (it already has the
   lever). Confirm fast-mode (`MaxKeys=100` sample) never produces a counted
   saving (it must be advisory — sample size is unreliable).

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return []`
    fallback. Specifically:
    - `_route_bucket_error` routes `AccessDenied` / `AllAccessDisabled` / `403`
      to `ctx.permission_issue` and everything else to `logger.debug`. Confirm
      EVERY bucket-level call (location, lifecycle, intelligent-tiering, website,
      versioning, replication, logging, multipart, list-objects, **and the
      `s3:GetMetricsConfiguration` / `GetRequests` reads in `_assess_bucket_coldness`**)
      routes through it. The coldness assessment needs `s3:GetMetricsConfiguration`
      — confirm a denial there surfaces as a permission_issue, not a silent
      `"unknown"` → $0 advisory that hides a real (unmeasurable) opportunity.
    - The top-level `get_s3_bucket_analysis` / `get_enhanced_s3_checks` handlers
      route `s3:ListAllMyBuckets` denial to `ctx.permission_issue`.
    - `_mark_region_dead` / `_is_endpoint_unreachable`: a dead CloudWatch region
      short-circuits size metrics with one `ctx.warn` — confirm it does not emit
      a counted saving for buckets it could not measure.
11. Does a pricing miss fall back to `0.0`/blank and still emit a counted
    finding? A counted finding with `$0` is a bug — it must be advisory. Confirm
    `_s3_price_per_gb` returning 0 (engine price `<= 0`) falls through to the
    constant, and that a `delta <= 0` yields `SavingsDelta = 0.0` → advisory.
12. **CloudWatch gating / fast-mode:** confirm `requires_cloudwatch` /
    `reads_fast_mode` are declared (they are) and that `fast_mode` skips the
    expensive per-bucket × 6-class `BucketSizeBytes` reads AND the coldness
    assessment (`not fast_mode` guard on the savings branch). Confirm a
    throttle/permission failure on the metric reads is recorded on `ctx`.
13. Are opt-in / "enable X" nudges (`enable S3 Storage Class Analysis`,
    `enable request metrics`) emitted as `$0` records that inflate the count?
    They must stay advisory ($0, excluded from `total_recommendations` by the
    `parse_dollar_savings(...) > 0` gate). Confirm `advisory_count` accounts for
    every one.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Handler wiring:** both sources have registered handlers —
    `("s3","s3_bucket_analysis") → _render_s3_bucket_analysis` and
    `("s3","enhanced_checks") → _render_s3_enhanced_checks`. `s3` is in
    `_PHASE_B_SKIP_PER_REC` (no per-rec fallback). Confirm BOTH handlers fire
    (a source with no registered handler in a skip-per-rec service renders
    nothing silently), and that `render_s3_top_tables(service_data)` is appended
    (html_report_generator ~line 3365) for the top-cost / top-size tables.
15. **Counted == rendered:** `_render_s3_bucket_analysis` groups by opportunity
    class and shows `EstimatedSavings` strings; the headline counts only
    `SavingsDelta > 0` and `parse_dollar_savings > 0`. Confirm the per-tab
    counted total equals the sum of the COUNTED rendered findings, that advisory
    cards are visible but contribute $0, and that the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings` +
    reconciliation footnote) reconciles against the per-service total. Confirm
    the source-type label override `("s3","enhanced_checks") → "Audit Based"`
    (not the generic "Metric Backed") still matches the no-CloudWatch nature of
    enhanced checks (audit L3-S3-003).
16. Confirm no finding is counted in `total_recommendations` /
    `total_monthly_savings` but dropped from the table (or vice-versa), across
    both sources and all `CheckCategory` groups, including the
    `_DEDICATED_CATEGORIES` filtered records (visible, $0, uncounted).

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to S3:
    `python3 cli.py <region> --scan-only s3`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service s3`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and bucket appearing in >1 source/tab. Reconcile the
    headline against the per-source sum. Caveats: try a second region if the
    first is sparse; exercise both the full-mode (CloudWatch) and `--fast-mode`
    paths; ensure at least one bucket has request metrics enabled to exercise the
    `cold`/`warm` branches and one without to exercise the `$0 advisory` branch;
    a cross-region bucket exercises the `for_region` home-region path. Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
18. For any duplication claim, prove it: show the same bucket in two sources or
    counted twice (the canonical example: a `lifecycle_missing` bucket counted by
    both `s3_bucket_analysis` and an enhanced `Storage Class Optimization`
    record). For any accuracy claim, show the AWS Pricing API value (Standard /
    Standard-IA per-GB rate at the bucket's region) next to the scanner's delta.

### Deliverable
- The complete check list (Phase 1.2), per source, with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved. Cross-reference `docs/audits/S3_AUDIT_FINDINGS.md` (S3-A…S3-I) and
  flag any regression of an already-fixed finding.

### Implementation (only after I approve)
- Extend `tests/test_s3_adapter.py` (and `tests/test_pricing_engine.py` for rate
  lookups), mirroring `tests/test_lambda_audit_fixes.py`: test the pure helpers
  directly (`parse_dollar_savings` boundaries, `_classify_opportunities`,
  `_s3_price_per_gb` home-region selection, `_cost_from_class_sizes` per-class
  pricing, the `_DEDICATED_CATEGORIES` dedup, count hygiene) and drive
  `S3Module.scan` with a `SimpleNamespace` ctx + monkeypatched helpers + fake
  boto3 clients/paginators for the describe / CloudWatch paths. Cover every fix:
  evidence-gated savings, fallback region scaling, home-region pricing,
  per-class cost, coldness gating, fast-mode advisory, render wiring,
  counted==rendered, advisory $0 gating, Cost-Hub orphan resolution.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for s3 first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-failure / pricing / dedup bug in a sibling adapter
  out of scope, note it as a follow-up (don't fix unprompted).
- Update the `s3.py` row in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (size/class) with NO usage metric
  → fabricated $. (S3: a storage-class saving needs proven-cold request metrics.)
- Wrong storage-class/region pricing; bucket priced at scan region not home region.
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`,
  OR double-applied on an already-region-correct engine path.
- Per-unit RATE string (`$/GB`, `$/request`) counted as a monthly total —
  rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier/free allotment recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic by NORMALIZED id.
- Two heuristic checks stacking on one resource, or SUBSET redundancy — fix by removal.
- Reduction factor instead of exact price delta (price×factor vs current−target).
- $0 "enable X"/opt-in placeholder counted instead of converted to `ctx.warn` and dropped.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (`Counted=False`).
- Cost Hub: (a) `currentResourceType` with no `type_map` bucket → dropped (warns
  only on full scan); (b) bucket populated but consumed by NO adapter → dropped
  silently (known orphans: elasticache/opensearch/redshift/s3 — **verify the s3
  bucket IS consumed; today it is not**).
- A source emitted with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC` service
  → renders nothing silently.
- Render-time substring/category filter desyncing headline from cards (filter at SOURCE).
- Coverage gated to a hardcoded class/type/state allowlist.
- CloudWatch/Cost Explorer permission/throttle failure logged via logger only,
  not `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic assuming a usage target ("shrink to 20GB") with no usage evidence.
- Cross-adapter overlap (same bucket/volume in two tabs).
- Fixed per-rec estimate treated as a realized saving rather than advisory.
- Each counted finding must carry a structured AuditBasis (rate/region/metric-window/formula); counted == rendered.

#### S3-specific items
- **Cost-Hub orphan (live):** `ctx.cost_hub_splits["s3"]` is populated by
  `_prefetch_advisor_data` (`S3Bucket → s3`) but no adapter consumes it — S3 CoH
  recommendations are fetched and silently dropped. Decide: consume + dedup, or
  remove `s3` from `_HUB_SERVICES`/`type_map`.
- **`_DEDICATED_CATEGORIES` string drift:** the dedup filter matches category
  strings (`"Storage Class Optimization"`, `"Static Website Optimization"`) by
  exact value; if `get_enhanced_s3_checks` renames a category the filter silently
  stops deduping and the bucket is double-counted. Pin the strings to shared
  constants and test the join.
- **Coldness false-negative:** `_assess_bucket_coldness` requires a *whole-bucket*
  request-metrics filter (no `Filter` key). A bucket with only prefix-scoped
  metric filters returns `"unknown"` → $0 advisory even if globally cold;
  confirm this is intended (conservative) and surfaced, not a silent miss.
- **Fast-mode sample cost:** the `MaxKeys=100` fast-mode sample drives
  `EstimatedMonthlyCost` with a "may be significantly understated" warning;
  confirm this estimate never feeds a counted `SavingsDelta` (it must not — the
  savings branch is `not fast_mode`-gated).
- **Express One Zone / Intelligent-Tiering monitoring fee:** `S3_STORAGE_COSTS`
  carries `EXPRESS_ONE_ZONE` ($0.11) and a `$0.0025` IT monitoring fee that are
  not currently part of any counted saving; confirm no check silently prices an
  Express-One-Zone bucket at the Standard delta.
- **`GLACIER` key drift in `S3_REGIONAL_MULTIPLIERS`:** newer regions use
  `"GLACIER"` while older use `"GLACIER_FLEXIBLE_RETRIEVAL"`; a missed lookup
  falls through to 1.0. Confirm only STANDARD/STANDARD_IA multipliers materially
  affect the counted delta (they do), so the drift is cosmetic — but verify.

## PROMPT (end)
</content>
</invoke>
