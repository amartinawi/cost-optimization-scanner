# AMI Adapter Cost-Audit Prompt

A deep, AMI-specific audit brief in the same structure as the Network / Lambda /
RDS audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* AMI code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`ami`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. The
AMI saving is **entirely EBS-snapshot storage**, so the canonical sibling
references for this adapter's patterns are the **EBS** adapter
(`services/adapters/ebs.py` + `services/ebs.py` snapshot handling) for the
snapshot-ownership boundary, and the **Lambda** adapter
(`services/adapters/lambda_svc.py`) for the `parse_dollar_savings` / advisory-`$0`
discipline and the test style I expect (`tests/test_lambda_audit_fixes.py`,
`tests/test_rds_audit_fixes.py`).

### NOTE on structure (AMI is a thin field-extraction adapter — NOT multi-source)
- `services/adapters/ami.py` → `AmiModule.scan` is a thin wrapper over
  `services/ami.py:compute_ami_checks`. All logic is in the shim.
- It emits **two SourceBlocks**, split purely by age for confidence/presentation
  — **both are quantified deletion candidates** (not a counted/advisory split):
  - `old_amis`    — unused AMIs **older than 90 days** (`OLD_SNAPSHOT_DAYS`).
  - `unused_amis` — unused AMIs with **30 < age ≤ 90 days** (`UNUSED_MIN_DAYS`).
- The saving is the backing **EBS-snapshot storage** cost. The adapter sums each
  rec's float `EstimatedMonthlySavings` field for the total (it does NOT use
  `parse_dollar_savings` on the string, even though `services/adapters/CLAUDE.md`
  lists AMI under "Parse-rate / `parse_dollar_savings()`" — reconcile this).
- **AMI has NO Cost Optimization Hub source, NO Compute Optimizer source, and NO
  CloudWatch reads.** There is no `AmiImage` entry in
  `scan_orchestrator._prefetch_advisor_data.type_map`, and the adapter declares
  neither `requires_cloudwatch` nor `reads_fast_mode`. So "missing CoH/CO source"
  is NOT fair game — savings are expected to be locally derived from snapshot
  storage. Drop the CoH/CO/CloudWatch axes; focus on snapshot pricing accuracy,
  the unused-detection correctness (false-positive deletion risk), the AMI↔EBS
  snapshot-ownership boundary, silent failures, and render wiring.
- Pricing: `core/pricing_engine.py:get_ebs_snapshot_price_per_gb()` (region-correct,
  Standard tier `FALLBACK_EBS_SNAPSHOT_GB_MONTH = $0.05/GB-mo us-east-1`,
  Archive tier available but not used here). Fallback path is
  `0.05 × pricing_multiplier`.

### Phase 0 — Orient (5-minute map before judging)
0a. Confirm identity in `services/adapters/ami.py`: `key="ami"`,
    `cli_aliases=("ami",)`, `display_name="AMI"`, no `reads_fast_mode`/
    `requires_cloudwatch`, `required_clients()=("ec2","autoscaling")`.
0b. Read `services/adapters/CLAUDE.md` — note AMI is listed as Parse-rate
    (`parse_dollar_savings()`) but the adapter actually sums the float
    `EstimatedMonthlySavings` field. Decide which is authoritative and flag the
    doc/code mismatch.
0c. **Render path is the subtle one:** `ami` is NOT in `PHASE_A_DESCRIPTORS`, NOT
    in `PHASE_B_HANDLERS`, and NOT in `_PHASE_B_SKIP_PER_REC`
    (`reporter_phase_b.py`). Therefore `should_fallback_to_per_rec("ami")` is
    True and both sources render via the **generic per-record renderer**. Confirm
    that renderer reads `EstimatedSavings` / `Recommendation` / `CheckCategory`
    off the AMI rec shape — a field-name mismatch would render blank cards.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/ami.py` and `services/ami.py` in full
   (`compute_ami_checks`, `_snapshot_storage_gb`, constants `OLD_SNAPSHOT_DAYS=90`,
   `UNUSED_MIN_DAYS=30`); `core/pricing_engine.py:get_ebs_snapshot_price_per_gb`
   and `FALLBACK_EBS_SNAPSHOT_GB_MONTH`; `core/contracts.py`;
   `core/result_builder.py`; the generic per-rec renderer in `reporter_phase_b.py`
   / `html_report_generator.py`; and the EBS snapshot path
   (`services/ebs.py:compute_ebs_checks` + `_current_ami_snapshot_ids`) for the
   ownership boundary (Phase 3).
2. List **every** cost check (there are effectively two — `old_amis` and
   `unused_amis`, same logic, split by age) with: trigger condition, data source
   (`describe_images` Owners=self, snapshot sizing via `describe_snapshots`), the
   savings formula (`total_snapshot_gb × snapshot_rate`), and the unused-detection
   inputs (running/stopped instance `ImageId`s, launch-template `ImageId`s, ASG
   launch-configuration `ImageId`s).

### Phase 2 — Accuracy of every number (validate with MCP)
3. Re-derive the snapshot rate from the live AWS Pricing API and confirm:
   - **SKU**: `get_ebs_snapshot_price_per_gb()` should resolve the EBS **Snapshot
     Standard** tier (`AmazonEC2`, `productFamily=Storage Snapshot`,
     `usagetype` like `*EBS:SnapshotUsage`), ≈ $0.05/GB-mo us-east-1. Validate
     the live value and confirm the `$0.05` fallback is us-east-1 and is
     region-scaled by `pricing_multiplier` ONLY on the fallback path (not on the
     engine path — no double-multiply).
   - **Stored-bytes vs provisioned**: `_snapshot_storage_gb` prefers
     `FullSnapshotSizeInBytes` (actual stored blocks) over `VolumeSize`, then
     falls back to `VolumeSize` (and to a hardcoded `8` GB on a
     `describe_snapshots` error). Confirm `FullSnapshotSizeInBytes` is the right
     billing basis and that the `VolumeSize`/`8`-GB fallbacks **overstate** (the
     saving string is labelled "max estimate" — confirm that label is honest).
   - **Incremental & shared snapshots**: EBS snapshots are incremental and a
     block can be shared across an AMI lineage. Confirm whether the same physical
     snapshot id can be summed under **multiple AMIs** (no cross-AMI snapshot-id
     dedup) — if so, the account total can exceed the real billable storage.
4. Confirm the basis is defensible from the report alone: each rec carries
   `SnapshotSizeGB`, `SnapshotIds`, `AgeDays`, and an `EstimatedSavings` string
   with the "max estimate" qualifier. Add a structured `AuditBasis`
   (rate/region/GB/formula) mirroring the EBS snapshot recs, which already carry
   one.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** `old_amis` and `unused_amis` are mutually exclusive by age
   (`is_old = age_days > OLD_SNAPSHOT_DAYS`) — confirm no AMI lands in both, and
   that the adapter sums each rec once.
6. **Cross-AMI snapshot sharing:** as in Phase 2, confirm whether two AMIs that
   share a backing snapshot id each count that storage (no de-dup across AMIs).
7. **Cross-adapter (AMI ↔ EBS snapshots — the important one):** the EBS shim
   (`services/ebs.py:compute_ebs_checks`) excludes snapshots that back a
   **currently-registered self-owned AMI** via `_current_ami_snapshot_ids`, and
   routes only **deregistered-AMI ("Created by CreateImage")** and standalone
   old snapshots to the EBS Snapshots tab. Verify the boundary holds **both
   ways**: (a) every snapshot the AMI tab counts is excluded by EBS; (b) there is
   no *gap* — e.g. an unused AMI **younger than 30 days** is skipped by AMI but
   its snapshot is still "ami-backed" so EBS excludes it too → counted by
   **neither** tab (a conservative coverage gap; confirm it is intentional, not a
   silent loss of a real saving). Show the `_current_ami_snapshot_ids` set next
   to the set AMI actually emits.

### Phase 4 — Coverage (works for ALL AMIs, not a subset)
8. Confirm `describe_images` is **paginated** (it is — `Owners=["self"]`) and
   that `describe_instances` (running+stopped) is paginated for the
   in-use set. Note `describe_launch_templates` and
   `describe_auto_scaling_groups` are NOT paginated — flag any account that could
   exceed the default page and silently mark an in-use AMI as unused.
9. **Unused-detection completeness (false-positive DELETION risk — highest
   stakes):** the in-use set is built from (a) running/stopped instance
   `ImageId`, (b) all launch-template versions' `ImageId`, (c) ASG
   **LaunchConfiguration** `ImageId`. Confirm what is **missing**: ASGs using a
   **LaunchTemplate** (or MixedInstancesPolicy) rather than a launch
   configuration; EC2 Fleet / Spot Fleet request AMIs; AMIs referenced by other
   accounts (shared); AMIs in an Image Builder pipeline. Any reference path not
   covered means an in-use AMI is flagged "unused → deregister and delete
   snapshots" — a destructive false positive. (The launch-template global scan
   partially covers ASG-LT AMIs, but confirm.)
10. Instance-store AMIs (no EBS snapshots) correctly yield `total_gb == 0` and
    are **skipped** rather than emitted at `$0` — confirm.

### Phase 5 — Silent failures (nothing fails quietly)
11. Find every `except: pass`, bare `except`, and `logger`-only path:
    - **Outer `except` in `compute_ami_checks` (≈ line 174)** logs
      `logger.warning("Could not get AMI checks")` only — a denied/throttled
      `describe_images` or `describe_instances` makes the **entire AMI tab
      silently empty** with NO `ctx.warn` / `ctx.permission_issue`. Classify
      `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`, other →
      `ctx.warn`. (Canonical prior-audit silent-failure class.)
    - **Launch-template and ASG resolution use `except: pass`** (≈ lines 101-119)
      — if `describe_launch_templates` / `describe_auto_scaling_groups` /
      `describe_launch_configurations` is denied, those AMIs vanish from the
      in-use set and become **false-positive deletion candidates**. This is the
      most dangerous silent failure: a permission gap converts a safe AMI into a
      "delete me" rec. Record it on `ctx` and consider failing safe (treat
      unknown as in-use).
    - **`_snapshot_storage_gb` inner `except`** logs `logger.warning` and falls
      back to `VolumeSize` or `8` GB — confirm a `describe_snapshots` permission
      error is surfaced, not silently inflated.
12. Does a pricing miss fall back to `0.0` and still emit a finding? The rate
    fallback is `0.05 × multiplier` (never 0), and `total_gb == 0` AMIs are
    skipped — confirm no `$0` rec is ever appended/counted.

### Phase 6 — Reporting (one tab, counted == rendered)
13. **Generic per-rec render (verify carefully):** with `ami` absent from
    `PHASE_A_DESCRIPTORS`, `PHASE_B_HANDLERS`, and `_PHASE_B_SKIP_PER_REC`, both
    `old_amis` and `unused_amis` go through the generic per-record renderer.
    Trace `html_report_generator._get_detailed_recommendations` /
    `_render_*` for the per-rec fallback and confirm it reads the AMI rec fields
    (`EstimatedSavings`, `Recommendation`, `CheckCategory`, `ImageId`). Confirm
    both sources surface under a single AMI tab and nothing renders blank.
14. **Counted == rendered:** `total_monthly_savings` sums the float
    `EstimatedMonthlySavings`; the rendered card shows the `EstimatedSavings`
    *string* ($X.XX/month). Confirm the parsed/summed number and the displayed
    string agree (they derive from the same `monthly_snapshot_cost`), and that
    `total_recommendations == len(old_amis) + len(unused_amis)`. Reconcile the
    AMI per-service total against the executive-summary headline.

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to AMI:
    `.venv/bin/python cli.py <region> --scan-only ami`
    then `.venv/bin/python tools/scan_doctor.py <json> --service ami`.
    Triage every: silent failure, `$0`/missing-savings finding, and AMI whose
    snapshot id also appears in the EBS Snapshots tab. Caveats: use an account
    with self-owned AMIs and some deregistered-AMI snapshots (exercise the AMI↔EBS
    boundary); use `.venv/bin/python` (3.14) — system `python3` lacks
    `datetime.UTC` (and `services/ami.py` imports `from datetime import UTC`).
16. For any duplication claim, prove it: show a snapshot id counted under an AMI
    and also surfaced by EBS. For any accuracy claim, show the AWS Pricing API
    EBS-snapshot $/GB value next to the scanner's rate.

### Deliverable
- The complete check list (Phase 1.2), with the unused-detection inputs
  enumerated and gaps marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact (and, for unused-detection gaps, the **deletion-safety**
  impact). Separate **confirmed bugs** from **known limitations**. End with an
  ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_ami_audit_fixes.py` mirroring `tests/test_lambda_audit_fixes.py`
  / `tests/test_rds_audit_fixes.py`: test the pure logic
  (`_snapshot_storage_gb` stored-bytes-vs-VolumeSize, age split at 90/30, the
  in-use set assembly, instance-store skip) and drive `compute_ami_checks` /
  `AmiModule.scan` with a `SimpleNamespace` ctx + fake boto3 paginators for
  `describe_images`/`describe_instances`/`describe_launch_templates`/
  `describe_auto_scaling_groups`/`describe_snapshots`. Cover every fix:
  silent-failure classification, fail-safe unused detection on permission error,
  snapshot rate region scaling, cross-AMI snapshot dedup (if adopted), generic
  render wiring, counted==rendered.
- Record a structured `AuditBasis` (rate/region/GB/formula) on each finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for AMI first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Reconcile the `ami.py` row in `services/adapters/CLAUDE.md` (parse-rate vs
  float-field) to match reality.
- Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage/storage savings computed from a config dimension alone with no usage
  evidence → fabricated $.
- Region: hardcoded constant/fallback ($0.05) not region-scaled, OR
  `pricing_multiplier` double-applied on the region-correct engine path.
- Per-unit RATE string ($/GB) counted as a monthly total — must be rejected by
  `parse_dollar_savings` → $0 advisory (here the float field bypasses the parser
  entirely; confirm that is safe).
- Free / no-cost resource recommended for a saving it cannot realize
  (instance-store AMI with no EBS snapshot → must be skipped).
- Two checks/populations overlapping or SUBSET redundancy — fix by removal.
- Reduction factor / max-estimate instead of exact billed storage (incremental
  snapshots; `VolumeSize` overstates ~2× vs `FullSnapshotSizeInBytes`).
- `$0` placeholder counted instead of dropped.
- A source with no `PHASE_B` handler → renders nothing (here AMI relies on the
  generic per-rec fallback — confirm it actually renders).
- Coverage gated to a hardcoded state/owner allowlist; an in-use resource flagged
  for deletion because a reference path (ASG launch template, EC2 Fleet, shared
  AMI) was not consulted.
- `describe_*` permission/throttle failure logged via `logger` only, not recorded
  via `ctx.warn`/`ctx.permission_issue` (AccessDenied → permission_issue).
- Cross-adapter overlap (same snapshot in the AMI tab and the EBS Snapshots tab).
- Each counted finding must carry a structured `AuditBasis`; counted == rendered.

#### AMI-specific items (found in this code)
- **Outer `except` empties the whole AMI tab silently** (`logger.warning` only,
  no `ctx.warn`/`permission_issue`) — a denied `describe_images` looks like "no
  unused AMIs".
- **Launch-template / ASG `except: pass`** converts a permission gap into
  **false-positive deletion recs** for AMIs that ARE in use — the highest-stakes
  bug (recommends deleting a live AMI's snapshots). Fail safe: treat unresolved
  references as in-use.
- **Unused detection misses reference paths**: ASGs using a `LaunchTemplate`/
  MixedInstancesPolicy (only `LaunchConfigurationName` is checked), EC2/Spot
  Fleet, and shared (cross-account) AMIs — each gap is a deletion-safety risk.
- **No cross-AMI snapshot-id dedup** — two AMIs sharing a snapshot lineage can
  each count the same (incremental, shared) storage, inflating the account total.
- **`describe_launch_templates` / `describe_auto_scaling_groups` are not
  paginated** — a large account can drop an in-use AMI off the page and flag it
  unused.
- **`8`-GB and `VolumeSize` fallbacks in `_snapshot_storage_gb`** overstate the
  saving vs `FullSnapshotSizeInBytes`; the "max estimate" label must stay honest.
- **Doc/code mismatch**: `services/adapters/CLAUDE.md` lists AMI as
  `parse_dollar_savings()` parse-rate, but the adapter sums the float
  `EstimatedMonthlySavings` field.

## PROMPT (end)
