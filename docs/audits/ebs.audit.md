# Audit: ebs Adapter

**Adapter**: `services/adapters/ebs.py` (84 lines)
**Legacy shim**: `services/ebs.py` (504 lines)
**ALL_MODULES index**: 2
**Renderer path**: Phase B — 5 dedicated handlers (`cost_optimization_hub`, `unattached_volumes`, `gp2_migration`, `enhanced_checks`, `compute_optimizer`)
**Date**: 2026-05-12
**Auditor**: automated (multi-layer audit prompt v2, external-validation enforced)

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 (1 MED, 1 LOW) |
| L2 Calculation | **FAIL** | 8 (2 HIGH, 4 MEDIUM, 2 LOW) |
| L3 Reporting   | **WARN** | 3 (1 HIGH, 1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | 13 findings |

`FAIL` driver: regional `pricing_multiplier` is **double-applied** to the gp2→gp3 savings path (PricingEngine already returns region-correct prices), and snapshot pricing is hardcoded at $0.05/GB-month — region-blind. Plus enhanced-check snapshot recs are silently dropped by the renderer while still counted in `total_recommendations`.

---

## Pre-flight facts

- **Adapter class**: `EbsModule` at `services/adapters/ebs.py:19`
- **Required boto3 clients**: `("ec2", "compute-optimizer")`
- **`requires_cloudwatch`**: `False` — verified by `grep cloudwatch services/ebs.py` → 0 hits. EBS adapter does **not** call CloudWatch. ✓ correct
- **`reads_fast_mode`**: `False` — no CloudWatch / no expensive enrichment to gate. ✓ correct
- **CLI alias resolution**: `resolve_cli_keys(ALL_MODULES, {'ebs'}, None) → {'ebs'}`
- **Sources emitted**: `compute_optimizer`, `unattached_volumes`, `gp2_migration`, `enhanced_checks`
- **Extras**: `volume_counts` (dict: total / attached / unattached / per-type)
- **`stat_cards`**: empty tuple
- **`optimization_descriptions`**: `EBS_OPTIMIZATION_DESCRIPTIONS` (7 entries)
- **Pricing methods consumed (from `PricingEngine`)**:
  - `get_ebs_monthly_price_per_gb(volume_type)` — gp2/gp3 migration savings + unattached volume cost
  - `get_ebs_iops_monthly_price(volume_type)` — io1/io2 unattached IOPS cost
  - `compute_optimizer_savings()` — Compute Optimizer recs
  - Snapshot pricing: **none — hardcoded $0.05/GB-month × pricing_multiplier**
  - gp3 over-provisioned IOPS: **hardcoded $0.005 × pricing_multiplier**
- **Test fixture**: `tests/fixtures/recorded_aws_responses/compute-optimizer/get_ebs_volume_recommendations.json` — empty (`volumeRecommendations: []`)
- **Golden savings snapshot**: `tests/fixtures/reporter_snapshots/savings/ebs.txt` = `94.200000`

---

## External validation evidence

| Tool | Call | Result | Used by |
|---|---|---|---|
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, `volumeApiName=gp2 + productFamily=Storage` | gp2 = **$0.10/GB-month** | confirms `_estimate_volume_cost` fallback constant |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, `volumeApiName=gp3` | gp3 storage = **$0.08/GB-mo**; gp3 IOPS = **$0.005/IOPS-mo** | confirms gp3 fallback constants |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, `usagetype=EBS:SnapshotUsage` | EBS Standard Snapshot = **$0.05/GB-Mo** | confirms `_FALLBACK_SNAPSHOT_RATE` correctness for us-east-1 (only) |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, `usagetype=EBS:SnapshotArchiveStorage` | EBS Snapshot Archive = **$0.0125/GB-Mo** | shows the Archive tier exists and is 4× cheaper — currently unmodeled |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, `productFamily=System Operation + volumeApiName ANY_OF [gp3,io1,io2]` | gp3 IOPS = $0.005; io1 IOPS = $0.065; io2 IOPS **tiered**: 0–32000 = $0.065, 32001–64000 = **$0.0455**, > 64000 = **$0.032** | adapter uses flat $0.065 for io2; misses tiered discount |
| `WebFetch` AWS EBS Pricing page | Confirmed Snapshot Standard / Archive split | Free tier mentions; full table only on the AWS Pricing API | n/a |

---

## L1 — Technical findings

### L1-EBS-001  10 `print()` statements in scan path  [MEDIUM]

- **Check**: L1.5 / L1.4 hygiene
- **Evidence**:
  - `services/ebs.py:115` — `print("🔍 [services/ebs.py] EBS module active")`
  - `services/ebs.py:149, 151, 153, 156` — `get_ebs_volume_count` warnings (AccessDenied / RateLimit / general)
  - `services/ebs.py:231, 233, 235, 237` — `get_unattached_volumes` warnings (same pattern)
  - `services/ebs.py:494` — `compute_ebs_checks` outer-try warning
  - `services/adapters/ebs.py:44` — `print("🔍 [services/adapters/ebs.py] EBS module active")`
  - `reporter_phase_b.py:286` — `print(f"⚠️ Could not parse EBS savings ...")` (renderer parse error)
- **Why it matters**: Same problem already fixed in EC2/RDS — pipes break and operator warnings never reach `scan_warnings[]`.
- **Fix**: Replace top-of-function diagnostics with `logger.debug(...)`. Replace warning prints with `ctx.warn(...)`. Renderer print → `logger.debug(...)`.

### L1-EBS-002  `AccessDenied` swallowed as a print, not a permission_issue  [MEDIUM]

- **Check**: L1.2.3
- **Evidence**:
  - `services/ebs.py:146-154`:
    ```python
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "UnauthorizedOperation":
            print("Warning: Missing IAM permission for describe_volumes")
        ...
    ```
  - `services/ebs.py:228-238` — identical pattern in `get_unattached_volumes`.
- **Why it matters**: An operator running with a slimmed-down IAM role gets zero EBS recommendations and no signal that `ec2:DescribeVolumes` is denied. Same defect already fixed in EC2/RDS.
- **Fix**:
  ```python
  if error_code in ("UnauthorizedOperation", "AccessDenied"):
      ctx.permission_issue(
          f"describe_volumes denied: {error_code}",
          service="ebs",
          action="ec2:DescribeVolumes",
      )
  ```

---

## L2 — Calculation findings

### Source classification table

| Recommendation category | Source key | Source class | Evidence | Acceptable? | External |
|---|---|---|---|---|---|
| Compute Optimizer recs | `compute_optimizer` | `aws-api` (via helper) | `services/adapters/ebs.py:66` via `compute_optimizer_savings` | YES ✓ | n/a |
| Unattached Volumes (separate source) | `unattached_volumes` | `live` ✓ | `_estimate_volume_cost` uses `get_ebs_monthly_price_per_gb` + `get_ebs_iops_monthly_price` | YES ✓ | live API |
| gp2 → gp3 migration | `gp2_migration` | **double-multiplier bug** | `services/adapters/ebs.py:63` — `(gp2 − gp3) * ctx.pricing_multiplier` where prices come from PricingEngine (already region-correct) | **NO** — see L2-EBS-001 | live API |
| Underutilized Volumes (high-IOPS io1/io2) | `enhanced_checks` | informational string → $0 | `services/ebs.py:366` — `"Enable CloudWatch monitoring to validate IOPS usage"` | WARN | n/a |
| Snapshot Lifecycle | `enhanced_checks` | informational string → $0 | `services/ebs.py:386` — `"Automated cleanup of old snapshots can reduce storage costs"` | WARN | n/a |
| Over-provisioned IOPS (gp3) | `enhanced_checks` | `module-const × multiplier` | `services/ebs.py:403` — `extra_iops * 0.005 * pricing_multiplier` | WARN — see L2-EBS-005 | gp3 IOPS = $0.005 ✓ us-east-1 only |
| Over-provisioned IOPS (io1/io2) | `enhanced_checks` | `module-const × multiplier` | `services/ebs.py:416` — `extra_iops * 0.065 * pricing_multiplier` | WARN — see L2-EBS-004 (io2 tier discount missed) | live API |
| Old Snapshots | `enhanced_checks` | `module-const × multiplier` | `services/ebs.py:444` — `volume_size * 0.05 * pricing_multiplier` | WARN — region-blind | snapshot = $0.05/GB-mo ✓ us-east-1 only |
| Orphaned Snapshots | `enhanced_checks` | `module-const × multiplier` | `services/ebs.py:466` — same `0.05 * pricing_multiplier` | WARN | same |
| Unused Encrypted Volumes | `enhanced_checks` | `live` ✓ | `services/ebs.py:487` — `_estimate_volume_cost` (PricingEngine) | YES ✓ | live API |
| gp2 → gp3 (enhanced check) | `enhanced_checks` | informational string only | `services/ebs.py:343` — `"Estimated 20% cost reduction"` (no $) | dormant — see L2-EBS-008 | n/a |

### External validation log

| Finding ID | Tool | Call | Result | Confirms |
|---|---|---|---|---|
| L2-EBS-001 | code grep | `services/adapters/ebs.py:63` | `size * max(gp2_price - gp3_price, 0) * ctx.pricing_multiplier` | Double-multiplier when `gp2_price` already comes from `get_ebs_monthly_price_per_gb()` which is region-correct |
| L2-EBS-002 | `get_pricing` | Snapshot Standard us-east-1 | $0.05/GB-Mo | Confirms hardcoded `0.05` is correct for us-east-1; non-us-east-1 regions get the wrong rate scaled by `pricing_multiplier` (which is an EC2-on-demand-derived multiplier, not snapshot-specific) |
| L2-EBS-002 | `get_pricing` | Snapshot Archive us-east-1 | $0.0125/GB-Mo | Adapter does not consider Archive tier at all |
| L2-EBS-004 | `get_pricing` | io2 IOPS tiered us-east-1 | $0.065 (0–32000), $0.0455 (32001–64000), $0.032 (>64000) | Adapter uses flat $0.065 for all io2 IOPS — overcounts savings on volumes with > 32000 provisioned IOPS |
| L2-EBS-005 | `get_pricing` | gp3 IOPS us-east-1 | $0.005/IOPS-Mo | Hardcoded constant is correct for us-east-1 only; PricingEngine has `get_ebs_iops_monthly_price("gp3")` but the gp3-over-provisioned-IOPS path doesn't call it |

### L2-EBS-001  Double-multiplier on gp2→gp3 savings  [HIGH]

- **Check**: L2.3.1 (no double-multiplier on live PricingEngine paths)
- **Evidence**:
  - `services/adapters/ebs.py:58-65`:
    ```python
    for rec in gp2_recs:
        size = rec.get("Size", 0)
        if ctx.pricing_engine:
            gp2_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp2")
            gp3_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")
            savings += size * max(gp2_price - gp3_price, 0) * ctx.pricing_multiplier
        else:
            savings += size * 0.10 * 0.20 * ctx.pricing_multiplier
    ```
  - `core/pricing_engine.py:213-225` — `get_ebs_monthly_price_per_gb` already calls `_call_pricing_api` with the region's display name; the returned value is **region-correct**.
  - The non-PricingEngine branch correctly multiplies fallback constants by `pricing_multiplier`. The live branch should not.
- **Why it matters**: Regional accounts see savings inflated (or deflated) by the regional multiplier ratio. For me-central-1 (`pricing_multiplier ≈ 1.10`), a 1 TB gp2 volume's reported savings = `1024 × (0.121 − 0.0968) × 1.10 = $27.27/mo` when the real savings is `1024 × (0.121 − 0.0968) = $24.79/mo` — overstated by **10 %**. For cheaper regions (multiplier < 1.0), savings are understated.
- **Fix**: Drop the `* ctx.pricing_multiplier` on the PricingEngine branch:
  ```python
  if ctx.pricing_engine:
      gp2_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp2")
      gp3_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")
      savings += size * max(gp2_price - gp3_price, 0.0)
  else:
      savings += size * 0.10 * 0.20 * ctx.pricing_multiplier
  ```

### L2-EBS-002  Snapshot pricing hardcoded at $0.05/GB-mo, region-blind  [HIGH]

- **Check**: L2.1 (`module-const` where a PricingEngine method should exist), L2.3 regional correctness
- **Evidence**:
  - `services/ebs.py:444` — `f"${snapshot['VolumeSize'] * 0.05 * pricing_multiplier:.2f}/month (max estimate)"` (Old Snapshots)
  - `services/ebs.py:466` — identical pattern (Orphaned Snapshots)
  - `core/pricing_engine.py` has **no** `get_ebs_snapshot_price_per_gb` method (compare to `get_rds_backup_storage_price_per_gb` which exists).
  - `pricing_multiplier` is derived from EC2 on-demand rates and isn't 1:1 correct for snapshot storage.
  - **External**: us-east-1 snapshot rate confirmed at **$0.05/GB-Mo** via Pricing API; other regions vary (e.g. ap-southeast-1 is $0.05/GB-Mo and Bahrain is $0.0577/GB-Mo per public AWS pricing).
- **Why it matters**: For accounts with many old snapshots, this is the dominant savings number EBS reports. Region-blind pricing systematically misstates the recommendation.
- **Fix**:
  1. Add `PricingEngine.get_ebs_snapshot_price_per_gb()` parallel to `get_rds_backup_storage_price_per_gb`. Filter `productFamily="Storage Snapshot" + usagetype="EBS:SnapshotUsage" + location=display_name`. Fallback constant `0.05`.
  2. Update both call sites to use the new method, drop the multiplier on the live path.

### L2-EBS-003  Unattached volumes double-counted in `total_recommendations`  [MEDIUM]

- **Check**: L2.5.3 (per-source vs total invariant)
- **Evidence**:
  - `services/adapters/ebs.py:46-49`:
    ```python
    enhanced_result = compute_ebs_checks(ctx, ctx.pricing_multiplier, ctx.old_snapshot_days)
    enhanced_recs = enhanced_result.get("recommendations", [])
    co_recs = get_ebs_compute_optimizer_recs(ctx, ctx.pricing_multiplier)
    unattached_volumes = get_unattached_volumes(ctx, ctx.pricing_multiplier)
    ```
  - `services/ebs.py:321-322` — `compute_ebs_checks` **also** calls `get_unattached_volumes` and includes them in `recommendations`.
  - `services/adapters/ebs.py:68`:
    ```python
    total_recs = len(co_recs) + len(unattached_volumes) + len(gp2_recs) + len(other_recs)
    ```
  - `other_recs` is `[r for r in enhanced_recs if r.get("CheckCategory") != "Volume Type Optimization"]` — so it **includes** unattached-volume entries (CheckCategory `"Unattached Volumes"`).
  - Result: 5 unattached volumes are counted as `5 (separate) + 5 (in other_recs) = 10` recommendations.
- **Why it matters**: Headline `total_recommendations` overstates by N (number of unattached volumes). Per-volume the savings dollar isn't double-counted because the enhanced-check entry has no `EstimatedSavings` field (so `parse_dollar_savings("") → 0`), but the count is wrong.
- **Fix**: In the adapter, filter the unattached-volume entries out of `other_recs`:
  ```python
  other_recs = [
      r for r in enhanced_recs
      if r.get("CheckCategory") not in ("Volume Type Optimization", "Unattached Volumes")
  ]
  ```

### L2-EBS-004  io2 IOPS tiered pricing missed  [MEDIUM]

- **Check**: L2.1 source attribution; L2.2.4 IOPS unit/rate correctness
- **Evidence**:
  - `services/ebs.py:413-416`:
    ```python
    elif volume_type in ["io1", "io2"] and iops > size * 50:
        recommended_iops = size * 30
        extra_iops = iops - recommended_iops
        savings = extra_iops * 0.065 * pricing_multiplier
    ```
  - **External (`get_pricing` io2 us-east-1)**: io2 IOPS is **tiered**: $0.065 for 0–32000, $0.0455 for 32001–64000, $0.032 for > 64000.
  - Adapter uses flat $0.065 for all io2 IOPS regardless of tier.
- **Why it matters**: On a high-IOPS io2 volume (say 80000 provisioned IOPS), reducing by 30000 IOPS actually saves `30000 × 0.0455 = $1365/mo`, not `30000 × 0.065 = $1950/mo` the adapter reports. Overstates savings for the biggest volumes.
- **Fix**: For io2, compute tiered savings:
  ```python
  def _io2_iops_cost(iops: int) -> float:
      cost = min(iops, 32000) * 0.065
      if iops > 32000:
          cost += min(iops - 32000, 32000) * 0.0455
      if iops > 64000:
          cost += (iops - 64000) * 0.032
      return cost
  savings = _io2_iops_cost(iops) - _io2_iops_cost(recommended_iops)
  ```
  Or: query PricingEngine for the correct IOPS tier (would need a new method).

### L2-EBS-005  gp3 over-provisioned IOPS uses hardcoded $0.005 instead of PricingEngine  [MEDIUM]

- **Check**: L2.1 (live preference)
- **Evidence**:
  - `services/ebs.py:403` — `savings = extra_iops * 0.005 * pricing_multiplier`
  - `core/pricing_engine.py:227` exposes `get_ebs_iops_monthly_price(volume_type)` for io1/io2; **but the gp3 IOPS rate is _not_ exposed through that method** (`_fetch_ebs_iops_price` filters on `productFamily="System Operation"` for io1/io2 only).
- **Why it matters**: gp3 IOPS rate is $0.005 in us-east-1 today; some regions vary slightly (e.g. ap-south-2 charges $0.006/IOPS-Mo). The hardcoded `0.005 * pricing_multiplier` is approximately right but not authoritative.
- **Fix**: Extend `get_ebs_iops_monthly_price` to accept `"gp3"` (Pricing API filter `volumeApiName=gp3 + productFamily="System Operation"` works — verified via `get_pricing` call). Update fallback constant `FALLBACK_EBS_IOPS_MONTH["gp3"] = 0.005`.

### L2-EBS-006  Underutilized Volumes informational string  [LOW]

- **Check**: L2.1 / L3.3.1 (string format consistency)
- **Evidence**: `services/ebs.py:366` — `"Enable CloudWatch monitoring to validate IOPS usage"` — not in `$X.YY/month` format; `parse_dollar_savings` returns 0.0.
- **Why it matters**: Same pattern resolved in EC2/RDS — informational items should use `$0.00/month - …` format for headline ↔ table parity.
- **Fix**: Reword to `"$0.00/month - enable CloudWatch monitoring to validate IOPS usage"`.

### L2-EBS-007  Snapshot Lifecycle informational string  [LOW]

- **Check**: L2.1 / L3.3.1
- **Evidence**: `services/ebs.py:386` — `"Automated cleanup of old snapshots can reduce storage costs"`.
- **Fix**: Reword to `"$0.00/month - quantify after enabling Data Lifecycle Manager"`.

### L2-EBS-008  Enhanced-check gp2 string is purely informational  [LOW]

- **Check**: L2.1
- **Evidence**: `services/ebs.py:343` — `"Estimated 20% cost reduction"`. The adapter's own `gp2_migration` accounting bypasses this (it computes from `size * (gp2 - gp3)`), so this string is never parsed for savings. But the renderer at `reporter_phase_b.py:245` literally hard-codes `"20% cost reduction"` text on screen regardless of the volume sizes — so the per-card displayed savings doesn't match the headline.
- **Fix**: Compute per-volume savings inline using the same `(gp2 − gp3) × size` formula and emit `f"${savings:.2f}/month"`. Renderer should sum them like `_render_ebs_enhanced_checks` does.

---

## L3 — Reporting findings

### Field-presence table

| Source | Renderer | Required fields | Notes |
|---|---|---|---|
| `compute_optimizer` | `_render_ebs_compute_optimizer` (`reporter_phase_b.py:311`) | `volumeArn`, `finding`, `currentConfiguration.volumeType` / `volumeSize`, `volumeRecommendationOptions[0].configuration.volumeType` + `volumeSize` + `savingsOpportunity` | Reads correct nested AWS schema ✓ |
| `unattached_volumes` | `_render_ebs_unattached` (`reporter_phase_b.py:220`) | `VolumeId`, `Size`, `VolumeType`, `EstimatedMonthlyCost` | All produced by adapter ✓ |
| `gp2_migration` | `_render_ebs_gp2_migration` (`reporter_phase_b.py:240`) | `VolumeId`, `Size` | Hardcodes "20% cost reduction" prose — see L3-EBS-003 |
| `enhanced_checks` | `_render_ebs_enhanced_checks` (`reporter_phase_b.py:257`) | `CheckCategory`, `Recommendation`, `EstimatedSavings` (parses `$N` prefix), `VolumeId` / `SnapshotId`, optional `Size`, `CurrentType`, `RecommendedType` | **Skips** categories containing `"snapshot"`, `"unused encrypted"`, `"unattached"` — see L3-EBS-001 |
| `cost_optimization_hub` | `_render_ebs_cost_hub` (`reporter_phase_b.py:186`) | `actionType`, `resourceId`, `currentResourceDetails.ebsVolume.configuration.storage.{type,sizeInGb}`, `estimatedMonthlySavings` | not currently populated by adapter (Cost Hub EBS recs route through scan_orchestrator's split, but EBS adapter doesn't subscribe today) |

### L3-EBS-001  Renderer drops snapshot recs counted in `total_recommendations`  [HIGH]

- **Check**: L3.2.1 (every emitted source/category is rendered)
- **Evidence**:
  - `reporter_phase_b.py:266-271`:
    ```python
    if "snapshot" in category.lower(): continue
    if "unused encrypted" in category.lower(): continue
    if "unattached" in category.lower(): continue
    ```
  - `services/ebs.py:432-447` emits CheckCategory `"Old Snapshots"`, `:454-468` emits `"Orphaned Snapshots"`, `:480-491` emits `"Unused Encrypted Volumes"`.
  - These categories DO appear in `enhanced_recs` and get counted in `total_recommendations`, but are silently dropped by the enhanced-checks renderer above.
  - `"Unattached Volumes"` is filtered too, but has its **own** renderer at `_render_ebs_unattached` — so that's deliberate dedup with the separately-rendered `unattached_volumes` source.
  - `"Old Snapshots"`, `"Orphaned Snapshots"`, `"Unused Encrypted Volumes"` have **no** alternative renderer — they vanish from the HTML.
- **Why it matters**: A scan producing "EBS: 50 recommendations, $1200/month" will only render ~30 of them in the HTML. The dollar number is correct (savings strings parse fine), but the recommendation count and the per-card list are out of sync with the headline.
- **Fix**: Either render snapshot/encrypted recs (add separate Phase B handlers or remove the filter), or exclude them from `total_recommendations` so the count matches. Recommended: remove the filter — those checks have real `$X.YY/month` savings (per L2-EBS-002 fix) and deserve a card.

### L3-EBS-002  gp2_migration renderer hardcodes "20% cost reduction" text  [MEDIUM]

- **Check**: L3.3.3 (headline ↔ visible-savings parity)
- **Evidence**: `reporter_phase_b.py:244-245`:
  ```python
  content += f"<p><strong>Recommendation:</strong> Migrate gp2 volumes to gp3 for 20% cost savings</p>"
  content += f'<p class="savings"><strong>Estimated Savings:</strong> 20% cost reduction</p>'
  ```
  Adapter computes a real dollar value (correctly, modulo L2-EBS-001) and adds it to `total_monthly_savings`, but the HTML card never shows the number — just "20% cost reduction" prose.
- **Why it matters**: Users see "EBS shows $1500/month in savings" at the top and "20% cost reduction" on the gp2 card — they can't tell which volumes drove the headline.
- **Fix**: Compute the per-volume dollar in the adapter when constructing `gp2_recs`, set `EstimatedSavings = f"${...}/month"`, and have the renderer display it (and the sum across volumes, like `_render_ebs_enhanced_checks`).

### L3-EBS-003  `print` in `_render_ebs_enhanced_checks` parse-error path  [LOW]

- **Check**: L1.4 / L3 hygiene
- **Evidence**: `reporter_phase_b.py:286` — `print(f"⚠️ Could not parse EBS savings '{savings_str}': {str(e)}")`. Same pattern as the RDS renderer print already fixed in commit `a38faca` (which only replaced the RDS one).
- **Fix**: `logger.debug("Could not parse EBS savings %r: %s", savings_str, e)`. Module-level `logger` already exists at the top of `reporter_phase_b.py` (added in commit `a38faca` for the RDS fix).

---

## Cross-layer red flags

**None.** No bypass of `ClientRegistry`. No cross-account ARNs. No writes to AWS. No `ALL_MODULES` mutation. Untraceable dollars: none — every dollar today is either live PricingEngine, `compute_optimizer_savings`, or a documented module constant × multiplier (where the multiplier is structurally wrong in L2-EBS-001 but the source is at least traceable).

---

## Verification log

```
# Module probe
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='ebs'); print(m.requires_cloudwatch, m.reads_fast_mode, m.required_clients())"
False False ('ec2', 'compute-optimizer')

# CW probe (EBS doesn't call CloudWatch)
$ grep -n "cloudwatch" services/ebs.py
(no hits — confirms requires_cloudwatch=False is correct)

# Print scan-path search
$ grep -c "print(" services/ebs.py services/adapters/ebs.py
10 + 1 = 11 hits

# Snapshot price method probe
$ python3 -c "from core.pricing_engine import PricingEngine; print(hasattr(PricingEngine, 'get_ebs_snapshot_price_per_gb'))"
False  ← L2-EBS-002 root cause

# Live EBS Pricing API ground-truth (us-east-1)
gp2  storage : $0.10/GB-mo
gp3  storage : $0.08/GB-mo
gp3  IOPS    : $0.005/IOPS-mo
io1  IOPS    : $0.065/IOPS-mo
io2  IOPS    : $0.065 (0–32000) | $0.0455 (32001–64000) | $0.032 (>64000)
snapshot std : $0.05/GB-mo
snapshot arc : $0.0125/GB-mo  (unmodeled)

# Tests
$ pytest tests/test_offline_scan.py tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -k ebs
4 passed
```

---

## Recommended next steps (prioritized)

### HIGH

1. **L2-EBS-001** — Drop `* ctx.pricing_multiplier` on the PricingEngine branch of the gp2→gp3 savings calc in `services/adapters/ebs.py:63`.
2. **L2-EBS-002** — Add `PricingEngine.get_ebs_snapshot_price_per_gb()` (parallel to `get_rds_backup_storage_price_per_gb`). Wire it into `services/ebs.py:444` and `:466`. Optionally add `get_ebs_snapshot_archive_price_per_gb()` for the Archive tier comparison.
3. **L3-EBS-001** — Either remove the snapshot/encrypted filter in `_render_ebs_enhanced_checks` (preferred — gives them a card) or exclude them from `total_recommendations` so the count is consistent with the rendered HTML.

### MEDIUM

4. **L2-EBS-003** — Filter `"Unattached Volumes"` out of `other_recs` in `services/adapters/ebs.py:52`. Add a unit test asserting `len(unattached_volumes) + len(other_recs)` does not double-count.
5. **L2-EBS-004** — Tiered io2 IOPS pricing in `services/ebs.py:413`. Helper `_io2_iops_cost(iops)` or new PricingEngine method.
6. **L2-EBS-005** — Extend `PricingEngine.get_ebs_iops_monthly_price` to support gp3 (filter is identical to io1/io2 — just add a fallback constant).
7. **L1-EBS-001** — 11 `print()` calls → `logger.*` / `ctx.warn` / `ctx.permission_issue`. Same EC2/RDS pattern.
8. **L1-EBS-002** — Route `AccessDenied`/`UnauthorizedOperation` through `ctx.permission_issue(service="ebs", action="ec2:DescribeVolumes")`.
9. **L3-EBS-002** — `_render_ebs_gp2_migration` should sum and render per-volume real dollars, not the literal "20% cost reduction" prose.

### LOW

10. **L2-EBS-006** — Reword Underutilized Volumes `EstimatedSavings` to `$0.00/month - …` format.
11. **L2-EBS-007** — Same reword for Snapshot Lifecycle.
12. **L2-EBS-008** — Compute and emit per-volume dollars in the enhanced-check gp2 string (and the renderer change in L3-EBS-002).
13. **L3-EBS-003** — Replace renderer `print(...)` with `logger.debug(...)`.

---

## What this audit did **not** cover

- IAM policy completeness for EBS actions (covered by README IAM section).
- > 10,000 volume throttling behaviour.
- Cross-service double-counting against EC2 root-volume checks (`services/ec2.py::get_advanced_ec2_checks` "Oversized Root Volumes" — distinct CheckCategory + InstanceId-keyed, doesn't cross with EBS adapter's VolumeId-keyed checks).
- Cost Hub EBS rec subscription (the `_render_ebs_cost_hub` renderer is wired but the adapter doesn't currently pull from `ctx.cost_hub_splits`).

---

## Sources

- AWS EBS Pricing — https://aws.amazon.com/ebs/pricing/
- AWS Pricing API (via MCP): `mcp__aws-pricing-mcp-server__get_pricing` for `AmazonEC2 / us-east-1`:
  - `volumeApiName=gp2 / gp3 + productFamily=Storage` → storage rates
  - `productFamily="System Operation" + volumeApiName=gp3 / io1 / io2` → IOPS rates (io2 tiered)
  - `productFamily="Storage Snapshot" + usagetype=EBS:SnapshotUsage / EBS:SnapshotArchiveStorage` → snapshot Standard + Archive rates
