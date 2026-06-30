# File Systems (EFS / FSx) Adapter Cost-Audit Prompt

A deep, file-systems-specific audit brief in the same structure as the Network /
Lambda / RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* EFS/FSx code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.
`file_systems` is the canonical **CloudWatch-gated / advisory-split** adapter —
the worked example of a clean counted-vs-advisory separation with a NET
(not gross) metric-backed saving.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `file_systems`:
> - This service emits `$0` advisory recs ALONGSIDE counted ones (it is a counted/advisory split, not advisory-only) — verify the tab still renders even when ALL recs happen to be advisory (D2; the tab gate keys off RENDERED cards, counted + advisory, not the counted-only headline count), and confirm no `Counted=False` rec carries a non-zero numeric (advisory-leak, B1).

You are auditing the **`file_systems`** adapter (EFS + FSx) of this AWS
cost-optimization scanner. Scope is strictly cost: every emitted recommendation
must produce a concrete, account-specific dollar saving. Work read-only first
(understand + validate), then propose fixes grouped by severity, and only
implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat **file_systems itself**
as the canonical CloudWatch-gated/advisory model: an EFS IA-lifecycle saving is
counted ONLY when access metrics prove a cold Standard set AND the saving is
net-positive after the IA access charge; otherwise it is advisory. Treat the
recently-audited **Lambda** adapter (`services/adapters/lambda_svc.py`) as the
worked example for the `mark_zero_savings_advisory` pattern, rate-string
rejection, AuditBasis, and the test style I expect. The **S3** adapter
(`services/adapters/s3.py`, `docs/audits/S3_AUDIT_FINDINGS.md`) is the sibling
evidence-gated model worth cross-referencing.

### NOTE on structure (file_systems is NOT shaped like a CoH adapter)
- The adapter `services/adapters/file_systems.py` (`FileSystemsModule.scan`)
  aggregates two helpers in `services/efs_fsx.py` (`get_efs_findings`,
  `get_fsx_findings`) plus pure decision logic in
  `services/file_systems_logic.py`.
- It emits **three SourceBlocks**: `efs_lifecycle_analysis` (counted EFS),
  `fsx_optimization_analysis` (counted FSx), and `advisory` (uncounted
  best-practice) — all rendered under the **single File Systems tab**.
- EFS/FSx are covered by **neither Cost Optimization Hub nor Compute Optimizer**
  (per `coh-co-resource-coverage` memory) — every number is derived locally from
  DescribeFileSystems + the live Pricing API. A "missing CoH/CO source" finding
  is NOT fair game here.
- Counted savings carry `EstimatedSavings` (`$X.XX/month`), `_savings` (float),
  and a structured `AuditBasis`. Advisory findings carry `Counted: False` and
  NO dollar figure (or a non-parseable string). The adapter sums counted via
  `parse_dollar_savings` and `dedupe_counted` keeps **one counted finding per
  file-system id, highest saving wins** (never stack idle + lifecycle + one-zone).
- Rendering is **Phase A**: `reporter_phase_a.py:render_file_systems` (special-
  cased in `html_report_generator.py` ~line 3309, BEFORE `PHASE_A_DESCRIPTORS`).
  `file_systems` is in BOTH `_PHASE_A_SERVICES` and `_PHASE_B_SKIP_PER_REC`.
  There is also a Phase-B generic path `_render_generic_file_systems_rec`
  (~line 1584) and an `_extract_file_systems_resources` extractor (~line 391 in
  `html_report_generator.py`) — confirm which one actually fires.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `file_systems.py` row (Live
    Pricing). **Reconcile the doc against reality:** confirm `core/pricing_engine.py`
    exposes `get_efs_monthly_price_per_gb` (~line 751),
    `get_efs_ia_access_price_per_gb` (~line 773), and
    `get_fsx_storage_price_per_gb` (~line 791), and the fallback constants
    `FALLBACK_EFS_GB_MONTH` / `FALLBACK_EFS_GB_MONTH_BY_CLASS` /
    `FALLBACK_EFS_IA_ACCESS_GB` / `FALLBACK_FSX_GB_MONTH` /
    `FALLBACK_FSX_MULTI_AZ_GB_MONTH` (~lines 232–273) and
    `_EFS_STORAGE_CLASS_LABELS`.
0b. Confirm module identity in `services/adapters/file_systems.py`:
    `key="file_systems"`, `cli_aliases=("efs","fsx","file_systems")`,
    `display_name="File Systems"`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`, `required_clients()` → `("efs","fsx","cloudwatch")`.
0c. Note the counted/advisory contract: `total_recommendations = len(efs_counted)
    + len(fsx_counted)` (counted only); advisory recs live in their own
    SourceBlock and never touch the headline. There is no `advisory_count` in
    `extras` here (unlike S3) — confirm the advisory SourceBlock count is the
    only place advisory totals surface.

### Phase 1 — Understand the code (read before judging)
1. Read every file in the path: `services/adapters/file_systems.py`,
   `services/efs_fsx.py` (`get_efs_findings`, `get_fsx_findings`,
   `get_efs_file_system_count`, `get_fsx_file_system_count`, `_efs_rate`,
   `_efs_ia_access_rate`, `_efs_access_signal`, `_fsx_rate`,
   `_fsx_deployment_option`, `_fsx_advisory`, `_report_aws_error`),
   `services/file_systems_logic.py` (the pure savers + `dedupe_counted` +
   `fs_id` + constants), `services/_savings.py` (`parse_dollar_savings`),
   `core/contracts.py`, `core/pricing_engine.py` (EFS/FSx methods + fallbacks),
   `core/scan_orchestrator.py`, `core/result_builder.py`, and the reporter
   (`reporter_phase_a.py:render_file_systems` ~line 25 + `_fs_savings` ~line 92;
   `reporter_phase_b.py:_render_generic_file_systems_rec` ~line 1584;
   `html_report_generator.py:_extract_file_systems_resources` ~line 391 and
   dispatch ~line 3309).
2. List **every** cost check across both helpers, and for each give: trigger
   condition, the data source (EFS/FSx describe-API, CloudWatch
   `DataReadIOBytes`/`DataWriteIOBytes`, or pure config heuristic), the exact
   `EstimatedSavings` string template, the constant/rate it embeds, and whether
   it is COUNTED or ADVISORY. The known inventory to confirm:
   - **EFS counted:** (1) Idle delete (no mount targets → 100% of storage cost);
     (2) IA-lifecycle NET saving (CloudWatch-gated, net-positive after IA access
     charge, `cold_gb >= EFS_MIN_LIFECYCLE_GB`).
   - **EFS advisory:** IA-lifecycle when no metrics / fast-mode / net≤0 (gross
     indicative); One Zone migration (durability tradeoff); Archive-missing;
     Provisioned→Elastic throughput.
   - **FSx counted:** SSD→HDD swap (Windows ONLY, `capacity >= FSX_SSD_TO_HDD_MIN_GB`
     = 2000, deterministic delta).
   - **FSx advisory:** Lustre storage optimization; Lustre/OpenZFS
     Intelligent-Tiering; Windows dedup; Windows Single-AZ migration; ONTAP data
     efficiency / capacity-pool tiering; backup retention; File Cache.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** savings figure, re-derive it from the live AWS Pricing
   API and confirm it matches:
   - **EFS** (`AmazonEFS`, attribute `storageClass`): validate the
     **Standard** ($0.30/GB-mo us-east-1), **IA** ($0.016/GB-mo), **One Zone**,
     **One Zone-IA**, and the **IA access** rate ($0.01/GB) against the API.
     Confirm `std_class`/`ia_class` selection (`One Zone` vs `Standard`) follows
     `AvailabilityZoneName`. Confirm the idle saving = `total_gb × std_rate`
     (100%) and the lifecycle NET = `cold_gb × (std − ia) − accessed × ia_access`
     where `cold_gb = standard_gb − monthly_access_gb` (anything touched in the
     window is treated hot — conservative). Confirm the saving uses the MEASURED
     `SizeInBytes.ValueInStandard`, not total bytes.
   - **FSx** (`AmazonFSx`): validate Windows **SSD** vs **HDD** $/GB-mo for both
     Single-AZ ($0.130 → $0.013) and Multi-AZ ($0.230 → $0.025) against the API,
     and confirm `_fsx_rate` pins the correct `(fs_type, storage_type,
     deployment)` SKU. Confirm SSD→HDD is **Windows-only** (`_FSX_HDD_COUNTED_ELIGIBLE`);
     ONTAP has no HDD and Lustre HDD is Persistent-only at a different throughput
     tier — confirm both stay advisory (no fabricated counted delta).
   - **Region scaling:** PricingEngine methods are region-correct; the fallback
     path (`pricing_engine is None`) multiplies the fallback constant by
     `pricing_multiplier`. Confirm the multiplier is applied ONLY on the fallback
     path and NOT double-applied on the engine path (the engine already returns
     region-correct). Spot-check `_efs_rate`/`_fsx_rate` for the double-apply bug.
   - **Deterministic filter:** confirm the pricing methods pin a single SKU
     (no `MaxResults=1` over multiple deployment/throughput SKUs).
4. Confirm the savings basis is defensible from the report alone: each counted
   finding carries a structured `AuditBasis` (metric / region / size / rates /
   formula). An EFS lifecycle saving emitted without the
   `DataReadIOBytes+DataWriteIOBytes` window evidence, or as a GROSS (not NET)
   figure, is a finding. Confirm the advisory gross-indicative string
   (`up to ~$X/month gross before IA read-access charges`) never parses to a
   counted dollar.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** `dedupe_counted` keeps one counted finding per `fs_id`
   (highest `_savings`). Prove an EFS that qualifies for BOTH idle-delete AND
   IA-lifecycle (or an FSx matching multiple checks) is counted once. Confirm
   `fs_id` resolves `FileSystemId` OR `FileCacheId` and that anonymous findings
   get a unique `_anon_` key (so two un-id'd findings are not collapsed).
6. **Cross-source:** EFS counted and FSx counted are separate id-spaces; confirm
   no advisory finding for the same fs_id duplicates a counted one in a way the
   render double-shows. Confirm `core/result_builder.py` does not blindly sum
   counted + advisory.
7. **Cross-adapter:** confirm no `_extract_*` helper pulls EFS/FSx into a
   synthetic tab and double-counts, and that an FSx-backed workload is not also
   counted by EC2/storage adapters.

### Phase 4 — Coverage (works for ALL file systems, not a subset)
8. Confirm full pagination of `describe_file_systems` for BOTH `efs` and `fsx`
   clients, plus `describe_file_caches` for FSx. Confirm EFS skips transient
   `LifeCycleState` (only `available`/`""`) so a just-created FS does not emit a
   spurious idle-delete, and FSx skips non-`AVAILABLE` lifecycle.
9. Are whole classes skipped? Confirm all four FSx types (Lustre / Windows /
   ONTAP / OpenZFS) AND File Cache are enumerated and routed (counted vs
   advisory). Confirm One Zone EFS is detected via `AvailabilityZoneName` and
   that One Zone systems are excluded from the Regional→One-Zone advisory.
   Confirm the SSD→HDD `FSX_SSD_TO_HDD_MIN_GB` (2000 GiB) floor matches the AWS
   HDD minimum and is documented (an HDD swap below the minimum is not realizable).

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `return []`
    fallback. Specifically:
    - `_report_aws_error` routes `AccessDenied`/`AccessDeniedException`/
      `UnauthorizedOperation` → `ctx.permission_issue`, everything else →
      `ctx.warn`. Confirm EVERY describe / lifecycle-config / CloudWatch read
      routes through it (EFS describe, lifecycle config, access metrics; FSx
      describe, file-caches; counts). A throttled/denied metric read must never
      silently become "no usage → no saving / advisory only" without a `ctx`
      record.
    - `_efs_access_signal` returns `None` when NEITHER metric has datapoints →
      caller warns and keeps the finding advisory. Confirm AWS errors propagate
      to the caller for permission classification (not swallowed inside).
11. Does a pricing miss fall back to `0.0`/blank and still emit a counted
    finding? Confirm a `0`/negative rate delta yields no counted finding (the
    `if savings > 0` / `if est.net_savings > 0` guards). A counted `$0` is a bug.
12. **CloudWatch gating / fast-mode:** confirm `fast_mode` sets
    `cloudwatch = None`, emits ONE warning, and forces every EFS lifecycle
    opportunity to advisory (no per-FS metric reads). Confirm FSx (no CloudWatch
    dependency) still runs in fast mode. Confirm `requires_cloudwatch` /
    `reads_fast_mode` are declared.
13. Are opt-in / "enable X" nudges (enable dedup, enable Intelligent-Tiering,
    enable Archive) emitted as advisory (`Counted: False`, no parseable $), not
    counted? Confirm every advisory `EstimatedSavings` string
    (`Depends on …`, `Best-practice (requires usage data…)`,
    `~$X/month if migrated (durability tradeoff)`) parses to $0 / is not counted.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Handler wiring:** `file_systems` is special-cased to
    `render_file_systems(sources)` in `html_report_generator.py` (~line 3309),
    BEFORE the `PHASE_A_DESCRIPTORS` lookup, even though `file_systems` also sits
    in `_PHASE_B_SKIP_PER_REC`. Confirm the Phase-A renderer is the one that
    fires and that the Phase-B `_render_generic_file_systems_rec` /
    `_extract_file_systems_resources` paths are dead code for the normal flow (or
    document where they fire). A source with no reachable renderer renders
    nothing silently.
15. **Counted == rendered:** `render_file_systems` groups counted findings by
    `CheckCategory`, sums `_fs_savings(r)` per group, and renders the advisory
    source as a separate uncounted block. Confirm `_fs_savings` (which reads
    `_savings` then falls back to a `$`-regex) equals the adapter's
    `parse_dollar_savings`-based total — a divergence between the two parsers
    would desync the rendered per-group sum from the headline. Reconcile the
    executive-summary headline (`_calculate_service_savings` + reconciliation
    footnote) against the per-service total.
16. Confirm no counted finding is dropped from the table (or vice-versa), and
    that the advisory block shows every uncounted finding without contributing $0
    to the headline.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to file systems:
    `python3 cli.py <region> --scan-only file_systems`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service file_systems`.
    Triage every: silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and file system appearing in >1 source/tab.
    Reconcile the headline against the per-source sum. Caveats: try a second
    region if the first is sparse; exercise EFS WITH access metrics (counted NET
    branch) and WITHOUT (advisory gross branch) and `--fast-mode` (advisory);
    exercise a Windows FSx ≥2000 GiB SSD (counted SSD→HDD) and Lustre/ONTAP/
    OpenZFS (advisory). Use `.venv/bin/python` (3.14) — system `python3` lacks
    `datetime.UTC`.
18. For any duplication claim, prove it: show the same fs_id counted twice or in
    two sources. For any accuracy claim, show the AWS Pricing API value (EFS
    Standard/IA/One-Zone/IA-access, FSx Windows SSD/HDD per-GB) next to the
    scanner's rate.

### Deliverable
- The complete check list (Phase 1.2), per helper, with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs** (Lustre/ONTAP/OpenZFS advisory, ONTAP capacity-pool tiering, backup
  retention). End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add/extend a `tests/test_file_systems_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: test the pure logic directly
  (`efs_idle_savings`, `efs_lifecycle_net_savings` net-after-access-charge,
  `efs_one_zone_savings`, `fsx_ssd_to_hdd_savings`, `dedupe_counted`
  highest-wins + anon keys, `parse_dollar_savings` boundaries on the advisory
  strings) and drive `FileSystemsModule.scan` with a `SimpleNamespace` ctx +
  monkeypatched helpers + fake boto3 clients/paginators for describe / CloudWatch
  paths. Cover every fix: NET (not gross) lifecycle, fast-mode advisory,
  permission classification, fallback region scaling, Windows-only SSD→HDD,
  per-fs dedup, render parser parity, counted==rendered.
- For any heuristic that assumes usage with no metric (ONTAP cold bytes, backup
  size), keep it a $0 advisory — never fabricate a $.
- Confirm the structured **AuditBasis** on every counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for file_systems first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same bug in a sibling adapter out of scope, note it as a
  follow-up (don't fix unprompted).
- Update the `file_systems.py` row in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (size/class) with NO usage metric
  → fabricated $. (file_systems: lifecycle needs measured access metrics.)
- Wrong storage-class/region pricing; file system priced at scan region not home region.
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`,
  OR double-applied on an already-region-correct engine path.
- Per-unit RATE string (`$/GB`, `$/request`) counted as a monthly total —
  rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier/free allotment recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic by NORMALIZED id. (N/A here: no CoH/CO coverage.)
- Two heuristic checks stacking on one resource, or SUBSET redundancy — fix by
  removal (`dedupe_counted` is the mechanism).
- Reduction factor instead of exact price delta (price×factor vs current−target).
  (EFS advisory gross uses `EFS_IA_TRANSITION_FRACTION=0.5` — must stay advisory.)
- $0 "enable X"/opt-in placeholder counted instead of converted to `ctx.warn`/advisory.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (`Counted=False`).
- Cost Hub: (a) `currentResourceType` with no `type_map` bucket → dropped;
  (b) bucket populated but consumed by NO adapter → dropped silently. (N/A: EFS/FSx
  are not CoH/CO covered — do NOT flag a "missing CoH source".)
- A source emitted with no renderer in a skip-per-rec service → renders nothing silently.
- Render-time substring/category filter desyncing headline from cards (filter at SOURCE).
- Coverage gated to a hardcoded class/type/state allowlist.
- CloudWatch permission/throttle failure logged via logger only, not
  `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic assuming a usage target with no usage evidence.
- Cross-adapter overlap (same volume/file system in two tabs).
- Fixed per-rec estimate treated as a realized saving rather than advisory.
- Each counted finding must carry a structured AuditBasis; counted == rendered.

#### file_systems-specific items
- **Gross-vs-NET lifecycle:** the EFS IA-lifecycle COUNTED path must subtract the
  IA per-GB access charge (`efs_lifecycle_net_savings`); a regression to the
  GROSS `efs_lifecycle_savings × 0.5 fraction` as a counted figure is a fabricated
  saving. Confirm the gross helper is used ONLY for the advisory indicative string.
- **`cold_gb` definition:** `cold_gb = standard_gb − monthly_access_gb` treats
  ALL bytes touched in the 30d window as hot. Validate this is conservative (it
  over-counts hot, under-counts savings) and that `monthly_access_gb` is GB of
  read+write IO bytes, not request count.
- **SSD→HDD Windows-only / 2000 GiB floor:** confirm ONTAP (no HDD) and Lustre
  (Persistent-only HDD, different throughput tier) cannot reach the counted
  branch, and the `FSX_SSD_TO_HDD_MIN_GB` floor matches the live AWS HDD minimum.
- **Renderer parser parity:** `reporter_phase_a._fs_savings` and the adapter's
  `parse_dollar_savings` are two different parsers over the same `EstimatedSavings`
  string; an advisory string that `_fs_savings`'s loose `$`-regex would match but
  `parse_dollar_savings` rejects (or vice-versa) desyncs rendered sums from the
  headline. Confirm parity, ideally by routing advisory through the same gate.
- **One Zone is a durability tradeoff:** confirm the deterministic One-Zone price
  delta stays ADVISORY (single-AZ resilience loss), never counted, even though
  the math is exact.
- **Dead Phase-B path:** `_render_generic_file_systems_rec` /
  `_extract_file_systems_resources` exist but `render_file_systems` is special-
  cased first; confirm they are unreachable for the normal flow or remove the
  confusion.

## PROMPT (end)
</content>
