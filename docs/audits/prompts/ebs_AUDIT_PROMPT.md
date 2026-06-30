# EBS Adapter Cost-Audit Prompt

A deep, EBS-specific audit brief in the same structure as the Network / Lambda /
RDS audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* EBS code path so the auditor starts from
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
> Service-specific live-audit findings for `ebs`:
> - A snapshot recoverable that rounds to $0.00 (a few stored MB) must be suppressed — gate on the rounded `potential`, not just `size_gb > 0`, or a '$0.00/mo recoverable' noise card renders.
> - EBS snapshot recs are `$0` `Counted=False` advisories; AMI-backed snapshots are owned by the AMI tab — keep them out of the EBS snapshot source (cross-adapter dedup).
> - AUDIT-METHOD: unattached-volume savings live in `EstimatedMonthlyCost` (not `EstimatedSavings`/`EstimatedMonthlySavings`), and CoH recs in camelCase `estimatedMonthlySavings` — a naive '$0 string' sweep false-flags them.
> - EBS consumes both CoH (`ctx.cost_hub_splits['ebs']`) and CO; verify the scan JSON shows no 'dropped type' warnings for `EbsVolume` — a 3-layer wire-up gap silently discards AWS-computed savings (E2 orphan-bucket check).

You are auditing the **`ebs`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. EBS
shares its multi-source dedup shape with **EC2** (`services/adapters/ec2.py` —
the canonical CoH > CO > heuristic model), and EBS pure decision logic is
already extracted into `services/ebs_logic.py` for unit-testing. Treat the
**RDS** (`services/adapters/rds.py` + `services/rds_logic.py`) and **Lambda**
(`services/adapters/lambda_svc.py`) adapters as the worked examples for the
`AuditBasis` / advisory-`$0` discipline and the test style I expect
(`tests/test_rds_audit_fixes.py`, `tests/test_lambda_audit_fixes.py`,
`tests/test_ec2_audit_fixes.py`).

### NOTE on structure (EBS is a 6-source multi-source adapter with a cross-tab leak-out)
- `services/adapters/ebs.py` → `EbsModule.scan` aggregates **six SourceBlocks**:
  `cost_optimization_hub`, `compute_optimizer`, `unattached_volumes`,
  `gp2_migration`, `enhanced_checks`, and `ebs_snapshots`. The first five are
  counted toward the EBS tab; **`ebs_snapshots` is intentionally NOT counted** —
  it is handed to the dedicated **Snapshots** tab.
- Pure logic is in `services/ebs_logic.py` (`dedupe_by_authority`,
  `partition_enhanced_recs`, `gp2_to_gp3_net_savings`, `gp2_baseline_iops`,
  `recommend_iops_from_usage`, `normalize_volume_id`, `is_actionable_co_finding`);
  AWS-touching logic is in `services/ebs.py` (`compute_ebs_checks`,
  `get_unattached_volumes`, `get_ebs_compute_optimizer_recs`,
  `_scan_over_provisioned_iops`, `_estimate_volume_cost`, `_current_ami_snapshot_ids`).
- **Cost Optimization Hub**: `ctx.cost_hub_splits["ebs"]`
  (`type_map["EbsVolume"]="ebs"`), filtered by `_coh_is_renderable` (must have
  `actionType`, an `ebsVolume` detail, and finding != `Optimized`) AND
  re-validated live by `_drop_stale_delete_recs` (a "delete" rec on a now-attached
  volume is dropped with a `ctx.warn`).
- **Compute Optimizer**: `get_ebs_compute_optimizer_recs`; the `$0`
  `compute-optimizer-service` opt-in placeholder → `ctx.warn` + dropped;
  non-positive-savings recs dropped; savings via `compute_optimizer_savings`
  (`volumeRecommendationOptions[N].savingsOpportunity`).
- **Dedup**: `dedupe_by_authority(coh, co, [unattached, gp2, other])` by
  normalized `vol-…` id, authority CoH > CO > heuristic.
- EBS is Phase B and in `_PHASE_B_SKIP_PER_REC`; five sources have
  `PHASE_B_HANDLERS` entries — **`ebs_snapshots` deliberately does not** (it
  renders via the Snapshots tab, not here). See Phase 6.

### Phase 0 — Orient (5-minute map before judging)
0a. Read the `ebs.py` row in `services/adapters/CLAUDE.md`
    (`get_ebs_monthly_price_per_gb`; CO via `compute_optimizer_savings`). Note
    that gp2→gp3 savings are netted against gp3 IOPS parity for large volumes.
0b. Confirm identity in `services/adapters/ebs.py`: `key="ebs"`,
    `cli_aliases=("ebs",)`, `display_name="EBS"`, `reads_fast_mode=True`,
    `requires_cloudwatch=True` (the over-provisioned IOPS check reads AWS/EBS
    metrics), `required_clients()=("ec2","compute-optimizer","cloudwatch")`.
0c. EBS HAS both AWS advisory sources (CoH + CO). Most risk lives in: the
    gp2→gp3 net-savings math, the IOPS rightsizing CloudWatch gating, the
    snapshot-ownership boundary with the AMI tab, and the `ebs_snapshots`
    leak-out renderer wiring.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/ebs.py`, `services/ebs.py`, and `services/ebs_logic.py`
   in full; `services/_savings.py`; `services/advisor.py`
   (`get_ebs_compute_optimizer_recommendations` / `_recs`, the placeholder);
   `core/pricing_engine.py` (`get_ebs_monthly_price_per_gb`,
   `get_ebs_iops_monthly_price`, `get_ebs_io2_iops_cost` + io2 tiers,
   `get_ebs_throughput_monthly_price`, `get_ebs_snapshot_price_per_gb`, and the
   `FALLBACK_EBS_*` constants: gp2 0.10 / gp3 0.08, IOPS gp3 0.005 / io1,io2
   0.065, snapshot 0.05); `core/scan_orchestrator.py` (`type_map`/`_HUB_SERVICES`);
   `core/result_builder.py`; and the reporter (`_render_ebs_cost_hub`,
   `_render_ebs_unattached`, `_render_ebs_gp2_migration`,
   `_render_ebs_enhanced_checks`, `_render_ebs_compute_optimizer`, and the
   Snapshots-tab extractor in `html_report_generator.py` that consumes
   `ebs_snapshots`).
2. List **every** cost check, by source, with: trigger, data source, savings
   formula, constant/rate, counted-vs-advisory. Known inventory to confirm:
   CoH (flat `estimatedMonthlySavings`); CO (`compute_optimizer_savings`);
   **unattached_volumes** (full `EstimatedMonthlyCost` = 100% on delete, via
   `_estimate_volume_cost`); **gp2_migration** (`size × (gp2−gp3 $/GB) − max(0,
   gp2_baseline_iops−3000) × gp3_iops_rate`, per `gp2_to_gp3_net_savings`);
   **enhanced_checks/other** (over-provisioned IOPS, CloudWatch-gated); and the
   **ebs_snapshots** leak-out (old + orphaned snapshots, `VolumeSize × snapshot
   rate`, "max estimate").

### Phase 2 — Accuracy of every number (validate with MCP)
3. Re-derive each rate from the live AWS Pricing API (`AmazonEC2`,
   `productFamily=Storage` / `System Operation` / `Storage Snapshot`):
   - **gp2/gp3 $/GB-mo**: validate `get_ebs_monthly_price_per_gb("gp2")` and
     `("gp3")`; confirm the fallback delta (0.10 − 0.08 = $0.02) is us-east-1 and
     is region-scaled by `pricing_multiplier` ONLY on the fallback path
     (`_gp2_to_gp3_savings_per_gb`), with NO multiply on the engine path.
   - **gp3 IOPS parity**: `gp2_to_gp3_net_savings` subtracts
     `max(0, gp2_baseline_iops(size) − 3000) × get_ebs_iops_monthly_price("gp3")`.
     Confirm `gp2_baseline_iops` (3 IOPS/GB, floor 100, ceiling 16,000) is
     correct and that the gp3 IOPS rate (~$0.005/IOPS-mo) is right. **Note the
     documented caveat: throughput parity is NOT modelled** — a >170 GB gp2
     volume has >125 MB/s baseline throughput that gp3 may need provisioned
     (priced via `get_ebs_throughput_monthly_price`); quantify whether the
     omission overstates the saving for large gp2 volumes.
   - **over-provisioned IOPS**: `_scan_over_provisioned_iops` prices gp3 (billable
     IOPS above 3,000 × gp3 rate), io2 (tiered via `get_ebs_io2_iops_cost` —
     confirm the 32k/64k tier breakpoints and rates $0.065/$0.0455/$0.032), and
     io1 (flat $/IOPS-mo). Confirm the saving is `(billable_current −
     billable_recommended)` and that `recommend_iops_from_usage` only fires when
     `peak × 1.3 headroom < provisioned` (real CloudWatch evidence).
   - **unattached_volumes**: `_estimate_volume_cost` (size×$/GB + provisioned
     IOPS + throughput). Validate it uses region-correct PricingEngine values and
     does NOT double-apply `pricing_multiplier` when the engine is present;
     confirm 100%-on-delete is the right basis for an `available` volume.
   - **snapshot rate (leak-out)**: `get_ebs_snapshot_price_per_gb()` ≈ $0.05/GB-mo;
     the snapshot saving uses `VolumeSize × rate` ("max estimate") — confirm the
     overstatement is labelled and that it lands in the Snapshots tab, not the
     EBS counted total.
4. Confirm the basis is defensible from the report alone — the gp2 recs and IOPS
   recs already carry an `AuditBasis`; confirm `unattached`, CoH, and CO findings
   carry rate/region/formula too, and add `AuditBasis` where missing.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** `partition_enhanced_recs` routes `Volume Type Optimization`
   → `gp2_migration`, snapshot categories → `ebs_snapshots`, and **excludes the
   `Unattached Volumes` category** from `other` (it has its own source). Confirm
   no volume appears in both `gp2_migration` and `enhanced_checks/other`, and that
   the removed "unused encrypted volumes" check (every `available` encrypted
   volume is already an unattached volume) stays removed.
6. **Cross-source:** `dedupe_by_authority` removes CO recs whose `vol-…` is in
   the CoH set, then removes heuristic recs (unattached / gp2 / other) whose id
   is in `coh ∪ co`. Prove `normalize_volume_id` handles `arn:…:volume/vol-abc`,
   `…/vol-abc`, and bare ids identically across `coh_volume_id` / `co_volume_id`
   (uses `volumeArn`) / `heuristic_volume_id` (uses `VolumeId`). Show one volume
   surfaced by all three counted once.
7. **Cross-adapter:**
   - **EBS ↔ EC2**: confirm `get_advanced_ec2_checks` emits NO root-volume resize
     (ceded to EBS) so the same volume dollars aren't in both tabs.
   - **EBS ↔ AMI / Snapshots**: `compute_ebs_checks` excludes snapshots backing a
     currently-registered self-owned AMI (`_current_ami_snapshot_ids`) and routes
     only deregistered-AMI ("Created by CreateImage") + standalone old snapshots
     to `ebs_snapshots`. Verify the AMI tab and the EBS Snapshots tab never count
     the same snapshot id (cross-check with the AMI adapter's emitted set).

### Phase 4 — Coverage (works for ALL volumes, not a subset)
8. Confirm pagination of `describe_volumes` (unattached, gp2, IOPS) and
   `describe_snapshots`. Confirm the gp2 scan filters `volume-type=gp2`, the IOPS
   scan filters `io1/io2/gp3`, and the unattached scan filters `status=available`
   AND excludes volumes attached to **stopped** instances (`stopped_instance_volumes`).
9. Are whole classes skipped intentionally? `st1`/`sc1` (no gp2→gp3, no
   provisioned IOPS); gp3 with throughput over-provisioned but IOPS ≤ 3,000 (not
   covered by the IOPS check); volumes attached to stopped instances (excluded
   from unattached — confirm that's right since a stopped instance still bills
   for its EBS). Confirm each skip is documented.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except`, `logger`-only, and `return []`/`return 0.0` path:
    - `_drop_stale_delete_recs` records `ctx.warn` on a verification failure and
      on each dropped stale rec — good; confirm it only re-validates **delete**
      actions (a CoH "rightsize attached volume" on a now-deleted volume is NOT
      re-checked).
    - `compute_ebs_checks` outer `except` → generic `ctx.warn` (NOT
      permission-classified) — a denied `describe_volumes`/`describe_snapshots`
      should be `ctx.permission_issue`. `get_unattached_volumes` DOES classify
      permission (good) — make the two consistent.
    - `_observed_peak_iops` / `_estimate_volume_cost` / `_current_ami_snapshot_ids`:
      confirm a CloudWatch or pricing miss returns None/skip with a trace, never a
      fabricated saving.
11. Does a pricing miss fall back to `0.0` and still emit a finding? `gp2_total`
    sums per-volume nets (>0 guarded by `max(…,0)`); confirm a `$0` net gp2 rec
    is not counted, and that the gp2 source rec's placeholder string
    (`"$0.00/month - adapter computes per-volume"`) is overwritten with the real
    per-volume figure before render.
12. **CloudWatch gating / fast-mode:** `_scan_over_provisioned_iops` records a
    single `ctx.warn` and bails under `ctx.fast_mode`, and emits a per-volume
    `ctx.warn` when a candidate has no CloudWatch IOPS data (no fabricated
    saving). Confirm `reads_fast_mode`/`requires_cloudwatch` are honoured end to
    end and that throttling/permission failures on the IOPS metric reads are
    recorded.

### Phase 6 — Reporting (one tab, counted == rendered)
13. **`ebs_snapshots` leak-out (verify carefully):** the adapter emits an
    `ebs_snapshots` SourceBlock, but EBS is in `_PHASE_B_SKIP_PER_REC` and there
    is **no `("ebs","ebs_snapshots")` handler** in `PHASE_B_HANDLERS` — so it
    renders **nothing in the EBS tab** by design, and is consumed by the
    Snapshots tab. Trace the Snapshots-tab extractor in `html_report_generator.py`
    and **prove it actually reads the `ebs_snapshots` block**. If no extractor
    consumes it, those snapshot findings are silently dropped from the whole
    report (a dead-renderer tell).
14. **Counted == rendered:** `_coh_is_renderable` must mirror
    `_render_ebs_cost_hub`'s filter exactly (actionType + ebsVolume + not
    Optimized). Confirm `total_monthly_savings` = CoH + CO + unattached + gp2 +
    other (NOT snapshots), and equals the sum of the rendered counted cards.
    Confirm `total_recommendations` counts the five counted sources + `gp2_kept`
    (note `total_recs` excludes `snapshot_recs`).
15. Reconcile the EBS per-service total against the executive-summary headline.

### Phase 7 — Tooling & evidence
16. Run a real scan scoped to EBS:
    `.venv/bin/python cli.py <region> --scan-only ebs`
    then `.venv/bin/python tools/scan_doctor.py <json> --service ebs`.
    Triage every: silent failure, `$0`/missing-savings finding, and volume in >1
    source. Caveats: exercise a large gp2 volume (>1 TB, baseline IOPS > 3,000) to
    test the gp3 IOPS-parity net; exercise io2 > 32k IOPS for the tiered cost;
    exercise the fast-mode IOPS skip; confirm the Snapshots tab renders the
    `ebs_snapshots` leak-out. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC`.
17. For any duplication claim, prove it: show one `vol-…` in two sources. For any
    accuracy claim, show the AWS Pricing API value (gp2/gp3 $/GB, gp3/io2 IOPS,
    snapshot $/GB) next to the scanner's constant.

### Deliverable
- The complete check list (Phase 1.2), per source, counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations**.
  End with an ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Extend the EBS tests (mirror `tests/test_rds_audit_fixes.py` /
  `tests/test_ec2_audit_fixes.py`): unit-test the pure logic in
  `services/ebs_logic.py` (`dedupe_by_authority`, `partition_enhanced_recs`,
  `gp2_to_gp3_net_savings`, `gp2_baseline_iops`, `recommend_iops_from_usage`,
  `normalize_volume_id`), and drive `EbsModule.scan` with a `SimpleNamespace` ctx
  + monkeypatched sources + fake boto3 clients/paginators. Cover every fix:
  permission-classified outer except, throughput-parity (if adopted), snapshot
  leak-out render, cross-source dedup, fast-mode skip, counted==rendered.
- Record a structured `AuditBasis` (rate/region/metric-window/formula) on each
  counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for EBS first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the `ebs.py` row in `services/adapters/CLAUDE.md` if behaviour changes.
- Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings computed from a config dimension alone (size/IOPS) with no usage
  metric → fabricated $.
- Wrong type/tier pricing (gp2 priced as gp3; io2 base rate flat-multiplied over
  the tiered SKU; throughput parity omitted).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded fallback (0.10/0.08/0.005/0.065/0.05) not region-scaled, OR
  `pricing_multiplier` double-applied on the region-correct engine path.
- Per-unit RATE string ($/GB) counted as a monthly total — rejected by
  `parse_dollar_savings` → $0 advisory.
- Free-tier resource recommended for a saving it cannot realize (gp3 free 3,000
  IOPS / free 125 MB/s throughput).
- Same volume counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic by NORMALIZED `vol-…` id.
- Two heuristic checks stacking on one volume, or SUBSET redundancy (unused
  encrypted ⊆ unattached) — fix by removal.
- Reduction factor instead of exact price delta — gp2→gp3 must be the real
  $/GB delta net of IOPS parity, not a flat %.
- `$0` "enable X"/opt-in placeholder (CO `compute-optimizer-service`) counted
  instead of `ctx.warn`'d and dropped.
- Metric-gated `$0` nudge rendered as COUNTED instead of advisory.
- Cost Hub: `EbsVolume` type with no bucket → dropped; bucket populated but
  consumed by no adapter → dropped silently; a stale "delete" rec on a
  now-attached volume acted on.
- A source the adapter emits with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC`
  service → renders nothing silently (the `ebs_snapshots` leak-out).
- Render-time `Optimized`/finding filter desyncing the headline from cards
  (filter at SOURCE — `_coh_is_renderable` vs `_render_ebs_cost_hub`).
- Coverage gated to a hardcoded type/state allowlist (st1/sc1 skipped;
  stopped-instance volumes excluded from unattached).
- CloudWatch/CO/CoH permission or throttling failure logged via `logger`/generic
  `ctx.warn` only, not classified as `ctx.permission_issue`.
- CloudWatch reads not gated on `ctx.fast_mode` (the IOPS check).
- Cross-adapter overlap (same volume in EC2+EBS; same snapshot in AMI+Snapshots).
- Each counted finding must carry a structured `AuditBasis`; counted == rendered.

#### EBS-specific items (found in this code)
- **`ebs_snapshots` leak-out has no EBS `PHASE_B` handler** — verify the
  Snapshots-tab extractor in `html_report_generator.py` actually consumes it;
  otherwise the snapshot findings vanish from the report (dead-renderer tell).
- **gp2→gp3 throughput parity not modelled** (only IOPS parity) — large gp2
  volumes (>170 GB, >125 MB/s baseline) may overstate the net saving; the code
  documents this as a caveat.
- **`compute_ebs_checks` outer `except` is not permission-classified** while
  `get_unattached_volumes` is — a denied `describe_volumes`/`describe_snapshots`
  on the enhanced path is a generic `ctx.warn`, not a `ctx.permission_issue`.
- **`_drop_stale_delete_recs` only re-validates `delete` actions** — a stale CoH
  rightsize/upgrade rec on a deleted/detached volume is not re-checked.
- **Snapshot "max estimate" (`VolumeSize × rate`)** overstates incremental
  storage; confirm it never leaks into the EBS counted total and that the
  Snapshots tab labels it honestly.
- **io2 tiered cost** (`get_ebs_io2_iops_cost`) — confirm the 32k/64k breakpoints
  and per-tier SKUs against the live API so big-io2 savings aren't flat-rated.

## PROMPT (end)
