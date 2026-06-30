# CloudFront Adapter Cost-Audit Prompt

A deep, CloudFront-specific audit brief in the same structure as the Lambda /
RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* CloudFront code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `cloudfront`:
> - This service is (largely) ADVISORY-ONLY — verify it still renders a TAB despite `$0` counted savings (the tab gate keys off RENDERED cards, counted + advisory, not the counted-only headline count), and confirm no `Counted=False` rec carries a non-zero `EstimatedMonthlySavings` (advisory-leak).
> - The outer `except Exception: ctx.warn()` in `services/cloudfront.py` around `list_distributions` (line 131) does not route `AccessDenied`/throttle through `record_aws_error` — a distribution-enumeration failure silently yields zero recs rather than a `permission_issue` record (E1).

You are auditing the **`cloudfront`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **network** adapter
(`services/adapters/network.py`) as the canonical model for the parse-rate /
rate-string boundary and the `$0` advisory pattern; the **S3** /
**file_systems** adapters for the CloudWatch-evidence-gating pattern (a saving
that depends on measured bytes must be backed by a CloudWatch metric or demoted
to a `$0` advisory); and the recently-audited **Lambda** adapter for the
metric-gated `$0` advisory + test style I expect (`tests/test_lambda_audit_fixes.py`).

### NOTE on structure (cloudfront is a Phase A, single-source, currently-$0 adapter)
- `services/adapters/cloudfront.py` → `CloudfrontModule.scan` consults ONE helper,
  `services/cloudfront.py:get_enhanced_cloudfront_checks`, and emits a **single
  `enhanced_checks` SourceBlock**. There is no second source.
- **It consumes neither Cost Optimization Hub nor Compute Optimizer.** CloudFront
  is not in `core/scan_orchestrator.py`'s `_HUB_SERVICES` / `type_map`, and pulls
  no CO helper. A "missing CoH/CO source" finding is NOT fair game here.
- **CloudFront is a global service** (`required_clients()` returns `("cloudfront",)`;
  the client routes through the global-service path). Distribution pricing
  (data-transfer-out) is **tiered + per-edge-region**, not region-scaled by
  `ctx.pricing_multiplier`.
- **The adapter currently counts $0 for everything.** `CloudfrontModule.scan`
  sets `EstimatedMonthlySavings = 0.0` on every rec and attaches a
  `PricingWarning` ("requires CW BytesDownloaded metric and distribution
  PriceClass for quantified savings"); `total_monthly_savings` is hard-`0.0`.
  This is a deliberate "honest $0 advisory" stance after a prior flat-$0.10/GB +
  fictional-0.5KB/request estimator was removed (see the code comment ~lines
  41–48). The unused-distribution branch (`CheckCategory == "CloudFront Unused
  Distribution"`) is also set to `$0` — but note the helper never actually emits
  that category (see Phase 1).
- **Doc-vs-reality conflict to resolve in Phase 0:** `services/adapters/CLAUDE.md`
  lists cloudfront under **Parse-rate** as "**Fixed $25/rec**". The code does NOT
  parse a rate and does NOT use $25 — it emits $0. Reconcile and flag the doc.
- Rendering is **Phase A**, not Phase B: `cloudfront` ∈ `_PHASE_A_SERVICES`, with a
  `PHASE_A_DESCRIPTORS["cloudfront"]` descriptor (`savings_mode="always"`,
  `_extract_cloudfront_details`). There is NO `("cloudfront", …)` entry in
  `PHASE_B_HANDLERS`; the source is rendered by `render_grouped_by_category`.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`, find the `cloudfront.py` Parse-rate row
    ("Fixed $25/rec"). **Reconcile against reality:** the adapter neither parses a
    dollar amount nor applies $25 — `scan` forces `EstimatedMonthlySavings=0.0`
    on every rec and a `PricingWarning`. Decide whether the doc is stale (most
    likely) or whether a $25 fallback was intended; either way the row must be
    corrected to "$0 advisory (honest; needs CW BytesDownloaded + PriceClass)".
0b. Confirm module identity in `services/adapters/cloudfront.py`: `key="cloudfront"`,
    `cli_aliases=("cloudfront",)`, `display_name="CloudFront"`,
    `required_clients()=("cloudfront",)`. Note it does **NOT** declare
    `requires_cloudwatch` / `reads_fast_mode` even though
    `get_enhanced_cloudfront_checks` reads CloudWatch (`AWS/CloudFront` `Requests`
    and `CacheHitRate`) AND calls `cloudfront` per-distribution — flag this
    (fast-mode is not honored; CW reads run unconditionally).
0c. CloudFront has **no AWS advisory source** (no CoH/CO). Savings must be locally
    derived from PriceClass + measured traffic. Focus on: the $0/advisory
    boundary, the dead/empty check categories, the fictional-request comment, the
    un-gated CloudWatch reads, and the swallowed CW exceptions.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/cloudfront.py` (`CloudfrontModule.scan`,
   the `optimization_descriptions` block); `services/cloudfront.py`
   (`get_enhanced_cloudfront_checks`); `core/contracts.py`;
   `core/scan_orchestrator.py` (confirm cloudfront is absent from `_HUB_SERVICES`);
   `core/result_builder.py`; and the reporter
   (`reporter_phase_a.py:_extract_cloudfront_details` ~line 131,
   `PHASE_A_DESCRIPTORS["cloudfront"]` ~line 274, `render_grouped_by_category`;
   `reporter_phase_b.py:_PHASE_A_SERVICES`, `_GENERIC_SOURCE_TYPES` /
   `source_type_badge` for the `enhanced_checks → Metric Backed` badge;
   `html_report_generator.py` Phase A dispatch ~lines 3297–3313).
2. List **every** cost check the helper emits and, for each: trigger condition,
   data source (`cloudfront` describe-API vs `AWS/CloudFront` CloudWatch), the
   `EstimatedSavings` string, and whether it is **counted** or **$0 advisory**.
   The known inventory to confirm in `get_enhanced_cloudfront_checks`:
   - `checks["price_class_optimization"]` (`CheckCategory == "CloudFront Price
     Class Optimization"`): the ONLY category actually populated. Gated on
     `PriceClass_All` AND `Enabled` AND `> 1000` requests/week (CW `Requests`,
     7d). `EstimatedSavings = "20-50% on data transfer costs for regional
     traffic"` — a **percentage string**, not a dollar figure. The adapter
     overwrites it to `EstimatedMonthlySavings = 0.0` + `PricingWarning`.
   - `checks["low_traffic_distributions"]` and `checks["origin_shield_unnecessary"]`:
     initialized as empty lists and **NEVER appended to** — the disabled-distribution
     and origin-shield findings were deliberately removed (comments ~lines 83–85
     and ~130–132). Confirm they are dead/empty and that the origin-shield CW math
     (`CacheHitRate`, `Requests`) is fully computed and then discarded
     (`_ = (should_check_origin_shield, origins)`) — wasted API calls.
   - The adapter references `CheckCategory == "CloudFront Unused Distribution"`
     (`scan` ~line 56) but the helper never emits that category → confirm this is
     a dead branch in the adapter.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Because the adapter counts **$0**, the accuracy question is "would the
   *intended* number be defensible if re-enabled?" Validate the building blocks so
   a fix is grounded:
   - **CloudFront data-transfer-out is tiered + per-edge-location-group**, NOT a
     flat rate. Validate with the AWS Pricing MCP (`service_code="AmazonCloudFront"`,
     region omitted — CloudFront is global): US/EU first 10 TB ≈ **$0.085/GB**,
     next 40 TB ≈ **$0.080/GB**; Asia/other tiers higher (the adapter's own
     comment ~lines 41–44 cites these). Confirm the tier boundaries and the
     PriceClass→edge-region mapping (`PriceClass_100` = US/EU/IL only,
     `PriceClass_200` adds more, `PriceClass_All` = everywhere).
   - The removed estimator's two errors are the cautionary tale: a **flat
     $0.10/GB** (wrong — ignores tiering/region) and a **fictional 0.5 KB/request**
     size assumption (fabricated bytes with no `BytesDownloaded` metric). Any
     re-enabled saving MUST be `(current_priceclass_blended_rate −
     target_priceclass_blended_rate) × measured_GB`, where `measured_GB` comes
     from the `AWS/CloudFront` `BytesDownloaded` metric — never from request count
     × an assumed object size.
   - A PriceClass downgrade only saves money on the *fraction* of traffic served
     from the now-excluded edge regions; a blended-rate delta on total bytes
     overstates it. Note this as the calibration risk for any fix.
4. Confirm the savings basis would be defensible from the report alone: a
   re-enabled finding must record PriceClass (current→target), the
   measured-bytes window, and the tier rates used (a structured `AuditBasis`).
   Today there is none because everything is $0 — confirm the `PricingWarning`
   string is the only basis and that it renders.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** only one category is populated and it is $0, so there is no
   stacking today. Confirm that if `low_traffic_distributions` /
   `origin_shield_unnecessary` are ever re-enabled, the same distribution cannot
   be counted under price-class AND low-traffic AND origin-shield (subset
   overlap). Decide single ownership per distribution.
6. **Cross-source:** N/A — no CoH/CO. State this explicitly.
7. **Cross-adapter / synthetic tabs:** confirm no `_extract_*`/`_extract_…`
   helper in `html_report_generator.py` pulls CloudFront distributions into a
   synthetic tab, and that CloudFront data-transfer is not also attributed by the
   network or monitoring adapters.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Pagination: `get_enhanced_cloudfront_checks` paginates `list_distributions`
   (`get_paginator`). Confirm it covers all distributions, and that
   `get_distribution_config(Id=dist_id)` is called per distribution (it is — note
   the extra API cost).
9. Hardcoded gates that silently exclude valid resources:
   - The price-class check only fires for `PriceClass_All` — a distribution on
     `PriceClass_200` that could drop to `PriceClass_100` is never considered.
     Confirm intentional.
   - `> 1000` requests/week threshold and `Enabled` gate — low-but-nonzero-traffic
     distributions are skipped. Confirm.
   - Disabled distributions: explicitly excluded ($0 data-transfer when disabled)
     — but a *disabled-and-still-billed* edge case (in-progress, or per-request
     invalidation/field-level-encryption costs) is not considered. Note as a
     coverage limitation, not necessarily a bug.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return`
    fallback. Specifically in `services/cloudfront.py`:
    - The inner `except Exception: pass` around the `Requests` CW read (~line 80)
      and around the `CacheHitRate`/`Requests` origin-shield reads (~line 127)
      **swallow CloudWatch failures silently** — a throttled/denied CW call drops
      the price-class recommendation with NO `ctx.warn`/`ctx.permission_issue`.
      Classify AccessDenied/Unauthorized → `ctx.permission_issue`, throttling/other
      → `ctx.warn`.
    - The per-distribution `get_distribution_config` `except Exception as e:` (~line
      133) and the outer `except Exception as e:` (~line 136) DO call `ctx.warn` —
      confirm they classify AccessDenied as a permission issue (currently a plain
      warn).
    - In the adapter, `scan` wraps the helper in try/except → `ctx.warn` (OK).
11. Pricing miss → `$0`: today EVERY finding is `$0` by design, which the spec
    flags as a smell ("a finding with $0 counted savings is a bug — it must be
    advisory or skipped"). Confirm these $0 recs are rendered as **advisory**
    (visible, not counted) via the Phase A `savings_mode="always"` path — and that
    they are NOT inflating any counted total. If a distribution genuinely has no
    quantifiable saving, decide whether it should render at all or be dropped.
12. **CloudWatch gating / fast-mode:** the adapter declares neither
    `requires_cloudwatch` nor `reads_fast_mode`, yet the helper issues multiple
    `AWS/CloudFront` CW reads per distribution unconditionally. Confirm these are
    NOT skipped under `ctx.fast_mode` (a real finding — mirror the Lambda/network
    fast-mode fix) and that failures are recorded on `ctx`.
13. Opt-in / `$0` nudges: the price-class rec is effectively an "enable/review"
    nudge with a percentage string and $0 value. Confirm it lands as advisory
    (Counted=False / not summed), not as a counted opportunity. Confirm the dead
    `origin_shield`/`low_traffic` structures are not re-introduced as $0 counted
    noise.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Source → renderer:** the single `enhanced_checks` source is rendered via
    **Phase A** (`should_skip_source_loop("cloudfront")` is True;
    `PHASE_A_DESCRIPTORS["cloudfront"]` → `render_grouped_by_category` with
    `_extract_cloudfront_details`). Confirm there is intentionally NO
    `PHASE_B_HANDLERS` entry and that the Phase A path actually renders the
    price-class recs (and the `PricingWarning`). Confirm the
    `enhanced_checks → "Metric Backed"` source badge
    (`_GENERIC_SOURCE_TYPES`) is appropriate given the recs are CW-gated but
    $0 — consider whether "Audit Based" is more honest (compare the S3 override
    `("s3","enhanced_checks") → "Audit Based"`).
15. **Counted == rendered:** `total_recommendations = len(recs)` but
    `total_monthly_savings = 0.0` always. Confirm the per-tab headline shows
    "N recommendations, $0 counted" (advisory) and does NOT claim a dollar figure
    the cards don't support. Reconcile the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings`) — CloudFront
    should contribute $0 to the grand total while still listing its distributions.
16. Confirm no finding is counted in `total_recommendations` but dropped from the
    Phase A table (or vice-versa), and that `savings_mode="always"` does not
    accidentally print a non-$0 figure from a leftover `EstimatedSavings`
    percentage string.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to cloudfront:
    `.venv/bin/python cli.py <region> --scan-only cloudfront`
    then pass the JSON through
    `.venv/bin/python tools/scan_doctor.py <json> --service cloudfront`.
    Triage every: silent failure (swallowed CW exception), `$0` finding (all of
    them — confirm they are honest advisories, not leakage), and any cross-tab
    overlap. Caveats: CloudFront is global, so the region arg barely matters; you
    need a distribution on `PriceClass_All` with >1000 requests/week to populate
    the only live category. Confirm `--fast` does NOT (today) skip the CW reads —
    that is itself a finding. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC` (note `services/cloudfront.py` imports `UTC` directly).
18. For any accuracy claim (about a re-enabled saving), show the AWS Pricing API
    CloudFront DTO tier rates next to the proposed formula and the
    `BytesDownloaded`-derived GB. For the $0 stance, show that no counted dollar
    is emitted anywhere in the JSON.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked (all $0
  advisory today) and the dead/empty categories called out.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs** (the honest-$0 stance is a tradeoff, not a bug). End with a short,
  ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add a `tests/test_cloudfront_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: drive `CloudfrontModule.scan` with a
  `SimpleNamespace` ctx + monkeypatched `get_enhanced_cloudfront_checks` + fake
  `cloudfront`/`cloudwatch` clients/paginators. Cover every fix: CW-exception
  classification (`except: pass` → `ctx.warn`/`ctx.permission_issue`), fast-mode
  skip of CW reads, the $0-advisory rendering (counted==rendered, $0 grand-total
  contribution), removal/quantification of the dead categories, and — if a real
  saving is re-enabled — the tiered-rate × `BytesDownloaded` formula with a
  structured `AuditBasis`.
- If you re-enable a quantified saving, price the PriceClass delta from the live
  CloudFront DTO tiers against measured `BytesDownloaded` GB (respect
  `ctx.fast_mode`); never fabricate bytes from request count × an assumed object
  size (the prior bug).
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for cloudfront first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `cloudfront.py` row in `services/adapters/CLAUDE.md` to match reality
  ($0 honest advisory, not "Fixed $25/rec").
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against
First, the UNIVERSAL catalogue (every adapter), then cloudfront-specific items.

#### Universal (from prior audits)
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

#### CloudFront-specific (found from the code)
- **Doc-vs-reality drift**: `services/adapters/CLAUDE.md` lists cloudfront as
  Parse-rate "**Fixed $25/rec**", but `scan` emits `$0` + `PricingWarning` on
  every rec and never parses a rate or applies $25. The row is stale and must be
  corrected.
- **Swallowed CloudWatch exceptions** (`services/cloudfront.py` ~lines 80 and
  127): `except Exception: pass` drops the price-class recommendation on any CW
  throttle/permission failure with no `ctx.warn`/`ctx.permission_issue` — the
  canonical silent-failure class.
- **Un-gated CloudWatch reads / undeclared fast-mode**: the adapter sets neither
  `requires_cloudwatch` nor `reads_fast_mode`, yet the helper issues `Requests`
  and `CacheHitRate` reads (plus `get_distribution_config`) per distribution
  unconditionally — `--fast` does not skip them.
- **Dead / wasted compute**: `low_traffic_distributions` and
  `origin_shield_unnecessary` are initialized but never populated; the
  origin-shield `CacheHitRate`/`Requests` math is fully computed then discarded
  (`_ = (should_check_origin_shield, origins)`). The adapter's
  `CheckCategory == "CloudFront Unused Distribution"` branch matches a category
  the helper never emits. All are dead code burning API calls.
- **Percentage `EstimatedSavings` string** ("20-50% on data transfer costs") that
  would parse to $0 — correctly overwritten to $0 today, but confirm it cannot
  leak into the Phase A `savings_mode="always"` render as a non-$0 figure.
- **Honest-$0 stance vs the "$0 is a bug" rule**: every finding is $0. This is an
  intentional advisory after a fabricated-estimator removal — confirm it renders
  as advisory (visible, $0-counted) and contributes $0 to the grand total, and
  decide whether to (a) keep $0 advisory, (b) re-enable a `BytesDownloaded`-backed
  tiered-rate quantification, or (c) drop distributions with no quantifiable
  saving entirely.
- **Source badge honesty**: `enhanced_checks → "Metric Backed"` for a $0,
  config-pattern recommendation may overstate confidence; compare the S3 override
  to "Audit Based".

## PROMPT (end)
