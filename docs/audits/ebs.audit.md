# Audit: ebs Adapter (v2 — post-fix re-audit, all findings cleared)

**Adapter**: `services/adapters/ebs.py` (115 lines after fixes)
**Legacy shim**: `services/ebs.py` (560 lines after fixes)
**ALL_MODULES index**: 2
**Renderer path**: Phase B — 5 dedicated handlers
**Date**: 2026-05-13
**Auditor**: automated re-audit confirming all 13 findings from v1

## Verdict

| Layer | v1 | **v2** | Δ |
|-------|----|--------|---|
| L1 Technical   | WARN | **PASS** | L1-EBS-001 + L1-EBS-002 fixed |
| L2 Calculation | FAIL | **PASS** | All 8 L2 findings fixed |
| L3 Reporting   | WARN | **PASS** | L3-EBS-001/002/003 all fixed |
| **Overall**    | FAIL | **PASS** | All 13 findings resolved |

---

## Finding-by-finding closeout

### HIGH

| ID | Title | Status | Verification |
|---|---|---|---|
| **L2-EBS-001** | Double-multiplier on gp2→gp3 savings | ✅ FIXED | `services/adapters/ebs.py:22-34` — new `_gp2_to_gp3_savings_per_gb(ctx)` returns the region-correct delta from PricingEngine **without** `pricing_multiplier`. Live path: `max(gp2 − gp3, 0.0)`. Fallback: `0.10 × 0.20 × ctx.pricing_multiplier`. Regression test in `tests/test_ebs_adapter.py::test_live_path_ignores_pricing_multiplier` asserts the old bug stays gone. |
| **L2-EBS-002** | Snapshot pricing hardcoded $0.05/GB-mo | ✅ FIXED | `core/pricing_engine.py:295-322` — new `get_ebs_snapshot_price_per_gb(*, archive_tier=False)`. `services/ebs.py:20-24` helper `_snapshot_price_per_gb(ctx, mult)` wraps it; called at lines 504 (old snapshots) and 532 (orphaned). Archive tier now modelled (`FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH = 0.0125`). |
| **L3-EBS-001** | Renderer drops snapshot recs counted in total | ✅ FIXED | `reporter_phase_b.py:266-279` — removed the `"snapshot"` and `"unused encrypted"` filters; only `"unattached"` stays (it has its own dedicated source/renderer). Snapshot + encrypted categories now render alongside other enhanced checks. Reporter snapshot regenerated. |

### MEDIUM

| ID | Title | Status | Verification |
|---|---|---|---|
| **L2-EBS-003** | Unattached volumes double-counted in total | ✅ FIXED | `services/adapters/ebs.py:78-82` — `dedicated_categories = {"Volume Type Optimization", "Unattached Volumes"}` excludes both from `other_recs`. `total_recs` now counts each unattached volume exactly once (via `len(unattached_volumes)`). |
| **L2-EBS-004** | io2 IOPS tiered pricing | ✅ FIXED | `core/pricing_engine.py:249-273` — new `get_ebs_io2_iops_cost(iops)` implements tiered math ($0.065 / $0.0455 / $0.032 per IOPS-Mo, region-scaled via base rate). `services/ebs.py:34-52` wraps it as `_io2_iops_cost(ctx, iops, mult)`. `services/ebs.py:467-471` calls the helper for io2; io1 keeps flat rate. Three new pricing_engine tests cover single/two/three-tier scenarios. |
| **L2-EBS-005** | gp3 over-provisioned IOPS hardcoded | ✅ FIXED | `core/pricing_engine.py:60-63` — `FALLBACK_EBS_IOPS_MONTH["gp3"] = 0.005` added; existing `get_ebs_iops_monthly_price("gp3")` works because `_fetch_ebs_iops_price` already filters on `volumeApiName=<type>`. `services/ebs.py:27-31` helper `_gp3_iops_price(ctx, mult)`. `services/ebs.py:461` calls it. New `test_ebs_gp3_iops_fallback`. |
| **L1-EBS-001** | 11 `print()` in scan path | ✅ FIXED | `grep "print(" services/ebs.py services/adapters/ebs.py` → 0 hits. All routed through `logger.*` / `ctx.warn` / `ctx.permission_issue`. |
| **L1-EBS-002** | AccessDenied uses `print`, not permission_issue | ✅ FIXED | `services/ebs.py:158-167` (`get_ebs_volume_count`) + `:248-263` (`get_unattached_volumes`) — both now route AccessDenied/UnauthorizedOperation through `ctx.permission_issue(action="ec2:DescribeVolumes")`. |
| **L3-EBS-002** | gp2_migration renderer hardcodes "20% cost reduction" | ✅ FIXED | `reporter_phase_b.py:240-265` — renderer now sums per-rec `EstimatedSavings` numbers and emits `${total:.2f}/month`. Per-volume `<li>` entries show the dollar value too. Adapter writes per-volume `$X.YY/month` onto each gp2 rec at `services/adapters/ebs.py:87-93`. |

### LOW

| ID | Title | Status | Verification |
|---|---|---|---|
| **L2-EBS-006** | Underutilized Volumes informational string | ✅ FIXED | `services/ebs.py:420` — `"$0.00/month - enable CloudWatch monitoring to validate IOPS usage"` (project-wide format). |
| **L2-EBS-007** | Snapshot Lifecycle informational string | ✅ FIXED | `services/ebs.py:441` — `"$0.00/month - quantify after enabling Data Lifecycle Manager"`. |
| **L2-EBS-008** | gp2 enhanced-check carries informational string | ✅ FIXED | `services/ebs.py:343` legacy rec sets `EstimatedSavings = "$0.00/month - adapter computes per-volume"`. Real per-volume dollars set by adapter at `services/adapters/ebs.py:90`. |
| **L3-EBS-003** | renderer `print()` for parse errors | ✅ FIXED | `reporter_phase_b.py:286` — replaced with `logger.debug(...)`. |

---

## Source-classification table (final state)

| Recommendation category | Source class | Backing |
|---|---|---|
| Compute Optimizer (rightsizing) | `aws-api` | `compute_optimizer_savings` reads nested AWS schema |
| Unattached Volumes | `live` | `_estimate_volume_cost` → `get_ebs_monthly_price_per_gb` + `get_ebs_iops_monthly_price` |
| gp2 → gp3 migration | `live` | `_gp2_to_gp3_savings_per_gb(ctx)` × volume size (NO multiplier on PricingEngine path) |
| Over-provisioned IOPS (gp3) | `live` | `_gp3_iops_price(ctx, mult)` → `get_ebs_iops_monthly_price("gp3")` |
| Over-provisioned IOPS (io1) | `live` | flat `get_ebs_iops_monthly_price("io1")` |
| Over-provisioned IOPS (io2) | `live, tiered` | `_io2_iops_cost(ctx, iops, mult)` → `get_ebs_io2_iops_cost(iops)` (3-tier) |
| Old Snapshots | `live` | `_snapshot_price_per_gb(ctx, mult)` → `get_ebs_snapshot_price_per_gb()` |
| Orphaned Snapshots | `live` | same |
| Unused Encrypted Volumes | `live` | `_estimate_volume_cost` (PricingEngine) |
| Underutilized Volumes (io1/io2 high IOPS) | informational | `$0.00/month - …` honest zero |
| Snapshot Lifecycle | informational | `$0.00/month - …` (Data Lifecycle Manager hint) |

No `arbitrary` or `module-const` classifications remain. Every dollar in `total_monthly_savings` is traceable to either an AWS API call or a documented PricingEngine method.

---

## Verification log

```
# Module probe (post-fix)
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='ebs'); print(m.requires_cloudwatch, m.reads_fast_mode, m.required_clients())"
False False ('ec2', 'compute-optimizer')

# Print-free
$ grep -n "print(" services/ebs.py services/adapters/ebs.py
(empty)

# New PricingEngine methods exist
$ python3 -c "from core.pricing_engine import PricingEngine; print(hasattr(PricingEngine, 'get_ebs_snapshot_price_per_gb'), hasattr(PricingEngine, 'get_ebs_io2_iops_cost'))"
True True

# Adapter helper exists
$ python3 -c "from services.adapters.ebs import _gp2_to_gp3_savings_per_gb; print(callable(_gp2_to_gp3_savings_per_gb))"
True

# No prose percentage strings in scan path
$ grep -n 'EstimatedSavings.*reduction\|EstimatedSavings.*can reduce' services/ebs.py
(empty)

# Tests
$ pytest tests/
176 passed, 1 skipped in 6.03s
# +9 pricing_engine tests (snapshot Standard/Archive fallback+cache,
# io2 single/two/three-tier, gp3 IOPS fallback)
# +4 ebs_adapter tests (gp2→gp3 delta live + fallback + no-double-multiplier
# regression + no-negative-delta)
```

---

## External validation (cumulative)

| Round | Tool | Resource | Result |
|---|---|---|---|
| v1 | `mcp__aws-pricing-mcp-server__get_pricing` | EBS gp2 us-east-1 | $0.10/GB-mo — matches `_estimate_volume_cost` fallback |
| v1 | `get_pricing` | EBS gp3 storage us-east-1 | $0.08/GB-mo — matches fallback |
| v1 | `get_pricing` | EBS gp3 IOPS us-east-1 | $0.005/IOPS-mo — matches new `FALLBACK_EBS_IOPS_MONTH["gp3"]` |
| v1 | `get_pricing` | EBS io1 IOPS us-east-1 | $0.065/IOPS-mo (flat) — matches |
| v1 | `get_pricing` | EBS io2 IOPS tiered us-east-1 | $0.065 / $0.0455 / $0.032 — drives new tiered helper |
| v1 | `get_pricing` | EBS Snapshot Standard us-east-1 | $0.05/GB-mo — matches `FALLBACK_EBS_SNAPSHOT_GB_MONTH` |
| v1 | `get_pricing` | EBS Snapshot Archive us-east-1 | $0.0125/GB-mo — newly modelled |

---

## Cross-layer red flags

**None.** No `arbitrary` dollars. No `module-const` paths without PricingEngine fallback. No `pricing_multiplier` double-application on live paths. No untraceable savings. No bypass of `ClientRegistry`. No cross-account ARNs.

---

## Closeout

The EBS adapter section is **complete**. All 13 v1 findings resolved with external validation against AWS Pricing API. The new `PricingEngine.get_ebs_snapshot_price_per_gb` and `get_ebs_io2_iops_cost` methods (plus extended gp3 IOPS support) are reusable for any future adapter touching EBS pricing. Reporter snapshot regenerated to reflect the now-rendered snapshot + encrypted categories that were silently dropped pre-fix.

### Shared infrastructure added during this round (reusable by future audits)

- `PricingEngine.get_ebs_snapshot_price_per_gb(*, archive_tier=False)` — Standard + Archive tiers
- `PricingEngine.get_ebs_io2_iops_cost(iops)` — tiered cost helper for high-IOPS io2 volumes
- `FALLBACK_EBS_IOPS_MONTH["gp3"] = 0.005` — completes gp3 support in `get_ebs_iops_monthly_price`
- `FALLBACK_IO2_IOPS_TIER2_MONTH = 0.0455`, `FALLBACK_IO2_IOPS_TIER3_MONTH = 0.032`
- `FALLBACK_EBS_SNAPSHOT_GB_MONTH = 0.05`, `FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH = 0.0125`

### Cumulative state across all three audited adapters

| Service | v1 Verdict | Final v2 Verdict | Findings Fixed |
|---|---|---|---|
| EC2 | FAIL (v2) | **PASS** (v4) | 13 |
| RDS | WARN (v1) | **PASS** (v2) | 11 |
| EBS | FAIL (v1) | **PASS** (v2) | 13 |
| **Total** | | | **37 findings fixed** across 3 adapters, 24 new tests added |
