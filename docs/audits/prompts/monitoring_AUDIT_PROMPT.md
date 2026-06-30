# Monitoring Adapter Cost-Audit Prompt

A deep, monitoring-specific audit brief in the same structure as the Network /
Lambda / RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* monitoring code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving — and
monitoring is the single highest-risk adapter for best-practice/health nudges
leaking into a cost report.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `monitoring`:
> - This service emits `$0` advisory recs ALONGSIDE counted ones (it is a counted/advisory split, not advisory-only) — verify the tab still renders even when ALL recs happen to be advisory (D2; the tab gate keys off RENDERED cards, counted + advisory, not the counted-only headline count), and confirm no `Counted=False` rec carries a non-zero numeric (advisory-leak, B1).
> - The `backup` and `route53` sub-shims aggregated here use `ctx.warn()` for ALL exceptions; an `AccessDenied`/throttle on `backup:ListBackupPlans` or `route53:ListHostedZones` is never routed through `record_aws_error` and so is never classified as `permission_issue` (E1 gap — the `monitoring.py` CloudWatch/CloudTrail paths are correctly classified, but these two sub-shims are not).

You are auditing the **`monitoring`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving (no log-hygiene, no "set retention as a
best practice", no health/operational nudges). Work read-only first (understand
+ validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **S3** adapter
(`services/adapters/s3.py`) as the canonical model for the *evidence-gated,
CloudWatch-backed → otherwise $0-advisory* pattern, the **step_functions**
adapter for the *CloudWatch-metric → priced* pattern, and the **network**
adapter (`docs/NETWORK_AUDIT_PROMPT.md`) as the worked example for the
parse-rate / rate-string boundary, multi-source render wiring, and the
`mark_zero_savings_advisory` discipline and test style I expect.

### NOTE on structure (monitoring is NOT shaped like a single-domain adapter)
- The monitoring adapter is a **COMPOSITE of four AWS domains**, aggregated in
  `services/adapters/monitoring.py` → `MonitoringModule.scan`:
  - `get_cloudwatch_checks(ctx, multiplier)` → `services/monitoring.py`
    (CloudWatch Logs + custom metrics + alarms)
  - `get_cloudtrail_checks(ctx)` → `services/monitoring.py`
  - `get_backup_checks(ctx)` → `services/backup.py`
  - `get_route53_checks(ctx, multiplier)` → `services/route53.py`
- It emits **four per-domain SourceBlocks**: `cloudwatch_checks`,
  `cloudtrail_checks`, `backup_checks`, `route53_checks` — NOT a single
  `enhanced_checks` block. Remember this for Phase 6: there are **four** source
  names, and the adapter is in `_PHASE_B_SKIP_PER_REC` (no per-rec fallback), so
  **every one of the four source names must have a `PHASE_B_HANDLERS` entry** or
  it renders nothing silently.
- Pricing strategy is **fixed per-rec estimates / parse-rate** (the adapter is
  listed under "Parse-rate" in `services/adapters/CLAUDE.md`, method "Fixed
  per-rec estimates"). All rates are **module constants** in the sub-shims, not
  `PricingEngine` calls — `core/pricing_engine.py` exposes **no** CloudWatch /
  CloudTrail / Backup / Route 53 method (only `get_eip_monthly_price` /
  `get_nat_gateway_monthly_price` exist, for network). So there is **no live
  pricing path** to reconcile; the audit is about whether the hardcoded
  constants are correct, region-scaled, and tied to a measured quantity.
- Monitoring consumes **neither Cost Optimization Hub nor Compute Optimizer**.
  It is not in `scan_orchestrator._prefetch_advisor_data`'s `_HUB_SERVICES` /
  `type_map`, and pulls no CO helper. A "missing CoH/CO source" finding is NOT
  fair game here.
- Savings flow: the adapter prefers a numeric `EstimatedMonthlySavings` field
  when the rec carries one (CloudWatch + Route 53 recs do), else falls back to
  `services/_savings.py:parse_dollar_savings` on the `EstimatedSavings` **string**
  (which counts only a bare/`/month` dollar figure and **rejects per-unit rate
  strings** and percentages). `mark_zero_savings_advisory` then flags any rec
  that resolves to `$0` as `Counted=False` (advisory). The counted total sums
  only `Counted=True`. **Backup and CloudTrail recs carry NO numeric field and
  non-$ strings → they are all advisory by construction** — confirm that is
  intended and that none sneak into the count.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`, Parse-rate table, `monitoring.py` row
    ("Fixed per-rec estimates"). Reconcile the doc against reality: the adapter
    spans **four** AWS services and **two** sub-shim files
    (`services/monitoring.py` holds both CloudWatch and CloudTrail;
    `services/backup.py` and `services/route53.py` are separate). Note any drift.
0b. Confirm module identity in `services/adapters/monitoring.py`:
    `key="monitoring"`, `cli_aliases=("monitoring",)`,
    `display_name="Monitoring & Logging"`, `required_clients()` returns
    `("cloudwatch", "logs", "cloudtrail", "backup", "route53")`. It sets
    `requires_cloudwatch=True` but does **NOT** set `reads_fast_mode` — yet
    `get_cloudwatch_checks` calls `list_metrics` + `describe_alarms` and the
    other sub-shims hit paginated describe APIs. Flag the missing
    `reads_fast_mode` (see Phase 5).
0c. Map ownership: there is **no** standalone `BackupModule` or `Route53Module`
    in `services/__init__.py:ALL_MODULES` — backup/route53 findings flow ONLY
    through `monitoring`. But `reporter_phase_a.py:PHASE_A_DESCRIPTORS` and the
    `html_report_generator.py` service-order list both still contain dead
    `"backup"` / `"route53"` entries, and `reporter_phase_a.py` defines
    `_extract_backup_details` / `_extract_route53_details`. Confirm these are
    inert for the current registration (monitoring owns rendering via Phase B)
    and note the dead descriptors as a tidy-up, not a double-render — but PROVE
    they don't double-render in Phase 6.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the monitoring path: `services/adapters/monitoring.py`;
   the four sub-shims (`services/monitoring.py` for both
   `get_cloudwatch_checks` and `get_cloudtrail_checks`, `services/backup.py`,
   `services/route53.py`); `services/_savings.py` (`parse_dollar_savings`,
   `mark_zero_savings_advisory`); `core/contracts.py`, `core/scan_context.py`
   (`ctx.warn`, `ctx.permission_issue`, `ctx.fast_mode`, `ctx.pricing_multiplier`);
   `core/scan_orchestrator.py`; `core/result_builder.py`; and the reporter
   (`reporter_phase_b.py:_render_monitoring_enhanced_checks` ~line 1213, the
   `PHASE_B_HANDLERS` block ~line 2474, `_PHASE_B_SKIP_PER_REC` ~line 2501,
   `html_report_generator.py`).
2. List **every** cost check across all four sub-domains, and for each give:
   trigger condition, the data source (describe-API field, CloudWatch metric, or
   pure config heuristic), the exact `EstimatedSavings` string template **and**
   whether a numeric `EstimatedMonthlySavings` is set, the constant/rate it
   embeds, and whether it resolves to a **counted** dollar or a **$0 advisory**.
   Map each check to its emitting SourceBlock. The known live inventory to
   confirm (most historical checks were deliberately removed — verify which
   actually emit):
   - **CloudWatch** (`cloudwatch_checks`): `never_expiring_logs`
     (`retentionInDays is None` → `stored_gb × CW_LOGS_GB_MONTH(0.03) ×
     multiplier`, COUNTED); `unused_custom_metrics` (non-`AWS/` namespace with
     `count > 100` → tiered `_cw_custom_metrics_monthly_cost(count) −
     _cw_custom_metrics_monthly_cost(count // 2)`, COUNTED). The
     `excessive_logging`, `high_resolution_metrics`, `unused_alarms`,
     `duplicate_metrics` buckets exist but are **never populated** (findings
     removed) — confirm.
   - **CloudTrail** (`cloudtrail_checks`): every finding (multi-region,
     data-events-S3/Lambda, duplicate trails, insights, expensive storage) has
     been **removed** — the function walks trails/selectors but appends nothing.
     Confirm `ct_recs` is always empty and decide whether the walk (and its
     `get_event_selectors` calls) is dead cost.
   - **Backup** (`backup_checks`): `excessive_retention`
     (`DeleteAfterDays > 2555` → string `"Reduce retention to lower storage
     costs"`, **no $, ADVISORY**); `daily_static_data` (`"daily"`/`"cron(0 "`
     schedule → string `"Weekly/monthly backups can reduce costs by 70-85%"`,
     **percentage, ADVISORY**). All other buckets removed.
   - **Route 53** (`route53_checks`): `unused_hosted_zones`
     (`ResourceRecordSetCount <= 2` → `_route53_zone_monthly_cost(1, ...) ×
     multiplier`, COUNTED); `duplicate_private_zones` (same private-zone name
     repeated → `_route53_zone_monthly_cost(removable, ...) × multiplier`,
     COUNTED). `unnecessary_health_checks`, `complex_routing_simple_use`,
     `old_records_deleted_resources` removed.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive it from the live AWS Pricing API and
   confirm the constant. Validate EACH domain constant separately, with the
   correct service code and SKU:
   - **CloudWatch Logs storage** — `CW_LOGS_GB_MONTH = 0.03` (`services/monitoring.py`).
     Service code `AmazonCloudWatch`; confirm the **stored/archived logs**
     $/GB-month SKU (the code cites SKU `JRHJQ2UMPUB5K73A`). CRITICAL nuance:
     `never_expiring_logs` charges the **full** `stored_bytes × $0.03` as the
     saving ("if 30-day retention enforced") — this assumes 100% of stored bytes
     would be deleted by retention. That is only true if every byte is older than
     the new retention window. Confirm whether this overstates (no age
     distribution is read) and whether it should be a $0 advisory or a fraction.
     Also confirm **ingestion** ($0.50/GB) is *not* what's being saved (retention
     does not refund ingestion) — only storage.
   - **CloudWatch custom metrics** — tiered `CW_CUSTOM_METRIC_TIER_1/2/3 =
     0.30/0.10/0.05` at limits `10_000 / 250_000`. Validate the three published
     tiers and breakpoints. CRITICAL: the saving is `cost(count) −
     cost(count // 2)` — a **50% reduction factor with no evidence** that half
     the metrics are unused. There is no per-metric staleness check. Flag this as
     a fabricated quantity (Phase 5/Known issues) — it should be advisory or
     driven by a real "metrics with no datapoints in N days" signal.
   - **Route 53 hosted zones** — `ROUTE53_HOSTED_ZONE_TIER_1/2 = 0.50/0.10`,
     tier-1 limit 25 (`services/route53.py`). Service code `AmazonRoute53`;
     confirm first-25 $0.50/zone-month and additional $0.10. Confirm the tier
     math in `_route53_zone_monthly_cost` (removes the cheapest tier-2 zones
     first when `base_zones_in_account > 25`, else tier-1) is defensible. Route 53
     is **global** — confirm the `pricing_multiplier` applied to zone savings is
     1.0 for all regions (the shim comment says it keeps the multiplier "for
     consistency"); a non-1.0 multiplier on a global service would be WRONG.
   - **Backup / CloudTrail** — no counted $, so no rate to validate. But CONFIRM
     they stay advisory: `excessive_retention` and `daily_static_data` carry no
     numeric field and non-$ strings; `parse_dollar_savings` must return 0 and
     `mark_zero_savings_advisory` must set `Counted=False`. If AWS Backup storage
     ($/GB-mo, e.g. warm `$0.05`, cold) or CloudTrail data-event ($/100k events)
     pricing is ever wired to make these counted, it must read a real measured
     volume first.
4. Confirm the savings basis is defensible from the report alone. A CloudWatch
   custom-metrics or never-expiring-logs saving emitted as a flat number with no
   age/staleness window is a finding — prefer a measured number or a $0 advisory.
   Record a structured **AuditBasis** (rate / region / metric-window / formula)
   on each counted finding, as the Lambda/RDS/S3 audits did. Remember Route 53 is
   global → AuditBasis region = "global".

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-domain:** can one resource match multiple checks and stack savings?
   (A private hosted zone counted in both `unused_hosted_zones` AND
   `duplicate_private_zones`: a zone with ≤2 records that is also a duplicate of
   another zone — confirm it isn't double-counted. A namespace counted once in
   `unused_custom_metrics` is fine; confirm no overlap with a removed bucket that
   somehow re-emits.)
6. **Cross-domain (intra-adapter):** the four sub-domains are disjoint AWS
   services, so cross-domain stacking is unlikely — but confirm
   `_route53_zone_monthly_cost` is not invoked twice for the same zone across the
   two Route 53 checks, and that `total_recommendations = len(all_recs)` simply
   concatenates the four lists without re-summing.
7. **Cross-adapter / dead descriptors:** Route 53 and AWS Backup are **also**
   referenced by dead `PHASE_A_DESCRIPTORS["backup"]` / `["route53"]` and the
   `html_report_generator` service-order list. Prove these do NOT cause the same
   backup/route53 finding to render under a second tab or be summed twice in
   `core/result_builder.py` / `_calculate_service_savings`. Also confirm no
   `_extract_*` synthetic-tab helper pulls monitoring resources elsewhere.

### Phase 4 — Coverage (works for ALL resources, not a subset) — and SCOPE LEAKAGE
8. Are checks gated to states that silently exclude valid resources? Confirm full
   pagination of `describe_log_groups` (nextToken loop), `describe_alarms`
   (paginator), `list_metrics` (paginator), `list_backup_plans` /
   `list_backup_selections` (paginators), `list_hosted_zones` /
   `list_resource_record_sets` / `list_health_checks` (paginators),
   `describe_trails` (note: **not** paginated — confirm trail count can't exceed
   one page silently).
9. **Scope leakage is the #1 monitoring risk.** Walk every still-emitted finding
   and prove it produces a concrete account-specific $ saving, NOT a
   best-practice / health / hygiene nudge:
   - `never_expiring_logs` — legit only insofar as storage $ is real and
     attributable; "set a retention policy" as generic advice is NOT a cost rec.
   - `unused_custom_metrics` "review necessity" — is this a real saving or a
     hygiene nudge? The 50%-reduction number is the only thing making it look
     counted.
   - `excessive_retention` (Backup) / `daily_static_data` — these are advisory by
     construction; confirm they render as advisory and are not presented as if
     they save money. If they cannot be quantified, consider whether they belong
     in a **cost** report at all (the prior audits removed many sibling checks for
     exactly this reason — see the `_ = (...)` "finding removed" comments).
   - Confirm the removed-finding comment blocks (multi-region trails, data
     events, health checks, complex routing, ephemeral backups, cross-region
     copies) stay removed and no regression re-adds a $0/percentage nudge.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger.warning`-only, and
    `return []` fallback, and classify error handling per domain:
    - `services/monitoring.py` (`get_cloudwatch_checks` and
      `get_cloudtrail_checks`) swallow failures with **`logger.warning` only**
      (e.g. ~lines 160, 193, 196, 254, 256, 266) — a CloudWatch/CloudTrail
      permission gap or throttle vanishes from the report with NO `ctx.warn` /
      `ctx.permission_issue`. This is the canonical prior-audit silent-failure
      class. Classify: `AccessDenied`/`UnauthorizedOperation`/`OptInRequired` →
      `ctx.permission_issue`; other → `ctx.warn`.
    - Contrast `services/backup.py` and `services/route53.py`, which **do** call
      `ctx.warn(...)` — confirm they classify permission errors too (they
      currently funnel everything to `ctx.warn` without an AccessDenied →
      `permission_issue` split).
11. Does a missing metric / empty describe fall back to `0.0`/blank and still
    emit a finding? Verify `mark_zero_savings_advisory` covers EVERY non-$ path
    (Backup strings, CloudTrail, percentage strings) — none should be counted. A
    never-expiring log group with `storedBytes == 0` produces a `$0.00/month`
    string → confirm it lands advisory, not as a counted $0.
12. **CloudWatch gating / fast-mode (declared `requires_cloudwatch` but NOT
    `reads_fast_mode`):** `get_cloudwatch_checks` calls `list_metrics` and
    `describe_alarms` unconditionally; none of the four sub-shims check
    `ctx.fast_mode`. Confirm whether these reads should be skipped under
    `ctx.fast_mode` (mirror the Lambda/S3 fast-mode fix) and whether
    throttle/permission failures are recorded on `ctx`. Decide whether
    `reads_fast_mode=True` should be declared.
13. Are best-practice / "set retention" / "review necessity" nudges emitted as
    findings that inflate the count? They must be advisory (`Counted=False`),
    rendered but excluded from counts. Confirm the Backup and CloudTrail recs
    (and any $0 CloudWatch/Route 53 rec) land as advisory, not counted.

### Phase 6 — Reporting (one tab, counted == rendered, FOUR sources wired)
14. **Source-name vs handler mismatch (verify all four):** the adapter emits four
    sources (`cloudwatch_checks`, `cloudtrail_checks`, `backup_checks`,
    `route53_checks`) and `monitoring` is in `_PHASE_B_SKIP_PER_REC` (no per-rec
    fallback). Confirm `PHASE_B_HANDLERS` registers ALL FOUR
    `("monitoring", "<source>") → _render_monitoring_enhanced_checks` (lines
    ~2474–2477). A source with NO registered handler in a skip-per-rec service
    renders **nothing, silently**. If a fifth source is ever added, it must be
    wired too.
15. **Counted == rendered:** `total_recommendations = len(all_recs)` (includes
    advisory) but `total_monthly_savings` sums only `Counted=True`. Confirm
    `_render_monitoring_enhanced_checks` renders advisory cards (visible but $0)
    and that it shows `resources[0]['EstimatedSavings']` (the raw string) per
    category — check the displayed string does not desync from the parsed counted
    number (e.g. an advisory backup card shows "70-85%" while contributing $0 —
    that's acceptable IF labelled advisory; a counted card must show a real $).
    Reconcile the per-tab headline (counted/advisory split) against the rendered
    cards and against the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings` +
    reconciliation footnote).
16. Confirm no finding is counted in `total_recommendations`/
    `total_monthly_savings` but dropped from the table (or vice-versa), across all
    four domains and every `CheckCategory` group, and that the dead Phase-A
    `backup`/`route53` descriptors contribute nothing.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to monitoring:
    `python3 cli.py <region> --scan-only monitoring`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service monitoring`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and resource appearing in >1 source/tab. Reconcile the
    headline against the per-source sum. Caveats: an account may have only log
    groups, or only hosted zones, or no backup plans — try a second region and an
    account with each domain populated so all four branches are exercised. Route
    53/Backup/CloudTrail are global-ish — confirm the region argument doesn't
    double-list a global zone. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC` (the sub-shims import `from datetime import UTC`).
18. For any accuracy claim, show the AWS Pricing API value (CloudWatch Logs
    $/GB-month, custom-metric tier rates, Route 53 zone rates) next to the
    scanner's constant. For any leakage claim, show the finding's `EstimatedSavings`
    string and prove it is a best-practice nudge, not a quantified saving.

### Deliverable
- The complete check list (Phase 1.2), per sub-domain, with counted-vs-advisory
  marked and the emitting SourceBlock named.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add a `tests/test_monitoring_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the pure helpers directly
  (`_cw_custom_metrics_monthly_cost` tier math, `_route53_zone_monthly_cost` tier
  selection, `parse_dollar_savings` boundaries on the Backup percentage strings,
  `mark_zero_savings_advisory`) and drive `MonitoringModule.scan` with a
  `SimpleNamespace` ctx + monkeypatched sub-shims + fake boto3
  clients/paginators for the describe/CloudWatch paths. Cover every fix:
  silent-failure classification (logger → ctx.warn/permission_issue),
  never-expiring-logs overstatement, custom-metrics 50%-factor → advisory or
  measured, fast-mode skip, all four sources rendered, counted==rendered,
  advisory `$0` gating, Route 53 global multiplier = 1.0.
- For any heuristic that assumes a quantity with no evidence (custom-metrics
  "reduce by 50%", never-expiring-logs "all bytes deletable"), replace it with a
  real measured signal or keep it a $0 advisory — never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for monitoring first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-failure / scope-leak bug in a sibling adapter out
  of scope, note it as a follow-up (don't fix unprompted).
- Update the `monitoring.py` row in `services/adapters/CLAUDE.md` to match
  reality (four domains, two shim files), and consider removing the dead
  `backup`/`route53` Phase-A descriptors.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone with NO usage metric → fabricated $
  (e.g. never-expiring-logs charging full `storedBytes` as deletable; custom
  metrics assuming 50% are removable).
- **Non-cost / best-practice / health / hygiene nudge leaking into a cost report**
  — the #1 monitoring risk. "Set retention policy", "review necessity", "verify
  if needed", "reduce retention to lower costs" without a quantified $ are NOT
  cost recommendations. Scope is strictly cost.
- Wrong tier/SKU pricing (CloudWatch Logs storage vs ingestion; custom-metric
  tier breakpoints; Route 53 first-25 vs additional-zone rate).
- Non-deterministic pricing filter (N/A here — module constants — but confirm the
  constants match the live published rates, not stale ones).
- Region: a hardcoded constant not region-scaled via `pricing_multiplier`, OR a
  multiplier double-applied / wrongly applied to a **global** service (Route 53
  zones must be charged at the global rate, multiplier 1.0).
- Per-unit RATE string ($/GB, $/zone, $/100k events) or a percentage ("70-85%",
  "up to 90%") counted as a monthly total — must be rejected by
  `parse_dollar_savings` → $0 advisory.
- Keyword/threshold-based estimate (`count > 100`, `record_count <= 2`,
  `DeleteAfterDays > 2555`) not tied to a real account-specific spend quantity.
- Free-tier recommended for a saving it cannot realize (CloudWatch free tier: 10
  custom metrics, 5 GB logs ingestion; first alarms free — confirm savings sit
  above the free allotment).
- Same resource counted twice (a private zone in both Route 53 checks); authority
  dedup by normalized id.
- Two heuristic checks stacking, or SUBSET redundancy — fix by removal.
- Reduction factor instead of exact delta (custom-metrics `count // 2`).
- $0 "enable X"/"set retention" placeholder counted instead of converted to
  advisory and dropped from the count.
- Metric-gated / non-$ nudge rendered as COUNTED instead of advisory
  (`Counted=False`).
- Cost Hub: N/A (monitoring consumes neither CoH nor CO) — do not flag a missing
  advisory source.
- **A source emitted with no `PHASE_B_HANDLERS` handler in a
  `_PHASE_B_SKIP_PER_REC` service renders nothing silently — monitoring has FOUR
  sources; verify each (`cloudwatch_checks`, `cloudtrail_checks`,
  `backup_checks`, `route53_checks`) has a handler.**
- Render-time category filter desyncing the headline from the cards (filter at
  source, not at render).
- Coverage gated to a hardcoded type/state allowlist, or an un-paginated
  describe (`describe_trails`) silently truncating.
- CloudWatch / CloudTrail permission/throttle failure logged via `logger` only,
  not `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized →
  `permission_issue`). Backup/Route 53 funnel everything to `ctx.warn` without
  the permission split.
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared
  despite `list_metrics`/`describe_alarms` calls).
- Heuristic assuming usage with no evidence (every monitoring counted check is
  threshold-triggered, not spend-measured — scrutinize each).
- Cross-adapter overlap / dead descriptors (Route 53 + Backup also referenced by
  inert `PHASE_A_DESCRIPTORS` and the html service-order list).
- Each counted finding must carry a structured AuditBasis (rate/region/
  metric-window/formula); counted == rendered.

## PROMPT (end)
</content>
</invoke>
