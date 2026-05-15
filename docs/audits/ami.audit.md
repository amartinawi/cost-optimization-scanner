# Audit: ami Adapter

**Adapter**: `services/adapters/ami.py` (62 lines)
**Legacy shim**: `services/ami.py` (135 lines) — analysis lives here, adapter is a thin wrapper
**ALL_MODULES index**: 1 (registered immediately after `ec2`)
**Renderer path**: Generic per-record fallback (`reporter_phase_b.render_generic_per_rec` → `_render_generic_other_rec`). AMI is not in `_PHASE_A_SERVICES`, has no entry in `PHASE_B_HANDLERS`, and is not in `_PHASE_B_SKIP_PER_REC`.
**Date**: 2026-05-15
**Auditor**: automated (per `docs/audits/AUDIT_PROMPT.md`)

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **FAIL** | 7 (1 CRITICAL, 4 HIGH, 1 MEDIUM, 1 LOW) |
| L2 Calculation | **FAIL** | 5 (3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 4 (1 HIGH, 1 MEDIUM, 2 LOW/INFO) |
| **Overall**    | **FAIL** | scope-rule + paginator + hardcoded-price triple-strike |

---

## Pre-flight facts

- **Adapter class**: `AmiModule` at `services/adapters/ami.py:12` (inherits `BaseServiceModule`)
- **Required boto3 clients**: `("ec2", "autoscaling")`
- **Flags**: `requires_cloudwatch=False`, `reads_fast_mode=False` — matches code (no CW calls, no fast-mode branching)
- **Sources emitted**: `{"old_amis"}` — a single `SourceBlock` that **mislabels** the unused-AMI recommendations (the block's `recommendations` tuple is `result["old_amis"] + result["unused_amis"]`)
- **Pricing methods consumed**: **none** from `PricingEngine`. Hardcoded `0.05 * pricing_multiplier` in `services/ami.py:110`
- **Test files**: `tests/test_offline_scan.py`, `tests/test_regression_snapshot.py`, `tests/test_reporter_snapshots.py`, `tests/test_result_builder.py`, fixture seeded in `tests/fixtures/aws_state/us_east_1.py:_seed_amis`

---

## L1 — Technical findings

### L1-001 `print()` calls escape into stdout instead of logger / ctx.warn  [HIGH]
- **Check**: violates `~/.claude/rules/python/hooks.md` and standard adapter hygiene
- **Evidence**:
  - `services/adapters/ami.py:35` — `print("\U0001f50d [services/adapters/ami.py] AMI module active")`
  - `services/ami.py:22` — `print("🔍 [services/ami.py] AMI module active")`
  - `services/ami.py:102` — `print(f"⚠️ Error getting snapshot details for {snapshot_id}: {str(e)}")`
  - `services/ami.py:129` — `print(f"Warning: Could not get AMI checks: {e}")`
- **Why it matters**: per-scan stdout pollution; errors invisible to `ctx._warnings`/`ctx._permission_issues` → never surface in the HTML "Scan Warnings" or "Permission Issues" sections
- **Recommended fix**: route module-active banners to `logger.debug`; route the two error sites through `ctx.warn(..., service="ami")` (or `ctx.permission_issue` for AccessDenied)

### L1-002 Silent `except Exception: pass` swallowing four AWS-call sites  [HIGH]
- **Check**: L1.2.2 — every `except` must record to `ctx.warn`/`ctx.permission_issue` or re-raise
- **Evidence**:
  - `services/ami.py:47-48` — launch-template-versions describe swallowed
  - `services/ami.py:49-50` — launch-templates list swallowed
  - `services/ami.py:62-63` — launch-configurations describe swallowed
  - `services/ami.py:64-65` — auto-scaling-groups list swallowed
- **Why it matters**: an IAM gap on any of these four calls silently drops a class of "AMI in use" detections, which **inflates** the unused/old AMI counts and the savings number
- **Recommended fix**: classify each via the `_route_bucket_error`-style helper (or inline `ctx.permission_issue` on `AccessDenied`/`UnauthorizedOperation`, `ctx.warn` otherwise)

### L1-003 `except Exception as e: print(...)` instead of ctx.warn  [HIGH]
- **Check**: L1.2.4 — non-permission errors must route through `ctx.warn`
- **Evidence**:
  - `services/ami.py:101-103` — snapshot describe failure prints + falls back to `block_device["Ebs"].get("VolumeSize", 8)`
  - `services/ami.py:128-129` — top-level failure prints and returns whatever was collected
- **Why it matters**: same surface problem as L1-001; additionally, the snapshot-describe fallback masks repeated IAM denies behind a fabricated size (see L2-003)
- **Recommended fix**: replace each `print` with `ctx.warn(f"AMI snapshot describe failed for {snapshot_id}: {e}", service="ami")` and surface AccessDenied via `ctx.permission_issue`

### L1-004 No `AccessDenied` / `UnauthorizedOperation` discrimination  [HIGH]
- **Check**: L1.2.3 — IAM gaps must route through `ctx.permission_issue`
- **Evidence**: `services/ami.py` has **zero** references to `AccessDenied`, `UnauthorizedOperation`, or `ctx.permission_issue`; all error paths are bare `except Exception`
- **Why it matters**: AMI runs against `ec2:DescribeImages`, `ec2:DescribeInstances`, `ec2:DescribeLaunchTemplates`, `ec2:DescribeLaunchTemplateVersions`, `ec2:DescribeSnapshots`, `autoscaling:DescribeAutoScalingGroups`, `autoscaling:DescribeLaunchConfigurations`. An account missing any one will silently produce wrong results; the "Permission Issues" section of the HTML report stays empty
- **Recommended fix**: wrap each AWS call in a `_route_ami_error` helper (reuse the pattern in `services/s3.py:_route_bucket_error`)

### L1-005 None of the five list/describe calls uses a paginator  [HIGH]
- **Check**: L1.2.1 — list operations must use paginators
- **Evidence** (`services/ami.py`):
  - line 28 — `ec2.describe_images(Owners=["self"])` — paginator name `describe_images` exists per boto3 docs (verified: see External validation)
  - line 39 — `ec2.describe_launch_templates()` — paginator exists (`describe_launch_templates`)
  - line 42 — `ec2.describe_launch_template_versions(...)` — paginator exists
  - line 53 — `autoscaling.describe_auto_scaling_groups()` — paginator exists
  - line 57 — `autoscaling.describe_launch_configurations(...)` — paginator exists
  - line 98 — `ec2.describe_snapshots(SnapshotIds=[snapshot_id])` — single-ID lookup, paginator not required here but the call is inside a per-block-device loop, multiplying API roundtrips (consider batching `SnapshotIds=` list of 200 per call instead)
- **Why it matters**: accounts with >1000 AMIs / >100 launch templates / >100 ASGs hit the default `MaxResults` ceiling and skip the trailing pages — silently dropping in-use detections, inflating "unused" recs
- **Recommended fix**: use `get_paginator("describe_images").paginate(Owners=["self"])` (already used correctly for `describe_instances` on line 31 — the rest of the file should match). For `describe_snapshots`, collect all `SnapshotIds` up front and pass them as a single batched list

### L1-006 SourceBlock mislabel — `old_amis` block contains both old AND unused recs  [HIGH]
- **Check**: L1.1.8 / L3.1.3 — source name must reflect the recommendations it contains
- **Evidence**:
  - `services/ami.py:131-135` — return dict's `"recommendations"` key is `checks["old_amis"] + checks["unused_amis"]`
  - `services/adapters/ami.py:37-44` — `recs = result.get("recommendations", [])` then `sources={"old_amis": SourceBlock(count=len(recs), recommendations=tuple(recs))}` — a rec with `CheckCategory: "Unused AMIs"` (services/ami.py:84) is filed under the `old_amis` source name
- **Why it matters**: every downstream consumer (JSON output, HTML "Source" label, snapshot tests) sees unused-AMI recs labelled as if they were old-AMI recs. Confusing for the audit trail and breaks per-source dedup / dashboards
- **Recommended fix**: emit two distinct `SourceBlock`s — `"old_amis"` and `"unused_amis"` — and extend `optimization_descriptions` to cover both. Match `total_recommendations = len(old) + len(unused)` and `total_monthly_savings = sum(old EstimatedMonthlySavings)` (unused contribute $0 today; see L2-004 for the scope-rule consequence)

### L1-007 Scope-rule violation — `unused_amis` emits `"varies by AMI size"` placeholder  [CRITICAL]
- **Check**: cost-only scope rule (added 2026-05-14, `docs/audits/AUDIT_PROMPT.md` §"Scope Rule: Cost Only") — every check must produce a concrete $ saving; "varies by X" placeholder is REMOVE
- **Evidence**: `services/ami.py:83` — `"EstimatedSavings": "Snapshot storage costs (varies by AMI size)"` with no `EstimatedMonthlySavings` numeric field
- **Why it matters**: the precedent purge in `CHANGELOG.md [3.4.0]` cleared exactly this shape across the codebase; the AMI shim is a regression. The unused-AMI count inflates `total_recommendations` while contributing $0 to `total_monthly_savings` — exactly the metric-divergence the rule was added to stop
- **Recommended fix**: either (a) compute snapshot-size × `PricingEngine.get_ebs_snapshot_price_per_gb()` for unused AMIs the same way old AMIs do — they share the cost basis — or (b) drop the `unused_amis` recommendation generator entirely. Option (a) is preferred because the detection signal is genuinely useful

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| `old_amis` — snapshot $/GB-month | `module-const` (`0.05`) | `services/ami.py:110` | **WARN** — duplicates `core/pricing_engine.py:70` `FALLBACK_EBS_SNAPSHOT_GB_MONTH=0.05`; should use `ctx.pricing_engine.get_ebs_snapshot_price_per_gb()` | AWS Pricing API → `EBS:SnapshotUsage` → confirmed $0.05/GB-Mo us-east-1 (matches the constant) |
| `old_amis` — total_snapshot_size_gb | derived from `describe_snapshots[].VolumeSize` | `services/ami.py:100` | **WARN** — uses full volume size, not incremental block count | AWS EBS docs → snapshots are incremental, "saves only blocks changed since the most recent snapshot" |
| `old_amis` — fallback size `8 GB` | `arbitrary` | `services/ami.py:103, 106` | **FAIL** — invented constant when describe_snapshots fails or `VolumeSize` unset | n/a — no documented basis |
| `unused_amis` — savings string | `arbitrary` (placeholder) | `services/ami.py:83` | **FAIL** — "varies by AMI size" placeholder violates cost-only scope rule | n/a |

### L2-001 Hardcoded snapshot $/GB-month duplicates PricingEngine fallback  [HIGH]
- **Check**: L2.1 source classification + L2.3.1/L2.3.2 — should be `live` via PricingEngine, not `module-const`
- **Evidence**: `services/ami.py:110` — `monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * pricing_multiplier`
- **Why it matters**: (i) bypasses regional accuracy beyond a flat multiplier (Sao Paulo standard snapshots are not $0.05; eu-west-1 is not $0.05); (ii) ignores Snapshot Archive tier ($0.0125/GB-Mo, FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH at line 71); (iii) duplicates a constant that already exists in `core/pricing_engine.py:70`. EBS adapter uses `PricingEngine.get_ebs_snapshot_price_per_gb()` correctly — AMI should follow that template
- **Recommended fix**: replace `0.05 * pricing_multiplier` with `ctx.pricing_engine.get_ebs_snapshot_price_per_gb()` (engine already returns region-correct $/GB-month). Remove the `pricing_multiplier` argument from `compute_ami_checks` for the live path

### L2-002 Full volume size used, not incremental snapshot delta  [HIGH]
- **Check**: L2.5.5 / L2.6.1 — savings basis must match AWS billing model
- **Evidence**: `services/ami.py:108-110` — comment acknowledges the issue: "NOTE: Uses full EBS volume size, not incremental snapshot size. Actual incremental snapshots are typically 10-30% of full volume size." Yet the math at line 110 is `total_snapshot_size_gb * 0.05 * pricing_multiplier` with no incremental discount
- **Why it matters**: savings reported are **3×–10× too high** for any account with active EBS snapshots. AWS docs (External validation below) confirm "snapshots are incremental … we save only the blocks on the volume that have changed since the most recent snapshot". A 100-GB AMI snapshot bills closer to $0.50–$1.50/month, not the $5/month the adapter reports
- **Recommended fix**: either (a) accept the conservative overstatement and rename the field to "MaxEstimatedSavings" + add a confidence prefix (already done: "max estimate" appears in the display string at line 123 — good), OR (b) apply a documented incremental ratio (~0.2) and label it explicitly. Option (a) is acceptable IF the display string and JSON field both carry the "max" qualifier; option (b) is more accurate. **Today the JSON `EstimatedMonthlySavings` numeric field has no qualifier**, only the human-facing string does — so any aggregator reading the numeric field sees the inflated value as ground truth

### L2-003 Invented `8 GB` fallback when snapshot describe fails  [MEDIUM]
- **Check**: L2.1 — `arbitrary` values forbidden
- **Evidence**: `services/ami.py:103` — `total_snapshot_size_gb += block_device["Ebs"].get("VolumeSize", 8)`; `services/ami.py:105-106` — `if total_snapshot_size_gb == 0: total_snapshot_size_gb = 8`
- **Why it matters**: `8` is not the AWS default volume size (no such default exists for snapshots); it appears chosen for the t2.micro Amazon Linux AMI root volume size. For AMIs with multiple block devices or larger root volumes, this under-estimates; for tiny snapshots, it over-estimates. Either way the dollar value is unsourced
- **Recommended fix**: when `describe_snapshots` fails AND `block_device["Ebs"]["VolumeSize"]` is missing, skip the AMI entirely or emit it as `EstimatedMonthlySavings: 0.0` with a `pricing_warning` field. Never invent a size

### L2-004 `unused_amis` recs carry no `EstimatedMonthlySavings` field  [HIGH]
- **Check**: L2.5.1 — every counted rec must contribute a traceable dollar or be explicitly zero
- **Evidence**: `services/ami.py:73-86` — the `unused_amis.append({...})` dict has `EstimatedSavings` (string) but not `EstimatedMonthlySavings` (numeric). Adapter sum at `services/adapters/ami.py:38` skips them silently because `rec.get("EstimatedMonthlySavings", 0)` returns `0`
- **Why it matters**: `total_recommendations` increments but `total_monthly_savings` does not — a divergence the scope rule was explicitly added to stop. Compounded with L1-006 (mislabel) the user cannot tell from the JSON which recs are counted vs visibility-only
- **Recommended fix**: see L1-007. Either compute the snapshot-storage savings the same way old-AMI uses (the block-device snapshot IDs are available on the AMI image record itself) or remove the category

### L2-005 `pricing_multiplier` correctly applied to module-const path  [LOW / INFO]
- **Check**: L2.3.2 — module-const path must apply multiplier
- **Evidence**: `services/ami.py:110` — `total_snapshot_size_gb * 0.05 * pricing_multiplier` ✓
- **Status**: PASS — recorded as a positive control for the failures above

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing` | `service_code=AmazonEC2, region=us-east-1, filters=[productFamily=Storage Snapshot, usagetype=EBS:SnapshotUsage]` | `$0.0500000000 per GB-Mo` (SKU 7U7TWP44UP36AT3R, effectiveDate 2026-05-01) | confirms the constant is numerically right today, but argues it should live in `PricingEngine` not in the shim |
| L2-001 | `core/pricing_engine.py:70` | `FALLBACK_EBS_SNAPSHOT_GB_MONTH: float = 0.05` | duplicate constant already exists in `PricingEngine` | confirms duplication and that a `get_ebs_snapshot_price_per_gb()` helper is the right call |
| L2-002 | `WebFetch` | `https://docs.aws.amazon.com/ebs/latest/userguide/ebs-snapshots.html` | "A snapshot is an **incremental backup**, which means that we save only the blocks on the volume that have changed since the most recent snapshot" | confirms full-volume-size math overstates cost; refutes the implicit "size == cost" assumption at line 110 |
| L1-005 | `WebFetch` | `https://docs.aws.amazon.com/boto3/latest/reference/services/ec2/paginator/DescribeImages.html` | "Yes, boto3 EC2 client does expose a paginator for `describe_images`" | confirms the paginator exists; current code at services/ami.py:28 should use it |

---

## L3 — Reporting findings

### Field-presence table

| Source | Renderer | Required fields | Missing fields |
|---|---|---|---|
| `old_amis` (in fact: old+unused merged) | `_render_generic_other_rec` (generic per-rec fallback) | `CheckCategory` (resolved via `source_name.title()` when missing) → "Old Amis"; `ImageId`; `Name`; `AgeDays`; `Recommendation`; `EstimatedSavings`; (optional) `priority`/`severity` | `ImageId` is **not in** the resource-id resolver list at `reporter_phase_b.py:1622-1668` — recs render as h4 `"Old Amis: Resource"` literal string |

### L3-001 Single source name carries two semantically distinct rec sets  [HIGH]
- **Check**: L3.2.1 — every source name must reflect its content
- **Evidence**: same as L1-006. JSON output (`ScanResultBuilder._serialize`) emits `"sources": {"old_amis": {"count": N, "recommendations": [...]}}` where N = old + unused; `optimization_descriptions` only covers `old_amis` (`services/adapters/ami.py:46-60`)
- **Why it matters**: the HTML report's per-source heading and the snapshot test `tests/test_regression_snapshot.py::test_ami_has_total_count` cannot distinguish the two rec classes. Anyone reading the JSON downstream sees a `total_recommendations: 12` against `total_monthly_savings: $30` and cannot reconcile (half the recs contribute $0)
- **Recommended fix**: split into two SourceBlocks; add a second `optimization_descriptions["unused_amis"]` entry

### L3-002 Generic renderer cannot resolve `ImageId` → h4 reads literal "Resource"  [MEDIUM]
- **Check**: L3.2.2 — field names the renderer reads must be present in the rec
- **Evidence**: `reporter_phase_b.py:1622-1668` — resource_id resolver tries `LoadBalancerName`, `AutoScalingGroupName`, `VpcEndpointId`, `NatGatewayId`, `AllocationId`, `LogGroupName`, ... `SnapshotId`, ... `ServerId`, `ClusterArn`, ... but **never** `ImageId`. AMI recs at `services/ami.py:75, 114` set `ImageId` as the primary identifier
- **Why it matters**: every AMI rec card in the HTML report has the header "Old Amis: Resource" instead of "Old Amis: ami-0abc123...". User cannot tell which AMI to deregister from the report
- **Recommended fix**: add `or rec.get("ImageId")` to the resolver chain (cheapest fix); the EC2 enhanced-checks renderer already does this at `reporter_phase_b.py:33`

### L3-003 No `priority`/`severity` field on recs  [LOW]
- **Check**: L3.2.3 — priority class drops to "" when no recognized field is set
- **Evidence**: neither `old_amis` nor `unused_amis` rec dicts set `priority`, `Priority`, or `severity`. `reporter_phase_b._priority_class` returns "" → no CSS class → no visual differentiation
- **Why it matters**: a 600-day-old AMI gets the same visual weight as a 91-day-old AMI. Cosmetic but reduces the report's usefulness
- **Recommended fix**: derive `priority="high"` when `AgeDays > 365` and `priority="medium"` for 90-365 days. Cheap, no schema change

### L3-004 Renderer path is generic per-rec — SOURCE_TYPE_MAP label OK  [INFO]
- **Check**: L3.2.1
- **Evidence**: `reporter_phase_b.py:2106` — `"old_amis": "Audit Based"` in `_GENERIC_SOURCE_TYPES`. No PHASE_B_HANDLERS entry for ami; not in `_PHASE_A_SERVICES`; not in `_PHASE_B_SKIP_PER_REC` → falls through to `render_generic_per_rec(service_key="ami") → _render_generic_other_rec`. Labeling is accurate
- **Status**: PASS — recorded for completeness

---

## Cross-layer red flags

**None auto-FAIL** under §4 of the audit prompt:
- Adapter is read-only (no AWS write APIs)
- No `ClientRegistry` bypass — both clients come from `ctx.client(...)`
- No mutation of `ALL_MODULES`
- No service-key collision (`ami` is unique)
- Recommendations carry only `ImageId` / `SnapshotId` (no cross-account ARNs)

However the combination of L1-007 (scope-rule violation) + L2-002 (full-volume overstatement) + L2-003 (invented 8 GB) + L1-006 (mislabel) makes the total dollar figure both **systematically inflated** AND **traceable to undocumented constants** — exactly the failure mode the cost-only scope rule was added to prevent. Hence L1 FAIL.

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/ami.py services/ami.py
All checks passed!

$ python3 -m mypy services/adapters/ami.py services/ami.py
(no errors specific to ami.py — pre-existing errors are in aurora.py, bedrock.py, ebs.py, rds.py)

# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m = next(x for x in ALL_MODULES if x.key == 'ami'); \
    print('key:', m.key); print('aliases:', m.cli_aliases); \
    print('clients:', m.required_clients()); print('cw:', getattr(m, 'requires_cloudwatch', False))"
key: ami
aliases: ('ami',)
clients: ('ec2', 'autoscaling')
cw: False

# 3. CLI resolution
$ python3 -c "from core.filtering import resolve_cli_keys; from services import ALL_MODULES; \
    print(resolve_cli_keys(ALL_MODULES, {'ami'}, None))"
{'ami'}

# 4. Offline + 5. Regression / snapshot tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k ami -v
============================= 5 passed, 121 deselected in 0.06s ==============================

# 6. PricingEngine confirmation that a snapshot helper exists
$ grep -n "get_ebs_snapshot_price_per_gb\|FALLBACK_EBS_SNAPSHOT" core/pricing_engine.py
51:FALLBACK_EBS_GB_MONTH ...
70:FALLBACK_EBS_SNAPSHOT_GB_MONTH: float = 0.05
71:FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH: float = 0.0125
280:def get_ebs_snapshot_price_per_gb(self, *, archive_tier: bool = False) -> float:
582:def _fetch_ebs_snapshot_price(self, *, archive_tier: bool = False) -> float | None:
```

Tests pass against the recorded fixture (3 old AMIs, snapshot sizes seeded). Pass status reflects fixture-shape conformance, **not** correctness of the dollar amount — the inflated-by-incremental-ratio bug (L2-002) is masked because the golden was captured against the same flawed math.

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L1-007** — Decide unused_amis disposition: compute live snapshot-storage savings the same way old_amis does (preferred — the detection signal is valuable and the cost basis is identical) OR remove the unused_amis recommendation generator entirely. Either way bring `total_recommendations` in lockstep with `total_monthly_savings`.
2. **[HIGH] L2-001** — Replace `0.05 * pricing_multiplier` with `ctx.pricing_engine.get_ebs_snapshot_price_per_gb()`. Drop the `pricing_multiplier` arg from `compute_ami_checks` for the live path. Adds Snapshot Archive tier support for free.
3. **[HIGH] L2-002** — Either rename the JSON numeric field to `MaxEstimatedMonthlySavings` (cheap, honest) or apply a documented incremental ratio with a clear confidence prefix. Don't ship the unqualified inflated number through the JSON contract.
4. **[HIGH] L1-002 / L1-003 / L1-004** — Wrap every AWS call in an error router that distinguishes `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue` from other exceptions → `ctx.warn`. Reuse the `services/s3.py:_route_bucket_error` pattern.
5. **[HIGH] L1-005** — Add paginators to `describe_images`, `describe_launch_templates`, `describe_launch_template_versions`, `describe_auto_scaling_groups`, `describe_launch_configurations`. Batch `describe_snapshots` calls to one request per ~200 IDs.
6. **[HIGH] L1-006 / L3-001** — Split the single `old_amis` SourceBlock into `old_amis` and `unused_amis`. Add a matching `optimization_descriptions["unused_amis"]` entry.
7. **[HIGH] L1-001** — Remove the four `print` calls; route module-active banners to `logger.debug`.
8. **[MEDIUM] L2-003** — Stop inventing `8 GB`. On snapshot describe failure with no `VolumeSize` fallback, set `EstimatedMonthlySavings = 0.0` and attach a `pricing_warning` field.
9. **[MEDIUM] L3-002** — Add `ImageId` to the resource-id resolver chain in `reporter_phase_b._render_generic_other_rec`.
10. **[LOW] L3-003** — Derive `priority` from `AgeDays` (≥365 → high, ≥90 → medium).
11. **[INFO]** — After fixes, re-capture the regression goldens; current golden masks L2-002.
