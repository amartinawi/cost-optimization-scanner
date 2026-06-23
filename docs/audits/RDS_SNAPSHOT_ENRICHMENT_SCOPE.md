# Scope: RDS/Aurora snapshot savings — from upper-bound to actual

**Date**: 2026-06-23
**Status**: SCOPE / DESIGN — not implemented.
**Trigger**: snapshot savings are the dominant line in real RDS reports (e.g. M360
ap-south-1: snapshots = ~96% of the $838.83 headline) and are currently an
**upper bound** (provisioned `AllocatedStorage` × backup rate), labelled as such
after audit B3. The ask: replace the upper bound with the *actual billable* size.

---

## 1. Current state

`services/rds.py` prices each old manual snapshot as
`AllocatedStorage (GB) × backup_rate`, where:
- `backup_rate` is correct and region/engine-aware (Aurora $0.021–0.023 vs
  standard $0.095/GB-mo) — validated.
- `AllocatedStorage` is the **provisioned** DB/cluster size at snapshot time, NOT
  the billed backup bytes. AWS bills snapshots on **actual** (incremental,
  compressed) bytes beyond the free allotment, which is typically lower — and for
  some Aurora cluster snapshots the API reports `AllocatedStorage = 0` (now handled
  as advisory, B1).

So today's snapshot $ is a defensible **ceiling**, not the realizable saving.

---

## 2. What AWS actually exposes (validated 2026-06-23 via AWS docs)

| Source | Per-snapshot? | Covers orphaned snapshots? | Limits |
|---|---|---|---|
| `describe_db_snapshots` / `describe_db_cluster_snapshots` | size = `AllocatedStorage` only (provisioned, sometimes 0) | yes | not the billed size |
| CloudWatch `SnapshotStorageUsed`, `TotalBackupStorageBilled`, `BackupRetentionPeriodStorageUsed` (AWS/RDS, per DB **instance/cluster**) | per **parent** (aggregate of its snapshots), not per snapshot | **no** — metric dies with the parent | live resources only |
| Cost Explorer `GetCostAndUsage` (group by `USAGE_TYPE`) | no — region/usage-type aggregate | yes (it's billing data) | 13-mo history; ~$0.01/request; no per-resource |
| Cost Explorer `GetCostAndUsageWithResources` | resource-level | snapshots not reliably distinct resource IDs | **opt-in**, **14-day** window, daily granularity |

**The blocker:** most cost-relevant snapshots are **orphaned** (parent cluster/DB
deleted — names like `*-final-snapshot`, `*-before-deleted`). For those, neither
CloudWatch (parent gone) nor CE resource-level (14-day + no snapshot ID) yields a
per-snapshot actual size. **A precise per-snapshot actual figure is not
obtainable from any AWS API for orphaned snapshots.** Any scope claiming otherwise
is wrong; the honest goal is *bounding the estimate with real spend*, not
per-snapshot precision.

---

## 3. Proposed design (two independent tiers)

### Tier 1 — Cost Explorer reconciliation ceiling (recommended)

Fetch the account's **actual billed backup spend** for the region and use it to
bound/annotate the snapshot estimates.

- **Query**: `ce.get_cost_and_usage` (the CostExplorer client is already obtained
  in `core/trend_analysis.py` — reuse the pattern), last full month,
  `GROUP BY DIMENSION=USAGE_TYPE`, `FILTER SERVICE = "Amazon Relational Database
  Service"`. Sum the groups whose usage-type key ends in `:ChargedBackupUsage`
  (standard) or `Aurora:BackupUsage` (Aurora) — robust across region prefixes
  (`APS3-`, `EU-`, none). Returns `UnblendedCost` ($) and `UsageQuantity` (GB-Mo).
- **Use it three ways**:
  1. **Reconcile/cap**: if Σ(per-snapshot upper-bound) > actual billed backup,
     report the actual as the realizable ceiling and cap the counted snapshot
     savings at it (manual snapshots are a *subset* of total backup spend, so the
     cap is conservative-correct).
  2. **Display**: surface "actual billed RDS/Aurora backup: $X/mo (Cost Explorer,
     <month>)" on the snapshot section so the reader sees the real envelope.
  3. **Enrich AuditBasis**: add `actual_billed_backup_month` + source so the
     estimate is reconcilable from the report.
- **Caveat**: total billed backup = automated backups (beyond free) **+** manual
  snapshots. CE doesn't split them, so the cap is an over-estimate of *manual*
  snapshot spend, not exact — but it's a real, defensible upper bound far tighter
  than provisioned size, and it's labelled.

### Tier 2 — CloudWatch per-live-parent actuals (optional, partial)

For snapshots whose parent cluster/instance **still exists**, read
`SnapshotStorageUsed` (AWS/RDS, per `DBInstanceIdentifier`/`DBClusterIdentifier`)
to replace the provisioned proxy for that parent's snapshots (apportioned or
reported at parent level). Respect `ctx.fast_mode`. **Does not help orphaned
snapshots** (the majority), so it's a smaller, secondary win.

---

## 4. Implementation sketch (Tier 1)

| Piece | Change |
|---|---|
| `services/advisor.py` (or new `services/cost_usage.py`) | `get_rds_backup_actuals(ctx) -> {"standard": (gb, usd), "aurora": (gb, usd)}` via `ce.get_cost_and_usage`; classify `AccessDenied`/`DataUnavailable` → `ctx.permission_issue`/`ctx.warn`; return `{}` on miss. Cache on `ctx` (one CE call per scan). |
| `services/adapters/rds.py` | call it once; pass actuals into `resolve_rds_findings`. Add `ce` to `required_clients()` / reuse the existing CE client. |
| `services/rds_logic.py` | `reconcile_snapshot_savings(snapshot_recs, actual_billed)`: if Σ upper-bound > actual, scale down (or cap total) and tag each rec's AuditBasis with the reconciliation; return adjusted total. |
| reporter | snapshot section shows "actual billed backup $X/mo (Cost Explorer)" alongside the (now-capped) estimate. |
| permissions | needs `ce:GetCostAndUsage` (already used by trend_analysis, so typically present). |
| tests | fake CE client returning usage-type groups; assert cap applied when upper-bound > actual, no-op when actual ≥ upper-bound, permission classification, region-prefixed usage-type matching. |

---

## 5. Accuracy improvement

- **Today**: snapshot $ = Σ provisioned size × rate → systematic over-estimate
  (free-tier offset + compression + allocated-vs-used), e.g. M360 ≈ $804/mo of
  the headline is this upper bound.
- **With Tier 1**: counted snapshot $ ≤ actual billed RDS backup for the region —
  i.e. the headline can never exceed what AWS actually charges for backups. Still
  not per-snapshot exact, but bounded by real spend and clearly sourced.

---

## 6. Risks / costs

- CE calls cost ~$0.01 each and are rate-limited — mitigate with one cached call
  per scan; skip in `fast_mode`.
- CE data lags ~24h and is monthly-granular — use the last *complete* month; label
  the period.
- Management-vs-member account: CE returns the linked account's own usage when
  called with that profile (correct here).
- Reconciliation is a **cap**, not attribution — must be labelled "bounded by
  actual billed backup (incl. automated backups)", not presented as exact.

---

## 7. Effort & recommendation

- **Tier 1**: ~0.5–1 day (one CE helper + reconciliation + reporter line + tests).
  High value: converts the single largest, softest line in the RDS report into a
  spend-bounded figure. **Recommended.**
- **Tier 2**: ~0.5 day, partial coverage (live parents only), lower value given
  orphaned snapshots dominate. Optional / defer.

**Recommendation:** implement **Tier 1** only. It removes the over-estimate risk
on the dominant line with a single cached CE call and honest labelling. Defer
Tier 2 unless a customer specifically wants live-cluster snapshot precision.
Do **not** promise per-snapshot actuals — AWS doesn't expose them for orphaned
snapshots.
